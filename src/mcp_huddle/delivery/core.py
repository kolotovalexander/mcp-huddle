"""``message_send`` / ``message_targets`` orchestration.

Ties together target resolution, the hops/idempotency guards, the envelope,
the ordered postmen in :mod:`.methods`, and the delivery log. See
``docs/delivery.md`` for the full behavioral contract.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Optional

from . import config as delivery_config
from . import envelope as envelope_mod
from . import idempotency
from . import methods
from . import targets as targets_mod

# "native"/"resume" tokens in a harness's configured order map to one
# concrete method id per harness.
_NATIVE_METHOD = {
    "claude": "claude.native",
    "codex": "codex.native",
    "hermes": "hermes.native",
    "opencode": "opencode.native",
}
_RESUME_METHOD = {
    "claude": "claude.resume",
    "codex": "codex.resume",
    "hermes": "hermes.resume",
    "opencode": "opencode.resume",
    "agy": "agy.resume",
}


def _method_id(harness: str, token: str) -> str:
    if token == "spool":
        return "spool"
    if token == "native":
        return _NATIVE_METHOD.get(harness, f"{harness}.native")
    if token == "resume":
        return _RESUME_METHOD.get(harness, f"{harness}.resume")
    if "." in token:
        return token
    return f"{harness}.{token}"


def _applicability_skip_reason(method_id: str, target: targets_mod.Target,
                                cfg: delivery_config.DeliveryConfig) -> Optional[str]:
    """Return why `method_id` should not even be attempted for this target,
    or None if it's worth trying."""
    if method_id == "claude.native" and not target.live:
        return "target is not live"
    if method_id == "claude.resume" and target.live:
        return "target is live; claude.native is used instead"
    if method_id == "hermes.native" and not (target.extra or {}).get("peer"):
        return "no peer name given"
    if method_id == "opencode.native" and not cfg.opencode_server_url():
        return "opencode.server_url not configured"
    return None


def _run_method(method_id: str, target: targets_mod.Target, envelope_text: str,
                 cfg: delivery_config.DeliveryConfig, msg_id: str) -> methods.MethodResult:
    if method_id == "spool":
        return methods.spool(target, envelope_text, cfg, msg_id)
    fn = methods.DISPATCH.get(method_id)
    if fn is None:
        return methods.MethodResult(False, method_id, "unknown method")
    return fn(target, envelope_text, cfg)


def _log_attempt(msg_id: str, to: str, harness: str, method: str, ok: bool,
                  detail: str, text: str) -> None:
    entry = {
        "ts": time.time(),
        "msg_id": msg_id,
        "to": to,
        "harness": harness,
        "method": method,
        "ok": ok,
        "detail": detail,
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "text_len": len(text),
    }
    path = delivery_config.log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def _refusal(msg_id: str, note: str, idempotency_key: str) -> str:
    result = json.dumps({
        "msg_id": msg_id, "delivered": False, "method": None, "attempts": [], "note": note,
    })
    if idempotency_key:
        idempotency.store(idempotency_key, result)
    return result


def message_send(to: str, text: str, mode: str = "auto", from_name: str = "",
                  reply_to: str = "", idempotency_key: str = "") -> str:
    """Send `text` to `to`, trying delivery methods per the configured order
    (or a single forced method). Returns a JSON string:
    ``{msg_id, delivered, method, attempts, note}``.

    Honesty: ``delivered=True`` for a ``*.resume`` method means the resume
    process was *started*, not that it was read; for ``spool`` it means the
    envelope was written for a hook to pick up later, not that it was read.
    """
    cached = idempotency.get_cached(idempotency_key) if idempotency_key else None
    if cached is not None:
        return cached

    msg_id = uuid.uuid4().hex[:16]
    cfg = delivery_config.load()

    try:
        hops = envelope_mod.next_hops(text, cfg.hops_limit())
    except envelope_mod.HopsExceeded as exc:
        return _refusal(msg_id, f"refused: hops {exc.hops} >= limit {exc.limit}", idempotency_key)

    try:
        target = targets_mod.resolve(to)
    except targets_mod.AmbiguousTarget as exc:
        return _refusal(msg_id, "ambiguous target: " + ", ".join(exc.candidates), idempotency_key)
    except targets_mod.TargetNotFound as exc:
        return _refusal(msg_id, f"target not found: {exc}", idempotency_key)

    if mode == "auto":
        order_tokens = cfg.order(target.harness)
        method_ids = [_method_id(target.harness, tok) for tok in order_tokens]
    else:
        method_ids = [mode if "." in mode or mode == "spool" else _method_id(target.harness, mode)]

    attempts = []
    delivered = False
    delivered_method = None
    for method_id in method_ids:
        if not cfg.method_enabled(method_id):
            attempts.append({"method": method_id, "ok": False, "detail": "disabled by config"})
            continue
        skip_reason = _applicability_skip_reason(method_id, target, cfg)
        if skip_reason:
            attempts.append({"method": method_id, "ok": False, "detail": skip_reason})
            continue

        env_text = envelope_mod.build_envelope(
            from_name=from_name, method=method_id, msg_id=msg_id, hops=hops,
            reply_to=reply_to, text=text,
        )
        result = _run_method(method_id, target, env_text, cfg, msg_id)
        _log_attempt(msg_id, to, target.harness, method_id, result.ok, result.detail, text)
        attempts.append({"method": method_id, "ok": result.ok, "detail": result.detail})
        if result.ok:
            delivered = True
            delivered_method = method_id
            break

    if not delivered:
        note = "not delivered: all methods failed, were skipped, or were disabled"
    elif delivered_method == "spool":
        note = "spooled (delivered when a hook reads it) -- not confirmed read by the recipient"
    elif delivered_method.endswith(".resume"):
        note = "resume process started -- not confirmed read by the recipient"
    else:
        note = "delivered to the recipient's live session"

    out = json.dumps({
        "msg_id": msg_id,
        "delivered": delivered,
        "method": delivered_method,
        "attempts": attempts,
        "note": note,
    })
    if idempotency_key:
        idempotency.store(idempotency_key, out)
    return out


def message_targets(harness: str = "") -> list:
    """List resolvable targets (never returns tokens or file contents)."""
    cfg = delivery_config.load()
    out = []
    for t in targets_mod.list_targets(harness):
        order_tokens = cfg.order(t.harness)
        method_ids = [_method_id(t.harness, tok) for tok in order_tokens]
        out.append({
            "harness": t.harness,
            "id": t.id,
            "name": t.name,
            "live": t.live if t.live is not None else "unknown",
            "methods": method_ids,
        })
    return out
