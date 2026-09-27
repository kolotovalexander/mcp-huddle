"""Bounded replacement of a failed pilot member through the real wake/exit path."""

import pytest

from mcp_huddle import (bus, child_processes, room_workspace, server, spawn,
                        swarm_pilot, swarm_replacement)


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "huddle-home"
    monkeypatch.setattr(bus, "HUDDLE_HOME", home)
    monkeypatch.setattr(bus, "BUS_DIR", home / "rooms")
    monkeypatch.setattr(bus, "NOTIFICATIONS_DIR", home / "notifications")
    monkeypatch.setattr(bus, "NOTIFICATION_LOCKS_DIR", home / "internal" / "notification-locks")

    specs = {
        "Alpha": {"name": "Alpha", "cmd": ["alpha-cli"], "enabled": True},
        "Backup": {"name": "Backup", "cmd": ["backup-cli"], "enabled": True,
                   "swarm_replacement": True},
        "Plain": {"name": "Plain", "cmd": ["plain-cli"], "enabled": True},
    }
    launched = []
    monkeypatch.setattr(spawn, "load_registry", lambda: list(specs.values()))
    monkeypatch.setattr(spawn, "_raw_registry", lambda: list(specs.values()))
    monkeypatch.setattr(spawn, "get_enabled_spec", lambda n: specs.get(n))
    monkeypatch.setattr(spawn, "spec_fingerprint", lambda s: "fp:" + s["name"])

    def fake_spawn(spec, prompt, cwd, log_dir, **kw):
        # Same path rule as spawn.spawn_agent: the room identity owns the log.
        log, last = bus._agent_paths(kw["owner_room_id"],
                                     kw.get("log_name") or spec["name"], create=True)
        launched.append({"name": spec["name"], "log_name": kw.get("log_name"),
                         "on_exit": kw["on_exit"], "prompt": prompt})
        return 4242 + len(launched), str(log), str(last)
    monkeypatch.setattr(spawn, "spawn_agent", fake_spawn)
    # The reaper fires on_exit only after the exact Popen exited.
    monkeypatch.setattr(child_processes, "state", lambda room, handle: "exited")
    monkeypatch.setattr(server, "_log_tail", lambda *a, **k: "HTTP 429 rate limit")
    monkeypatch.setattr(server, "_handle_rate_limit_on_exit", lambda *a: False)
    monkeypatch.setattr(server, "_announce_noreply_on_exit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_record_claude_model_receipt", lambda *a: None)
    return launched


def _room(workspace=None):
    """Pilot room whose Alpha is woken by the real request path."""
    room = swarm_pilot.create("pilot", "Org", "tiny", "team", ["Alpha"])
    bus.invite_agent(room, "Alpha")
    server._merge_agent_meta(room, "Alpha", {})
    if workspace:
        bus._update_meta_locked(room, lambda m: {**m, "room_workspace": workspace})
    server.swarm_pilot_pump(room)
    req = next(m["id"] for m in bus._load_messages(room) if m["kind"] == "request")
    return room, req


def _alpha(room):
    return bus.get_room_info(room)["agent_meta"]["Alpha"]


def _pending(room):
    return server.room_status(room)["agents"]["Alpha"]["pending_request_ids"]


def test_rate_limited_member_continues_on_backup_under_same_identity(env):
    launched = env
    room, req = _room()
    member_id = swarm_pilot.status(room)["member_ids"]["Alpha"]
    status = bus.get_status_details(room)["Alpha"]
    assert [c["name"] for c in launched] == ["Alpha"]
    assert status["phase"] == "working" and str(status["task_id"]) == str(req)

    launched[0]["on_exit"](1)                               # exact child: 429

    assert [c["name"] for c in launched] == ["Alpha", "Backup"]
    assert launched[1]["log_name"] == "Alpha"
    assert "You are: Alpha" in launched[1]["prompt"]
    info = _alpha(room)
    assert info["log_path"] == str(bus._agent_paths(room, "Alpha")[0])
    assert info["swarm_route"]["profile"] == "Backup"
    assert info["wake_claim_id"] == info["swarm_route"]["generation"]
    # The old diagnostic receipt stays, yet the request is open again.
    receipts = bus.get_status_details(room)["Alpha"]["terminal_failure_receipts"]
    assert [str(r["task_id"]) for r in receipts] == [str(req)]
    assert req in _pending(room)
    detail = server.swarm_pilot_status(room)["members_detail"][0]
    assert (detail["member_id"], detail["name"], detail["profile"]) == (member_id, "Alpha", "Backup")
    assert any("продолжает через Backup" in m["body"] for m in bus._load_messages(room))

    # The backup answers as Alpha and completes the same member's round.
    server._post_message_checked(room, "Alpha", "done", kind="result", reply_to=req)
    server.swarm_pilot_round_done(room, "Alpha", "done")
    state = swarm_pilot.status(room)
    assert "Alpha" in state["done"] and state["member_ids"]["Alpha"] == member_id
    assert req not in _pending(room)


def test_stale_or_foreign_generation_never_relaunches(env, monkeypatch):
    launched = env

    # Another owner takes the member before this callback's route CAS.
    room, _ = _room()
    real_plan = swarm_replacement.plan_replacement

    def racing_plan(*args, **kwargs):
        server._merge_agent_meta(room, "Alpha", {"wake_id": "foreign",
                                                 "wake_claim_id": "foreign"})
        return real_plan(*args, **kwargs)
    monkeypatch.setattr(swarm_replacement, "plan_replacement", racing_plan)
    launched[-1]["on_exit"](1)
    assert [c["name"] for c in launched] == ["Alpha"]
    assert _alpha(room)["wake_claim_id"] == "foreign"
    assert "swarm_route" not in _alpha(room)
    monkeypatch.setattr(swarm_replacement, "plan_replacement", real_plan)

    # A duplicate callback of the replaced generation is ignored; the one
    # backup is spent afterwards, so its failure is terminal and settles.
    room, req = _room()
    old_exit = launched[-1]["on_exit"]
    old_exit(1)
    generation = _alpha(room)["swarm_route"]["generation"]
    count = len(launched)
    old_exit(1)
    assert len(launched) == count
    assert _alpha(room)["swarm_route"]["generation"] == generation
    launched[-1]["on_exit"](1)
    assert len(launched) == count
    route = _alpha(room)["swarm_route"]
    assert (route["terminal"], route["profile"]) == ("terminal", "Backup")
    assert req not in _pending(room)

    # A pending permission prompt is never answered by a replacement.
    room, _ = _room()
    server._merge_agent_meta(room, "Alpha", {"waiting_for": "permission"})
    count = len(launched)
    launched[-1]["on_exit"](1)
    assert len(launched) == count
    assert _alpha(room)["swarm_route"]["terminal"] == "needs_user"


def test_write_room_never_launches_denied_backup(env, monkeypatch):
    launched = env

    def launch(meta, member, spec):
        if spec["name"] == "Backup":
            raise ValueError("write room refuses Backup")
        return spec, meta.get("cwd", "")
    monkeypatch.setattr(room_workspace, "launch", launch)
    room, _ = _room({"write_policy": room_workspace.SHARED_WRITE, "root": "/w"})
    assert [c["name"] for c in launched] == ["Alpha"]

    launched[0]["on_exit"](1)

    assert [c["name"] for c in launched] == ["Alpha"]
    assert _alpha(room)["swarm_route"]["terminal"] == "terminal"
    assert "wake_claim_id" not in _alpha(room)
