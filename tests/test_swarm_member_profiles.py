"""Optional member -> registry profile mapping for the swarm pilot."""

import pytest

from mcp_huddle import bus, server, spawn, swarm_pilot


LUNA = {
    "name": "Codex Pilot Luna",
    "cmd": ["codex", "exec", "--json", "{brief}"],
    "enabled": True,
    "model": "gpt-6-luna",
    "effort": "low",
}
MIMO = {
    "name": "MiMo",
    "cmd": ["python3", "-m", "mcp_huddle.mimo_runner", "--agent", "MiMo", "{brief}"],
    "enabled": True,
}


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "huddle-home"
    monkeypatch.setattr(bus, "HUDDLE_HOME", home)
    monkeypatch.setattr(bus, "BUS_DIR", home / "rooms")
    monkeypatch.setattr(bus, "NOTIFICATIONS_DIR", home / "notifications")
    monkeypatch.setattr(bus, "NOTIFICATION_LOCKS_DIR", home / "internal" / "notification-locks")
    registry = {LUNA["name"]: dict(LUNA), MIMO["name"]: dict(MIMO)}
    monkeypatch.setattr(server.spawn, "get_enabled_spec", registry.get)
    monkeypatch.setattr(server.spawn, "_raw_registry", lambda: list(registry.values()))
    monkeypatch.setattr(server, "_wake_agents_for_request", lambda *args: [])
    return registry


def test_two_members_share_one_profile_and_old_calls_stay_compatible(isolated_home):
    members = ["Luna A", "Luna B"]
    mapping = {member: LUNA["name"] for member in members}
    created = server.swarm_pilot_create(
        "pilot", "Organizer", "Tiny goal", "team", members,
        member_profiles=mapping,
    )
    room = created["room_id"]
    assert [item["status"] for item in created["dispatched"]] == ["dispatched"] * 2

    meta = bus.get_room_info(room)
    state = swarm_pilot.status(room)
    assert state["member_profiles"] == mapping
    assert len(set(state["member_ids"].values())) == 2
    assert set(members) <= set(meta["agent_meta"])  # one wake slot per member
    for member in members:
        spec, drift = server._member_launch_spec(meta, member)
        assert spec["name"] == LUNA["name"] and drift is False
        assert server._swarm_assigned_profile(meta, member) == LUNA["name"]

    # A pinned fingerprint belongs to the mapped profile; changing it drifts.
    fingerprint = spawn.spec_fingerprint(LUNA)

    def pin(value):
        def update(m):
            m["swarm_pilot"]["expected_specs"] = {member: value for member in members}
            return m
        bus._update_meta_locked(room, update)

    pin(fingerprint)
    assert server._member_launch_spec(bus.get_room_info(room), "Luna A")[1] is False
    pin("sha256:" + "0" * 64)
    assert server._member_launch_spec(bus.get_room_info(room), "Luna A")[1] is True

    # Omitted mapping: the member name is the profile, exactly as before.
    legacy = server.swarm_pilot_create(
        "legacy", "Organizer", "Tiny goal", "team", [LUNA["name"]],
    )
    legacy_state = swarm_pilot.status(legacy["room_id"])
    assert "member_profiles" not in legacy_state
    assert legacy["dispatched"][0]["status"] == "dispatched"

    with pytest.raises(ValueError, match="pilot members"):
        server.swarm_pilot_create(
            "bad", "Organizer", "Tiny goal", "team", ["Luna A"],
            member_profiles={"Stranger": LUNA["name"]},
        )
    # Log paths are lower-cased names: case-only differences are refused.
    with pytest.raises(ValueError, match="case-insensitively"):
        server.swarm_pilot_create(
            "case", "Organizer", "Tiny goal", "team", ["Luna A", "luna a"],
            member_profiles={"Luna A": LUNA["name"], "luna a": LUNA["name"]},
        )
    with pytest.raises(ValueError, match="case-insensitively"):
        server.swarm_pilot_create(
            "owner-case", "LUNA A", "Tiny goal", "council", ["Luna A"],
            member_profiles={"Luna A": LUNA["name"]},
        )
    # A runner with a fixed --agent identity cannot serve a renamed member.
    with pytest.raises(ValueError, match="not a supported"):
        server.swarm_pilot_create(
            "runner", "Organizer", "Tiny goal", "team", ["MiMo A"],
            member_profiles={"MiMo A": MIMO["name"]},
        )
    assert not [room for room in bus.list_rooms() if room.get("name") in {"case", "owner-case", "runner"}]
    with pytest.raises(ValueError, match="not a supported"):
        server.swarm_pilot_create(
            "missing", "Organizer", "Tiny goal", "team", ["Luna A"],
            member_profiles={"Luna A": "No Such Profile"},
        )
