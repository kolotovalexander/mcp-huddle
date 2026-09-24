"""One Claude CLI log receipt belongs to one Huddle process generation."""

import json
import os

from mcp_huddle import bus, server, swarm_pilot


def _event(model: str, session: str) -> bytes:
    return (json.dumps({"type": "assistant", "session_id": session,
                        "parent_tool_use_id": None,
                        "message": {"model": model}}) + "\n").encode()


def test_claude_receipt_ignores_older_segment_and_stale_callback(tmp_path, monkeypatch):
    home = tmp_path / "huddle-home"
    monkeypatch.setattr(bus, "HUDDLE_HOME", home)
    monkeypatch.setattr(bus, "BUS_DIR", home / "rooms")
    monkeypatch.setattr(bus, "NOTIFICATIONS_DIR", home / "notifications")
    monkeypatch.setattr(bus, "NOTIFICATION_LOCKS_DIR", home / "internal" / "notification-locks")
    room = swarm_pilot.create("pilot", "Organizer", "Tiny result", "council", ["Reviewer"])
    agent = "Reviewer"
    log, _ = bus._agent_paths(room, agent, create=True)
    old = _event("claude-sonnet-5", "older-session")
    log.write_bytes(old)
    spec = {"profile": server.spawn._SUBSCRIPTION_OPUS_REVIEW_PROFILE}

    assert server._reserve_initial_spawn(room, agent, "initial-generation", {})
    first = server._claude_receipt_log_open(
        room, agent, "initial-generation", "initial_spawn_id", spec,
    )
    file_stat = os.stat(log)
    first(len(old), str(log), file_stat.st_dev, file_stat.st_ino)
    current = _event("claude-opus-5-5", "current-session")
    with log.open("ab") as stream:
        stream.write(current)
    server._record_claude_model_receipt(room, agent, "initial-generation", "initial_spawn_id")
    assert bus.get_room_info(room)["agent_meta"][agent]["claude_model_receipt"]["reported_model"] == "claude-opus-5-5"

    server._mark_initial_spawn_finished(room, agent, "initial-generation")
    assert server._claim_explicit_wake(room, agent, "wake-generation")
    second = server._claude_receipt_log_open(
        room, agent, "wake-generation", "wake_id", spec,
    )
    second(len(old) + len(current), str(log), file_stat.st_dev, file_stat.st_ino)
    with log.open("ab") as stream:
        stream.write(_event("claude-haiku-5", "next-session"))
    server._record_claude_model_receipt(room, agent, "wake-generation", "wake_id")
    server._record_claude_model_receipt(room, agent, "initial-generation", "initial_spawn_id")
    receipt = bus.get_room_info(room)["agent_meta"][agent]["claude_model_receipt"]
    assert receipt["reported_model"] == "claude-haiku-5"
    assert receipt["generation"] == "wake-generation"
