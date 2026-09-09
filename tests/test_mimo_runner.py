"""Tests for the MiMo runner's output validator ("checker")."""
from types import SimpleNamespace

from mcp_huddle import mimo_runner


def test_is_error_output_flags_provider_403() -> None:
    """`mimo run` exits 0 but prints the free-provider 403 to stdout; that text
    must be detected as an error so the runner posts nothing instead of garbage."""
    err = 'Error: mimo-free bootstrap failed: 403 {"error": {"code": "403", "type": "illegal_access"}}'
    assert mimo_runner._is_error_output(err) is True


def test_is_error_output_flags_bare_error_line() -> None:
    assert mimo_runner._is_error_output("Error: something went wrong") is True


def test_is_error_output_allows_normal_reply() -> None:
    """A real discussion reply that merely mentions 'error' or '403' is fine."""
    reply = (
        "I reviewed db.py. The 403 you saw is likely an auth header issue; "
        "add error handling around the request and retry with backoff."
    )
    assert mimo_runner._is_error_output(reply) is False
    assert mimo_runner._is_error_output("") is False


def test_call_mimo_uses_sanitized_environment(monkeypatch, tmp_path) -> None:
    captured = {}
    monkeypatch.setattr(mimo_runner, "_ISOLATED_CONFIG_DIR", str(tmp_path / "mimo-home"))
    monkeypatch.setenv("AUDIT_UNRELATED_SECRET", "must-not-pass")
    monkeypatch.setenv("GH_TOKEN", "must-not-pass")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-pass")
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path / "huddle"))

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="review result", stderr="")

    monkeypatch.setattr(mimo_runner.subprocess, "run", fake_run)
    answer, _ = mimo_runner.call_mimo("mimo", "prompt", 1)

    assert answer == "review result"
    env = captured["env"]
    assert env["MCP_HUDDLE_HOME"] == str(tmp_path / "huddle")
    assert env["MIMOCODE_DISABLE_CLAUDE_CODE_MCP"] == "1"
    assert "AUDIT_UNRELATED_SECRET" not in env
    assert "GH_TOKEN" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
