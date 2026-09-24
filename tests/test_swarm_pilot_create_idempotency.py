"""Create-side retry and spec-drift guards for swarm pilot rooms."""

import pytest
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from mcp_huddle import bus, server, swarm_pilot


_REAL_PILOT_PUMP = server.swarm_pilot_pump


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "huddle-home"
    monkeypatch.setattr(bus, "HUDDLE_HOME", home)
    monkeypatch.setattr(bus, "BUS_DIR", home / "rooms")
    monkeypatch.setattr(bus, "NOTIFICATIONS_DIR", home / "notifications")
    monkeypatch.setattr(bus, "NOTIFICATION_LOCKS_DIR", home / "internal" / "notification-locks")
    monkeypatch.setattr(server.spawn, "get_enabled_spec", lambda name: {"name": name})
    monkeypatch.setattr(server, "room_invite", lambda *_args, **_kwargs: "ok")
    monkeypatch.setattr(server, "swarm_pilot_pump", lambda _room: [{"status": "dispatched"}])
    return home


def _create_kwargs(**overrides):
    value = {
        "name": "pilot",
        "organizer": "Organizer",
        "goal": "Make a tiny result",
        "mode": "swarm",
        "members": ["Claude", "Codex"],
        "start": True,
        "client_request_id": "request-001",
    }
    value.update(overrides)
    return value


def _candidate(name, fingerprint, *, enabled=True, static_ok=True):
    return {
        "id": name,
        "enabled": enabled,
        "static_ok": static_ok,
        "spec_fingerprint": fingerprint,
    }


def test_same_request_id_reuses_room_without_second_pump(isolated_home, monkeypatch):
    pumps = []
    def fake_pump(room):
        pumps.append(room)
        swarm_pilot.mark_dispatched(room, "Claude", 1)
        swarm_pilot.mark_dispatched(room, "Codex", 2)
        return [
            {"member": "Claude", "request_id": 1, "status": "dispatched"},
            {"member": "Codex", "request_id": 2, "status": "dispatched"},
        ]
    monkeypatch.setattr(server, "swarm_pilot_pump", fake_pump)

    first = server.swarm_pilot_create(**_create_kwargs())
    second = server.swarm_pilot_create(**_create_kwargs())

    assert first["room_id"] == second["room_id"]
    assert first["reused"] is False
    assert first["availability"] == {
        "static_only": False,
        "registry_availability_checked": True,
        "provider_response_verified": False,
    }
    assert second["reused"] is True
    assert second["dispatched"] == []
    assert second["started"] is True
    assert second["start_requested"] is True
    assert second["previous_dispatch"] == [
        {"member": "Claude", "request_id": 1, "status": "dispatched"},
        {"member": "Codex", "request_id": 2, "status": "dispatched"},
    ]
    assert pumps == [first["room_id"]]
    assert second["availability"] == {
        "static_only": False,
        "registry_availability_checked": True,
        "provider_response_verified": False,
    }
    assert len(list((isolated_home / "rooms").glob("room_*"))) == 1


def test_same_request_id_with_changed_payload_is_conflict(isolated_home, monkeypatch):
    created = server.swarm_pilot_create(**_create_kwargs(start=False))
    pumps = []
    monkeypatch.setattr(server, "swarm_pilot_pump", lambda room: pumps.append(room) or [])

    with pytest.raises(ValueError, match="client_request_id conflict"):
        server.swarm_pilot_create(**_create_kwargs(goal="Different goal", start=False))

    assert len(list((isolated_home / "rooms").glob("room_*"))) == 1
    assert not pumps
    assert created["reused"] is False


