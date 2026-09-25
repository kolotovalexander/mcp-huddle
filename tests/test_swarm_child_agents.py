"""Participant child agents: opt-in policy, verified parent, atomic slot."""

from types import SimpleNamespace

import pytest

from mcp_huddle import bus, server, spawn

CHILD = {
    "name": "Codex Child",
    "cmd": ["codex", "-a", "never", "exec", "--json", "-s", "read-only", "{brief}"],
    "enabled": True,
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "huddle-home"
    monkeypatch.setattr(bus, "HUDDLE_HOME", home)
    monkeypatch.setattr(bus, "BUS_DIR", home / "rooms")
    monkeypatch.setattr(bus, "NOTIFICATIONS_DIR", home / "notifications")
    monkeypatch.setattr(bus, "NOTIFICATION_LOCKS_DIR", home / "internal" / "notification-locks")
    monkeypatch.setenv("MCP_HUDDLE_READONLY", "1")
    registry = {CHILD["name"]: dict(CHILD)}
    monkeypatch.setattr(spawn, "_raw_registry", lambda: list(registry.values()))
    monkeypatch.setattr(spawn, "get_enabled_spec", registry.get)
    launches = []

    def fake_spawn(spec, prompt, cwd, log_dir, **kwargs):
        if launches and launches[-1] == "fail-next":
            launches.pop()
            raise RuntimeError("provider down")
        launches.append({"spec": spec, "prompt": prompt, **kwargs})
        return 4242, str(log_dir / "child.events.jsonl"), None

    monkeypatch.setattr(spawn, "spawn_agent", fake_spawn)
    return launches


def _room(child_agents=None):
    created = server.swarm_pilot_create(
        "pilot", "Organizer", "Tiny goal", "team", ["A", "B"], start=False,
        child_agents=child_agents,
    )
    return created["room_id"]


def _verified_ctx(room, member, wake):
    def claim(meta):
        meta.setdefault("agent_meta", {}).setdefault(member, {})["wake_claim_id"] = wake
        return meta
    bus._update_meta_locked(room, claim)
    secret = server._issue_member_token(room, member, wake)
    request = SimpleNamespace(headers={spawn.MEMBER_TOKEN_HEADER: secret})
    return SimpleNamespace(request_context=SimpleNamespace(request=request))


def test_child_agent_pilot_is_opt_in_bounded_and_read_only(env):
    launches = env
    # Disabled by default, even for a verified member.
    plain = _room()
    with pytest.raises(PermissionError, match="disabled"):
        server.swarm_spawn_child(plain, CHILD["name"], "help", _verified_ctx(plain, "A", "w0"))
    with pytest.raises(ValueError, match="read-only Codex or Claude"):
        _room({"max_children": 1, "profiles": ["Unknown"]})

    room = _room({"max_children": 2, "profiles": [CHILD["name"]]})
    ctx = _verified_ctx(room, "A", "wakeA")
    unverified = SimpleNamespace(request_context=SimpleNamespace(request=None))
    with pytest.raises(PermissionError, match="verified"):
        server.swarm_spawn_child(room, CHILD["name"], "help", unverified)
    with pytest.raises(PermissionError):
        server.swarm_spawn_child(room, "Other Profile", "help", ctx)

    # A failed launch releases its slot and claim.
    launches.append("fail-next")
    with pytest.raises(ValueError, match="launch failed"):
        server.swarm_spawn_child(room, CHILD["name"], "first try", ctx)
    meta = bus.get_room_info(room)
    assert meta["swarm_pilot"]["children"]["A-child-1"]["status"] == "failed"
    assert "wake_claim_id" not in meta["agent_meta"]["A-child-1"]

    result = server.swarm_spawn_child(room, CHILD["name"], "summarize risks", ctx)
    assert result["child"] == "A-child-2" and result["parent"] == "A"
    launch = launches[-1]
    assert launch["log_name"] == "A-child-2" and "member_token" not in launch
    assert "read-only" in launch["spec"]["cmd"]
    meta = bus.get_room_info(room)
    assert "A-child-2" in meta["participants"]
    request = [m for m in bus._load_messages(room) if m["id"] == result["request_id"]][0]
    assert (request["agent"], request["to"], request["kind"]) == ("A", "A-child-2", "request")

    server.swarm_spawn_child(room, CHILD["name"], "second helper", ctx)
    with pytest.raises(PermissionError, match="limit"):
        server.swarm_spawn_child(room, CHILD["name"], "one too many", ctx)

    # The parent's identity dies with its wake claim.
    def release(meta):
        meta["agent_meta"]["A"].pop("wake_claim_id")
        return meta
    bus._update_meta_locked(room, release)
    with pytest.raises(PermissionError, match="verified"):
        server.swarm_spawn_child(room, CHILD["name"], "late", ctx)


def test_child_is_one_shot_and_reaches_exact_terminal_state(env, monkeypatch):
    launches = env
    room = _room({"max_children": 2, "profiles": [CHILD["name"]]})
    ctx = _verified_ctx(room, "A", "wakeA")

    # Normal run: running after launch, exited after its own callback.
    first = server.swarm_spawn_child(room, CHILD["name"], "normal", ctx)
    assert first["status"] == "running"
    # While live, the child owes a reply to its own request only.
    waiting = {item["id"]: item["waiting_for"]
               for item in server.room_status(room)["pending_requests"]}
    assert first["child"] in waiting[first["request_id"]]
    launches[-1]["on_exit"](0)
    child = bus.get_room_info(room)["swarm_pilot"]["children"][first["child"]]
    assert (child["status"], child["returncode"]) == ("exited", 0)
    assert "wake_claim_id" not in bus.get_room_info(room)["agent_meta"][first["child"]]

    # Fast exit: the callback fires before spawn_agent returns; the late
    # publish must not overwrite the terminal state with running.
    real_fake = spawn.spawn_agent

    def fast_exit(spec, prompt, cwd, log_dir, **kwargs):
        result = real_fake(spec, prompt, cwd, log_dir, **kwargs)
        kwargs["on_exit"](1)
        return result

    monkeypatch.setattr(spawn, "spawn_agent", fast_exit)
    second = server.swarm_spawn_child(room, CHILD["name"], "fast", ctx)
    assert second["status"] == "exited"
    child = bus.get_room_info(room)["swarm_pilot"]["children"][second["child"]]
    assert (child["status"], child["returncode"]) == ("exited", 1)

    # A later ordinary request never relaunches a finished child, even if a
    # registry profile had the same name.
    count = len(launches)
    monkeypatch.setattr(spawn, "get_enabled_spec", lambda name: dict(CHILD, name=name))
    msg = server.message_post(room, "B", "one more?", "request", to=first["child"])
    assert server._wake_agents_for_request(
        room, "B", "one more?", first["child"], None, msg) == []
    assert server._claim_wake(room, first["child"], msg, "manual") is False
    with pytest.raises(ValueError, match="one-shot"):
        server._spawn_fresh_room_agent(room, first["child"], "x", bus.get_room_info(room))
    assert len(launches) == count

    # Neither a late direct nor a late broadcast request waits for a child.
    broadcast = server.message_post(room, "B", "anyone?", "request")
    status = server.room_status(room)
    children = {first["child"], second["child"]}
    for item in status["pending_requests"]:
        assert not children & set(item["waiting_for"]), item
    ids = {item["id"] for item in status["pending_requests"]}
    assert msg not in ids  # direct request to the finished child
    assert broadcast in ids  # still waits for real members
    assert all(not status["agents"][name]["pending_request_ids"] for name in children)
