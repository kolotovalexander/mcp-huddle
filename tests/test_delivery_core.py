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


def test_claude_unknown_liveness_blocks_native_and_resume_falls_to_spool(claude_dir, tmp_path):
    """Regression for Codex review defect #2: an alive pid whose socket can't
    be confirmed used to be reported live=False, so `auto` mode would run
    `claude.resume` against a session that might still be running. It must
    now fall straight through to spool instead."""
    import os
    _write_claude_session(claude_dir, "1.json", pid=os.getpid(), sessionId="unknown-1",
                           name="unknown", messagingSocketPath=str(tmp_path / "missing.sock"))
    out = json.loads(core.message_send("claude:unknown-1", "hi"))
    method_names = [a["method"] for a in out["attempts"]]
    assert "claude.native" in method_names
    assert "claude.resume" in method_names
    for a in out["attempts"]:
        if a["method"] in ("claude.native", "claude.resume"):
            assert a["ok"] is False
    assert out["method"] == "spool"
    assert out["delivered"] is True


# ── hermes peer vs. session target model ────────────────────────────────────

def test_hermes_peer_target_never_falls_through_to_resume(monkeypatch):
    """Regression for Codex review defect #3: a hermes peer name must never
    be used as a session id for hermes.resume."""
    def failing_native(target, text, cfg):
        return methods.MethodResult(False, "hermes.native", "simulated failure")

    def unexpected_resume(target, text, cfg):
        raise AssertionError("hermes.resume must not run against a peer target")

    monkeypatch.setitem(methods.DISPATCH, "hermes.native", failing_native)
    monkeypatch.setitem(methods.DISPATCH, "hermes.resume", unexpected_resume)
    out = json.loads(core.message_send("hermes:peer:desktop", "hi"))
    resume_attempt = next(a for a in out["attempts"] if a["method"] == "hermes.resume")
    assert resume_attempt["ok"] is False
    assert "session" in resume_attempt["detail"]
    assert out["method"] == "spool"


def test_hermes_session_target_never_uses_native(monkeypatch):
    def unexpected_native(target, text, cfg):
        raise AssertionError("hermes.native must not run against a session target (no peer)")

    def ok_resume(target, text, cfg):
        return methods.MethodResult(True, "hermes.resume", "started")

    monkeypatch.setitem(methods.DISPATCH, "hermes.native", unexpected_native)
    monkeypatch.setitem(methods.DISPATCH, "hermes.resume", ok_resume)
    out = json.loads(core.message_send("hermes:session:sess-1", "hi"))
    assert out["delivered"] is True
    assert out["method"] == "hermes.resume"
    native_attempt = next(a for a in out["attempts"] if a["method"] == "hermes.native")
    assert native_attempt["ok"] is False


def test_opencode_ambiguous_native_failure_blocks_resume(monkeypatch):
    """Generalization of the codex ambiguous-blocks-resume mechanism to every
    harness (Codex review defect #3), exercised here on opencode, whose
    native/resume aren't mutually exclusive the way hermes's peer/session
    split now makes them -- a timeout must still block the paired resume."""
    def ambiguous_native(target, text, cfg):
        return methods.MethodResult(False, "opencode.native", "timed out", ambiguous=True)

    def unexpected_resume(target, text, cfg):
        raise AssertionError("opencode.resume must not run after an ambiguous native failure")

    monkeypatch.setitem(methods.DISPATCH, "opencode.native", ambiguous_native)
    monkeypatch.setitem(methods.DISPATCH, "opencode.resume", unexpected_resume)
    cfg_path = delivery_config.config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({"opencode": {"server_url": "http://127.0.0.1:9999"}}))
    out = json.loads(core.message_send("opencode:sess-amb", "hi"))
    assert out["method"] == "spool"
    resume_attempt = next(a for a in out["attempts"] if a["method"] == "opencode.resume")
    assert resume_attempt["ok"] is False
    assert "ambiguous" in resume_attempt["detail"]


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


def test_forced_mode_harness_mismatch_is_refused_with_no_attempt(monkeypatch):
    """Regression for Codex review defect #1: forcing a method that belongs
    to a different harness than the resolved target (the reproduced
    `claude -p --resume ... ` dispatched for `to="agy:..."`) must be refused
    before anything is attempted -- not sent, not even skipped-with-reason."""
    def unexpected_claude_resume(*a, **kw):
        raise AssertionError("claude.resume must never run against an agy target")

    monkeypatch.setitem(methods.DISPATCH, "claude.resume", unexpected_claude_resume)
    out = json.loads(core.message_send("agy:conv-1", "hi", mode="claude.resume"))
    assert out["delivered"] is False
    assert out["method"] is None
    assert out["attempts"] == []
    assert "harness" in out["note"]


