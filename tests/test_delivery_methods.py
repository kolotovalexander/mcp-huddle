"""Postman methods: fake AF_UNIX server for claude.native, fake recording
executables on PATH for every subprocess-based method. Nothing here touches a
real socket, a real ~/.claude / ~/.codex, or a real CLI."""

import json
import os
import shutil
import socket
import stat
import tempfile
import threading
import time

import pytest

from mcp_huddle.delivery import config as delivery_config
from mcp_huddle.delivery import methods
from mcp_huddle.delivery.targets import Target

FAKE_BIN_SCRIPT = """#!/usr/bin/env python3
import json, os, sys
out = os.environ.get("FAKE_BIN_OUT")
record = {"name": os.path.basename(sys.argv[0]), "argv": sys.argv[1:], "cwd": os.getcwd()}
with open(out, "a") as f:
    f.write(json.dumps(record) + "\\n")
sys.exit(int(os.environ.get("FAKE_BIN_EXIT", "0")))
"""


@pytest.fixture
def fake_bin(tmp_path, monkeypatch):
    """Prepend a tmp dir with fake `claude`/`codex`/`hermes`/`opencode`/`agy`
    executables onto PATH; each records its argv (as JSON, one line) to the
    file named by FAKE_BIN_OUT."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("claude", "codex", "hermes", "opencode", "agy"):
        script = bin_dir / name
        script.write_text(FAKE_BIN_SCRIPT)
        script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    out_file = tmp_path / "argv.jsonl"
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("FAKE_BIN_OUT", str(out_file))
    return {"dir": bin_dir, "out": out_file}


def _read_records(out_file, expect=1, timeout=5.0):
    """Detached spawns race the test; poll briefly for their argv record."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if out_file.is_file():
            lines = [l for l in out_file.read_text().splitlines() if l.strip()]
            if len(lines) >= expect:
                return [json.loads(l) for l in lines]
        time.sleep(0.05)
    lines = out_file.read_text().splitlines() if out_file.is_file() else []
    raise AssertionError(f"expected {expect} record(s), got {len(lines)}: {lines}")


@pytest.fixture
def cfg():
    return delivery_config.load()


ENVELOPE = '<agent-message from="a" via="huddle:x" id="m1" hops="1" reply_to="">\nhello\nworld\n</agent-message>'


# ── claude.native (fake AF_UNIX server) ─────────────────────────────────────

def _serve_once(sock_path, received: list, ready: threading.Event):
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
    srv.listen(1)
    ready.set()
    srv.settimeout(5.0)
    try:
        conn, _ = srv.accept()
    except socket.timeout:
        srv.close()
        return
    with conn:
        conn.settimeout(5.0)
        chunks = []
        try:
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        except socket.timeout:
            pass
        received.append(b"".join(chunks))
    srv.close()


@pytest.fixture
def unix_socket(tmp_path):
    # macOS caps AF_UNIX paths at ~104 chars: use /tmp directly, not tmp_path.
    tmpdir = tempfile.mkdtemp(dir="/tmp")
    sock_path = os.path.join(tmpdir, "s.sock")
    received = []
    ready = threading.Event()
    thread = threading.Thread(target=_serve_once, args=(sock_path, received, ready), daemon=True)
    thread.start()
    assert ready.wait(2.0)
    try:
        yield sock_path, received, thread
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_claude_native_sends_envelope_over_socket(unix_socket, cfg):
    sock_path, received, thread = unix_socket
    target = Target(harness="claude", id="sess-1", live=True, socket_path=sock_path)
    result = methods.claude_native(target, ENVELOPE, cfg)
    thread.join(2.0)
    assert result.ok is True
    assert result.method == "claude.native"
    assert len(received) == 1
    payload = json.loads(received[0].decode("utf-8"))
    assert payload["type"] == "user"
    assert payload["message"]["role"] == "user"
    assert payload["message"]["content"] == ENVELOPE  # byte-identical


def test_claude_native_skips_when_not_live(cfg):
    target = Target(harness="claude", id="sess-1", live=False, socket_path="/whatever")
    result = methods.claude_native(target, ENVELOPE, cfg)
    assert result.ok is False
    assert "not live" in result.detail


def test_claude_native_reports_socket_error_when_unreachable(tmp_path, cfg):
    target = Target(harness="claude", id="sess-1", live=True, socket_path=str(tmp_path / "nope.sock"))
    result = methods.claude_native(target, ENVELOPE, cfg)
    assert result.ok is False
    assert "socket error" in result.detail


# ── resume methods (detached subprocess) ────────────────────────────────────

def test_claude_resume_spawns_detached_with_envelope(fake_bin, cfg, tmp_path):
    target = Target(harness="claude", id="sess-2", live=False, cwd=str(tmp_path))
    result = methods.claude_resume(target, ENVELOPE, cfg)
    assert result.ok is True
    records = _read_records(fake_bin["out"])
    assert records[0]["argv"][-1] == ENVELOPE  # text unchanged, passed as argv, not shell
    assert "--resume" in records[0]["argv"]
    assert "sess-2" in records[0]["argv"]
    assert records[0]["cwd"] == str(tmp_path)