def test_spec_drift_blocks_room_creation_even_when_start_is_false(isolated_home, monkeypatch):
    expected = {"Claude": "sha256:" + "a" * 64, "Codex": "sha256:" + "b" * 64}
    monkeypatch.setattr(server, "_swarm_plan_candidates", lambda: [
        _candidate("Claude", "sha256:" + "c" * 64),
        _candidate("Codex", expected["Codex"]),
    ])
    create_calls = []
    enabled_checks = []
    monkeypatch.setattr(server.swarm_pilot, "create", lambda *a, **k: create_calls.append((a, k)))
    monkeypatch.setattr(server.spawn, "get_enabled_spec", lambda name: enabled_checks.append(name))

    with pytest.raises(ValueError, match="registry spec drift for participant: Claude"):
        server.swarm_pilot_create(**_create_kwargs(start=False, expected_specs=expected))

    assert create_calls == []
    assert enabled_checks == []
    assert not (isolated_home / "rooms").exists()


def test_expected_specs_require_exact_member_keys_before_creation(isolated_home, monkeypatch):
    candidates = []
    monkeypatch.setattr(server, "_swarm_plan_candidates", lambda: candidates)
    create_calls = []
    monkeypatch.setattr(server.swarm_pilot, "create", lambda *a, **k: create_calls.append((a, k)))

    with pytest.raises(ValueError, match="exactly match members"):
        server.swarm_pilot_create(**_create_kwargs(
            start=False, expected_specs={"Claude": "sha256:" + "a" * 64},
        ))

    assert create_calls == []
    assert not (isolated_home / "rooms").exists()
    assert candidates == []


def test_expected_specs_are_persisted_as_safe_fingerprints(isolated_home, monkeypatch):
    expected = {"Claude": "sha256:" + "a" * 64, "Codex": "sha256:" + "b" * 64}
    monkeypatch.setattr(server, "_swarm_plan_candidates", lambda: [
        _candidate(name, fingerprint) for name, fingerprint in expected.items()
    ])

    created = server.swarm_pilot_create(**_create_kwargs(start=False, expected_specs=expected))

    state = swarm_pilot.status(created["room_id"])
    assert state["expected_specs"] == expected
    assert state["server_create_state"] == "ready"
    assert state["client_request_fingerprint"].startswith("sha256:")


def test_reusing_request_with_different_expected_specs_is_conflict(isolated_home, monkeypatch):
    original = {"Claude": "sha256:" + "a" * 64, "Codex": "sha256:" + "b" * 64}
    changed = {"Claude": "sha256:" + "c" * 64, "Codex": "sha256:" + "b" * 64}
    monkeypatch.setattr(server, "_swarm_plan_candidates", lambda: [
        _candidate(name, fingerprint) for name, fingerprint in original.items()
    ])
    server.swarm_pilot_create(**_create_kwargs(start=False, expected_specs=original))

    with pytest.raises(ValueError, match="client_request_id conflict: expected agent specs"):
        server.swarm_pilot_create(**_create_kwargs(start=False, expected_specs=changed))

    assert len(list((isolated_home / "rooms").glob("room_*"))) == 1


def test_plan_hash_is_audit_only_on_idempotent_reuse(isolated_home):
    original = "sha256:" + "a" * 64
    changed = "sha256:" + "b" * 64
    first = server.swarm_pilot_create(**_create_kwargs(start=False, plan_hash=original))
    second = server.swarm_pilot_create(**_create_kwargs(start=False, plan_hash=changed))

    assert second["room_id"] == first["room_id"]
    assert second["reused"] is True
    assert swarm_pilot.status(first["room_id"])["plan_hash"] == original


def test_partial_deterministic_room_is_reported_and_never_pumped(isolated_home, monkeypatch):
    request = _create_kwargs(start=False)
    fingerprint = server._swarm_client_request_fingerprint(
        request["name"], request["organizer"], request["goal"], request["mode"],
        request["members"], request.get("cwd", ""), request.get("workspace_strategy", "shared_only"),
        request["start"],
    )
    room_id = server._swarm_request_room_id(request["organizer"], request["client_request_id"])
    swarm_pilot.create(
        request["name"], request["organizer"], request["goal"], request["mode"],
        request["members"], room_id=room_id, client_request_fingerprint=fingerprint,
    )
    pumps = []
    monkeypatch.setattr(server, "swarm_pilot_pump", lambda room: pumps.append(room) or [])

    with pytest.raises(ValueError, match="partial_room"):
        server.swarm_pilot_create(**request)

    assert pumps == []
    assert len(list((isolated_home / "rooms").glob("room_*"))) == 1


