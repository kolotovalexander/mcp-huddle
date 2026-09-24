"""Reviewable room proposals preserve user input without creating state."""

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


def _spec(name, executable, *, flags=()):
    return {
        "name": name,
        "cmd": [executable, *flags, "-p", "{brief}"],
        "enabled": True,
        "cost_class": "cheap",
    }


def _configure_registry(monkeypatch, specs):
    monkeypatch.setattr(server.spawn, "_raw_registry", lambda: [
        spawn._apply_readonly(spec) for spec in specs
    ])
    monkeypatch.setattr(server.spawn, "_readonly_enabled", lambda: True)
    monkeypatch.setattr(server.shutil, "which", lambda _name: "/installed/cli")
    monkeypatch.setattr(server.os, "access", lambda *_args: True)
    monkeypatch.setattr(server.Path, "is_file", lambda _self: True)
    monkeypatch.setattr(server.spawn, "probe_cli_login", lambda *_args: {
        "status": "authenticated", "reason": "cli_login_present",
    })


def test_room_proposal_preserves_inputs_and_returns_fingerprinted_create_args(monkeypatch):
    _configure_registry(monkeypatch, [
        _spec("Claude", "claude", flags=("--model", "sonnet")),
        _spec("Codex", "codex", flags=("exec",)),
    ])
    jev_inputs = []
    monkeypatch.setattr(server.swarm_jev, "choose_mode", lambda facts: (
        jev_inputs.append(facts)
        or swarm_jev.ChoiceResult("ok", "team", 0.9, "mode advice")
    ))
    monkeypatch.setattr(server.swarm_jev, "choose_candidate", lambda facts, candidates: (
        jev_inputs.append((facts, candidates))
        or swarm_jev.ChoiceResult("ok", "Codex", 0.9, "member advice")
    ))
    monkeypatch.setattr(server.swarm_pilot, "create", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("proposal must not create a room")
    ))
    monkeypatch.setattr(server.spawn, "spawn_agent", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("proposal must not start a process")
    ))

    goal = "  Make the requested small change exactly as described.  "
    requirements = _profile()
    result = server.swarm_room_proposal(
        name="Readable room", organizer="Human", goal=goal, requirements=requirements,
        cwd="/private/tmp/local-room-path",
    )

    assert result["goal"] == goal
    assert result["requirements"] == requirements
    assert result["plan"]["status"] == "planned"
    args = result["create_args"]
    assert args["name"] == "Readable room"
    assert args["organizer"] == "Human"
    assert args["goal"] == goal
    assert args["cwd"] == "/private/tmp/local-room-path"
    assert args["mode"] == "team"
    assert args["members"] == ["Codex", "Claude"]
    assert args["start"] is False
    assert args["workspace_strategy"] == "shared_only"
    assert args["plan_hash"] == result["plan"]["plan_hash"]
    assert args["expected_specs"] == {
        item["id"]: item["spec_fingerprint"] for item in result["plan"]["members"]
    }
    assert all(not hasattr(item, "goal") for item in jev_inputs)
    assert goal not in repr(jev_inputs)
    assert "Readable room" not in repr(jev_inputs)
    assert "/private/tmp/local-room-path" not in repr(jev_inputs)
    assert result["side_effects"] == {"room_created": False, "child_processes_started": False}
    assert result["preflight"]["static_plan"] == "planned"
    assert result["preflight"]["exact_model_provider_response"] == "not_checked"
    assert all(item["status"] == "authenticated"
               for item in result["preflight"]["cli_login"].values())


