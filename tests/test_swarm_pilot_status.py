"""Read-only member identity snapshot for a Swarm pilot room."""

import pytest

from mcp_huddle import bus, server, swarm_pilot


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "huddle-home"
    monkeypatch.setattr(bus, "HUDDLE_HOME", home)
    monkeypatch.setattr(bus, "BUS_DIR", home / "rooms")
    monkeypatch.setattr(bus, "NOTIFICATIONS_DIR", home / "notifications")
    monkeypatch.setattr(bus, "NOTIFICATION_LOCKS_DIR", home / "internal" / "notification-locks")
    return home


def _set_agent_meta(room_id, member, values):
    def update(meta):
        meta.setdefault("agent_meta", {})[member] = dict(values)
        return meta
    bus._update_meta_locked(room_id, update)


def test_status_exposes_codex_session_generation_and_delivery_cursors(isolated_home):
    room = swarm_pilot.create(
        "pilot", "Organizer", "Tiny result", "council", ["Codex"],
    )
    _set_agent_meta(room, "Codex", {
        "thread_id": "codex-thread-123",
        "initial_spawn_id": "first-generation",
        "wake_id": "current-wake",
        "wake_claim_id": "current-wake",
        "last_wake_msg_id": 8,
        "last_seen_id": 8,
        "last_wake_pid": 12345,
    })

    status = server.swarm_pilot_status(room)

    assert status["members"] == ["Codex"]
    assert status["members_detail"] == [{
        "member_id": status["member_ids"]["Codex"],
        "name": "Codex",
        "profile": "Codex",
        "native_session": {"kind": "codex_thread", "id": "codex-thread-123",
                           "source": "agent_meta.thread_id"},
        "process_generation": {"id": "current-wake", "source": "wake_id",
                               "claim_active": True},
        "delivery_cursor": {"last_wake_msg_id": 8, "last_seen_id": 8,
                            "read_receipt": False},
    }]
    assert "thread_id" not in bus.get_room_info(room)["swarm_pilot"]


def test_status_does_not_claim_non_codex_session_or_message_read(isolated_home):
    room = swarm_pilot.create(
        "pilot", "Organizer", "Tiny result", "team", ["Antigravity"],
    )
    _set_agent_meta(room, "Antigravity", {
        "thread_id": "not-a-supported-resume-handle",
        "initial_spawn_id": "initial-123",
        "initial_spawn_active": False,
        "last_wake_msg_id": 12,
        "last_seen_id": 12,
    })

    detail = server.swarm_pilot_status(room)["members_detail"][0]

    assert detail["name"] == "Antigravity"
    assert detail["native_session"] is None
    assert detail["process_generation"] == {
        "id": "initial-123", "source": "initial_spawn_id", "claim_active": False,
    }
    assert detail["delivery_cursor"] == {
        "last_wake_msg_id": 12, "last_seen_id": 12, "read_receipt": False,
    }


def test_status_exposes_legacy_member_ids_without_writing_schema_one(isolated_home):
    room = swarm_pilot.create(
        "pilot", "Organizer", "Tiny result", "swarm", ["A", "B"],
    )

    def make_legacy(meta):
        meta["swarm_pilot"]["schema"] = 1
        meta["swarm_pilot"].pop("member_ids", None)
        return meta
    bus._update_meta_locked(room, make_legacy)

    first = server.swarm_pilot_status(room)
    second = server.swarm_pilot_status(room)

    assert first["schema"] == 1
    assert first["members_detail"] == second["members_detail"]
    assert [detail["member_id"] for detail in first["members_detail"]] == [
        first["member_ids"]["A"], first["member_ids"]["B"],
    ]
    assert all(detail["native_session"] is None
               and detail["process_generation"] is None
               and detail["delivery_cursor"] == {
                   "last_wake_msg_id": None, "last_seen_id": None,
                   "read_receipt": False,
               }
               for detail in first["members_detail"])
    assert "member_ids" not in bus.get_room_info(room)["swarm_pilot"]


def test_status_exposes_only_safe_last_claude_receipt(isolated_home):
    room = swarm_pilot.create(
        "pilot", "Organizer", "Tiny result", "council", ["Reviewer"],
    )
    _set_agent_meta(room, "Reviewer", {
        "wake_id": "new-generation",
        "claude_model_receipt": {
            "reported_model": "claude-opus-5-5", "source": "assistant",
            "generation": "old-generation", "private_log": "must-not-leak",
        },
    })

    detail = server.swarm_pilot_status(room)["members_detail"][0]

    assert detail["last_model_receipt"] == {
        "reported_model": "claude-opus-5-5", "source": "assistant",
        "claim_scope": "cli_reported_identifier", "generation": "old-generation",
    }
    assert "must-not-leak" not in repr(detail)
