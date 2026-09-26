"""Per-harness, per-method "postmen".

Each function tries exactly one way to hand an already-enveloped message to a
resolved :class:`~mcp_huddle.delivery.targets.Target` and returns a
:class:`MethodResult`. Binaries are resolved with ``shutil.which``; argv is
always a list (never ``shell=True``); a missing binary is reported, not
raised.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import config as delivery_config
from .targets import Target


@dataclass
class MethodResult:
    ok: bool
    method: str
    detail: str


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


def _spawn_detached(argv, *, cwd: str = "", label: str) -> Path:
    log_path = _log_dir() / f"{label}-{int(time.time() * 1000)}.log"
    with open(log_path, "wb") as fh:
        subprocess.Popen(
            argv,
            cwd=cwd or None,
            stdout=fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    return log_path


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
        log_path = _spawn_detached(argv, cwd=target.cwd, label="claude-resume")
    except OSError as exc:
        return MethodResult(False, "claude.resume", f"spawn failed: {exc}")
    return MethodResult(True, "claude.resume", f"started; log: {log_path}")


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
        return MethodResult(False, "codex.native", "timed out")
    except OSError as exc:
        return MethodResult(False, "codex.native", f"exec failed: {exc}")
    if proc.returncode == 0:
        return MethodResult(True, "codex.native", "queued")
    detail = (proc.stderr or proc.stdout or "").strip()[:200]
    return MethodResult(False, "codex.native", f"exit {proc.returncode}: {detail}")


def codex_resume(target: Target, envelope_text: str, cfg: delivery_config.DeliveryConfig) -> MethodResult:
    argv_tpl = cfg.argv("codex.resume")
    binary = _resolve_binary(argv_tpl, "codex.resume")
    if not binary:
        return MethodResult(False, "codex.resume", "binary not found")
    argv = _fill(argv_tpl, id=target.id, text=envelope_text)
    try:
        log_path = _spawn_detached(argv, label="codex-resume")
    except OSError as exc:
        return MethodResult(False, "codex.resume", f"spawn failed: {exc}")
    return MethodResult(True, "codex.resume", f"started; log: {log_path}")


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
        return MethodResult(False, "hermes.native", "timed out")
    except OSError as exc:
        return MethodResult(False, "hermes.native", f"exec failed: {exc}")
    if proc.returncode == 0:
        return MethodResult(True, "hermes.native", "sent")
    detail = (proc.stderr or proc.stdout or "").strip()[:200]
    return MethodResult(False, "hermes.native", f"exit {proc.returncode}: {detail}")


def hermes_resume(target: Target, envelope_text: str, cfg: delivery_config.DeliveryConfig) -> MethodResult:
    argv_tpl = cfg.argv("hermes.resume")
    binary = _resolve_binary(argv_tpl, "hermes.resume")
    if not binary:
        return MethodResult(False, "hermes.resume", "binary not found")
    argv = _fill(argv_tpl, id=target.id, text=envelope_text)
    try:
        log_path = _spawn_detached(argv, label="hermes-resume")
    except OSError as exc:
        return MethodResult(False, "hermes.resume", f"spawn failed: {exc}")
    return MethodResult(True, "hermes.resume", f"started (unverified template); log: {log_path}")


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
    req = urllib.request.Request(
        f"{base}/session/{target.id}/prompt_async",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
    except urllib.error.URLError as exc:
        return MethodResult(False, "opencode.native", f"http error: {exc}")
    if 200 <= status < 300:
        return MethodResult(True, "opencode.native", f"posted (status {status})")
    return MethodResult(False, "opencode.native", f"http status {status}")


def opencode_resume(target: Target, envelope_text: str, cfg: delivery_config.DeliveryConfig) -> MethodResult:
    argv_tpl = cfg.argv("opencode.resume")
    binary = _resolve_binary(argv_tpl, "opencode.resume")
    if not binary:
        return MethodResult(False, "opencode.resume", "binary not found")
    argv = _fill(argv_tpl, id=target.id, text=envelope_text)
    try:
        log_path = _spawn_detached(argv, label="opencode-resume")
    except OSError as exc:
        return MethodResult(False, "opencode.resume", f"spawn failed: {exc}")
    return MethodResult(True, "opencode.resume", f"started; log: {log_path}")


# ── Agy ──────────────────────────────────────────────────────────────────────

def agy_resume(target: Target, envelope_text: str, cfg: delivery_config.DeliveryConfig) -> MethodResult:
    argv_tpl = cfg.argv("agy.resume")
    binary = _resolve_binary(argv_tpl, "agy.resume")
    if not binary:
        return MethodResult(False, "agy.resume", "binary not found")
    argv = _fill(argv_tpl, id=target.id, text=envelope_text)
    try:
        log_path = _spawn_detached(argv, label="agy-resume")
    except OSError as exc:
        return MethodResult(False, "agy.resume", f"spawn failed: {exc}")
    return MethodResult(True, "agy.resume", f"started; log: {log_path}")


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
