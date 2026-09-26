"""End-to-end message_send/message_targets: order, fallback, forced mode,
hops refusal, idempotency, config override, and the no-raw-text log
invariant. Hermetic: everything lives under MCP_HUDDLE_HOME (a tmp dir per
test, via the autouse `isolate_huddle_storage` fixture) plus the
delivery-specific env overrides for Claude/Codex registries.
"""

import json
import os
import stat
import subprocess
import sys
import time

import pytest

from mcp_huddle.delivery import config as delivery_config
from mcp_huddle.delivery import core, methods


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


@pytest.fixture
def claude_dir(tmp_path, monkeypatch):
    d = tmp_path / "claude-sessions"
    d.mkdir()
    monkeypatch.setenv("MCP_HUDDLE_DELIVERY_CLAUDE_SESSIONS_DIR", str(d))
    return d


@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    d = tmp_path / "codex-home"
    d.mkdir()
    monkeypatch.setenv("MCP_HUDDLE_DELIVERY_CODEX_HOME", str(d))
    return d


def _write_claude_session(directory, filename, **fields):
    (directory / filename).write_text(json.dumps(fields))


def _write_codex_index(codex_home, lines):
    path = codex_home / "session_index.jsonl"
    path.write_text("\n".join(json.dumps(l) for l in lines) + "\n")


def _spool_files():
    root = delivery_config.spool_dir()
    return list(root.rglob("*.md")) if root.is_dir() else []


# ── envelope text stays byte-identical end-to-end via spool ────────────────

def test_message_send_spooled_text_is_byte_identical(codex_home):
    _write_codex_index(codex_home, [{"id": "th-1", "thread_name": "review", "updated_at": 1}])
    text = "please look at db.py <urgent> & reply"
    out = json.loads(core.message_send("codex:th-1", text, mode="spool"))
    assert out["delivered"] is True
    assert out["method"] == "spool"
    files = _spool_files()
    assert len(files) == 1
    content = files[0].read_text(encoding="utf-8")
    inner = content.split(">\n", 1)[1].rsplit("\n</agent-message>", 1)[0]
    assert inner == text


def test_message_send_target_not_found(codex_home):
    out = json.loads(core.message_send("codex:nope", "hi"))
    assert out["delivered"] is False
    assert out["method"] is None
    assert "not found" in out["note"]
    assert out["attempts"] == []


def test_message_send_ambiguous_target(claude_dir, codex_home, tmp_path):
    sock = tmp_path / "s.sock"
    sock.write_text("")
    _write_claude_session(claude_dir, "1.json", pid=_dead_pid(), sessionId="s1",
                           name="dup", messagingSocketPath=str(sock))
    _write_claude_session(claude_dir, "2.json", pid=_dead_pid(), sessionId="s2",
                           name="dup", messagingSocketPath=str(sock))
    out = json.loads(core.message_send("claude:dup", "hi"))
    assert out["delivered"] is False
    assert "ambiguous" in out["note"]


# ── hops guard ───────────────────────────────────────────────────────────────

def test_message_send_refuses_at_hops_limit(codex_home):
    _write_codex_index(codex_home, [{"id": "th-2", "thread_name": "x", "updated_at": 1}])
    looping_text = '<agent-message from="a" hops="4" id="old">body</agent-message>'
    out = json.loads(core.message_send("codex:th-2", looping_text, mode="spool"))
    assert out["delivered"] is False
    assert "hops" in out["note"]
    assert _spool_files() == []


def test_message_send_forwards_hops_when_under_limit(codex_home):
    _write_codex_index(codex_home, [{"id": "th-3", "thread_name": "x", "updated_at": 1}])
    text = '<agent-message from="a" hops="2" id="old">body</agent-message>'
    out = json.loads(core.message_send("codex:th-3", text, mode="spool"))
    assert out["delivered"] is True
    files = _spool_files()
    assert 'hops="3"' in files[0].read_text(encoding="utf-8")


# ── idempotency ──────────────────────────────────────────────────────────────

def test_message_send_idempotency_key_dedupes(codex_home, monkeypatch):
    _write_codex_index(codex_home, [{"id": "th-4", "thread_name": "x", "updated_at": 1}])
    calls = []
    real_spool = methods.spool

    def counting_spool(*a, **kw):
        calls.append(1)
        return real_spool(*a, **kw)

    monkeypatch.setattr(methods, "spool", counting_spool)
    monkeypatch.setattr(core.methods, "spool", counting_spool)

    first = core.message_send("codex:th-4", "hello", mode="spool", idempotency_key="k1")
    second = core.message_send("codex:th-4", "hello", mode="spool", idempotency_key="k1")
    assert first == second
    assert len(calls) == 1  # second call served from cache, nothing re-sent
    assert len(_spool_files()) == 1


