"""Deterministic, bounded replacement planning for a failed Swarm member.

Pure function module: no IO, no network, no signals. The caller supplies facts
and applies the returned decision.

Inputs (plain dicts):
  failure   {"text": str, "kind": str?, "waiting_for": "permission"|"user_input"?,
             "progress": bool?}   ``kind`` is a caller-side hint, checked first.
  attempts  [{"route": str, "harness": str, "model": str, "provider": str?}]
            routes already tried, including the one that just failed.
  member    {"member_id": str, "responsibility": str, "harness": str,
             "write_rights": <opaque, copied verbatim; falsy = read-only>}
  candidates [{"route": str, "harness": str, "model": str, "provider": str?,
              "available": bool, "quality": 0..1, "reliability": 0..1,
              "cost": 0..1 (higher = pricier), "can_limit_writes": bool}]
  child_stopped  True ONLY when the exact owned child was proven stopped.
                 A persisted PID that merely looks dead is NOT proof.

Output: {"action", "reason", "failure_class", "attempts_left", ...}
  action: "replace"         -> use ``route``; ``member`` carries the unchanged
                               member_id, responsibility and write_rights.
          "wait_child_stop" -> replacement is safe only after the exact child stops.
          "check_progress"  -> ask for one useful status before replacing a quiet child.
          "needs_user"      -> permission / user-input wait; never auto-approved.
          "terminal"        -> no safe route left (exhausted or none eligible).
"""
from __future__ import annotations

from typing import Any

DEFAULT_MAX_ATTEMPTS = 3

_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("quota", ("rate_limit", "rate limit", "quota", "insufficient_quota",
               "usage limit", "429", "credit balance", "billing")),
    ("model_unavailable", ("model not found", "model_not_found", "unknown model",
                           "model unavailable", "does not exist", "not available for",
                           "unsupported model", "deprecated model")),
    ("transport", ("connection reset", "connection refused", "econnreset",
                   "timed out", "timeout", "network", "502", "503", "504",
                   "broken pipe", "stream disconnected", "overloaded")),
)
_KINDS = {"quota", "model_unavailable", "transport", "permission_wait",
          "user_input_wait", "no_progress"}


def classify_failure(failure: dict[str, Any]) -> str:
    """Return one of the known classes or ``unknown``. Waits outrank text."""
    waiting = str(failure.get("waiting_for") or "")
    if waiting == "permission":
        return "permission_wait"
    if waiting == "user_input":
        return "user_input_wait"
    kind = str(failure.get("kind") or "")
    if kind in _KINDS:
        return kind
    text = str(failure.get("text") or "").lower()
    for cls, needles in _PATTERNS:
        if any(n in text for n in needles):
            return cls
    if failure.get("progress") is False:
        return "no_progress"
    return "unknown"


def _route_id(item: dict[str, Any]) -> str:
    return str(item.get("route") or f"{item.get('harness', '')}/{item.get('model', '')}")


def _unit(c: dict[str, Any], key: str) -> float:
    try:
        return min(1.0, max(0.0, float(c.get(key, 0.5))))
    except (TypeError, ValueError):
        return 0.5


def _score(c: dict[str, Any]) -> float:
    return 0.5 * _unit(c, "quality") + 0.3 * _unit(c, "reliability") + 0.2 * (1.0 - _unit(c, "cost"))


def plan_replacement(
    failure: dict[str, Any],
    attempts: list[dict[str, Any]],
    member: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    child_stopped: bool = False,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> dict[str, Any]:
    cls = classify_failure(failure)
    left = max(0, max_attempts - len(attempts))

    def out(action: str, reason: str, **extra: Any) -> dict[str, Any]:
        return {"action": action, "reason": reason, "failure_class": cls,
                "attempts_left": left, **extra}

    if cls in ("permission_wait", "user_input_wait"):
        return out("needs_user", "native permission/user-input request is never auto-approved")
    if cls == "unknown":
        return out("terminal", "failure is not classified; inspect the concrete error")
    if cls == "no_progress":
        return out("check_progress", "request one bounded status update before replacement")
    if left <= 0:
        return out("terminal", "attempt budget exhausted")

    writes = bool(member.get("write_rights"))
    if child_stopped is not True:
        return out("wait_child_stop", "exact previous child not proven stopped; work not transferred")

    tried = {_route_id(a) for a in attempts}
    failed = attempts[-1] if attempts else {}
    bad_provider = failed.get("provider") if cls == "quota" else None
    home = member.get("harness") or failed.get("harness")

    eligible = []
    for c in candidates:
        if c.get("available") is not True or _route_id(c) in tried:
            continue
        if bad_provider and c.get("provider") == bad_provider:
            continue
        if writes and c.get("can_limit_writes") is not True:
            continue
        eligible.append(c)
    if not eligible:
        return out("terminal", "no eligible untried route")

    # Same harness first, then another harness; best score, stable tie-break.
    best = min(eligible, key=lambda c: (c.get("harness") != home, -_score(c), _route_id(c)))
    return out(
        "replace",
        "same-harness model" if best.get("harness") == home else "other harness",
        attempts_left=left - 1,
        route=_route_id(best),
        candidate=dict(best),
        member={"member_id": member.get("member_id"),
                "responsibility": member.get("responsibility"),
                "write_rights": member.get("write_rights")},
    )
