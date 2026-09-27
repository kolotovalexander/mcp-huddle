"""Per-wake Codex member identity over HTTP MCP; no processes or network."""

from types import SimpleNamespace

import pytest

from mcp_huddle import bus, server, spawn, swarm_pilot


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "huddle-home"
    monkeypatch.setattr(bus, "HUDDLE_HOME", home)
    monkeypatch.setattr(bus, "BUS_DIR", home / "rooms")
    monkeypatch.setattr(bus, "NOTIFICATIONS_DIR", home / "notifications")
    monkeypatch.setattr(bus, "NOTIFICATION_LOCKS_DIR", home / "internal" / "notification-locks")
    return home


def _ctx(headers):
    request = None if headers is None else SimpleNamespace(headers=headers)
    return SimpleNamespace(request_context=SimpleNamespace(request=request))


def _claim(room, member, wake_id):
    def update(meta):
        meta.setdefault("agent_meta", {}).setdefault(member, {})["wake_claim_id"] = wake_id
        return meta
    bus._update_meta_locked(room, update)


def test_member_token_verifies_only_own_active_claim(isolated_home):
    room = swarm_pilot.create("pilot", "Organizer", "Tiny goal", "team", ["A", "B"])
    _claim(room, "A", "wakeA1")
    _claim(room, "B", "wakeB1")
    secret_a = server._issue_member_token(room, "A", "wakeA1")
    secret_b = server._issue_member_token(room, "B", "wakeB1")
    assert secret_a and secret_b and secret_a != secret_b
    # Wrong wake or non-member: nothing is minted.
    assert server._issue_member_token(room, "A", "other") is None
    assert server._issue_member_token(room, "Organizer", "wakeA1") is None

    info = bus.get_room_info(room)["agent_meta"]["A"]
    assert info["member_token_sha256"] == spawn.member_token_digest(secret_a)
    assert secret_a not in str(bus.get_room_info(room))  # only the digest is stored

    header = spawn.MEMBER_TOKEN_HEADER
    me = server.swarm_whoami(room, _ctx({header: secret_a}))
    assert me["verified"] and me["member"] == "A"
    assert me["member_id"] == swarm_pilot.status(room)["member_ids"]["A"]
    assert server._verified_member(_ctx({header: secret_a}), room, "A")["member"] == "A"
    assert server._verified_member(_ctx({header: secret_a}), room, "B") is None

    for ctx in (_ctx({header: "forged"}), _ctx({}), _ctx(None), None):
        assert server.swarm_whoami(room, ctx)["verified"] is False

    # Claim released or replaced by a newer wake: the old secret is dead.
    assert server._clear_wake_claim(room, "A", "wakeA1")
    assert "member_token_sha256" not in bus.get_room_info(room)["agent_meta"]["A"]
    assert server.swarm_whoami(room, _ctx({header: secret_a}))["verified"] is False
    _claim(room, "A", "wakeA2")
    assert server.swarm_whoami(room, _ctx({header: secret_a}))["verified"] is False


def test_codex_route_carries_header_name_not_secret():
    spec = {"name": "Codex Pilot", "cmd": ["codex", "exec", "{brief}"],
            "mcp_url": "http://127.0.0.1:8014/mcp"}
    assert spawn.member_identity_supported(spec)
    assert not spawn.member_identity_supported({"name": "Codex", "cmd": ["codex", "exec"]})
    argv = spawn._apply_codex_mcp_route(spec, list(spec["cmd"]), member_header=True)
    config = argv[argv.index("-c") + 1]
    assert 'env_http_headers={"X-Huddle-Member"="HUDDLE_MEMBER_TOKEN"}' in config
    assert "bearer" not in config.lower()
    legacy = spawn._apply_codex_mcp_route(spec, list(spec["cmd"]))
    assert "env_http_headers" not in " ".join(legacy)
