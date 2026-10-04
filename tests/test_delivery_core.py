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


def test_declared_native_route_refuses_before_transport_or_idempotency(codex_home, monkeypatch):
    _write_codex_index(codex_home, [{"id": "native-th", "thread_name": "x", "updated_at": 1}])
    reserved = []
    finished = []
    monkeypatch.setattr(core.idempotency, "reserve", lambda *args: reserved.append(args))
    monkeypatch.setattr(core.idempotency, "finish", lambda *args: finished.append(args))
    out = json.loads(core.message_send(
        "codex:native-th", "please handle this", mode="spool",
        idempotency_key="native-route",
        native_routes=[{
            "target": "codex:native-th",
            "tool": "mcp__codex_app__send_message_to_thread",
            "reason": "The caller confirmed this tool can reach this thread.",
        }],
    ))
    assert out["delivered"] is False
    assert out["attempts"] == []
    assert out["reason"] == "native_route_required"
    assert out["suggested_tool"] == "mcp__codex_app__send_message_to_thread"
    assert "caller confirmed" in out["note"]
    assert _spool_files() == []
    assert reserved == []
    assert finished == []


@pytest.mark.parametrize("route,refused", [({"available": True}, True), ({"available": False}, False), ({"available": "true"}, False)])
def test_generic_native_availability_without_tool_name(codex_home, route, refused):
    _write_codex_index(codex_home, [{"id": "generic-th", "thread_name": "x", "updated_at": 1}])
    out = json.loads(core.message_send(
        "codex:generic-th", "hello", mode="spool",
        native_routes=[{"target": "codex:generic-th", **route}],
    ))
    assert (out.get("reason") == "native_route_required") is refused
    assert "suggested_tool" not in out
    assert len(_spool_files()) == (0 if refused else 1)
    if refused:
        assert out["attempts"] == []
        assert "native communication tools" in out["note"]


def test_unknown_native_availability_keeps_huddle_fallback(codex_home):
    _write_codex_index(codex_home, [{"id": "unknown-th", "thread_name": "x", "updated_at": 1}])
    out = json.loads(core.message_send("codex:unknown-th", "please handle this", mode="spool"))
    assert out["delivered"] is True
    assert out["method"] == "spool"
    assert len(_spool_files()) == 1


def test_nonmatching_native_route_does_not_block_other_direct_targets(codex_home):
    _write_codex_index(codex_home, [{"id": "room-th", "thread_name": "x", "updated_at": 1}])
    out = json.loads(core.message_send(
        "codex:room-th", "persistent council result", mode="spool",
        native_routes=[{
            "target": "codex:another-thread",
            "tool": "collaboration.send_message",
            "reason": "Internal subagent handoff only.",
        }],
    ))
    assert out["delivered"] is True
    assert out["method"] == "spool"


def test_shared_room_workflow_remains_available():
    from mcp_huddle import bus, server

    room_id = server.room_create("council", owner="Organizer", owner_pid=0)
    message_id = server.message_post(room_id, "Organizer", "review the proposal", "request")
    assert message_id > 0
    assert "review the proposal" in bus.read_messages(room_id)


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


def test_claude_old_name_lookup_never_resumes_a_live_session(claude_dir, tmp_path, monkeypatch):
    """Regression for Codex review finding A (second independent review): a
    stale record under an old name sharing the sessionId of a live record
    under a new name must not cause auto mode to dispatch claude.resume
    against that live session."""
    sock = tmp_path / "old-name-live.sock"
    sock.write_text("")
    _write_claude_session(claude_dir, "1.json", pid=_dead_pid(), sessionId="renamed-session",
                           name="old-name", messagingSocketPath=str(sock))
    _write_claude_session(claude_dir, "2.json", pid=os.getpid(), sessionId="renamed-session",
                           name="new-name", messagingSocketPath=str(sock), cwd=str(tmp_path))

    resume_calls = []

    def recording_resume(target, text, cfg):
        resume_calls.append(1)
        return methods.MethodResult(True, "claude.resume", "started")

    monkeypatch.setitem(methods.DISPATCH, "claude.resume", recording_resume)

    out = json.loads(core.message_send("claude:old-name", "hi"))
    assert resume_calls == [], "claude.resume must never run: the session is actually live"
    resume_attempt = next(a for a in out["attempts"] if a["method"] == "claude.resume")
    assert resume_attempt["ok"] is False
    assert "live" in resume_attempt["detail"]


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