def test_legacy_call_without_client_request_id_keeps_random_room_ids(isolated_home):
    kwargs = _create_kwargs(start=False)
    kwargs.pop("client_request_id")

    first = server.swarm_pilot_create(**kwargs)
    second = server.swarm_pilot_create(**kwargs)

    assert first["room_id"] != second["room_id"]
    assert first["reused"] is False and second["reused"] is False
    assert len(list((isolated_home / "rooms").glob("room_*"))) == 2


def test_invalid_client_request_id_is_rejected_before_room_creation(isolated_home):
    with pytest.raises(ValueError, match="client_request_id must be"):
        server.swarm_pilot_create(**_create_kwargs(client_request_id="../unsafe"))
    assert not (isolated_home / "rooms").exists()


def test_changing_start_flag_for_same_request_is_conflict(isolated_home):
    server.swarm_pilot_create(**_create_kwargs(start=False))

    with pytest.raises(ValueError, match="client_request_id conflict"):
        server.swarm_pilot_create(**_create_kwargs(start=True))

    assert len(list((isolated_home / "rooms").glob("room_*"))) == 1


def test_pump_failure_leaves_retryable_identity_marked_partial(isolated_home, monkeypatch):
    monkeypatch.setattr(server, "swarm_pilot_pump", lambda _room: (_ for _ in ()).throw(
        RuntimeError("pump failed")
    ))
    request = _create_kwargs(start=True)

    with pytest.raises(RuntimeError, match="pump failed"):
        server.swarm_pilot_create(**request)
    monkeypatch.setattr(server, "swarm_pilot_pump", lambda _room: [])

    with pytest.raises(ValueError, match="partial_room"):
        server.swarm_pilot_create(**request)

    assert len(list((isolated_home / "rooms").glob("room_*"))) == 1


def test_reused_start_false_reports_prepared_state(isolated_home):
    first = server.swarm_pilot_create(**_create_kwargs(start=False))
    second = server.swarm_pilot_create(**_create_kwargs(start=False))

    assert second["room_id"] == first["room_id"]
    assert second["started"] is False
    assert second["start_requested"] is False
    assert second["previous_dispatch"] == []


def test_start_false_without_spec_pinning_does_not_claim_registry_check(isolated_home):
    first = server.swarm_pilot_create(**_create_kwargs(start=False))
    second = server.swarm_pilot_create(**_create_kwargs(start=False))

    assert first["availability"]["static_only"] is True
    assert first["availability"]["registry_availability_checked"] is False
    assert second["availability"]["static_only"] is True
    assert second["availability"]["registry_availability_checked"] is False
    assert swarm_pilot.status(first["room_id"])["registry_availability_checked"] is False


