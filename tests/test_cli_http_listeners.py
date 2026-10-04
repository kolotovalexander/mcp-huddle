import asyncio
import socket
import sys
import plistlib
import subprocess
import types
from pathlib import Path

import httpx
import pytest

from mcp_huddle import __main__ as cli
from mcp_huddle import server as server_module


def test_cli_passes_both_bound_sockets_to_one_uvicorn_app(monkeypatch):
    dashboard_port, mcp_port = 18014, 45111
    class BoundSocket:
        def close(self):
            pass

    bound = [BoundSocket(), BoundSocket()]
    app = object()
    observed = {}

    def bind(host, ports):
        assert host == "127.0.0.1"
        assert ports == [dashboard_port, mcp_port]
        return bound

    class Config:
        def __init__(self, configured_app, **kwargs):
            observed["app"] = configured_app

    class Server:
        def __init__(self, config):
            pass

        def run(self, *, sockets):
            observed["sockets"] = sockets

    monkeypatch.setattr(cli, "_bind_http_sockets", bind)
    monkeypatch.setattr(server_module, "build_app", lambda: app)
    monkeypatch.setitem(sys.modules, "uvicorn", types.SimpleNamespace(Config=Config, Server=Server))
    monkeypatch.setattr(sys, "argv", ["mcp-huddle", "--http", "--port", str(dashboard_port),
                                       "--mcp-port", str(mcp_port)])
    cli.main()
    assert observed == {"app": app, "sockets": bound}


def test_shared_http_app_supports_stateful_mcp_session(monkeypatch):
    monkeypatch.setattr(server_module.mcp, "_session_manager", None)
    app = server_module.build_app()

    async def scenario():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:45111"
            ) as client:
                headers = {
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2025-03-26",
                }
                initialized = await client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-03-26", "capabilities": {},
                            "clientInfo": {"name": "dual-listener-test", "version": "1"},
                        },
                    },
                    headers=headers,
                )
                assert initialized.status_code == 200
                session_id = initialized.headers["mcp-session-id"]
                tool_list = await client.post(
                    "/mcp",
                    json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                    headers={**headers, "Mcp-Session-Id": session_id},
                )
                assert tool_list.status_code == 200
                assert "room_status" in tool_list.text

    asyncio.run(scenario())


def test_second_listener_bind_failure_closes_every_socket(monkeypatch):
    class FakeSocket:
        def __init__(self, fail=False):
            self.fail = fail
            self.closed = False

        def setsockopt(self, *args):
            pass

        def bind(self, address):
            if self.fail:
                raise OSError("occupied")

        def listen(self):
            pass

        def close(self):
            self.closed = True

    created = [FakeSocket(), FakeSocket(fail=True)]
    monkeypatch.setattr(cli.socket, "socket", lambda *args: created.pop(0))
    sockets = created
    # Keep handles before the factory consumes them.
    first, second = sockets[0], sockets[1]
    with pytest.raises(OSError, match="occupied"):
        cli._bind_http_sockets("127.0.0.1", [8014, 45111])
    assert first.closed and second.closed


def test_install_plan_preserves_custom_plist_and_prints_reversible_commands(tmp_path):
    source = tmp_path / "checkout"
    (source / "src" / "mcp_huddle").mkdir(parents=True)
    template = tmp_path / "dashboard.plist"
    bridge = tmp_path / "bridge.plist"
    candidate = tmp_path / "candidate.plist"
    backup = tmp_path / "dashboard-original.plist"
    original = {
        "Label": "com.example.dashboard",
        "ProgramArguments": ["old-server"],
        "EnvironmentVariables": {"CUSTOM_VALUE": "preserved", "PYTHONPATH": "/custom/path"},
        "KeepAlive": True,
        "WorkingDirectory": "/existing/working-dir",
    }
    template.write_bytes(plistlib.dumps(original))
    bridge.write_bytes(plistlib.dumps({"Label": "com.example.bridge"}))
    script = Path(__file__).parents[1] / "tools" / "plan_single_server.py"
    result = subprocess.run(
        [
            sys.executable, str(script), "--template", str(template),
            "--python", "/opt/venv/bin/python", "--source", str(source),
            "--output", str(candidate), "--backup", str(backup),
            "--bridge-plist", str(bridge), "--bridge-label", "com.example.bridge",
            "--bridge-was-enabled",
        ],
        check=True, capture_output=True, text=True,
    )
    rendered = plistlib.loads(candidate.read_bytes())
    assert rendered["EnvironmentVariables"] == {
        "CUSTOM_VALUE": "preserved",
        "PYTHONPATH": f"{source}/src:/custom/path",
    }
    assert rendered["KeepAlive"] is True
    assert rendered["WorkingDirectory"] == "/existing/working-dir"
    assert rendered["ProgramArguments"] == [
        "/opt/venv/bin/python", "-m", "mcp_huddle", "--http",
        "--port", "8014", "--mcp-port", "45111",
    ]
    assert candidate.stat().st_mode & 0o777 == 0o600
    assert "launchctl disable gui/" in result.stdout
    assert "launchctl bootout gui/" in result.stdout
    assert "launchctl enable gui/" in result.stdout
    assert "launchctl bootstrap gui/" in result.stdout
    assert "rollback" in result.stdout
