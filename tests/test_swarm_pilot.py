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


def test_member_brief_exits_after_round_done_instead_of_polling(isolated_home):
    room = swarm_pilot.create("pilot", "Organizer", "Decide", "swarm", ["A", "B"])
    brief = server._swarm_pilot_request(room, "A")
    assert "kind='responsibility', key='reporter'" in brief
    assert "end this CLI turn; do not poll or wait for peers" in brief
    assert "separate addressed final request" in brief


def test_round_done_stops_only_exact_local_generation_after_reply(
    isolated_home, monkeypatch,
):
    monkeypatch.setattr(server.spawn, "get_enabled_spec", lambda name: {"name": name})
    monkeypatch.setattr(server, "_wake_agents_for_request", lambda *args: [])
    timers = []
    terminated = []

    class DeferredTimer:
        def __init__(self, delay, callback, args=(), kwargs=None):
            self.delay = delay
            self.callback = callback
            self.args = args
            self.kwargs = kwargs or {}
            timers.append(self)

        def start(self):
            pass

    monkeypatch.setattr(server.threading, "Timer", DeferredTimer)
    monkeypatch.setattr(server.child_processes, "state",
                        lambda room, handle: "alive" if handle == "wake-A-1"
                        else "unknown")
    monkeypatch.setattr(server.child_processes, "terminate",
                        lambda room, handle: terminated.append((room, handle)) or "sent")

    created = server.swarm_pilot_create(
        "pilot", "Organizer", "Decide", "swarm", ["A", "B"], start=True,
    )
    room = created["room_id"]
    request_id = swarm_pilot.status(room)["dispatched"]["A"]
    server._merge_agent_meta(room, "A", {
        "wake_id": "wake-A-1", "external": True,
    })
    server.message_post(room, "A", "My result", "result", to="Organizer",
                        reply_to=request_id)

    response = server.swarm_pilot_round_done(room, "A", "done")

    assert response["state"]["done"]["A"]["summary"] == "done"
    assert swarm_pilot.status(room)["phase"] == "working"
    assert len(timers) == 1
    assert timers[0].delay == server._PILOT_MEMBER_EXIT_DELAY_SECONDS
    assert not terminated
    server._merge_agent_meta(room, "A", {"wake_id": "new-generation"})
    timers[0].callback(*timers[0].args, **timers[0].kwargs)
    assert not terminated
    server._merge_agent_meta(room, "A", {"wake_id": "wake-A-1"})
    timers[0].callback(*timers[0].args, **timers[0].kwargs)
    assert terminated == [(room, "wake-A-1")]


def test_round_done_timer_still_releases_reporter_after_peers_finish(
    isolated_home, monkeypatch,
):
    monkeypatch.setattr(server.spawn, "get_enabled_spec", lambda name: {"name": name})
    monkeypatch.setattr(server, "_wake_agents_for_request", lambda *args: [])
    timers = []
    terminated = []

    class DeferredTimer:
        def __init__(self, delay, callback, args=(), kwargs=None):
            self.callback = callback
            self.args = args
            self.kwargs = kwargs or {}
            timers.append(self)

        def start(self):
            pass

    monkeypatch.setattr(server.threading, "Timer", DeferredTimer)
    monkeypatch.setattr(server.child_processes, "state",
                        lambda room, handle: "alive" if handle == "wake-A-1"
                        else "unknown")
    monkeypatch.setattr(server.child_processes, "terminate",
                        lambda room, handle: terminated.append((room, handle)) or "sent")

    created = server.swarm_pilot_create(
        "pilot", "Organizer", "Decide", "team", ["A", "B"], start=True,
    )
    room = created["room_id"]
    server.swarm_pilot_record(room, "A", "responsibility", "reporter", "Synthesize")
    a_request = swarm_pilot.status(room)["dispatched"]["A"]
    server._merge_agent_meta(room, "A", {"wake_id": "wake-A-1"})
    server.message_post(room, "A", "A result", "result", to="Organizer",
                        reply_to=a_request)
    a_done = server.swarm_pilot_round_done(room, "A", "done")
    assert "B" in a_done["state"]["dispatched"]
    assert len(timers) == 1

    b_request = swarm_pilot.status(room)["dispatched"]["B"]
    server.message_post(room, "B", "B result", "result", to="Organizer",
                        reply_to=b_request)
    b_done = server.swarm_pilot_round_done(room, "B", "done")
    assert b_done["final_request"] is not None
    final_request = next(m for m in bus._load_messages(room)
                         if m["id"] == b_done["final_request"])
    assert final_request["to"] == "A"
    assert not terminated

    timers[0].callback(*timers[0].args, **timers[0].kwargs)

    assert terminated == [(room, "wake-A-1")]


