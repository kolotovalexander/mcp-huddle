"""Subscription review must not silently become API usage or a writable agent."""
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from mcp_huddle import spawn


@pytest.fixture
def profile(monkeypatch):
    for name in (*spawn._DIRECT_OPUS_REMOVED_ENV, "ANTHROPIC_API_KEY",
                 "ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS",
                 "ANTHROPIC_DEFAULT_OPUS_MODEL", "CLAUDE_CODE_API_KEY_HELPER"):
        monkeypatch.delenv(name, raising=False)
    return {**spawn._protected_opus_profiles()["Claude Opus 5 (subscription review)"],
            "enabled": True, "mcp_url": "http://127.0.0.1:45111/mcp"}


def test_subscription_native_spawn_is_fixed_and_readonly(profile, monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setenv("MCP_HUDDLE_READONLY", "0")

    def auth_probe(argv, **kwargs):
        captured["auth_argv"] = argv
        captured["auth_cwd"] = kwargs["cwd"]
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "loggedIn": True, "authMethod": "claude.ai",
            "apiProvider": "firstParty", "subscriptionType": "pro"}))

    def popen(argv, **kwargs):
        captured.update(argv=argv, **kwargs)
        return SimpleNamespace(pid=12345)

    monkeypatch.setattr(spawn.subprocess, "run", auth_probe)
    monkeypatch.setattr(spawn.subprocess, "Popen", popen)
    monkeypatch.setattr(spawn, "_reap_in_background", lambda *a, **k: None)
    project = tmp_path / "approved"
    project.mkdir()
    profile["cmd"] = ["untrusted-binary", "--dangerously-skip-permissions"]
    spawn.spawn_agent(profile, "review", str(project), tmp_path / "logs")
    argv = captured["argv"]
    assert argv[0] == (spawn._CLAUDE_BIN or "claude")
    assert argv[argv.index("--model") + 1] == "claude-opus-5"
    assert "--bare" not in argv and "--restricted" in argv
    assert "--dangerously-skip-permissions" not in argv
    assert "--fallback-model" not in argv
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert captured["cwd"] == captured["auth_cwd"] != str(project)
    assert argv[argv.index("--add-dir") + 1] == str(project.resolve())
    assert "--strict-mcp-config" in argv
    assert json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"] == {
        "huddle": {"type": "http", "url": profile["mcp_url"]}}
    allowed = argv[argv.index("--allowedTools") + 1]
    assert "*" not in allowed and "mcp__huddle__room_create" not in allowed
    assert "Bash" not in argv[argv.index("--tools") + 1]
    assert "ANTHROPIC_API_KEY" not in captured["env"]
    shutil.rmtree(captured["cwd"])


@pytest.mark.parametrize("key", ["ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_VERTEX",
    "ANTHROPIC_DEFAULT_OPUS_MODEL"])
def test_subscription_conflict_stops_before_model_or_auth_call(profile, monkeypatch, tmp_path, key):
    monkeypatch.setenv(key, "never-print-this-value")
    monkeypatch.setattr(spawn.subprocess, "run", lambda *a, **k: pytest.fail("auth must not start"))
    monkeypatch.setattr(spawn.subprocess, "Popen", lambda *a, **k: pytest.fail("model must not start"))
    with pytest.raises(spawn.AgentSpawnError, match=key) as err:
        spawn.spawn_agent(profile, "review", str(tmp_path), tmp_path / "logs")
    assert "never-print-this-value" not in str(err.value)


@pytest.mark.parametrize("url", ["https://example.com/mcp", "http://secret@localhost:45111/mcp",
                                  "http://127.0.0.1:45111/mcp?token=secret"])
def test_subscription_rejects_remote_or_credentialed_mcp(profile, monkeypatch, tmp_path, url):
    profile["mcp_url"] = url
    monkeypatch.setattr(spawn.subprocess, "run", lambda *a, **k: pytest.fail("auth must not start"))
    with pytest.raises(spawn.AgentSpawnError) as err:
        spawn.spawn_agent(profile, "review", str(tmp_path), tmp_path / "logs")
    assert url not in str(err.value)


def test_subscription_auth_mismatch_stops_and_removes_owned_cwd(profile, monkeypatch, tmp_path):
    created = []
    original = spawn.tempfile.mkdtemp

    def make_temp(**kwargs):
        p = original(dir=tmp_path, **kwargs)
        created.append(Path(p))
        return p

    monkeypatch.setattr(spawn.tempfile, "mkdtemp", make_temp)
    monkeypatch.setattr(spawn.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout=json.dumps({"loggedIn": True, "authMethod": "api_key",
                                      "apiProvider": "firstParty"})))
    monkeypatch.setattr(spawn.subprocess, "Popen", lambda *a, **k: pytest.fail("model must not start"))
    with pytest.raises(spawn.AgentSpawnError, match="claude.ai"):
        spawn.spawn_agent(profile, "review", str(tmp_path), tmp_path / "logs")
    assert created and all(not p.exists() for p in created)


def test_registry_cannot_change_subscription_model_permissions_or_auto_roster(profile):
    malicious = {**profile, "cmd": ["other-cli", "--dangerously-skip-permissions"],
                 "profile": "other", "auto": True}
    merged = spawn._merge_registry(spawn.DEFAULT_REGISTRY, [malicious])
    restored = next(p for p in merged if p["name"] == profile["name"])
    assert restored["cmd"] != malicious["cmd"]
    assert restored["profile"] == spawn._SUBSCRIPTION_OPUS_REVIEW_PROFILE
    assert restored["auto"] is False and restored["enabled"] is True
    assert restored["mcp_url"] == profile["mcp_url"]
    assert spawn._preserve_direct_opus_profile_contract([malicious]) == [restored]


@pytest.mark.parametrize("source", ["file", "env"])
def test_typed_alias_cannot_join_blanket_auto_spawn(profile, monkeypatch, source):
    alias = {**profile, "name": "Alias", "auto": True}
    monkeypatch.setattr(spawn, "_load_env_registry", lambda: [alias] if source == "env" else None)
    monkeypatch.setattr(spawn, "_load_registry_file", lambda: [alias])
    with pytest.raises(spawn.AgentSpawnError, match="canonical registry name"):
        spawn._raw_registry()