def test_message_send_without_idempotency_key_sends_each_time(codex_home):
    _write_codex_index(codex_home, [{"id": "th-5", "thread_name": "x", "updated_at": 1}])
    core.message_send("codex:th-5", "hello", mode="spool")
    core.message_send("codex:th-5", "hello", mode="spool")
    assert len(_spool_files()) == 2


# ── auto order + fallback ───────────────────────────────────────────────────

def test_auto_order_falls_back_when_native_fails(codex_home, monkeypatch):
    _write_codex_index(codex_home, [{"id": "th-6", "thread_name": "x", "updated_at": 1}])

    def failing_native(target, text, cfg):
        return methods.MethodResult(False, "codex.native", "simulated failure")

    def ok_resume(target, text, cfg):
        return methods.MethodResult(True, "codex.resume", "started")

    monkeypatch.setitem(methods.DISPATCH, "codex.native", failing_native)
    monkeypatch.setitem(methods.DISPATCH, "codex.resume", ok_resume)

    out = json.loads(core.message_send("codex:th-6", "hi"))
    assert out["delivered"] is True
    assert out["method"] == "codex.resume"
    assert [a["method"] for a in out["attempts"]] == ["codex.native", "codex.resume"]
    assert out["attempts"][0]["ok"] is False
    assert out["attempts"][1]["ok"] is True


def test_ambiguous_native_failure_blocks_resume_falls_to_spool(codex_home, monkeypatch):
    _write_codex_index(codex_home, [{"id": "th-amb", "thread_name": "x", "updated_at": 1}])

    def ambiguous_native(target, text, cfg):
        return methods.MethodResult(False, "codex.native", "timed out (ambiguous)", ambiguous=True)

    def unexpected_resume(target, text, cfg):
        raise AssertionError("codex.resume must not run after an ambiguous native failure")

    monkeypatch.setitem(methods.DISPATCH, "codex.native", ambiguous_native)
    monkeypatch.setitem(methods.DISPATCH, "codex.resume", unexpected_resume)

    out = json.loads(core.message_send("codex:th-amb", "hi"))
    assert out["delivered"] is True
    assert out["method"] == "spool"
    resume_attempt = next(a for a in out["attempts"] if a["method"] == "codex.resume")
    assert resume_attempt["ok"] is False
    assert "ambiguous" in resume_attempt["detail"]


def test_non_ambiguous_native_failure_still_allows_resume(codex_home, monkeypatch):
    _write_codex_index(codex_home, [{"id": "th-safe", "thread_name": "x", "updated_at": 1}])

    def not_loaded_native(target, text, cfg):
        return methods.MethodResult(False, "codex.native", "exit 7: thread not loaded", ambiguous=False)

    def ok_resume(target, text, cfg):
        return methods.MethodResult(True, "codex.resume", "started")

    monkeypatch.setitem(methods.DISPATCH, "codex.native", not_loaded_native)
    monkeypatch.setitem(methods.DISPATCH, "codex.resume", ok_resume)

    out = json.loads(core.message_send("codex:th-safe", "hi"))
    assert out["delivered"] is True
    assert out["method"] == "codex.resume"


def test_live_claude_never_resumes(claude_dir, tmp_path, monkeypatch):
    sock = tmp_path / "live.sock"
    sock.write_text("")
    _write_claude_session(claude_dir, "1.json", pid=os.getpid(), sessionId="live-1",
                           name="live", messagingSocketPath=str(sock), cwd=str(tmp_path))

    resume_calls = []

    def failing_native(target, text, cfg):
        return methods.MethodResult(False, "claude.native", "simulated failure")

    def recording_resume(target, text, cfg):
        resume_calls.append(1)
        return methods.MethodResult(True, "claude.resume", "started")

    monkeypatch.setitem(methods.DISPATCH, "claude.native", failing_native)
    monkeypatch.setitem(methods.DISPATCH, "claude.resume", recording_resume)

    out = json.loads(core.message_send("claude:live-1", "hi"))
    assert resume_calls == []  # never attempted: target is live
    method_names = [a["method"] for a in out["attempts"]]
    assert "claude.resume" in method_names
    resume_attempt = next(a for a in out["attempts"] if a["method"] == "claude.resume")
    assert resume_attempt["ok"] is False
    assert "live" in resume_attempt["detail"]
    # falls through to spool as the last resort
    assert out["method"] == "spool"
    assert out["delivered"] is True


