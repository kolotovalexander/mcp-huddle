"""Huddle-side advisory preview: static checks only, with no room/process work."""

from mcp_huddle import server, spawn, swarm_jev


def _profile(**overrides):
    value = {
        "task_type": "code_change",
        "needs_files": "read",
        "parts": "two_three",
        "sequential_dependency": False,
        "diverse_opinions": False,
        "max_members": 3,
        "budget": "any",
    }
    value.update(overrides)
    return value


def _spec(name, executable, *, enabled=True, cost="unknown", flags=()):
    return {
        "name": name,
        "cmd": [executable, *flags, "-p", "{brief}"],
        "enabled": enabled,
        "cost_class": cost,
    }


def _configure_registry(monkeypatch, specs):
    monkeypatch.setattr(server.spawn, "_raw_registry", lambda: [
        spawn._apply_readonly(spec) for spec in specs
    ])
    monkeypatch.setattr(server.spawn, "_readonly_enabled", lambda: True)
    monkeypatch.setattr(server.shutil, "which", lambda _name: "/installed/cli")
    monkeypatch.setattr(server.os, "access", lambda *_args: True)
    monkeypatch.setattr(server.Path, "is_file", lambda _self: True)


def test_preview_is_static_and_does_not_create_room_or_spawn(monkeypatch):
    _configure_registry(monkeypatch, [
        _spec("Claude", "claude", cost="cheap", flags=("--model", "sonnet")),
        _spec("Codex", "codex", cost="cheap", flags=("exec",)),
    ])
    jev_calls = []
    monkeypatch.setattr(server.swarm_jev, "choose_mode", lambda facts: (
        jev_calls.append(("mode", facts))
        or swarm_jev.ChoiceResult("ok", "team", 0.91, "advisory")
    ))
    monkeypatch.setattr(server.swarm_jev, "choose_candidate", lambda facts, candidates: (
        jev_calls.append(("candidate", facts, candidates))
        or swarm_jev.ChoiceResult("ok", "Codex", 0.87, "advisory")
    ))
    monkeypatch.setattr(server.swarm_pilot, "create", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("preview must not create a room")
    ))
    monkeypatch.setattr(server.spawn, "spawn_agent", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("preview must not start a process")
    ))

    result = server.swarm_plan_preview(_profile())

    assert result["status"] == "planned"
    assert result["mode"] == "team"
    assert [member["id"] for member in result["members"]] == ["Codex", "Claude"]
    assert result["side_effects"] == {"room_created": False, "child_processes_started": False}
    assert "provider authentication and response not verified" in result["readiness"]
    assert "Jev selected the first participant" in result["jev"]["roster_note"]
    assert [call[0] for call in jev_calls] == ["mode", "candidate"]
    assert not hasattr(jev_calls[0][1], "goal")


def test_explicit_mode_and_roster_override_and_skip_matching_jev_questions(monkeypatch):
    _configure_registry(monkeypatch, [
        _spec("Claude", "claude", cost="cheap", flags=("--model", "sonnet")),
        _spec("Codex", "codex", cost="cheap", flags=("exec",)),
    ])
    monkeypatch.setattr(server.swarm_jev, "choose_mode", lambda *_: (_ for _ in ()).throw(
        AssertionError("explicit mode should win")
    ))
    monkeypatch.setattr(server.swarm_jev, "choose_candidate", lambda *_: (_ for _ in ()).throw(
        AssertionError("explicit roster should win")
    ))

    result = server.swarm_plan_preview(
        _profile(), explicit_mode="relay", explicit_members=["Codex", "Claude"]
    )

    assert result["mode"] == "relay"
    assert [member["id"] for member in result["members"]] == ["Codex", "Claude"]
    assert result["jev"]["mode"]["reason"] == "organizer supplied the mode"
    assert result["jev"]["first_participant"]["reason"] == "organizer supplied the participant list"


def test_jev_failure_uses_deterministic_mode_and_registry_order(monkeypatch):
    _configure_registry(monkeypatch, [
        _spec("Claude", "claude", cost="cheap", flags=("--model", "sonnet")),
        _spec("Codex", "codex", cost="cheap", flags=("exec",)),
    ])
    monkeypatch.setattr(server.swarm_jev, "choose_mode", lambda *_: (_ for _ in ()).throw(
        RuntimeError("transport detail must not escape")
    ))
    monkeypatch.setattr(server.swarm_jev, "choose_candidate", lambda *_: (_ for _ in ()).throw(
        RuntimeError("transport detail must not escape")
    ))

    result = server.swarm_plan_preview(_profile())

    assert result["mode"] == "team"
    assert [member["id"] for member in result["members"]] == ["Claude", "Codex"]
    assert "transport detail" not in repr(result)
    assert "registry order" in result["jev"]["first_participant"]["reason"]


def test_unavailable_binary_is_excluded_without_live_registry_probe(monkeypatch):
    _configure_registry(monkeypatch, [
        _spec("Missing", "missing", cost="cheap"),
    ])
    monkeypatch.setattr(server.shutil, "which", lambda _name: None)
    mode_calls = []
    monkeypatch.setattr(server.swarm_jev, "choose_mode", lambda facts: (
        mode_calls.append(facts)
        or swarm_jev.ChoiceResult("fallback", None, None, "service_error")
    ))

    result = server.swarm_plan_preview(_profile())

    assert result["status"] == "blocked"
    assert result["members"] == []
    assert "CLI executable not found" in result["excluded"]["Missing"]
    assert mode_calls == []