def test_intentional_pilot_stop_keeps_exit_code_without_counting_failure(
    isolated_home, monkeypatch,
):
    monkeypatch.setattr(server, "_wake_agents_for_request", lambda *args: [])
    monkeypatch.setattr(server.child_processes, "state",
                        lambda room, handle: "alive" if handle == "wake-A-1"
                        else "unknown")
    terminated = []
    monkeypatch.setattr(server.child_processes, "terminate",
                        lambda room, handle: terminated.append((room, handle)) or "sent")
    rate_checks = []
    noreply_checks = []
    drains = []
    monkeypatch.setattr(server, "_handle_rate_limit_on_exit",
                        lambda *args: rate_checks.append(args) or False)
    monkeypatch.setattr(server, "_announce_noreply_on_exit",
                        lambda *args: noreply_checks.append(args))
    monkeypatch.setattr(server, "_drain_pending_wakes",
                        lambda *args: drains.append(args))

    room = swarm_pilot.create(
        "pilot", "Organizer", "Decide", "team", ["A", "B"],
    )
    request_id = server.message_post(
        room, "Organizer", "Do part A", "request", to="A",
    )
    swarm_pilot.mark_dispatched(room, "A", request_id)
    server.message_post(room, "A", "Part A done", "result", to="Organizer",
                        reply_to=request_id)
    swarm_pilot.round_done(room, "A", "done")
    server._merge_agent_meta(room, "A", {
        "wake_id": "wake-A-1", "wake_claim_id": "wake-A-1",
        "wake_claim_msg_id": request_id, "last_wake_msg_id": request_id,
        "last_wake_pid": 91234, "wake_fail_count": 2,
    })

    server._stop_completed_pilot_turn(room, "A", "wake-A-1", "round_done")
    stopped_info = (bus.get_room_info(room)["agent_meta"]["A"])
    assert terminated == [(room, "wake-A-1")]
    assert stopped_info["intentional_stop_wake_id"] == "wake-A-1"

    server._on_wake_exit(room, "A", "wake-A-1", -15)

    info = bus.get_room_info(room)["agent_meta"]["A"]
    assert info["last_wake_rc"] == -15  # Keep the real signal exit code.
    assert info["wake_fail_count"] == 2
    assert "wake_claim_id" not in info
    assert bus.get_status_details(room)["A"]["phase"] == "completed"
    assert not rate_checks and not noreply_checks
    assert drains == [(room, "A")]


