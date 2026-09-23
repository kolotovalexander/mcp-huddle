import pytest
from pathlib import Path
from mcp_huddle import spawn
from mcp_huddle.spawn import (
    SpawnSpec,
    _resolve_spawn_args,
    _placeholder_agent_meta,
    codex_resume,
    AgentSpawnError,
)

def test_resolve_spawn_args_codex():
    spec: SpawnSpec = {
        "name": "Codex",
        "cmd": ["codex", "-a", "never", "exec", "-c", 'model_reasoning_effort="medium"', "{brief}"],
        "enabled": True,
        "model": "gpt-5",
        "effort": "high"
    }
    argv, _ = _resolve_spawn_args(spec, "my_brief", Path("/tmp"))
    assert "gpt-5" in argv
    assert '-c' in argv
    assert 'model_reasoning_effort="high"' in argv
    assert 'model_reasoning_effort="medium"' not in argv

def test_resolve_spawn_args_claude():
    spec: SpawnSpec = {
        "name": "Claude",
        "cmd": ["claude", "--model", "sonnet", "-p", "{brief}"],
        "enabled": True,
        "model": "claude-3-opus",
        "effort": "max"
    }
    argv, _ = _resolve_spawn_args(spec, "brief", Path("/tmp"))
    assert argv == ["claude", "--model", "claude-3-opus", "--effort", "max", "-p", "brief"]

def test_resolve_spawn_args_opencode():
    spec: SpawnSpec = {
        "name": "OpenCode",
        "cmd": ["timeout", "1200", "opencode", "run", "{brief}"],
        "enabled": True,
        "variant": "high"
    }
    argv, _ = _resolve_spawn_args(spec, "brief", Path("/tmp"))
    # The flag should be before {brief}
    assert "--variant" in argv
    assert "high" in argv
    
def test_validation_rejection():
    # Invalid effort
    spec: SpawnSpec = {
        "name": "Codex",
        "cmd": ["codex", "{brief}"],
        "enabled": True,
        "effort": "invalid_effort"
    }
    with pytest.raises(AgentSpawnError, match="Codex unsupported effort"):
        _resolve_spawn_args(spec, "brief", Path("/tmp"))
        
    # Variant on claude
    spec2: SpawnSpec = {
        "name": "Claude",
        "cmd": ["claude", "{brief}"],
        "enabled": True,
        "variant": "high"
    }
    with pytest.raises(AgentSpawnError, match="Claude does not support 'variant'"):
        _resolve_spawn_args(spec2, "brief", Path("/tmp"))


@pytest.mark.parametrize("field,value", [
    ("model", ""), ("model", "  "), ("model", None), ("model", 4),
    ("effort", ""), ("effort", None), ("effort", []),
    ("variant", ""), ("variant", None), ("variant", {}),
])
def test_invalid_explicit_model_controls_are_rejected(field, value):
    spec = {"name": "OpenCode", "cmd": ["opencode", "run", "{brief}"], field: value}
    with pytest.raises(AgentSpawnError):
        _resolve_spawn_args(spec, "brief", Path("/tmp"))


def test_invalid_explicit_model_control_fails_before_popen(monkeypatch, tmp_path):
    monkeypatch.setattr(
        spawn.subprocess, "Popen",
        lambda *args, **kwargs: pytest.fail("invalid registry values must not reach Popen"),
    )
    with pytest.raises(AgentSpawnError):
        spawn.spawn_agent(
            {"name": "Codex", "cmd": ["codex", "exec", "{brief}"], "effort": "bogus"},
            "brief", str(tmp_path), tmp_path,
        )


def test_model_only_override_preserves_legacy_effort_and_config_aliases():
    spec = {
        "name": "Codex",
        "cmd": [
            "codex", "exec", "-m=old", "--config", "model_reasoning_effort=high",
            "-c=model_reasoning_effort=low", "-c", "sandbox_mode=read-only", "{brief}",
        ],
        "model": "new-model",
    }
    argv, _ = _resolve_spawn_args(spec, "brief", Path("/tmp"))
    assert argv.count("new-model") == 1
    assert "old" not in argv
    assert "model_reasoning_effort=high" in argv
    assert "-c=model_reasoning_effort=low" in argv
    assert "sandbox_mode=read-only" in argv


def test_effort_only_override_preserves_existing_model():
    spec = {
        "name": "Claude",
        "cmd": ["claude", "--model=sonnet", "--effort", "low", "-p", "{brief}"],
        "effort": "max",
    }
    argv, _ = _resolve_spawn_args(spec, "brief", Path("/tmp"))
    assert argv == ["claude", "--model=sonnet", "--effort", "max", "-p", "brief"]


def test_codex_effort_override_removes_all_config_spellings_without_touching_other_config():
    spec = {
        "name": "Codex",
        "cmd": [
            "codex", "exec", "-c", "model_reasoning_effort=low",
            "--config=model_reasoning_effort=medium", "-c", "sandbox_mode=read-only", "{brief}",
        ],
        "effort": "high",
    }
    argv, _ = _resolve_spawn_args(spec, "brief", Path("/tmp"))
    assert argv.count('model_reasoning_effort="high"') == 1
    assert "model_reasoning_effort=low" not in argv
    assert "model_reasoning_effort=medium" not in argv
    assert "sandbox_mode=read-only" in argv


def test_model_only_override_preserves_legacy_claude_effort():
    spec = {
        "name": "Claude",
        "cmd": ["claude", "--model", "sonnet", "--effort", "high", "-p", "{brief}"],
        "model": "opus",
    }
    argv, _ = _resolve_spawn_args(spec, "brief", Path("/tmp"))
    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--effort") + 1] == "high"
    assert argv[-2:] == ["-p", "brief"]


def test_placeholder_pins_effective_codex_model_and_effort():
    spec = {
        "name": "Codex",
        "cmd": ["codex", "exec", "-m", "old", "--config", "model_reasoning_effort=low", "{brief}"],
        "model": "pinned",
    }
    meta = _placeholder_agent_meta(spec, "brief", Path("/tmp"))
    assert meta["model_settings"] == {"model": "pinned", "effort": "low"}


def test_codex_resume_uses_room_pinned_settings_not_mutable_registry(monkeypatch, tmp_path):
    captured = {}

    class Proc:
        pid = 9876

    monkeypatch.setattr(spawn.subprocess, "Popen", lambda argv, **kwargs: captured.update(argv=argv, **kwargs) or Proc())
    monkeypatch.setattr(spawn, "_reap_in_background", lambda *args, **kwargs: None)
    monkeypatch.setattr(spawn, "get_enabled_spec", lambda _name: {
        "name": "Codex", "cmd": ["codex"], "model": "changed", "effort": "low",
    })
    codex_resume(
        "thread", "prompt", str(tmp_path), str(tmp_path / "events.jsonl"),
        model_settings={"model": "pinned", "effort": "high"},
    )
    argv = captured["argv"]
    assert argv[argv.index("-m") + 1] == "pinned"
    assert 'model_reasoning_effort="high"' in argv
    assert "changed" not in argv


def test_codex_resume_rejects_invalid_pinned_settings_before_popen(monkeypatch, tmp_path):
    monkeypatch.setattr(spawn.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("invalid settings must not spawn"))
    monkeypatch.setattr(spawn, "_reap_in_background", lambda *args, **kwargs: None)
    with pytest.raises(AgentSpawnError):
        codex_resume(
            "thread", "prompt", str(tmp_path), str(tmp_path / "events.jsonl"),
            model_settings={"model": "  "},
        )