# ── forced mode ──────────────────────────────────────────────────────────────

def test_forced_mode_tries_only_that_method(codex_home, monkeypatch):
    _write_codex_index(codex_home, [{"id": "th-7", "thread_name": "x", "updated_at": 1}])

    def unexpected_native(target, text, cfg):
        raise AssertionError("native should not be tried in forced mode")

    monkeypatch.setitem(methods.DISPATCH, "codex.native", unexpected_native)
    out = json.loads(core.message_send("codex:th-7", "hi", mode="spool"))
    assert out["delivered"] is True
    assert out["method"] == "spool"
    assert len(out["attempts"]) == 1


def test_forced_mode_reports_inapplicable_without_fallback(claude_dir, tmp_path):
    sock = tmp_path / "live2.sock"
    sock.write_text("")
    _write_claude_session(claude_dir, "1.json", pid=os.getpid(), sessionId="live-2",
                           name="live2", messagingSocketPath=str(sock))
    out = json.loads(core.message_send("claude:live-2", "hi", mode="claude.resume"))
    assert out["delivered"] is False
    assert len(out["attempts"]) == 1
    assert out["attempts"][0]["method"] == "claude.resume"
    assert "live" in out["attempts"][0]["detail"]


# ── config override ──────────────────────────────────────────────────────────

def test_config_override_changes_order(codex_home, monkeypatch):
    _write_codex_index(codex_home, [{"id": "th-8", "thread_name": "x", "updated_at": 1}])
    cfg_path = delivery_config.config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({"harnesses": {"codex": {"order": ["spool"]}}}))

    out = json.loads(core.message_send("codex:th-8", "hi"))
    assert out["delivered"] is True
    assert out["method"] == "spool"
    assert len(out["attempts"]) == 1


def test_config_can_disable_a_method(codex_home):
    _write_codex_index(codex_home, [{"id": "th-9", "thread_name": "x", "updated_at": 1}])
    cfg_path = delivery_config.config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({"methods": {"codex.native": {"enabled": False}}}))

    out = json.loads(core.message_send("codex:th-9", "hi", mode="codex.native"))
    assert out["delivered"] is False
    assert out["attempts"][0]["detail"] == "disabled by config"


# ── missing binary falls through the chain ──────────────────────────────────

def test_missing_binary_falls_back_through_chain(codex_home, monkeypatch):
    _write_codex_index(codex_home, [{"id": "th-10", "thread_name": "x", "updated_at": 1}])
    monkeypatch.setenv("PATH", str(delivery_config.state_dir()))  # no binaries at all
    out = json.loads(core.message_send("codex:th-10", "hi"))
    assert out["delivered"] is True
    assert out["method"] == "spool"
    details = [a["detail"] for a in out["attempts"]]
    assert "binary not found" in details


# ── log has no raw text ──────────────────────────────────────────────────────

def test_log_contains_hash_and_length_not_raw_text(codex_home):
    _write_codex_index(codex_home, [{"id": "th-11", "thread_name": "x", "updated_at": 1}])
    secret_text = "this exact string must never appear in the log"
    core.message_send("codex:th-11", secret_text, mode="spool")

    log_path = delivery_config.log_path()
    assert log_path.is_file()
    lines = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
    assert len(lines) == 1
    entry = lines[0]
    assert set(entry) >= {"ts", "msg_id", "to", "harness", "method", "ok", "detail",
                           "text_sha256", "text_len"}
    assert "text" not in entry
    raw_log_bytes = log_path.read_bytes()
    assert secret_text.encode("utf-8") not in raw_log_bytes
    assert entry["text_len"] == len(secret_text)
    import hashlib
    assert entry["text_sha256"] == hashlib.sha256(secret_text.encode("utf-8")).hexdigest()


# ── message_targets ──────────────────────────────────────────────────────────

def test_message_targets_lists_resolvable_sessions(claude_dir, codex_home, tmp_path):
    sock = tmp_path / "t.sock"
    sock.write_text("")
    _write_claude_session(claude_dir, "1.json", pid=_dead_pid(), sessionId="s1",
                           name="alice", messagingSocketPath=str(sock))
    _write_codex_index(codex_home, [{"id": "th-12", "thread_name": "planning", "updated_at": 1}])

    out = core.message_targets()
    harnesses = {t["harness"] for t in out}
    assert harnesses == {"claude", "codex"}
    for t in out:
        assert "id" in t and "name" in t and "live" in t and "methods" in t

    claude_only = core.message_targets("claude")
    assert {t["harness"] for t in claude_only} == {"claude"}