def test_preparing_retry_after_posted_request_reuses_idempotency_key(
    isolated_home, monkeypatch,
):
    real_post = server.message_post
    wake_targets = []
    post_count = 0
    fail_codex_once = True
    monkeypatch.setattr(server, "_wake_agents_for_request", lambda _room, _sender, _body, target, _reply, _msg: (
        wake_targets.append(target) or []
    ))

    def fail_after_first_member(*args, **kwargs):
        nonlocal post_count, fail_codex_once
        if args[3] == "request":
            post_count += 1
            target = kwargs.get("to", args[4] if len(args) > 4 else None)
            if target == "Codex" and fail_codex_once:
                fail_codex_once = False
                raise RuntimeError("pump failed after Claude request persisted")
        return real_post(*args, **kwargs)

    monkeypatch.setattr(server, "message_post", fail_after_first_member)
    monkeypatch.setattr(server, "swarm_pilot_pump", _REAL_PILOT_PUMP)
    request = _create_kwargs(start=True)

    with pytest.raises(RuntimeError, match="after Claude request persisted"):
        server.swarm_pilot_create(**request)
    room_id = server._swarm_request_room_id("Organizer", "request-001")
    state = swarm_pilot.status(room_id)
    assert state["server_create_state"] == "preparing"
    resumed = server.swarm_pilot_create(**request)
    retried = server.swarm_pilot_create(**request)

    assert resumed["room_id"] == room_id
    assert resumed["reused"] is True
    assert resumed["started"] is True
    assert {item["member"] for item in resumed["dispatched"]} == {"Codex"}
    assert retried["reused"] is True
    assert retried["dispatched"] == []
    assert {item["member"] for item in retried["previous_dispatch"]} == {"Claude", "Codex"}
    assert post_count == 3  # failed Codex attempt plus the two persisted requests
    assert wake_targets == ["Claude", "Codex"]
    messages = bus._load_messages(room_id)
    assert len(messages) == 2
    assert {message["to"] for message in messages} == {"Claude", "Codex"}