def test_forced_mode_same_harness_still_dispatches(codex_home, monkeypatch):
    """Sanity check alongside the mismatch refusal: forcing a method that DOES
    belong to the resolved target's harness must still work."""
    _write_codex_index(codex_home, [{"id": "th-forced", "thread_name": "x", "updated_at": 1}])

    def ok_native(target, text, cfg):
        return methods.MethodResult(True, "codex.native", "queued")

    monkeypatch.setitem(methods.DISPATCH, "codex.native", ok_native)
    out = json.loads(core.message_send("codex:th-forced", "hi", mode="codex.native"))
    assert out["delivered"] is True
    assert out["method"] == "codex.native"


def test_disabled_harness_refuses_even_in_forced_mode(codex_home):
    """Regression for Codex review defect #4: `harness_enabled` must gate
    both auto AND forced mode -- previously it was never called at all."""
    _write_codex_index(codex_home, [{"id": "th-disabled", "thread_name": "x", "updated_at": 1}])
    cfg_path = delivery_config.config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({"harnesses": {"codex": {"enabled": False}}}))

    out_auto = json.loads(core.message_send("codex:th-disabled", "hi"))
    assert out_auto["delivered"] is False
    assert out_auto["attempts"] == []
    assert "disabled" in out_auto["note"]

    out_forced = json.loads(core.message_send("codex:th-disabled", "hi", mode="codex.native"))
    assert out_forced["delivered"] is False
    assert out_forced["attempts"] == []
    assert "disabled" in out_forced["note"]


def test_config_can_disable_a_method(codex_home):
    _write_codex_index(codex_home, [{"id": "th-9", "thread_name": "x", "updated_at": 1}])
    cfg_path = delivery_config.config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({"methods": {"codex.native": {"enabled": False}}}))

    out = json.loads(core.message_send("codex:th-9", "hi", mode="codex.native"))
    assert out["delivered"] is False
    assert out["attempts"][0]["detail"] == "disabled by config"


def test_parallel_message_send_same_key_sends_exactly_once(codex_home, monkeypatch):
    """Regression for Codex review defect #5: two concurrent `message_send`
    calls with the same `idempotency_key` used to both pass the (unlocked)
    cache-miss check before either wrote a result, so both sent -- with
    different msg_ids. The reservation must be taken atomically before any
    send, so only one caller ever sends and the other gets `in_progress`."""
    import threading

    _write_codex_index(codex_home, [{"id": "th-race", "thread_name": "x", "updated_at": 1}])

    entered = threading.Event()
    release = threading.Event()
    calls = []
    real_spool = methods.spool

    def slow_spool(*a, **kw):
        calls.append(1)
        entered.set()
        release.wait(5.0)
        return real_spool(*a, **kw)

    monkeypatch.setattr(core.methods, "spool", slow_spool)

    results = {}

    def send_a():
        results["a"] = core.message_send("codex:th-race", "hi", mode="spool", idempotency_key="race-key")

    t1 = threading.Thread(target=send_a)
    t1.start()
    assert entered.wait(2.0), "first sender never reached the send step"

    # Second caller starts while the first is still mid-send (reserved, not
    # yet done) -- it must see the in-flight reservation and send nothing.
    second_result = core.message_send("codex:th-race", "hi", mode="spool", idempotency_key="race-key")

    release.set()
    t1.join(5.0)

    assert len(calls) == 1, "the envelope must be sent exactly once"
    out_a = json.loads(results["a"])
    out_b = json.loads(second_result)
    assert out_b.get("status") == "in_progress"
    assert out_b["msg_id"] == out_a["msg_id"]
    assert len(_spool_files()) == 1


def test_reserved_key_stale_after_owner_death_is_taken_over(codex_home):
    """A crashed sender's reservation (owner pid confirmably dead, older than
    the stale window) must eventually be retriable, not stuck forever."""
    from mcp_huddle.delivery import idempotency

    _write_codex_index(codex_home, [{"id": "th-stale", "thread_name": "x", "updated_at": 1}])
    path = idempotency._key_path("stale-key")
    path.parent.mkdir(parents=True, exist_ok=True)
    dead_pid = _dead_pid()
    path.write_text(json.dumps({
        "status": "reserved", "msg_id": "abandoned-msg", "pid": dead_pid,
        "ts": time.time() - idempotency.RESERVATION_STALE_SECONDS - 10,
    }))

    out = json.loads(core.message_send("codex:th-stale", "hi", mode="spool", idempotency_key="stale-key"))
    assert out["delivered"] is True
    assert out["method"] == "spool"
    assert out["msg_id"] != "abandoned-msg"


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