def test_pilot_stop_never_uses_pid_or_marks_a_new_wake(
    isolated_home, monkeypatch,
):
    room = swarm_pilot.create(
        "pilot", "Organizer", "Decide", "team", ["A", "B"],
    )
    request_id = server.message_post(
        room, "Organizer", "Do part A", "request", to="A",
    )
    swarm_pilot.mark_dispatched(room, "A", request_id)
    server.message_post(room, "A", "Part A done", "result", to="Organizer",
                        reply_to=request_id)
    swarm_pilot.round_done(room, "A", "done")
    server._merge_agent_meta(room, "A", {
        "wake_id": "new-wake", "wake_claim_id": "new-wake",
        "wake_claim_msg_id": request_id, "last_wake_msg_id": request_id,
        "last_wake_pid": 91234,
    })
    terminate_calls = []
    # A persisted PID is diagnostic only; this exact generation is not locally owned.
    monkeypatch.setattr(server.child_processes, "state", lambda *args: "unknown")
    monkeypatch.setattr(server.child_processes, "terminate",
                        lambda *args: terminate_calls.append(args) or "sent")

    server._stop_completed_pilot_turn(room, "A", "new-wake", "round_done")
    server._stop_completed_pilot_turn(room, "A", "old-wake", "round_done")
    server._on_wake_exit(room, "A", "old-wake", -15)

    info = bus.get_room_info(room)["agent_meta"]["A"]
    assert not terminate_calls
    assert "intentional_stop_wake_id" not in info
    assert info["wake_id"] == "new-wake"
    assert info.get("last_wake_rc") is None


def test_pilot_stop_marker_does_not_hide_a_non_sigterm_exit(
    isolated_home, monkeypatch,
):
    rate_checks = []
    noreply_checks = []
    monkeypatch.setattr(server, "_handle_rate_limit_on_exit",
                        lambda *args: rate_checks.append(args) or False)
    monkeypatch.setattr(server, "_announce_noreply_on_exit",
                        lambda *args: noreply_checks.append(args))
    monkeypatch.setattr(server, "_drain_pending_wakes", lambda *args: None)
    room = swarm_pilot.create(
        "pilot", "Organizer", "Decide", "team", ["A", "B"],
    )
    request_id = server.message_post(
        room, "Organizer", "Do part A", "request", to="A",
    )
    server._merge_agent_meta(room, "A", {
        "wake_id": "wake-A-1", "last_wake_msg_id": request_id,
        "wake_fail_count": 2, "intentional_stop_wake_id": "wake-A-1",
    })

    server._on_wake_exit(room, "A", "wake-A-1", 1)

    info = bus.get_room_info(room)["agent_meta"]["A"]
    assert info["last_wake_rc"] == 1
    assert info["wake_fail_count"] == 3
    assert rate_checks == [(room, "A")]
    assert noreply_checks


def test_round_done_does_not_stop_last_member_or_unowned_process(
    isolated_home, monkeypatch,
):
    monkeypatch.setattr(server.spawn, "get_enabled_spec", lambda name: {"name": name})
    monkeypatch.setattr(server, "_wake_agents_for_request", lambda *args: [])
    timers = []
    terminated = []

    class DeferredTimer:
        def __init__(self, delay, callback, args=(), kwargs=None):
            self.callback = callback
            self.args = args
            self.kwargs = kwargs or {}
            timers.append(self)

        def start(self):
            pass

    monkeypatch.setattr(server.threading, "Timer", DeferredTimer)
    monkeypatch.setattr(server.child_processes, "state", lambda *args: "unknown")
    monkeypatch.setattr(server.child_processes, "terminate",
                        lambda *args: terminated.append(args) or "sent")

    created = server.swarm_pilot_create(
        "pilot", "Organizer", "Decide", "team", ["A"], start=True,
    )
    room = created["room_id"]
    request_id = swarm_pilot.status(room)["dispatched"]["A"]
    server._merge_agent_meta(room, "A", {"wake_id": "foreign-wake"})
    server.message_post(room, "A", "My result", "result", to="Organizer",
                        reply_to=request_id)

    response = server.swarm_pilot_round_done(room, "A", "done")

    assert "A" in response["state"]["done"]
    assert isinstance(response["final_request"], int)
    assert not timers
    assert not terminated


