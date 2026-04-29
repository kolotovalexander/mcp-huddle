"""Smoke tests: server starts, key endpoints respond."""
import asyncio
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from mcp_huddle.server import build_app


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server_url() -> str:
    port = _free_port()
    config = uvicorn.Config(build_app(), host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=lambda: asyncio.run(server.serve()), daemon=True)
    thread.start()

    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                break
        except OSError:
            time.sleep(0.1)
    else:
        pytest.fail("server failed to start within 5s")

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=2)


def test_dashboard_html(server_url: str) -> None:
    r = httpx.get(f"{server_url}/dashboard")
    assert r.status_code == 200
    assert "<!DOCTYPE html>" in r.text


def test_static_css(server_url: str) -> None:
    r = httpx.get(f"{server_url}/static/dashboard.css")
    assert r.status_code == 200
    assert "lq-panel" in r.text


def test_static_js(server_url: str) -> None:
    r = httpx.get(f"{server_url}/static/dashboard.js")
    assert r.status_code == 200
    assert "renderRooms" in r.text


def test_api_rooms(server_url: str) -> None:
    r = httpx.get(f"{server_url}/api/rooms")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_mcp_initialize(server_url: str) -> None:
    r = httpx.post(
        f"{server_url}/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        },
        headers={
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-03-26",
        },
    )
    assert r.status_code == 200
    assert "mcp-session-id" in r.headers
