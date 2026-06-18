"""Tests for the MiMo runner's output validator ("checker")."""
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