def test_final_stops_reporter_only_after_final_message_is_durable(
    isolated_home, monkeypatch,
):
    monkeypatch.setattr(server.spawn, "get_enabled_spec", lambda name: {"name": name})
    monkeypatch.setattr(server, "_wake_agents_for_request", lambda *args: [])
    timers = []
    terminated = []

    class DeferredTimer:
        def __init__(self, delay, callback, args=(), kwargs=None):
            self.callback = callback
            self.args = args
            self.kwargs = kwargs or {}
            timers.append(self)

        def start(self):
            pass

    monkeypatch.setattr(server.threading, "Timer", DeferredTimer)
    monkeypatch.setattr(server.child_processes, "state",
                        lambda room, handle: "alive" if handle == "wake-A-1"
                        else "unknown")
    monkeypatch.setattr(server.child_processes, "terminate",
                        lambda room, handle: terminated.append((room, handle)) or "sent")

    created = server.swarm_pilot_create(
        "pilot", "Organizer", "Decide", "team", ["A"], start=True,
    )
    room = created["room_id"]
    server.swarm_pilot_record(room, "A", "responsibility", "reporter", "Synthesize")
    request_id = swarm_pilot.status(room)["dispatched"]["A"]
    server._merge_agent_meta(room, "A", {"wake_id": "wake-A-1"})
    server.message_post(room, "A", "My result", "result", to="Organizer",
                        reply_to=request_id)
    round_response = server.swarm_pilot_round_done(room, "A", "done")
    assert isinstance(round_response["final_request"], int)
    assert not timers  # The last member is kept alive through final routing.

    finished = server.swarm_pilot_finish(room, "A", "Combined result")

    assert finished["phase"] == "completed"
    finals = [m for m in bus._load_messages(room) if m["kind"] == "final"]
    assert len(finals) == 1 and finals[0]["agent"] == "A"
    assert len(timers) == 1
    assert not terminated
    timers[0].callback(*timers[0].args, **timers[0].kwargs)
    assert terminated == [(room, "wake-A-1")]


def test_round_done_accepts_result_to_organizer_recovery_request(
    isolated_home, monkeypatch,
):
    monkeypatch.setattr(server.spawn, "get_enabled_spec", lambda name: {"name": name})
    monkeypatch.setattr(server, "_wake_agents_for_request", lambda *args: [])
    created = server.swarm_pilot_create(
        "retry after provider failure", "Organizer", "Decide", "swarm",
        ["A", "B"], start=True,
    )
    room = created["room_id"]
    original_id = swarm_pilot.status(room)["dispatched"]["A"]
    retry_id = server.message_post(
        room, "Organizer", "Retry A after its CLI failed before replying.",
        "request", to="A",
    )

    with pytest.raises(ValueError, match="organizer's direct recovery request"):
        server.swarm_pilot_round_done(room, "A", "done")

    server.message_post(
        room, "A", "Recovered result", "result", to="Organizer",
        reply_to=retry_id,
    )
    response = server.swarm_pilot_round_done(room, "A", "recovered")
    assert original_id != retry_id
    assert response["state"]["done"]["A"]["summary"] == "recovered"
    assert response["next_dispatch"] == []
    assert original_id not in {
        item["id"] for item in server.room_status(room)["pending_requests"]
    }
    assert server._agent_replied_to_request(room, "A", original_id)


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
        completed = server.swarm_pilot_round_done(room, member, f"{member} done")

    if mode == "council":
        final_request = completed["final_request"]
        assert isinstance(final_request, int)
        assert bus._load_messages(room)[-1]["id"] == final_request
        assert bus._load_messages(room)[-1]["to"] == "Organizer"
    else:
        final_request = completed["final_request"]
        assert isinstance(final_request, int)
        assert bus._load_messages(room)[-1]["id"] == final_request
        assert bus._load_messages(room)[-1]["to"] == "A"

    author = "Organizer" if mode == "council" else "A"
    result = server.swarm_pilot_finish(room, author, "Combined result")
    assert result["phase"] == "completed"
    finals = [m for m in bus._load_messages(room) if m["kind"] == "final"]
    assert len(finals) == 1 and finals[0]["agent"] == author
    assert finals[0]["reply_to"] == final_request
    assert server.room_status(room)["pending_requests"] == []
    before_late_reply = len(bus._load_messages(room))
    with pytest.raises(ValueError, match="late final-request result discarded"):
        server.message_post(
            room, author, "Late final-request response", "result",
            to="Organizer", reply_to=final_request,
        )
    assert len(bus._load_messages(room)) == before_late_reply


