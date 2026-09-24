"""Create-side retry and spec-drift guards for swarm pilot rooms."""

import pytest

from mcp_huddle import bus, server, swarm_pilot


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
    monkeypatch.setattr(server, "swarm_pilot_pump", lambda room: pumps.append(room) or [
        {"status": "dispatched"}
    ])

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
    assert second["previous_dispatch"] == [{"status": "dispatched"}]
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
