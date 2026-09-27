"""child_room children: own room, selected context, relay, close."""

import time
from types import SimpleNamespace

import pytest

from mcp_huddle import bus, server, spawn

CHILD = {
    "name": "Codex Child",
    "cmd": ["codex", "-a", "never", "exec", "--json", "-s", "read-only", "{brief}"],
    "enabled": True,
}


@pytest.fixture
def launches(tmp_path, monkeypatch):
    home = tmp_path / "huddle-home"
    monkeypatch.setattr(bus, "HUDDLE_HOME", home)
    monkeypatch.setattr(bus, "BUS_DIR", home / "rooms")
    monkeypatch.setattr(bus, "NOTIFICATIONS_DIR", home / "notifications")
    monkeypatch.setattr(bus, "NOTIFICATION_LOCKS_DIR", home / "internal" / "notification-locks")
    monkeypatch.setenv("MCP_HUDDLE_READONLY", "1")
    registry = {CHILD["name"]: dict(CHILD)}
    monkeypatch.setattr(spawn, "_raw_registry", lambda: list(registry.values()))
    monkeypatch.setattr(spawn, "get_enabled_spec", registry.get)
    calls = []

    def fake_spawn(spec, prompt, cwd, log_dir, **kwargs):
        calls.append({"spec": spec, "prompt": prompt, "log_dir": log_dir, **kwargs})
        return 4242, str(log_dir / "child.events.jsonl"), None

    monkeypatch.setattr(spawn, "spawn_agent", fake_spawn)
    return calls


def _setup():
    room = server.swarm_pilot_create(
        "pilot", "Organizer", "Tiny goal", "team", ["A", "B"], start=False,
        child_agents={"max_children": 3, "profiles": [CHILD["name"]]},
    )["room_id"]
    for i in range(12):
        bus.post_message(room, "AB"[i % 2], f"note {i:02d}", "comment")

    def claim(meta):
        meta.setdefault("agent_meta", {}).setdefault("A", {})["wake_claim_id"] = "wakeA"
        return meta
    bus._update_meta_locked(room, claim)
    secret = server._issue_member_token(room, "A", "wakeA")
    request = SimpleNamespace(headers={spawn.MEMBER_TOKEN_HEADER: secret})
    return room, SimpleNamespace(request_context=SimpleNamespace(request=request))


def _child(room, name):
    return bus.get_room_info(room)["swarm_pilot"]["children"][name]


def _relays(room):
    return [m for m in bus._load_messages(room)
            if m.get("agent") == "System" and m.get("kind") == "comment"
            and m.get("to") == "A"]


def test_child_room_context_relay_and_close(launches):
    room, ctx = _setup()
    with pytest.raises(ValueError, match="child_room"):
        server.swarm_spawn_child(room, CHILD["name"], "x", ctx, history="recent")

    # recent context + relay=result
    first = server.swarm_spawn_child(room, CHILD["name"], "find risks", ctx,
                                     invite="child_room", history="recent")
    name, child_room = first["child"], first["child_room"]
    assert child_room != room and "not confidentiality" in first["note"]
    parent_meta = bus.get_room_info(room)
    assert name not in parent_meta["participants"]
    assert name not in (parent_meta.get("agent_meta") or {})
    assert name in bus.get_room_info(child_room)["participants"]
    launch = launches[-1]
    assert launch["owner_room_id"] == child_room and launch["log_name"] == name
    assert "read-only" in launch["spec"]["cmd"] and "member_token" not in launch
    assert "note 11" in launch["prompt"] and "note 00" not in launch["prompt"]
    assert "note 01" not in launch["prompt"]
    assert all(f"note {i:02d}" in launch["prompt"] for i in range(2, 12))
    assert server._claim_wake(child_room, name, 10_000, "again") is False  # one-shot

    server._post_message_checked(child_room, name, "answer 42", "result",
                                 reply_to=first["request_id"])
    launch["on_exit"](0)
    record = _child(room, name)
    assert (record["status"], record["delivery"], record["child_room_closed"]) == (
        "exited", "result", True)
    assert bus.get_room_info(child_room)["status"] == "closed"
    assert any("answer 42" in m["body"] for m in _relays(room))

    # no history + relay=none: nothing from the parent room, nothing relayed
    before = len(_relays(room))
    second = server.swarm_spawn_child(room, CHILD["name"], "quiet", ctx,
                                      invite="child_room", relay="none")
    assert "note 11" not in launches[-1]["prompt"]
    launches[-1]["on_exit"](0)
    assert _child(room, second["child"])["delivery"] == "no_result"
    assert len(_relays(room)) == before
    assert bus.get_room_info(second["child_room"])["status"] == "closed"

    # relay=result without an answer: short notice, no raw log
    third = server.swarm_spawn_child(room, CHILD["name"], "fail", ctx,
                                     invite="child_room")
    launches[-1]["on_exit"](1)
    notice = _relays(room)[-1]["body"]
    assert "without a result (exit 1)" in notice and third["child"] in notice
    with pytest.raises(PermissionError, match="limit"):
        server.swarm_spawn_child(room, CHILD["name"], "fourth", ctx, invite="child_room")