def test_new_codex_pilot_member_gets_initial_fresh_spawn(isolated_home, monkeypatch):
    monkeypatch.setattr(server.spawn, "get_enabled_spec", lambda name: {"name": name})
    launches = []
    monkeypatch.setattr(server, "_parse_owned_codex_thread_id", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_spawn_fresh_room_agent",
                        lambda room, name, prompt, meta, **kwargs:
                        (launches.append((name, prompt)) or (12345, "log", None)))
    created = server.swarm_pilot_create(
        "pilot", "Organizer", "One sentence", "council", ["Codex"], start=True,
    )
    assert created["dispatched"][0]["member"] == "Codex"
    assert len(launches) == 1
    assert launches[0][0] == "Codex"


def test_pilot_missing_reporter_prompts_all_then_dispatches(isolated_home, monkeypatch):
    monkeypatch.setattr(server.spawn, "get_enabled_spec", lambda name: {"name": name})
    monkeypatch.setattr(server, "_wake_agents_for_request", lambda *args: [])
    created = server.swarm_pilot_create(
        "missing reporter", "Organizer", "Produce one sentence", "team",
        ["A", "B"], start=True,
    )
    room = created["room_id"]
    for member in ("A", "B"):
        request_id = swarm_pilot.status(room)["dispatched"][member]
        server.message_post(room, member, f"{member} result", "result",
                            to="Organizer", reply_to=request_id)
        completed = server.swarm_pilot_round_done(room, member, f"{member} done")

    # Since no reporter is claimed, the final request should ask "all"
    final_req_msg_id = completed["final_request"]
    assert isinstance(final_req_msg_id, int)
    msg = bus._load_messages(room)[-1]
    assert msg["id"] == final_req_msg_id
    assert msg["to"] == "all"
    assert "no reporter is claimed" in msg["body"]

    with pytest.raises(ValueError, match="final request has not been delivered"):
        server.swarm_pilot_finish(room, "A", "Too early")

    # Now claim reporter
    updated_state = server.swarm_pilot_record(room, "A", "responsibility", "reporter", "Final")

    # Should dispatch final request to A
    msg2 = bus._load_messages(room)[-1]
    assert msg2["to"] == "A"
    assert "publish the combined result" in msg2["body"]
    assert updated_state["final_request"] == msg2["id"]
    assert final_req_msg_id not in {
        item["id"] for item in server.room_status(room)["pending_requests"]
    }
    assert server._agent_replied_to_request(room, "B", final_req_msg_id)
    server._merge_agent_meta(room, "B", {"last_wake_msg_id": final_req_msg_id - 1})
    assert server._next_pending_request(
        room, "B", {"last_wake_msg_id": final_req_msg_id - 1},
    ) is None

    # Duplicates are ignored due to idempotency keys
    updated_state2 = server.swarm_pilot_record(room, "A", "responsibility", "reporter", "Final")
    assert bus._load_messages(room)[-1]["id"] == msg2["id"]


