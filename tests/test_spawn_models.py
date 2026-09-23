import pytest
from pathlib import Path
from mcp_huddle.spawn import (
    SpawnSpec,
    _resolve_spawn_args,
    codex_resume,
    AgentSpawnError,
    _VALID_CODEX_EFFORTS,
    _VALID_CLAUDE_EFFORTS,
    _VALID_AGY_EFFORTS
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