def test_login_preflight_is_bounded_deduplicated_and_secret_free(monkeypatch):
    _configure_registry(monkeypatch, [
        _spec("Claude A", "claude", flags=("--model", "sonnet")),
        _spec("Claude B", "claude", flags=("--model", "opus")),
        _spec("Codex", "codex", flags=("exec",)),
    ])
    monkeypatch.setattr(server.tempfile, "gettempdir", lambda: "/neutral/temp")
    calls = []

    def probe(spec, cwd):
        calls.append((spec["name"], cwd))
        if spec["name"].startswith("Claude"):
            return {
                "status": "unauthenticated", "reason": "cli_logged_out",
                "account": "private@example.com",
            }
        return {
            "status": "unknown", "reason": "probe_timeout",
            "stderr": "secret provider token",
        }

    monkeypatch.setattr(server.spawn, "probe_cli_login", probe)
    result = server.swarm_room_proposal(
        name="Login status review", organizer="Human", goal="Inspect CLI login only",
        requirements=_profile(max_members=3), explicit_mode="team",
        explicit_members=["Claude A", "Claude B", "Codex"],
    )

    assert calls == [("Claude A", "/neutral/temp"), ("Codex", "/neutral/temp")]
    assert result["preflight"]["cli_login"] == {
        "Claude A": {"status": "unauthenticated", "reason": "cli_logged_out"},
        "Claude B": {"status": "unauthenticated", "reason": "cli_logged_out"},
        "Codex": {"status": "unknown", "reason": "probe_timeout"},
    }
    assert "private@example.com" not in repr(result)
    assert "secret provider token" not in repr(result)
    assert result["preflight"]["exact_model_provider_response"] == "not_checked"
    assert result["create_args"]["start"] is False

    skipped = server.swarm_room_proposal(
        name="No auth lookup", organizer="Human", goal="Prepare without a login check",
        requirements=_profile(parts="one", max_members=1), explicit_mode="council",
        explicit_members=["Codex"], check_cli_login=False,
    )
    assert skipped["preflight"]["cli_login"] == {
        "Codex": {"status": "skipped", "reason": "caller_disabled"},
    }
    assert len(calls) == 2


def test_exact_model_preflight_uses_only_sentinel_and_blocks_failed_route(monkeypatch):
    claude = _spec("Claude", "claude", flags=("--model", "sonnet", "--effort", "low"))
    claude["pass_env"] = ["PROVIDER_API_KEY"]
    codex = _spec(
        "Codex", "codex", flags=("exec", "-m", "gpt-6-sol", "-c", 'model_reasoning_effort="high"'),
    )
    _configure_registry(monkeypatch, [claude, codex])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "private-api-key")
    monkeypatch.setenv("PROVIDER_API_KEY", "private-provider-key")
    monkeypatch.setattr(server.tempfile, "gettempdir", lambda: "/neutral/tmp")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[0] == "claude":
            return spawn.subprocess.CompletedProcess(argv, 1, "", "private provider detail")
        return spawn.subprocess.CompletedProcess(argv, 0, "HUDDLE PREFLIGHT OK\n", "")

    monkeypatch.setattr(spawn.subprocess, "run", fake_run)
    result = server.swarm_room_proposal(
        name="Pinned model check", organizer="Human", goal="private room goal",
        requirements=_profile(parts="two_three", max_members=2),
        explicit_mode="team", explicit_members=["Claude", "Codex"],
        check_cli_login=False, check_exact_model=True,
    )

    assert len(calls) == 2
    argv, kwargs = calls[0]
    assert argv[0:2] == ["claude", "--restricted"]
    assert ["--model", "sonnet"] == argv[argv.index("--model"):argv.index("--model") + 2]
    assert ["--effort", "low"] == argv[argv.index("--effort"):argv.index("--effort") + 2]
    assert "--tools" in argv and argv[argv.index("--tools") + 1] == ""
    assert "--dangerously-skip-permissions" not in argv
    assert "--permission-prompts" in argv and argv[argv.index("--permission-prompts") + 1] == "none"
    assert "HUDDLE PREFLIGHT OK" in argv[-1]
    assert "private room goal" not in repr(argv)
    assert kwargs["cwd"] == "/neutral/tmp"
    assert "ANTHROPIC_API_KEY" not in kwargs["env"]
    assert "PROVIDER_API_KEY" not in kwargs["env"]
    codex_argv, codex_kwargs = calls[1]
    assert codex_argv[:3] == ["codex", "exec", "--ephemeral"]
    assert codex_argv[codex_argv.index("--sandbox") + 1] == "read-only"
    assert codex_argv[codex_argv.index("--model") + 1] == "gpt-6-sol"
    assert 'model_reasoning_effort="high"' in codex_argv
    assert "mcp_servers={}" in codex_argv
    assert "private room goal" not in repr(codex_argv)
    assert codex_kwargs["cwd"] == "/neutral/tmp"
    assert "ANTHROPIC_API_KEY" not in codex_kwargs["env"]
    assert "PROVIDER_API_KEY" not in codex_kwargs["env"]
    assert result["preflight"]["exact_model_provider_response"] == {
        "Claude": {"status": "failed", "reason": "provider_request_failed"},
        "Codex": {"status": "passed", "reason": "sentinel_response_received"},
    }
    assert result["create_args"] is None
    assert result["status"] == "not_ready"
    assert "Claude" in result["proposal_blockers"][0]
    assert "private provider detail" not in repr(result)

    repeated = server.swarm_room_proposal(
        name="Pinned model check again", organizer="Human", goal="different private goal",
        requirements=_profile(parts="two_three", max_members=2),
        explicit_mode="team", explicit_members=["Claude", "Codex"],
        check_cli_login=False, check_exact_model=True,
    )
    assert len(calls) == 2
    assert repeated["preflight"]["exact_model_provider_response"] == result["preflight"][
        "exact_model_provider_response"
    ]