def test_concurrent_same_key_create_serializes_preparing_resume(isolated_home, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    pump_count = 0
    message_ids = iter(range(1, 10))
    monkeypatch.setattr(server, "message_post", lambda *_args, **_kwargs: next(message_ids))

    def slow_pump(room):
        nonlocal pump_count
        pump_count += 1
        entered.set()
        release.wait(timeout=2)
        return _REAL_PILOT_PUMP(room)

    monkeypatch.setattr(server, "swarm_pilot_pump", slow_pump)
    kwargs = _create_kwargs(start=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(server.swarm_pilot_create, **kwargs)
        assert entered.wait(timeout=1)
        second = pool.submit(server.swarm_pilot_create, **kwargs)
        time.sleep(0.05)
        release.set()
        results = [first.result(timeout=2), second.result(timeout=2)]

    assert pump_count == 1
    assert results[0]["room_id"] == results[1]["room_id"]
    assert {result["reused"] for result in results} == {False, True}


@pytest.mark.parametrize("mode", ["council", "relay"])
def test_pump_reports_spec_drift_for_next_sequential_member(
    isolated_home, monkeypatch, mode,
):
    expected = {"Claude": "sha256:" + "a" * 64, "Codex": "sha256:" + "b" * 64}
    current = dict(expected)
    monkeypatch.setattr(server, "_swarm_plan_candidates", lambda: [
        _candidate(name, fingerprint) for name, fingerprint in current.items()
    ])
    created = server.swarm_pilot_create(**_create_kwargs(
        mode=mode, start=False, expected_specs=expected,
    ))
    state = swarm_pilot.status(created["room_id"])
    swarm_pilot.mark_dispatched(created["room_id"], "Claude", 1)
    swarm_pilot.round_done(created["room_id"], "Claude", "done")
    current["Codex"] = "sha256:" + "c" * 64
    monkeypatch.setattr(server.spawn, "get_enabled_spec", lambda member: {
        "name": member, "cmd": ["codex", "exec"], "enabled": True,
    })
    posts = []
    monkeypatch.setattr(server, "message_post", lambda *args, **kwargs: posts.append(args) or 99)
    monkeypatch.setattr(server.spawn, "spec_fingerprint", lambda spec: current[spec["name"]])

    result = _REAL_PILOT_PUMP(created["room_id"])

    assert result == [{"member": "Codex", "status": "spec_drift"}]
    assert posts == []
    assert state["expected_specs"] == expected


def test_create_with_spec_drift_keeps_room_preparing_until_dispatch_can_resume(
    isolated_home, monkeypatch,
):
    expected = {"Claude": "sha256:" + "a" * 64, "Codex": "sha256:" + "b" * 64}
    current = dict(expected)
    monkeypatch.setattr(server, "_swarm_plan_candidates", lambda: [
        _candidate(name, fingerprint) for name, fingerprint in expected.items()
    ])
    monkeypatch.setattr(server.spawn, "get_enabled_spec", lambda member: {"name": member})
    monkeypatch.setattr(server.spawn, "spec_fingerprint", lambda spec: current[spec["name"]])
    posts = []
    monkeypatch.setattr(server, "message_post", lambda *args, **kwargs: posts.append(kwargs.get("to")) or 1)
    monkeypatch.setattr(server, "swarm_pilot_pump", _REAL_PILOT_PUMP)
    current["Claude"] = "sha256:" + "c" * 64

    with pytest.raises(ValueError, match="partial_room.*spec_drift"):
        server.swarm_pilot_create(**_create_kwargs(expected_specs=expected))
    room_id = server._swarm_request_room_id("Organizer", "request-001")
    assert swarm_pilot.status(room_id)["server_create_state"] == "preparing"
    assert posts == ["Codex"]

    current["Claude"] = expected["Claude"]
    resumed = server.swarm_pilot_create(**_create_kwargs(expected_specs=expected))
    assert resumed["reused"] is True
    assert len(posts) == 2


def test_fresh_spawn_refuses_pinned_profile_drift(isolated_home, monkeypatch):
    expected = {"Claude": "sha256:" + "a" * 64, "Codex": "sha256:" + "b" * 64}
    monkeypatch.setattr(server, "_swarm_plan_candidates", lambda: [
        _candidate(name, fingerprint) for name, fingerprint in expected.items()
    ])
    room = server.swarm_pilot_create(**_create_kwargs(start=False, expected_specs=expected))
    monkeypatch.setattr(server.spawn, "get_enabled_spec", lambda member: {
        "name": member, "cmd": [member.lower(), "-p"], "enabled": True,
    })
    monkeypatch.setattr(server.spawn, "spec_fingerprint", lambda _spec: "sha256:" + "c" * 64)
    spawned = []
    monkeypatch.setattr(server.spawn, "spawn_agent", lambda *args, **kwargs: spawned.append(args))

    with pytest.raises(ValueError, match="spec_drift"):
        server._spawn_fresh_room_agent(
            room["room_id"], "Claude", "prompt", bus.get_room_info(room["room_id"]),
        )

    assert spawned == []


def test_direct_wake_refuses_pinned_profile_drift_before_cli_start(
    isolated_home, monkeypatch,
):
    expected = {"Claude": "sha256:" + "a" * 64, "Codex": "sha256:" + "b" * 64}
    monkeypatch.setattr(server, "_swarm_plan_candidates", lambda: [
        _candidate(name, fingerprint) for name, fingerprint in expected.items()
    ])
    room = server.swarm_pilot_create(**_create_kwargs(start=False, expected_specs=expected))
    room_id = room["room_id"]
    bus.register_external_agent(room_id, "Claude")
    msg_id = server._post_message_checked(
        room_id, "Human", "continue this pilot", "request", to="Claude",
    )
    monkeypatch.setattr(server, "_wake_in_progress", lambda *_args: False)
    monkeypatch.setattr(server, "_agent_in_rate_limit_cooldown", lambda *_args: False)
    monkeypatch.setattr(server, "_next_pending_request", lambda *_args: {"id": msg_id})
    monkeypatch.setattr(server.spawn, "get_enabled_spec", lambda name: {"name": name})
    monkeypatch.setattr(server.spawn, "spec_fingerprint", lambda _spec: "sha256:" + "c" * 64)
    spawned = []
    monkeypatch.setattr(server.spawn, "spawn_agent", lambda *args, **kwargs: spawned.append(args))

    wakes = server._wake_agents_for_request(
        room_id, "Human", "continue this pilot", "Claude", None, msg_id,
    )

    assert wakes == [{"agent": "Claude", "status": "spec_drift"}]
    assert spawned == []