def test_opencode_http_error_blocks_resume_in_auto_mode(monkeypatch):
    """Regression for Codex review finding B (second independent review):
    an HTTPError (401/500/503/...) from opencode.native does NOT prove the
    target has no live owner -- it must be ambiguous and block the paired
    opencode.resume, same as a timeout or connection reset."""
    import urllib.error

    def http_error_native(target, text, cfg):
        exc = urllib.error.HTTPError("http://x", 500, "Internal Server Error", {}, None)
        return methods.MethodResult(False, "opencode.native", f"http error: {exc}", ambiguous=True)

    def unexpected_resume(target, text, cfg):
        raise AssertionError("opencode.resume must not run after an HTTPError from opencode.native")

    monkeypatch.setitem(methods.DISPATCH, "opencode.native", http_error_native)
    monkeypatch.setitem(methods.DISPATCH, "opencode.resume", unexpected_resume)
    cfg_path = delivery_config.config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({"opencode": {"server_url": "http://127.0.0.1:9999"}}))
    out = json.loads(core.message_send("opencode:sess-http-err", "hi"))
    assert out["method"] == "spool"
    resume_attempt = next(a for a in out["attempts"] if a["method"] == "opencode.resume")
    assert resume_attempt["ok"] is False
    assert "ambiguous" in resume_attempt["detail"]


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


def test_dead_owner_reservation_yields_unknown_outcome_not_a_resend(codex_home, monkeypatch):
    """Regression for Codex review finding D (second independent review): a
    crashed sender's reservation must NOT be auto-taken-over and resent --
    the owner might have already delivered the message before dying, and a
    stale-takeover resend could double-send it. `message_send` must instead
    surface `unknown_outcome` and never call a send method again."""
    from mcp_huddle.delivery import idempotency

    _write_codex_index(codex_home, [{"id": "th-stale", "thread_name": "x", "updated_at": 1}])
    path = idempotency._key_path("stale-key")
    path.parent.mkdir(parents=True, exist_ok=True)
    dead_pid = _dead_pid()
    path.write_text(json.dumps({
        "status": "reserved", "msg_id": "abandoned-msg", "pid": dead_pid, "ts": time.time(),
    }))

    real_spool = core.methods.spool
    resend_allowed = {"value": False}

    def guarded_spool(*a, **kw):
        if not resend_allowed["value"]:
            raise AssertionError("a dead-owner reservation must never trigger an automatic resend")
        return real_spool(*a, **kw)

    monkeypatch.setattr(core.methods, "spool", guarded_spool)

    out = json.loads(core.message_send("codex:th-stale", "hi", mode="spool", idempotency_key="stale-key"))
    assert out["status"] == "unknown_outcome"
    assert out["msg_id"] == "abandoned-msg"
    assert out["delivered"] is None
    assert _spool_files() == []

    # A repeat with the SAME key stays "unknown" -- still never resent.
    out2 = json.loads(core.message_send("codex:th-stale", "hi", mode="spool", idempotency_key="stale-key"))
    assert out2["status"] == "unknown_outcome"
    assert out2["msg_id"] == "abandoned-msg"

    # A caller that actually wants delivery must use a NEW idempotency_key.
    resend_allowed["value"] = True
    out3 = json.loads(core.message_send("codex:th-stale", "hi", mode="spool", idempotency_key="fresh-key"))
    assert out3["delivered"] is True
    assert out3["method"] == "spool"


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


@pytest.mark.parametrize('sender,reason,refused', [
    ('codex', '', True), (' CODEX ', '  ', True),
    ('codex', 'Native tool cannot address this session', False),
    ('claude', '', False), ('', '', False), ('unknown', '', False),
])
def test_same_harness_guidance_and_fallback(codex_home, sender, reason, refused):
    _write_codex_index(codex_home, [{'id': 'same-th', 'thread_name': 'x'}])
    out = json.loads(core.message_send(
        'codex:same-th', 'hello', mode='spool', sender_harness=sender,
        native_unavailable_reason=reason, idempotency_key='same-route',
    ))
    assert out['delivered'] is not refused
    assert len(_spool_files()) == (0 if refused else 1)
    if refused:
        assert out['reason'] == 'native_route_required'
        assert out['attempts'] == []
        # Refusal must not reserve the key and prevent the explicit fallback.
        retry = json.loads(core.message_send(
            'codex:same-th', 'hello', mode='spool', sender_harness=sender,
            native_unavailable_reason='No exposed native session tool',
            idempotency_key='same-route',
        ))
        assert retry['delivered'] is True


def test_same_harness_fallback_does_not_override_guards(codex_home):
    from mcp_huddle.delivery.caller import Caller
    _write_codex_index(codex_home, [{'id': 'guard-th', 'thread_name': 'x'}])
    args = dict(sender_harness='codex', native_unavailable_reason='No native tool')
    out = json.loads(core.message_send('codex:guard-th', 'hello', mode='spool',
                                      caller=Caller(readonly=True), **args))
    assert out['reason'] == 'readonly_caller'
    out = json.loads(core.message_send('codex:guard-th', 'hello', mode='spool',
        native_routes=[{'target': 'codex:guard-th', 'available': True}], **args))
    assert out['reason'] == 'native_route_required'
    assert _spool_files() == []
