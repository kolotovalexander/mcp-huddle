"""Per-harness, per-method "postmen".

Each function tries exactly one way to hand an already-enveloped message to a
resolved :class:`~mcp_huddle.delivery.targets.Target` and returns a
:class:`MethodResult`. Binaries are resolved with ``shutil.which``; argv is
always a list (never ``shell=True``); a missing binary is reported, not
raised.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import config as delivery_config
from .targets import Target

# The Claude cross-session messaging socket rejects (or hangs on) payloads
# much past this; refuse locally instead of blocking on an oversized write.
_CLAUDE_SOCKET_MAX_CHARS = 1_000_000

# Substrings in a failed `codex queue`'s stderr/stdout that positively mean
# "no such thread is currently loaded" -- the only condition under which
# falling through to `codex.resume` cannot fork a live thread's history. Any
# other failure (including a timeout) is ambiguous and must not auto-resume.
_CODEX_NOT_LOADED_SIGNALS = ("not loaded", "no active session", "unknown thread")


@dataclass
class MethodResult:
    ok: bool
    method: str
    detail: str
    # Only meaningful when ok is False for a `*.native` method: True means we
    # can't tell whether the target session is actually live, so the caller
    # must not fall through to the paired `*.resume` method (that could fork
    # a live session's history). Defaults to False (safe to fall through) for
    # every method that doesn't set it explicitly.
    ambiguous: bool = False


def _fill(argv, **values) -> list:
    out = []
    for token in argv:
        for key, val in values.items():
            token = token.replace("{" + key + "}", str(val))
        out.append(token)
    return out


def _log_dir() -> Path:
    d = delivery_config.state_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _detached_grace() -> float:
    """Seconds to watch a detached resume for an immediate failure."""
    try:
        return max(0.0, float(os.environ.get("MCP_HUDDLE_DELIVERY_DETACHED_GRACE", "3")))
    except ValueError:
        return 3.0


def _spawn_detached(argv, *, cwd: str = "", label: str) -> tuple[Path, Optional[int]]:
    """Start a detached resume and watch it for a short grace window.

    Returns ``(log_path, returncode)``; ``returncode`` is ``None`` while the
    process is still running after the window. A resume that exits non-zero
    inside the window (e.g. an unknown conversation id) never started a turn,
    so callers must report it as a failure instead of "started".
    """
    log_path = _log_dir() / f"{label}-{int(time.time() * 1000)}.log"
    with open(log_path, "wb") as fh:
        proc = subprocess.Popen(
            argv,
            cwd=cwd or None,
            stdout=fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    try:
        return log_path, proc.wait(timeout=_detached_grace())
    except subprocess.TimeoutExpired:
        pass
    # Still running: reap it from a daemon thread so it never becomes a
    # zombie, without making the caller wait for the whole turn.
    threading.Thread(target=proc.wait, daemon=True).start()
    return log_path, None


def _log_tail(log_path: Path, limit: int = 160) -> str:
    try:
        lines = log_path.read_text(errors="replace").strip().splitlines()
    except OSError:
        return ""
    return lines[-1][:limit] if lines else ""


def _detached_result(method: str, log_path: Path, rc: Optional[int], note: str = "") -> MethodResult:
    if rc is not None and rc != 0:
        return MethodResult(False, method,
                            f"exited {rc} immediately: {_log_tail(log_path)}; log: {log_path}")
    state = "started" if rc is None else "started; exited 0"
    return MethodResult(True, method, f"{state}{note}; log: {log_path}")


def _resolve_binary(argv: Optional[list], method: str) -> Optional[str]:
    if not argv:
        return None
    return shutil.which(argv[0])


# ── Claude ───────────────────────────────────────────────────────────────────

def claude_native(target: Target, envelope_text: str, cfg: delivery_config.DeliveryConfig) -> MethodResult:
    if not target.live:
        return MethodResult(False, "claude.native", "target is not live")
    if not target.socket_path:
        return MethodResult(False, "claude.native", "no messagingSocketPath on session")
    if len(envelope_text) > _CLAUDE_SOCKET_MAX_CHARS:
        return MethodResult(
            False, "claude.native",
            f"envelope too large for the messaging socket "
            f"({len(envelope_text)} > {_CLAUDE_SOCKET_MAX_CHARS} chars)",
        )
    timeout = cfg.timeout("claude.native") or 5.0
    payload = json.dumps({"type": "user", "message": {"role": "user", "content": envelope_text}}) + "\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(target.socket_path)
            sock.sendall(payload.encode("utf-8"))
    except OSError as exc:
        return MethodResult(False, "claude.native", f"socket error: {exc}")
    return MethodResult(True, "claude.native", "sent over messaging socket")


def claude_resume(target: Target, envelope_text: str, cfg: delivery_config.DeliveryConfig) -> MethodResult:
    if target.live:
        return MethodResult(False, "claude.resume", "target is live; claude.native is used instead")
    if not target.id:
        return MethodResult(False, "claude.resume", "no sessionId to resume")
    argv_tpl = cfg.argv("claude.resume")
    binary = _resolve_binary(argv_tpl, "claude.resume")
    if not binary:
        return MethodResult(False, "claude.resume", "binary not found")
    argv = _fill(argv_tpl, id=target.id, text=envelope_text, cwd=target.cwd)
    try:
        log_path, rc = _spawn_detached(argv, cwd=target.cwd, label="claude-resume")
    except OSError as exc:
        return MethodResult(False, "claude.resume", f"spawn failed: {exc}")
    return _detached_result("claude.resume", log_path, rc)


# ── Codex ────────────────────────────────────────────────────────────────────

def codex_native(target: Target, envelope_text: str, cfg: delivery_config.DeliveryConfig) -> MethodResult:
    argv_tpl = cfg.argv("codex.native")
    binary = _resolve_binary(argv_tpl, "codex.native")
    if not binary:
        return MethodResult(False, "codex.native", "binary not found")
    argv = _fill(argv_tpl, id=target.id, text=envelope_text)
    timeout = cfg.timeout("codex.native") or 30.0
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout, text=True)
    except subprocess.TimeoutExpired:
        # A timeout tells us nothing about whether the thread is loaded --
        # it could just as well mean the Codex app has it open and busy.
        # Falling through to `codex.resume` here would risk forking that
        # live thread's history, so this is always ambiguous.
        return MethodResult(False, "codex.native", "timed out (ambiguous -- thread state unknown)",
                             ambiguous=True)
    except OSError as exc:
        return MethodResult(False, "codex.native", f"exec failed: {exc}")
    if proc.returncode == 0:
        return MethodResult(True, "codex.native", "queued")
    detail = (proc.stderr or proc.stdout or "").strip()[:200]
    not_loaded = any(sig in detail.lower() for sig in _CODEX_NOT_LOADED_SIGNALS)
    return MethodResult(False, "codex.native", f"exit {proc.returncode}: {detail}",
                         ambiguous=not not_loaded)


def codex_resume(target: Target, envelope_text: str, cfg: delivery_config.DeliveryConfig) -> MethodResult:
    argv_tpl = cfg.argv("codex.resume")
    binary = _resolve_binary(argv_tpl, "codex.resume")
    if not binary:
        return MethodResult(False, "codex.resume", "binary not found")
    argv = _fill(argv_tpl, id=target.id, text=envelope_text)
    try:
        log_path, rc = _spawn_detached(argv, label="codex-resume")
    except OSError as exc:
        return MethodResult(False, "codex.resume", f"spawn failed: {exc}")
    return _detached_result("codex.resume", log_path, rc)


# ── Hermes ───────────────────────────────────────────────────────────────────

def hermes_native(target: Target, envelope_text: str, cfg: delivery_config.DeliveryConfig) -> MethodResult:
    peer = (target.extra or {}).get("peer")
    if not peer:
        return MethodResult(False, "hermes.native", "no peer name given")
    argv_tpl = cfg.argv("hermes.native")
    binary = _resolve_binary(argv_tpl, "hermes.native")
    if not binary:
        return MethodResult(False, "hermes.native", "binary not found")
    argv = _fill(argv_tpl, peer=target.id, text=envelope_text)
    timeout = cfg.timeout("hermes.native") or 120.0
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout, text=True)
    except subprocess.TimeoutExpired:
        # A timeout tells us nothing about whether the peer actually received
        # the DM -- `hermes peer dm` could have delivered it and then hung on
        # something else. Ambiguous, same reasoning as codex.native: never
        # let the caller auto-fall-through to a paired resume.
        return MethodResult(False, "hermes.native", "timed out (ambiguous -- delivery status unknown)",
                             ambiguous=True)
    except OSError as exc:
        # The binary itself failed to exec -- this says nothing about the
        # peer's state, so it's a definite (non-ambiguous) failure.
        return MethodResult(False, "hermes.native", f"exec failed: {exc}")
    if proc.returncode == 0:
        return MethodResult(True, "hermes.native", "sent")
    detail = (proc.stderr or proc.stdout or "").strip()[:200]
    # There's no known-safe "peer definitely unreachable" signal for hermes
    # (unlike codex's "not loaded"/"unknown thread" strings), so any nonzero
    # exit is an unrecognized/ambiguous error.
    return MethodResult(False, "hermes.native", f"exit {proc.returncode}: {detail}", ambiguous=True)


def hermes_resume(target: Target, envelope_text: str, cfg: delivery_config.DeliveryConfig) -> MethodResult:
    argv_tpl = cfg.argv("hermes.resume")
    binary = _resolve_binary(argv_tpl, "hermes.resume")
    if not binary:
        return MethodResult(False, "hermes.resume", "binary not found")
    argv = _fill(argv_tpl, id=target.id, text=envelope_text)
    try:
        log_path, rc = _spawn_detached(argv, label="hermes-resume")
    except OSError as exc:
        return MethodResult(False, "hermes.resume", f"spawn failed: {exc}")
    return _detached_result("hermes.resume", log_path, rc, ' (unverified template)')


def _is_connection_refused(exc: BaseException) -> bool:
    """True only for a bare "nothing is listening on this address at all"
    failure (``ECONNREFUSED``), possibly wrapped in a ``urllib.error.URLError``.
    This is the one connection failure that proves there was never a live
    opencode server on the configured URL to collide with -- every other
    connection failure (reset mid-request, DNS, generic OSError) leaves open
    the possibility that a live process was actually reached, so it stays
    ambiguous."""
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, ConnectionRefusedError):
        return True
    return isinstance(reason, OSError) and reason.errno == errno.ECONNREFUSED


# ── OpenCode ─────────────────────────────────────────────────────────────────

def opencode_native(target: Target, envelope_text: str, cfg: delivery_config.DeliveryConfig) -> MethodResult:
    server_url = cfg.opencode_server_url()
    if not server_url:
        return MethodResult(False, "opencode.native", "opencode.server_url not configured")
    base = server_url.rstrip("/")
    timeout = cfg.timeout("opencode.native") or 10.0
    try:
        # Best-effort probe around upstream bug #46842 (a busy session silently
        # drops the turn); if the endpoint isn't there, just try the send.
        urllib.request.urlopen(urllib.request.Request(f"{base}/session/status"), timeout=timeout)
    except Exception:
        pass
    body = json.dumps({"parts": [{"type": "text", "text": envelope_text}]}).encode("utf-8")
    # target.id for opencode is taken as-is from `to` (no local registry to
    # validate it against, unlike claude/codex) -- URL-encode it so a crafted
    # id such as "../admin" or "x?y=z" can't alter the request path or add
    # query parameters against the local opencode server.
    safe_id = urllib.parse.quote(str(target.id), safe="")
    req = urllib.request.Request(
        f"{base}/session/{safe_id}/prompt_async",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
    except (socket.timeout, TimeoutError) as exc:
        # We can't tell whether the server received/queued the prompt before
        # the read timed out -- ambiguous, must not auto-resume.
        return MethodResult(False, "opencode.native", f"timed out (ambiguous -- delivery status unknown): {exc}",
                             ambiguous=True)
    except urllib.error.HTTPError as exc:
        # The server answered with an HTTP error status (401/403/409/429/
        # 500/503/...). That proves *something* handled the connection, but
        # it does NOT prove there's no live owner of the session, and a 5xx
        # in particular says nothing about whether the request had a side
        # effect. Codex review finding B: this used to be treated as a
        # definite, non-ambiguous failure, which let `auto` mode fall
        # through to `opencode.resume` on nothing more than an auth/server
        # error. Ambiguous, must not auto-resume.
        return MethodResult(False, "opencode.native", f"http error (ambiguous -- delivery status unknown): {exc}",
                             ambiguous=True)
    except (urllib.error.URLError, ConnectionError, OSError) as exc:
        # Connection dropped/reset with no confirmed response (e.g. reset
        # right after we sent the body), or a URLError wrapping some other
        # OSError -- upstream bug #46842 means a busy session can silently
        # drop the turn either way, so we can't tell if the prompt landed,
        # UNLESS the underlying cause is specifically "connection refused"
        # (nothing listening at all -- server not running, safe to resume).
        if _is_connection_refused(exc):
            return MethodResult(False, "opencode.native", f"connection refused (server not running): {exc}")
        return MethodResult(False, "opencode.native", f"connection error (ambiguous -- delivery status unknown): {exc}",
                             ambiguous=True)
    if 200 <= status < 300:
        return MethodResult(True, "opencode.native", f"posted (status {status})")
    # A non-2xx status that didn't raise HTTPError (unusual, but possible via
    # a custom opener/mock) is a non-definitive outcome -- ambiguous.
    return MethodResult(False, "opencode.native", f"http status {status} (ambiguous -- delivery status unknown)",
                         ambiguous=True)


def opencode_resume(target: Target, envelope_text: str, cfg: delivery_config.DeliveryConfig) -> MethodResult:
    argv_tpl = cfg.argv("opencode.resume")
    binary = _resolve_binary(argv_tpl, "opencode.resume")
    if not binary:
        return MethodResult(False, "opencode.resume", "binary not found")
    argv = _fill(argv_tpl, id=target.id, text=envelope_text)
    try:
        log_path, rc = _spawn_detached(argv, label="opencode-resume")
    except OSError as exc:
        return MethodResult(False, "opencode.resume", f"spawn failed: {exc}")
    return _detached_result("opencode.resume", log_path, rc)


# ── Agy ──────────────────────────────────────────────────────────────────────

def agy_resume(target: Target, envelope_text: str, cfg: delivery_config.DeliveryConfig) -> MethodResult:
    argv_tpl = cfg.argv("agy.resume")
    binary = _resolve_binary(argv_tpl, "agy.resume")
    if not binary:
        return MethodResult(False, "agy.resume", "binary not found")
    argv = _fill(argv_tpl, id=target.id, text=envelope_text)
    try:
        log_path, rc = _spawn_detached(argv, label="agy-resume")
    except OSError as exc:
        return MethodResult(False, "agy.resume", f"spawn failed: {exc}")
    return _detached_result("agy.resume", log_path, rc)


# ── Spool (hook fallback, all harnesses) ───────────────────────────────────

def spool(target: Target, envelope_text: str, cfg: delivery_config.DeliveryConfig, msg_id: str) -> MethodResult:
    safe_harness = (target.harness or "unknown").replace("/", "_")
    safe_id = (target.id or "unknown").replace("/", "_")
    d = delivery_config.spool_dir() / safe_harness / safe_id
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{msg_id}.md"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(envelope_text, encoding="utf-8")
    tmp.replace(path)
    return MethodResult(True, "spool", "spooled (delivered when a hook reads it)")


DISPATCH = {
    "claude.native": claude_native,
    "claude.resume": claude_resume,
    "codex.native": codex_native,
    "codex.resume": codex_resume,
    "hermes.native": hermes_native,
    "hermes.resume": hermes_resume,
    "opencode.native": opencode_native,
    "opencode.resume": opencode_resume,
    "agy.resume": agy_resume,
}
