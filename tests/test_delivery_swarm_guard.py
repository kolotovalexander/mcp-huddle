"""Two message_send policy guards for the swarm pilot (see docs/delivery.md
"Readonly-caller guard" and "Huddle-owned-session guard"):

1. A read-only Huddle discussant (or one whose read-only status is unknown)
   must not use message_send to hand work off to a privileged live session.
2. message_send must never deliver into a Codex thread Huddle itself
   currently owns via an active wake claim.

Hermetic: everything lives under MCP_HUDDLE_HOME (a tmp dir per test, via the
autouse ``isolate_huddle_storage`` fixture in conftest.py). No real ~/.claude,
~/.codex, sockets or CLIs are touched -- ``codex.native`` is stubbed via
``methods.DISPATCH`` (the same pattern ``test_delivery_core.py`` uses), never
a real subprocess.
"""

import json

import pytest

from mcp_huddle import bus
from mcp_huddle.delivery import caller as caller_mod
from mcp_huddle.delivery import core, methods


@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    d = tmp_path / "codex-home"
    d.mkdir()
    monkeypatch.setenv("MCP_HUDDLE_DELIVERY_CODEX_HOME", str(d))
    return d


def _write_codex_index(codex_home, lines):
    path = codex_home / "session_index.jsonl"
    path.write_text("\n".join(json.dumps(l) for l in lines) + "\n")


def test_readonly_caller_refused_before_any_attempt(codex_home, monkeypatch):
    _write_codex_index(codex_home, [{"id": "th-ro", "thread_name": "x", "updated_at": 1}])
    calls = []
    monkeypatch.setitem(
        methods.DISPATCH, "codex.native",
        lambda target, env_text, cfg: calls.append(1) or methods.MethodResult(True, "codex.native", "queued"),
    )
    caller = caller_mod.Caller(verified_member=True, readonly=True, room_id="room_1",
                                agent="codex-child-1", wake_id="w1")
    out = json.loads(core.message_send("codex:th-ro", "hi", mode="codex.native", caller=caller))
    assert out["delivered"] is False
    assert out["method"] is None
    assert out["attempts"] == []
    assert out["reason"] == "readonly_caller"
    assert calls == []  # no method was ever invoked


def test_unknown_readonly_status_of_huddle_spawned_caller_refused(codex_home, monkeypatch):
    """caller.readonly is None (Huddle knows it spawned this agent -- it
    carried the member-token header -- but couldn't establish its read-only
    status, e.g. room_id/claim didn't resolve) is fail-closed, same as True.
    """
    _write_codex_index(codex_home, [{"id": "th-unknown", "thread_name": "x", "updated_at": 1}])
    calls = []
    monkeypatch.setitem(
        methods.DISPATCH, "codex.native",
        lambda target, env_text, cfg: calls.append(1) or methods.MethodResult(True, "codex.native", "queued"),
    )
    caller = caller_mod.Caller(verified_member=False, readonly=None)
    out = json.loads(core.message_send("codex:th-unknown", "hi", mode="codex.native", caller=caller))
    assert out["delivered"] is False
    assert out["attempts"] == []
    assert out["reason"] == "readonly_caller"
    assert calls == []

    # Sanity: caller=None (no Huddle member-token header at all -- a human or
    # external client) is unaffected, exactly as before this guard existed.
    out_none = json.loads(core.message_send("codex:th-unknown", "hi", mode="codex.native", caller=None))
    assert out_none["delivered"] is True
    assert calls == [1]


def test_huddle_owned_codex_thread_refused_unclaimed_delivered(codex_home, monkeypatch):
    _write_codex_index(codex_home, [
        {"id": "th-owned", "thread_name": "x", "updated_at": 1},
        {"id": "th-free", "thread_name": "y", "updated_at": 1},
    ])
    monkeypatch.setitem(
        methods.DISPATCH, "codex.native",
        lambda target, env_text, cfg: methods.MethodResult(True, "codex.native", "queued"),
    )

    room_id = bus.create_room("swarm room", "System", 0, "")

    def _claim(meta):
        meta.setdefault("agent_meta", {})["codex-member"] = {
            "wake_claim_id": "wake-123", "thread_id": "th-owned",
        }
        return meta
    bus._update_meta_locked(room_id, _claim)

    # Owned thread: refused, nothing attempted.
    out_owned = json.loads(core.message_send("codex:th-owned", "hi", mode="codex.native"))
    assert out_owned["delivered"] is False
    assert out_owned["attempts"] == []
    assert out_owned["reason"] == "huddle_owned_session"

    # A different thread id with no active claim on it: delivered normally.
    out_free = json.loads(core.message_send("codex:th-free", "hi", mode="codex.native"))
    assert out_free["delivered"] is True
    assert out_free["method"] == "codex.native"

    # Clearing the claim on th-owned lets it through too.
    def _clear(meta):
        meta["agent_meta"]["codex-member"]["wake_claim_id"] = None
        return meta
    bus._update_meta_locked(room_id, _clear)
    out_after_clear = json.loads(core.message_send("codex:th-owned", "hi", mode="codex.native"))
    assert out_after_clear["delivered"] is True