def test_child_room_relay_failure_heals_via_retry_sweep(launches, monkeypatch):
    room, ctx = _setup()
    child = server.swarm_spawn_child(room, CHILD["name"], "find risks", ctx,
                                     invite="child_room")
    name = child["child"]
    server._post_message_checked(child["child_room"], name, "answer 42", "result",
                                 reply_to=child["request_id"])
    server._clear_wake_claim(room, "A", "wakeA")
    real_post = server._post_message_checked
    attempts = 0

    def flaky_post(room_id, *args, **kwargs):
        nonlocal attempts
        if room_id == room and kwargs.get("idempotency_key", "").startswith(
            "swarm-child-relay:"
        ):
            attempts += 1
            if attempts == 1:
                # The parent must not declare completion before relay is attempted.
                snapshot = server.room_status(room)
                assert snapshot["wait_recommended"] is True
                assert snapshot["pending_child_rooms"][0]["status"] == "running"
                raise OSError("temporary relay failure")
        return real_post(room_id, *args, **kwargs)

    monkeypatch.setattr(server, "_post_message_checked", flaky_post)
    on_exit = launches[-1]["on_exit"]
    on_exit(0)
    record = _child(room, name)
    assert record["delivery"] == "relay_failed"
    assert record["relay_status"] == "failed"
    assert record["relay_attempts"] == 1
    assert record["child_room_closed"] is True
    assert not _relays(room)

    # Sweeping before the backoff has elapsed must not retry yet.
    assert server._swarm_retry_failed_relays() == []
    assert _child(room, name)["relay_status"] == "failed"

    # Fast-forward past relay_next_at: the sweep (as the watchdog/startup call
    # it) re-attempts the relay post and it succeeds this time.
    future = time.time() + 3600
    monkeypatch.setattr(server.time, "time", lambda: future)
    retried = server._swarm_retry_failed_relays()
    assert retried == [f"{room}:{name}"]
    record = _child(room, name)
    assert record["delivery"] == "result"
    assert record["relay_status"] == "sent"
    assert len(_relays(room)) == 1

    # Further sweeps add nothing further, launch no new process, and the
    # child room (already closed on exit) stays closed.
    launches_before = len(launches)
    assert server._swarm_retry_failed_relays() == []
    assert len(_relays(room)) == 1
    assert len(launches) == launches_before
    assert bus.get_room_info(child["child_room"])["status"] == "closed"


def test_child_room_relay_gives_up_after_cap_and_uncertain_post_stays_single(
    launches, monkeypatch,
):
    room, ctx = _setup()
    child = server.swarm_spawn_child(room, CHILD["name"], "find risks", ctx,
                                     invite="child_room")
    name = child["child"]
    server._post_message_checked(child["child_room"], name, "answer 42", "result",
                                 reply_to=child["request_id"])
    server._clear_wake_claim(room, "A", "wakeA")
    real_post = server._post_message_checked
    calls = 0

    def flaky_then_uncertain(room_id, *args, **kwargs):
        nonlocal calls
        if room_id == room and kwargs.get("idempotency_key", "").startswith(
            "swarm-child-relay:"
        ):
            calls += 1
            if calls < 3:
                raise OSError("temporary relay failure")
            # 3rd attempt: the bus post actually lands, but the caller sees an
            # exception anyway (e.g. the response was lost) — an "uncertain"
            # outcome. Bus idempotency (durable across processes/restarts,
            # keyed by idempotency_key in messages.jsonl) must keep this to
            # exactly one stored message no matter how many times it's retried.
            real_post(room_id, *args, **kwargs)
            raise TimeoutError("uncertain: posted but response lost")
        return real_post(room_id, *args, **kwargs)

    monkeypatch.setattr(server, "_post_message_checked", flaky_then_uncertain)
    on_exit = launches[-1]["on_exit"]
    on_exit(0)  # attempt 1: fails
    assert _child(room, name)["relay_attempts"] == 1
    assert _child(room, name)["relay_status"] == "failed"

    clock = {"t": time.time()}

    def fast_forward():
        clock["t"] += 3600
        return clock["t"]

    monkeypatch.setattr(server.time, "time", fast_forward)

    server._swarm_retry_failed_relays()  # attempt 2: fails
    record = _child(room, name)
    assert record["relay_attempts"] == 2
    assert record["relay_status"] == "failed"
    assert not _relays(room)

    server._swarm_retry_failed_relays()  # attempt 3: uncertain, cap reached
    record = _child(room, name)
    assert record["relay_attempts"] == 3
    assert record["relay_status"] == "gave_up"
    assert record["delivery"] == "relay_failed"
    assert len(_relays(room)) == 1  # exactly one message despite the retries

    # Terminal: no further attempts, and it stays visible rather than making
    # the parent wait forever.
    assert server._swarm_retry_failed_relays() == []
    assert len(_relays(room)) == 1
    snapshot = server.room_status(room)
    issues = snapshot["child_relay_issues"]
    assert len(issues) == 1
    assert issues[0]["child"] == name
    assert issues[0]["relay_status"] == "gave_up"


