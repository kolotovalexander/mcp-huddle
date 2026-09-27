"""A login probe may inspect native CLI status without starting a model turn."""

import json
import subprocess

from mcp_huddle import spawn


def _spec(binary: str) -> spawn.SpawnSpec:
    return {"name": binary, "cmd": [binary, "-p", "{brief}"], "enabled": True}


def test_claude_login_probe_returns_only_closed_status(monkeypatch, tmp_path):
    secret = "private-account@example.com"
    observed = []

    def fake_run(argv, **kwargs):
        observed.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, json.dumps({
            "loggedIn": True, "email": secret, "organization": "secret-org",
            "authMethod": "claude.ai",
        }), "token=secret-token")

    monkeypatch.setattr(spawn.subprocess, "run", fake_run)
    result = spawn.probe_cli_login(_spec("claude"), str(tmp_path))

    assert result == {"status": "authenticated", "reason": "cli_login_present"}
    assert secret not in repr(result)
    assert observed[0][0] == ["claude", "auth", "status", "--json"]
    assert observed[0][1]["timeout"] <= 10
    assert observed[0][1]["capture_output"] is True
    assert "ANTHROPIC_API_KEY" not in observed[0][1]["env"]


def test_claude_logged_out_and_malformed_output(monkeypatch, tmp_path):
    monkeypatch.setattr(spawn.subprocess, "run", lambda argv, **kwargs:
                        subprocess.CompletedProcess(argv, 0, '{"loggedIn":false,"email":"secret"}', ""))
    assert spawn.probe_cli_login(_spec("claude"), str(tmp_path)) == {
        "status": "unauthenticated", "reason": "cli_logged_out"}

    monkeypatch.setattr(spawn.subprocess, "run", lambda argv, **kwargs:
                        subprocess.CompletedProcess(argv, 0, "secret malformed", ""))
    assert spawn.probe_cli_login(_spec("claude"), str(tmp_path)) == {
        "status": "unknown", "reason": "unrecognized_output"}


def test_codex_login_status_and_router_limit(monkeypatch, tmp_path):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "Logged in using ChatGPT\n", "email=private@example.com")

    monkeypatch.setattr(spawn.subprocess, "run", fake_run)
    result = spawn.probe_cli_login(_spec("codex"), str(tmp_path))
    assert result == {"status": "authenticated", "reason": "cli_login_present"}
    assert calls == [["codex", "login", "status"]]
    assert "private@example.com" not in repr(result)

    monkeypatch.setattr(spawn.subprocess, "run", lambda argv, **kwargs:
                        subprocess.CompletedProcess(argv, 1, "Not logged in\n", "secret"))
    assert spawn.probe_cli_login(_spec("codex"), str(tmp_path)) == {
        "status": "unauthenticated", "reason": "cli_logged_out"}


def test_codex_recognizes_login_on_stderr_without_leaking_it(monkeypatch, tmp_path):
    monkeypatch.setattr(spawn.subprocess, "run", lambda argv, **kwargs:
                        subprocess.CompletedProcess(argv, 0, "", "Logged in using ChatGPT\n"))
    assert spawn.probe_cli_login(_spec("codex"), str(tmp_path)) == {
        "status": "authenticated", "reason": "cli_login_present"}

    monkeypatch.setattr(spawn.subprocess, "run", lambda argv, **kwargs:
                        subprocess.CompletedProcess(argv, 1, "", "Not logged in\nprivate@example.com"))
    result = spawn.probe_cli_login(_spec("codex"), str(tmp_path))
    assert result == {"status": "unauthenticated", "reason": "cli_logged_out"}
    assert "private@example.com" not in repr(result)


def test_codex_conflicting_or_unrecognized_status_is_unknown(monkeypatch, tmp_path):
    monkeypatch.setattr(spawn.subprocess, "run", lambda argv, **kwargs:
                        subprocess.CompletedProcess(argv, 0, "Logged in using ChatGPT", "Not logged in"))
    assert spawn.probe_cli_login(_spec("codex"), str(tmp_path)) == {
        "status": "unknown", "reason": "unrecognized_output"}

    monkeypatch.setattr(spawn.subprocess, "run", lambda argv, **kwargs:
                        subprocess.CompletedProcess(argv, 0, "", "account=private@example.com"))
    assert spawn.probe_cli_login(_spec("codex"), str(tmp_path)) == {
        "status": "unknown", "reason": "unrecognized_output"}


def test_probe_unknown_harness_and_timeout_never_exposes_exception(monkeypatch, tmp_path):
    monkeypatch.setattr(spawn.subprocess, "run", lambda *args, **kwargs:
                        (_ for _ in ()).throw(AssertionError("must not call")))
    assert spawn.probe_cli_login(_spec("agy"), str(tmp_path)) == {
        "status": "unknown", "reason": "unsupported_harness"}
    assert spawn.probe_cli_login(_spec("opencode"), str(tmp_path)) == {
        "status": "unknown", "reason": "unsupported_harness"}

    def timed_out(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 1, output="private@example.com")

    monkeypatch.setattr(spawn.subprocess, "run", timed_out)
    assert spawn.probe_cli_login(_spec("codex"), str(tmp_path)) == {
        "status": "unknown", "reason": "probe_timeout"}


def test_probe_uses_configured_binary_without_model_arguments(monkeypatch, tmp_path):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "Logged in using ChatGPT", "")

    monkeypatch.setattr(spawn.subprocess, "run", fake_run)
    spec = _spec("/opt/tools/codex")
    spec["cmd"] = ["timeout", "30", "/opt/tools/codex", "exec", "--model", "private-model", "{brief}"]
    assert spawn.probe_cli_login(spec, str(tmp_path))["status"] == "authenticated"
    assert calls == [["/opt/tools/codex", "login", "status"]]


def test_probe_uses_native_config_but_omits_registry_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("PROVIDER_API_KEY", "private-key")
    observed = []

    def fake_run(argv, **kwargs):
        observed.append(kwargs["env"])
        return subprocess.CompletedProcess(argv, 0, "Logged in using ChatGPT", "")

    monkeypatch.setattr(spawn.subprocess, "run", fake_run)
    spec = _spec("codex")
    spec["pass_env"] = ["PROVIDER_API_KEY"]
    assert spawn.probe_cli_login(spec, str(tmp_path))["status"] == "authenticated"
    assert observed[0]["CODEX_HOME"] == str(tmp_path / "codex-home")
    assert "PROVIDER_API_KEY" not in observed[0]


def test_direct_api_profile_does_not_masquerade_as_native_login(monkeypatch, tmp_path):
    monkeypatch.setattr(spawn.subprocess, "run", lambda *args, **kwargs:
                        (_ for _ in ()).throw(AssertionError("must not call")))
    spec = _spec("claude")
    spec["profile"] = spawn._DIRECT_OPUS_REVIEW_PROFILE
    assert spawn.probe_cli_login(spec, str(tmp_path)) == {
        "status": "unknown", "reason": "non_native_profile"}
