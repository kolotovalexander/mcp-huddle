"""Stable identity metadata for safely retried swarm plan creation."""

import pytest

from mcp_huddle import bus, swarm_pilot


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "huddle-home"
    monkeypatch.setattr(bus, "HUDDLE_HOME", home)
    monkeypatch.setattr(bus, "BUS_DIR", home / "rooms")
    monkeypatch.setattr(bus, "NOTIFICATIONS_DIR", home / "notifications")
    monkeypatch.setattr(bus, "NOTIFICATION_LOCKS_DIR", home / "internal" / "notification-locks")
    return home


def test_create_uses_fixed_room_id_and_persists_supplied_fingerprints(isolated_home):
    request_fingerprint = "sha256:" + "a" * 64
    plan_hash = "sha256:" + "b" * 64

    room_id = swarm_pilot.create(
        "pilot", "Organizer", "Make a tiny result", "swarm", ["A"],
        room_id="room_1234abcd",
        client_request_fingerprint=request_fingerprint,
        plan_hash=plan_hash,
    )

    assert room_id == "room_1234abcd"
    state = swarm_pilot.status(room_id)
    assert state["client_request_fingerprint"] == request_fingerprint
    assert state["plan_hash"] == plan_hash


def test_create_keeps_legacy_generated_id_and_omits_empty_fingerprints(isolated_home):
    room_id = swarm_pilot.create(
        "pilot", "Organizer", "Make a tiny result", "swarm", ["A"],
    )

    assert room_id.startswith("room_")
    assert len(room_id) == 13
    state = swarm_pilot.status(room_id)
    assert "client_request_fingerprint" not in state
    assert "plan_hash" not in state
    assert state["mode"] == "swarm"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"room_id": "room_1234ABCd"},
        {"room_id": "../unsafe"},
        {"room_id": 123},
        {"client_request_fingerprint": "raw-credential-value"},
        {"client_request_fingerprint": "sha256:" + "a" * 63},
        {"plan_hash": "sha256:" + "A" * 64},
        {"plan_hash": None},
    ],
)
def test_create_rejects_malformed_identity_before_room_creation(
    isolated_home, monkeypatch, kwargs,
):
    create_calls = []
    original_create_room = bus.create_room

    def spy_create_room(*args, **call_kwargs):
        create_calls.append((args, call_kwargs))
        return original_create_room(*args, **call_kwargs)

    monkeypatch.setattr(bus, "create_room", spy_create_room)
    with pytest.raises(ValueError):
        swarm_pilot.create(
            "pilot", "Organizer", "Make a tiny result", "swarm", ["A"],
            **kwargs,
        )
    assert create_calls == []
    assert not (isolated_home / "rooms").exists()