def test_preview_redacts_credential_shaped_model_label(monkeypatch):
    _configure_registry(monkeypatch, [
        _spec(
            "Claude", "claude", cost="cheap",
            flags=("--model", "sk-super-secret-model-value-123456789012345678901234"),
        ),
    ])

    result = server.swarm_plan_preview(
        _profile(parts="one", max_members=1), explicit_mode="council"
    )

    assert result["members"][0]["model"] == "configured model (redacted)"
    assert "sk-super-secret" not in repr(result)


def test_read_only_opt_in_is_visible_and_only_applies_to_read_profile(monkeypatch):
    _configure_registry(monkeypatch, [
        _spec("OpenCode", "opencode", cost="cheap", flags=("run",)),
    ])
    result = server.swarm_plan_preview(
        _profile(parts="one", max_members=1), explicit_mode="council",
        allow_unenforced_read=True,
    )
    write_result = server.swarm_plan_preview(
        _profile(parts="one", max_members=1, needs_files="write"),
        allow_unenforced_read=True,
    )

    assert result["status"] == "planned"
    assert result["members"][0]["readonly_enforced"] is False
    assert any("explicitly allowed" in item for item in result["decisions"])
    assert write_result["status"] == "unsupported"
    assert write_result["members"] == []


def test_one_part_council_caps_automatic_roster_but_preserves_explicit_roster(monkeypatch):
    _configure_registry(monkeypatch, [
        _spec("Claude", "claude", cost="cheap", flags=("--model", "sonnet")),
        _spec("Codex", "codex", cost="cheap", flags=("exec",)),
        _spec("OpenCode", "opencode", cost="cheap", flags=("run",)),
    ])
    monkeypatch.setattr(server.swarm_jev, "choose_mode", lambda *_: (
        swarm_jev.ChoiceResult("ok", "council", 0.9, "advisory")
    ))
    monkeypatch.setattr(server.swarm_jev, "choose_candidate", lambda *_: (
        swarm_jev.ChoiceResult("fallback", None, None, "low_confidence")
    ))

    automatic = server.swarm_plan_preview(_profile(parts="one", max_members=3))
    explicit = server.swarm_plan_preview(
        _profile(parts="one", max_members=3), explicit_members=["Claude", "Codex", "OpenCode"],
        allow_unenforced_read=True,
    )

    assert len(automatic["members"]) == 2
    assert "Roster capped at 2" in automatic["jev"]["roster_note"]
    assert len(explicit["members"]) == 3


def test_static_preflight_checks_timeout_wrapper_executable(monkeypatch):
    _configure_registry(monkeypatch, [
        {
            **_spec("Claude", "timeout", cost="cheap"),
            "cmd": ["timeout", "120", "claude", "-p", "{brief}"],
        },
    ])
    monkeypatch.setattr(server.shutil, "which", lambda name: (
        None if name == "timeout" else "/installed/claude"
    ))

    result = server.swarm_plan_preview(
        _profile(parts="one", max_members=1), explicit_mode="council"
    )

    assert result["status"] == "blocked"
    assert result["members"] == []
    assert "timeout wrapper executable not found" in result["excluded"]["Claude"]


def test_jev_none_reason_is_visible_while_deterministic_mode_is_used(monkeypatch):
    _configure_registry(monkeypatch, [
        _spec("Claude", "claude", cost="cheap", flags=("--model", "sonnet")),
        _spec("Codex", "codex", cost="cheap", flags=("exec",)),
    ])
    monkeypatch.setattr(server.swarm_jev, "choose_mode", lambda *_: (
        swarm_jev.ChoiceResult("ok", "none", 0.9, "organizer should choose")
    ))

    result = server.swarm_plan_preview(_profile())

    assert result["mode"] == "team"
    assert result["jev"]["mode"]["choice"] is None
    assert "none" in result["jev"]["mode"]["reason"].lower()
    assert "organizer should choose" in result["jev"]["mode"]["reason"]


def test_jev_none_candidate_reason_is_visible_and_falls_back(monkeypatch):
    _configure_registry(monkeypatch, [
        _spec("Claude", "claude", cost="cheap", flags=("--model", "sonnet")),
        _spec("Codex", "codex", cost="cheap", flags=("exec",)),
    ])
    monkeypatch.setattr(server.swarm_jev, "choose_mode", lambda *_: (
        swarm_jev.ChoiceResult("ok", "team", 0.9, "advisory")
    ))
    monkeypatch.setattr(server.swarm_jev, "choose_candidate", lambda *_: (
        swarm_jev.ChoiceResult("ok", "none", 0.9, "no candidate fits")
    ))

    result = server.swarm_plan_preview(_profile())

    assert [member["id"] for member in result["members"]] == ["Claude", "Codex"]
    assert "none" in result["jev"]["first_participant"]["reason"].lower()
    assert "no candidate fits" in result["jev"]["first_participant"]["reason"]


def test_readonly_gate_rejects_untrusted_cli_extension(monkeypatch):
    _configure_registry(monkeypatch, [
        _spec(
            "Claude", "claude", cost="cheap",
            flags=("--settings", "unsafe.json"),
        ),
    ])

    result = server.swarm_plan_preview(
        _profile(parts="one", max_members=1), explicit_mode="council"
    )

    assert result["status"] == "blocked"
    assert "readonly_not_enforced" in result["excluded"]["Claude"]
