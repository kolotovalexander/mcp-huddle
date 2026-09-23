"""Small mechanical pilot checks; no model processes or network calls."""

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


@pytest.mark.parametrize("mode", ["council", "team", "relay", "swarm"])
def test_four_modes_dispatch_and_finish_without_cli(isolated_home, mode):
    room = swarm_pilot.create(
        "pilot", "Organizer", "Make a tiny result", mode, ["A", "B"],
    )
    assert bus.get_room_info(room)["owner_pid"] == 0
    assert swarm_pilot.due_members(room) == (["A"] if mode in ("council", "relay")
                                            else ["A", "B"])

    if mode != "council":
        swarm_pilot.record(room, "A", "responsibility", "reporter", "Publish final")
    swarm_pilot.record(room, "A", "responsibility", "part-a", "First part")
    swarm_pilot.record(room, "B", "responsibility", "part-b", "Second part")
    swarm_pilot.record(room, "A", "task", "tiny-task", "Produce first part")
    assert swarm_pilot.status(room)["tasks"]["tiny-task"]["member"] == "A"
    with pytest.raises(ValueError, match="another owner"):
        swarm_pilot.record(room, "B", "responsibility", "part-a", "Steal part")

    swarm_pilot.mark_dispatched(room, "A", 1)
    assert swarm_pilot.due_members(room) == (["B"] if mode in ("team", "swarm")
                                            else [])
    swarm_pilot.round_done(room, "A", "First part done")
    assert swarm_pilot.due_members(room) == (["B"] if mode in ("council", "relay")
                                            else ["B"])
    swarm_pilot.mark_dispatched(room, "B", 2)
    swarm_pilot.round_done(room, "B", "Second part done")
    assert swarm_pilot.due_members(room) == []

    final_author = "Organizer" if mode == "council" else "A"
    if mode == "council":
        with pytest.raises(PermissionError, match="organizer"):
            swarm_pilot.finish(room, "A", "wrong")
    else:
        with pytest.raises(PermissionError, match="reporter"):
            swarm_pilot.finish(room, "B", "wrong")
    final = swarm_pilot.finish(room, final_author, "Finished")
    assert final["phase"] == "completed"
    assert final["final"]["member"] == final_author


def test_council_server_pump_waits_for_real_result(isolated_home, monkeypatch):
    monkeypatch.setattr(server.spawn, "get_enabled_spec", lambda name: {"name": name})
    monkeypatch.setattr(server, "_wake_agents_for_request", lambda *args: [])
    room = swarm_pilot.create("pilot", "Organizer", "Decide", "council", ["A", "B"])
    bus.invite_agent(room, "A")
    bus.invite_agent(room, "B")
    first = server.swarm_pilot_pump(room)
    assert [x["member"] for x in first] == ["A"]
    assert server.swarm_pilot_pump(room) == []
    with pytest.raises(ValueError, match="post a result"):
        server.swarm_pilot_round_done(room, "A", "done")
    bus.post_message(room, "A", "My view", "result", to="Organizer",
                     reply_to=first[0]["request_id"])
    response = server.swarm_pilot_round_done(room, "A", "done")
    assert [x["member"] for x in response["next_dispatch"]] == ["B"]
    assert swarm_pilot.status(room)["done"]["A"]["summary"] == "done"


def test_pilot_room_survives_organizer_session_close(isolated_home):
    room = swarm_pilot.create("pilot", "Organizer", "Decide", "team", ["A"])
    assert bus.close_session_rooms("some-session") == []
    assert bus.check_zombie_rooms() == []
    assert bus.get_room_info(room)["status"] == "open"


@pytest.mark.parametrize("mode", ["council", "team", "relay", "swarm"])
def test_public_pilot_workflow_in_all_modes(isolated_home, monkeypatch, mode):
    """Exercise Huddle tools with simulated members, not live models."""
    monkeypatch.setattr(server.spawn, "get_enabled_spec", lambda name: {"name": name})
    monkeypatch.setattr(server, "_wake_agents_for_request", lambda *args: [])
    created = server.swarm_pilot_create(
        "tiny pilot", "Organizer", "Produce one sentence", mode,
        ["A", "B"], start=True,
    )
    room = created["room_id"]
    assert created["started"] is True
    expected_first = ["A"] if mode in ("council", "relay") else ["A", "B"]
    assert [item["member"] for item in created["dispatched"]] == expected_first
    if mode != "council":
        server.swarm_pilot_record(room, "A", "responsibility", "reporter", "Final")

    for member in ("A", "B"):
        request_id = swarm_pilot.status(room)["dispatched"][member]
        server.message_post(room, member, f"{member} result", "result",
                            to="Organizer", reply_to=request_id)
        server.swarm_pilot_round_done(room, member, f"{member} done")

    author = "Organizer" if mode == "council" else "A"
    result = server.swarm_pilot_finish(room, author, "Combined result")
    assert result["phase"] == "completed"
    finals = [m for m in bus._load_messages(room) if m["kind"] == "final"]
    assert len(finals) == 1 and finals[0]["agent"] == author