@pytest.mark.parametrize("block_status", ["unavailable", "spec_drift"])
def test_watchdog_reports_blocked_member_once_and_recovers_when_available(
    isolated_home, monkeypatch, block_status,
):
    available = {"A": True, "B": True}
    monkeypatch.setattr(server.spawn, "get_enabled_spec",
                        lambda name: {"name": name} if available[name] else None)
    monkeypatch.setattr(server, "_wake_agents_for_request", lambda *args: [])
    room = server.swarm_pilot_create(
        "blocked member", "Organizer", "Tiny result", "relay", ["A", "B"],
        start=True,
    )["room_id"]
    first = swarm_pilot.status(room)["dispatched"]["A"]
    server.message_post(room, "A", "A result", "result", to="Organizer",
                        reply_to=first)
    swarm_pilot.round_done(room, "A", "done")
    if block_status == "unavailable":
        available["B"] = False
    else:
        original_drift = server._swarm_spec_drift
        monkeypatch.setattr(server, "_swarm_spec_drift",
                            lambda meta, member, spec:
                            member == "B" or original_drift(meta, member, spec))

    first_tick = server._recover_swarm_pilots()
    second_tick = server._recover_swarm_pilots()
    blocked = [m for m in bus._load_messages(room)
               if m.get("idempotency_key", "").endswith(
                   f":B:blocked:{block_status}")]
    assert len(blocked) == 1 and blocked[0]["kind"] == "system"
    assert first_tick[0]["blocked"][0]["status"] == block_status
    assert second_tick == []

    available["B"] = True
    if block_status == "spec_drift":
        monkeypatch.setattr(server, "_swarm_spec_drift", original_drift)
    resumed = server._recover_swarm_pilots()
    assert resumed[0]["next_dispatch"][0]["member"] == "B"
    assert len(blocked) == 1


def test_watchdog_restores_final_message_after_completed_state(
    isolated_home, monkeypatch,
):
    monkeypatch.setattr(server.spawn, "get_enabled_spec", lambda name: {"name": name})
    monkeypatch.setattr(server, "_wake_agents_for_request", lambda *args: [])
    room = server.swarm_pilot_create(
        "final restart", "Organizer", "Tiny result", "team", ["A"],
        start=True,
    )["room_id"]
    server.swarm_pilot_record(room, "A", "responsibility", "reporter", "Final")
    first = swarm_pilot.status(room)["dispatched"]["A"]
    server.message_post(room, "A", "A result", "result", to="Organizer",
                        reply_to=first)
    final_request = server.swarm_pilot_round_done(room, "A", "done")["final_request"]
    swarm_pilot.finish(room, "A", "Combined")  # Process dies before message_post.

    server._recover_swarm_pilots()
    server._recover_swarm_pilots()

    finals = [msg for msg in bus._load_messages(room) if msg["kind"] == "final"]
    assert len(finals) == 1
    assert finals[0]["reply_to"] == final_request
    assert not server.room_status(room)["pending_requests"]


def test_existing_pilot_final_without_reply_to_is_settled_without_duplicate(
    isolated_home, monkeypatch,
):
    monkeypatch.setattr(server.spawn, "get_enabled_spec", lambda name: {"name": name})
    monkeypatch.setattr(server, "_wake_agents_for_request", lambda *args: [])
    room = server.swarm_pilot_create(
        "older final", "Organizer", "Tiny result", "team", ["A"],
        start=True,
    )["room_id"]
    server.swarm_pilot_record(room, "A", "responsibility", "reporter", "Final")
    first = swarm_pilot.status(room)["dispatched"]["A"]
    server.message_post(room, "A", "A result", "result", to="Organizer",
                        reply_to=first)
    final_request = server.swarm_pilot_round_done(room, "A", "done")["final_request"]
    swarm_pilot.finish(room, "A", "Combined")
    server.message_post(room, "A", "Combined", "final", to="Organizer",
                        idempotency_key=f"swarm-pilot:{room}:final")

    assert server._recover_swarm_pilots() == []
    assert server.room_status(room)["pending_requests"] == []
    assert server._agent_replied_to_request(room, "A", final_request)
    assert len([msg for msg in bus._load_messages(room) if msg["kind"] == "final"]) == 1