def test_blocked_or_unsupported_plan_has_no_create_args(monkeypatch):
    _configure_registry(monkeypatch, [_spec("Claude", "claude")])
    monkeypatch.setattr(server.shutil, "which", lambda _name: None)
    monkeypatch.setattr(server.swarm_jev, "choose_mode", lambda *_: None)
    blocked = server.swarm_room_proposal(
        name="Room", organizer="Human", goal="Do work", requirements=_profile(),
    )
    unsupported = server.swarm_room_proposal(
        name="Room", organizer="Human", goal="Do work",
        requirements=_profile(needs_files="write"),
    )

    assert blocked["plan"]["status"] in {"blocked", "unsupported"}
    assert blocked["create_args"] is None
    assert blocked["preflight"]["cli_login"] == {}
    assert unsupported["plan"]["status"] == "unsupported"
    assert unsupported["create_args"] is None
    assert unsupported["preflight"]["cli_login"] == {}


def test_proposal_rejects_unknown_requirement_fields_before_jev(monkeypatch):
    _configure_registry(monkeypatch, [_spec("Claude", "claude")])
    monkeypatch.setattr(server.swarm_jev, "choose_mode", lambda *_: (_ for _ in ()).throw(
        AssertionError("invalid profile must fail before Jev")
    ))

    try:
        server.swarm_room_proposal(
            name="Room", organizer="Human", goal="Do work",
            requirements={**_profile(), "extra": "not accepted"},
        )
    except ValueError as exc:
        assert "unsupported profile field" in str(exc)
    else:
        raise AssertionError("unknown requirement field should be rejected")


def test_proposal_rejects_invalid_or_oversized_human_fields(monkeypatch):
    _configure_registry(monkeypatch, [_spec("Claude", "claude")])
    monkeypatch.setattr(server.swarm_jev, "choose_mode", lambda *_: (_ for _ in ()).throw(
        AssertionError("invalid human fields must fail before Jev")
    ))
    for fields in (
        {"name": "  ", "organizer": "Human", "goal": "Do work"},
        {"name": "Room", "organizer": "Human", "goal": " "},
        {"name": "x" * 201, "organizer": "Human", "goal": "Do work"},
        {"name": "Room", "organizer": "Human", "goal": "x" * 5001},
    ):
        try:
            server.swarm_room_proposal(**fields, requirements=_profile())
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid fields should be rejected: {fields.keys()}")
