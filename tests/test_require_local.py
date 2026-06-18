"""Unit tests for the _require_local HTTP guard (loopback + optional token)."""
import pytest
from starlette.requests import Request

from mcp_huddle.server import _require_local


def _make_request(host, headers=None) -> Request:
    """Build a minimal Starlette Request with a given client host + headers."""
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/rooms_nuke",
        "headers": [(k.lower().encode(), v.encode())
                    for k, v in (headers or {}).items()],
        "client": (host, 12345) if host is not None else None,
    }
    return Request(scope)


@pytest.fixture(autouse=True)
def _clear_token(monkeypatch):
    # Default: no token configured (no behavior change vs. pre-auth server).
    monkeypatch.delenv("MCP_HUDDLE_TOKEN", raising=False)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_allowed(host):
    assert _require_local(_make_request(host)) is None


@pytest.mark.parametrize("host", ["10.0.0.5", "192.168.1.2", "8.8.8.8"])
def test_non_loopback_forbidden(host):
    resp = _require_local(_make_request(host))
    assert resp is not None
    assert resp.status_code == 403


def test_missing_client_forbidden():
    # No client info at all → treat as non-loopback, reject.
    resp = _require_local(_make_request(None))
    assert resp is not None
    assert resp.status_code == 403


def test_token_unset_no_token_required():
    # Env unset (fixture) → loopback request passes with no Authorization header.
    assert _require_local(_make_request("127.0.0.1")) is None


def test_token_required_when_env_set_and_missing(monkeypatch):
    monkeypatch.setenv("MCP_HUDDLE_TOKEN", "s3cret")
    resp = _require_local(_make_request("127.0.0.1"))
    assert resp is not None
    assert resp.status_code == 401


def test_token_required_when_env_set_and_wrong(monkeypatch):
    monkeypatch.setenv("MCP_HUDDLE_TOKEN", "s3cret")
    resp = _require_local(
        _make_request("127.0.0.1", {"Authorization": "Bearer nope"}))
    assert resp is not None
    assert resp.status_code == 401


def test_token_accepted_via_bearer(monkeypatch):
    monkeypatch.setenv("MCP_HUDDLE_TOKEN", "s3cret")
    assert _require_local(
        _make_request("127.0.0.1", {"Authorization": "Bearer s3cret"})) is None


def test_token_accepted_via_x_header(monkeypatch):
    monkeypatch.setenv("MCP_HUDDLE_TOKEN", "s3cret")
    assert _require_local(
        _make_request("127.0.0.1", {"X-Huddle-Token": "s3cret"})) is None


def test_non_loopback_rejected_before_token_check(monkeypatch):
    # Even with a valid token, a non-loopback client is forbidden (403, not 401).
    monkeypatch.setenv("MCP_HUDDLE_TOKEN", "s3cret")
    resp = _require_local(
        _make_request("10.0.0.5", {"Authorization": "Bearer s3cret"}))
    assert resp is not None
    assert resp.status_code == 403