def test_claude_resume_skipped_when_live(cfg):
    target = Target(harness="claude", id="sess-2", live=True)
    result = methods.claude_resume(target, ENVELOPE, cfg)
    assert result.ok is False
    assert "live" in result.detail


def test_codex_native_success_and_failure(fake_bin, cfg):
    target = Target(harness="codex", id="th-1")
    ok_result = methods.codex_native(target, ENVELOPE, cfg)
    assert ok_result.ok is True
    records = _read_records(fake_bin["out"])
    assert records[0]["argv"][-1] == ENVELOPE

    os.environ["FAKE_BIN_EXIT"] = "1"
    try:
        fail_result = methods.codex_native(target, ENVELOPE, cfg)
    finally:
        del os.environ["FAKE_BIN_EXIT"]
    assert fail_result.ok is False
    assert "exit 1" in fail_result.detail


def test_codex_resume_spawns_detached(fake_bin, cfg):
    target = Target(harness="codex", id="th-2")
    result = methods.codex_resume(target, ENVELOPE, cfg)
    assert result.ok is True
    records = _read_records(fake_bin["out"])
    assert records[0]["argv"][-1] == ENVELOPE


def test_hermes_native_requires_peer(fake_bin, cfg):
    target = Target(harness="hermes", id="desktop", extra={})
    result = methods.hermes_native(target, ENVELOPE, cfg)
    assert result.ok is False
    assert "peer" in result.detail


def test_hermes_native_sends_with_peer(fake_bin, cfg):
    target = Target(harness="hermes", id="desktop/reviewer", extra={"peer": "desktop", "agent": "reviewer"})
    result = methods.hermes_native(target, ENVELOPE, cfg)
    assert result.ok is True
    records = _read_records(fake_bin["out"])
    assert records[0]["argv"][-1] == ENVELOPE
    assert "desktop/reviewer" in records[0]["argv"]


def test_hermes_resume_spawns_detached(fake_bin, cfg):
    target = Target(harness="hermes", id="session-1")
    result = methods.hermes_resume(target, ENVELOPE, cfg)
    assert result.ok is True
    assert "unverified" in result.detail
    records = _read_records(fake_bin["out"])
    assert records[0]["argv"][-1] == ENVELOPE


def test_opencode_resume_spawns_detached(fake_bin, cfg):
    target = Target(harness="opencode", id="sess-3")
    result = methods.opencode_resume(target, ENVELOPE, cfg)
    assert result.ok is True
    records = _read_records(fake_bin["out"])
    assert records[0]["argv"][-1] == ENVELOPE


def test_agy_resume_spawns_detached(fake_bin, cfg):
    target = Target(harness="agy", id="conv-1")
    result = methods.agy_resume(target, ENVELOPE, cfg)
    assert result.ok is True
    records = _read_records(fake_bin["out"])
    assert records[0]["argv"][-1] == ENVELOPE


def test_missing_binary_is_skipped_not_raised(monkeypatch, cfg, tmp_path):
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    target = Target(harness="codex", id="th-4")
    result = methods.codex_native(target, ENVELOPE, cfg)
    assert result.ok is False
    assert result.detail == "binary not found"


# ── opencode.native (fake urllib) ───────────────────────────────────────────

class _FakeResponse:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_opencode_native_posts_envelope_unchanged(monkeypatch, cfg):
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        if req.full_url.endswith("/session/status"):
            raise OSError("no status endpoint")
        return _FakeResponse(200)

    monkeypatch.setattr(methods.urllib.request, "urlopen", fake_urlopen)
    raw_cfg = delivery_config.DeliveryConfig({"opencode": {"server_url": "http://127.0.0.1:9999"}})
    target = Target(harness="opencode", id="sess-5")
    result = methods.opencode_native(target, ENVELOPE, raw_cfg)
    assert result.ok is True
    post_calls = [c for c in calls if c.full_url.endswith("/prompt_async")]
    assert len(post_calls) == 1
    body = json.loads(post_calls[0].data.decode("utf-8"))
    assert body["parts"][0]["text"] == ENVELOPE


def test_opencode_native_without_server_url_configured(cfg):
    target = Target(harness="opencode", id="sess-6")
    result = methods.opencode_native(target, ENVELOPE, cfg)
    assert result.ok is False
    assert "server_url" in result.detail


# ── spool ────────────────────────────────────────────────────────────────────

def test_spool_writes_envelope_to_file(tmp_path, monkeypatch, cfg):
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path / "huddle"))
    target = Target(harness="agy", id="conv/with/slash")
    result = methods.spool(target, ENVELOPE, cfg, "msg-123")
    assert result.ok is True
    written = list((tmp_path / "huddle" / "delivery" / "spool" / "agy").rglob("msg-123.md"))
    assert len(written) == 1
    assert written[0].read_text(encoding="utf-8") == ENVELOPE