@pytest.mark.parametrize("mode", ["council", "relay"])
def test_watchdog_recovers_next_dispatch_after_persisted_round_done(
    isolated_home, monkeypatch, mode,
):
    monkeypatch.setattr(server.spawn, "get_enabled_spec", lambda name: {"name": name})
    wake_calls = []
    monkeypatch.setattr(server, "_wake_agents_for_request",
                        lambda *args: wake_calls.append(args) or [])
    room = server.swarm_pilot_create(
        "restart recovery", "Organizer", "Tiny result", mode, ["A", "B"],
        start=True,
    )["room_id"]
    first = swarm_pilot.status(room)["dispatched"]["A"]
    server.message_post(room, "A", "A result", "result", to="Organizer",
                        reply_to=first)
    swarm_pilot.round_done(room, "A", "done")  # Process dies before pump.
    assert "B" not in swarm_pilot.status(room)["dispatched"]

    first_tick = server._recover_swarm_pilots()
    second_tick = server._recover_swarm_pilots()

    requests = [m for m in bus._load_messages(room)
                if m["kind"] == "request" and m["to"] == "B"]
    assert len(requests) == 1
    assert swarm_pilot.status(room)["dispatched"]["B"] == requests[0]["id"]
    assert first_tick == [{"room_id": room, "next_dispatch": [{"member": "B",
                           "request_id": requests[0]["id"], "status": "dispatched"}],
                           "final_request": None}]
    assert second_tick == []
    assert len(wake_calls) == 2  # A at creation, B on recovery.


@pytest.mark.parametrize("mode, final_to", [
    ("council", "Organizer"), ("team", "A"), ("relay", "A"), ("swarm", "A"),
])
def test_watchdog_recovers_final_request_after_last_round_done(
    isolated_home, monkeypatch, mode, final_to,
):
    monkeypatch.setattr(server.spawn, "get_enabled_spec", lambda name: {"name": name})
    wake_calls = []
    terminate_calls = []
    monkeypatch.setattr(server, "_wake_agents_for_request",
                        lambda *args: wake_calls.append(args) or [])
    monkeypatch.setattr(server.child_processes, "terminate",
                        lambda *args: terminate_calls.append(args))
    room = server.swarm_pilot_create(
        "restart recovery", "Organizer", "Tiny result", mode, ["A"],
        start=True,
    )["room_id"]
    if mode != "council":
        swarm_pilot.record(room, "A", "responsibility", "reporter", "Final")
    first = swarm_pilot.status(room)["dispatched"]["A"]
    server.message_post(room, "A", "A result", "result", to="Organizer",
                        reply_to=first)
    swarm_pilot.round_done(room, "A", "done")  # Process dies before final post.
    server._merge_agent_meta(room, "A", {"last_wake_pid": 91234,
                                          "wake_id": "foreign-generation"})

    first_tick = server._recover_swarm_pilots()
    second_tick = server._recover_swarm_pilots()

    final_requests = [m for m in bus._load_messages(room)
                      if m["kind"] == "request" and m["to"] == final_to
                      and m.get("idempotency_key", "").endswith(":final-request")]
    assert len(final_requests) == 1
    assert first_tick[0]["final_request"] == final_requests[0]["id"]
    assert second_tick == []
    assert len(wake_calls) == 2  # First member request and final request.
    assert not terminate_calls


def test_watchdog_skips_preparing_unstarted_and_closed_pilots(
    isolated_home, monkeypatch,
):
    monkeypatch.setattr(server.spawn, "get_enabled_spec", lambda name: {"name": name})
    monkeypatch.setattr(server, "_wake_agents_for_request", lambda *args: [])
    preparing = server.swarm_pilot_create(
        "preparing", "Organizer", "Tiny result", "team", ["A"],
        start=False, client_request_id="preparing",
    )["room_id"]
    unstarted = server.swarm_pilot_create(
        "unstarted", "Organizer", "Tiny result", "team", ["A"],
        start=False,
    )["room_id"]
    closed = server.swarm_pilot_create(
        "closed", "Organizer", "Tiny result", "team", ["A"],
        start=True,
    )["room_id"]
    server._swarm_mark_create_state(preparing, "preparing", None, False, True)
    bus.close_room(closed, "Organizer")

    assert server._recover_swarm_pilots() == []
    assert swarm_pilot.status(preparing)["dispatched"] == {}
    assert swarm_pilot.status(unstarted)["dispatched"] == {}