def test_parent_waits_for_child_room_and_deleted_parent_does_not_orphan_it(launches):
    room, ctx = _setup()
    child = server.swarm_spawn_child(room, CHILD["name"], "finish independently", ctx,
                                     invite="child_room", relay="none")
    server._clear_wake_claim(room, "A", "wakeA")
    snapshot = server.room_status(room)
    assert snapshot["wait_recommended"] is True
    assert snapshot["all_terminal"] is False
    assert snapshot["pending_child_rooms"] == [{
        "child": child["child"], "room_id": child["child_room"],
        "status": "running",
    }]
    assert child["child"] not in snapshot["participants"]

    # The process belongs to the child room, so closing/deleting the parent
    # does not stop it. Its exact exit callback must still close that room.
    bus.close_room(room, "Organizer")
    bus.delete_room(room, "Organizer")
    server._post_message_checked(child["child_room"], child["child"], "done",
                                 "result", reply_to=child["request_id"])
    launches[-1]["on_exit"](0)
    assert bus.get_room_info(child["child_room"])["status"] == "closed"


def test_room_status_waits_on_relay_retry_pending_then_clears_after_sweep(
    launches, monkeypatch,
):
    """A child whose room already closed but whose relay is stuck at
    relay_status="failed" still has a retry sweep pending: room_status must
    keep recommending wait (not declare all_terminal) until the sweep either
    lands the relay or gives up, otherwise an organizer could finish the
    room before the answer is relayed.
    """
    room, ctx = _setup()
    child = server.swarm_spawn_child(room, CHILD["name"], "find risks", ctx,
                                     invite="child_room")
    name = child["child"]
    server._post_message_checked(child["child_room"], name, "answer 42", "result",
                                 reply_to=child["request_id"])
    server._clear_wake_claim(room, "A", "wakeA")
    real_post = server._post_message_checked

    def flaky_post(room_id, *args, **kwargs):
        if room_id == room and kwargs.get("idempotency_key", "").startswith(
            "swarm-child-relay:"
        ):
            raise OSError("temporary relay failure")
        return real_post(room_id, *args, **kwargs)

    monkeypatch.setattr(server, "_post_message_checked", flaky_post)
    launches[-1]["on_exit"](0)
    record = _child(room, name)
    assert record["relay_status"] == "failed"
    assert record["child_room_closed"] is True

    snapshot = server.room_status(room)
    assert snapshot["wait_recommended"] is True
    assert snapshot["all_terminal"] is False
    retry_entries = [c for c in snapshot["pending_child_rooms"] if c["child"] == name]
    assert retry_entries == [{
        "child": name, "room_id": child["child_room"], "status": "relay_retry",
    }]

    # Once the sweep lands the relay, room_status must go terminal.
    monkeypatch.setattr(server, "_post_message_checked", real_post)
    future = time.time() + 3600
    monkeypatch.setattr(server.time, "time", lambda: future)
    assert server._swarm_retry_failed_relays() == [f"{room}:{name}"]
    assert _child(room, name)["relay_status"] == "sent"

    snapshot = server.room_status(room)
    assert snapshot["wait_recommended"] is False
    assert snapshot["all_terminal"] is True
    assert snapshot["pending_child_rooms"] == []


def test_reordered_relay_completion_keeps_sent_sticky(launches, monkeypatch):
    """Two watchdogs can both see relay_status="failed" and race to call
    _swarm_deliver_child_room; the bus dedupes the relay message itself, but
    a stale writer finishing after a successful one must never resurrect a
    failure over an already-recorded success.
    """
    room, ctx = _setup()
    child = server.swarm_spawn_child(room, CHILD["name"], "find risks", ctx,
                                     invite="child_room")
    name = child["child"]
    server._post_message_checked(child["child_room"], name, "answer 42", "result",
                                 reply_to=child["request_id"])
    server._clear_wake_claim(room, "A", "wakeA")

    # Attempt A: the real, successful relay post — sets relay_status="sent".
    launches[-1]["on_exit"](0)
    record = _child(room, name)
    assert record["relay_status"] == "sent"
    assert record["delivery"] == "result"
    child_wake = record["wake_id"]
    assert len(_relays(room)) == 1

    # Attempt B: a stale/reordered watchdog write for the same wake, computed
    # from a pre-success snapshot, landing after A already wrote "sent".
    server._swarm_set_child_fields(room, name, child_wake, {
        "delivery": "relay_failed",
        "relay_status": "failed",
        "relay_attempts": 1,
        "relay_error": "OSError",
        "relay_next_at": int(time.time()) + 5,
    })

    record = _child(room, name)
    assert record["relay_status"] == "sent"
    assert record["delivery"] == "result"
    assert len(_relays(room)) == 1
