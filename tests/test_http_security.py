"""Integration coverage for the HTTP dashboard/MCP security boundary."""

import asyncio
from pathlib import Path

import httpx
from starlette.requests import Request

from mcp_huddle import server


BASE = "http://127.0.0.1:8014"
SAME_ORIGIN = {"Origin": BASE, "Sec-Fetch-Site": "same-origin"}


def _client(app, *, base_url=BASE, root_path="") -> httpx.AsyncClient:
    transport = httpx.ASGITransport(
        app=app, root_path=root_path, client=("127.0.0.1", 40123),
    )
    return httpx.AsyncClient(transport=transport, base_url=base_url)


def _initialize_headers(**extra):
    return {
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-03-26",
        **extra,
    }


def _initialize_body():
    return {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "security-test", "version": "1"},
        },
    }


def test_rest_origin_host_media_type_and_json_mutator(monkeypatch):
    monkeypatch.delenv("MCP_HUDDLE_TOKEN", raising=False)
    monkeypatch.setattr(server.bus, "list_rooms", lambda: [])
    calls = []
    monkeypatch.setattr(server.bus, "nuke_all_rooms", lambda: calls.append(True) or {"deleted": []})
    app = server.build_app()

    async def scenario():
        async with _client(app) as client:
            assert (await client.get("/api/rooms")).status_code == 200
            assert (await client.get("/api/rooms", headers={"Host": "evil.example"})).status_code == 403
            assert (await client.get(
                "/api/rooms", headers={"Origin": "https://evil.example"}
            )).status_code == 403
            assert (await client.get(
                "/api/rooms", headers={"Sec-Fetch-Site": "cross-site"}
            )).status_code == 403

            rejected = await client.post(
                "/api/rooms_nuke", content="{}",
                headers={"Origin": "https://evil.example", "Content-Type": "text/plain"},
            )
            assert rejected.status_code == 403
            assert calls == []

            wrong_type = await client.post(
                "/api/rooms_nuke", content="{}",
                headers={**SAME_ORIGIN, "Content-Type": "text/plain"},
            )
            assert wrong_type.status_code == 415
            assert calls == []

            accepted = await client.post("/api/rooms_nuke", json={}, headers=SAME_ORIGIN)
            assert accepted.status_code == 200
            assert calls == [True]

    asyncio.run(scenario())


def test_close_session_route_is_guarded_and_calls_bus_only_when_valid(monkeypatch):
    token = "session-hook-secret"
    monkeypatch.setenv("MCP_HUDDLE_TOKEN", token)
    calls = []
    monkeypatch.setattr(
        server.bus, "close_session_rooms",
        lambda session_id: calls.append(session_id) or ["room_one"],
    )
    app = server.build_app()

    async def scenario():
        async with _client(app) as client:
            no_auth = await client.post(
                "/api/rooms_close_session", json={"session_id": "session-a"},
            )
            assert no_auth.status_code == 401
            assert calls == []

            invalid = await client.post(
                "/api/rooms_close_session", json={"session_id": ""},
                headers={"X-Huddle-Token": token},
            )
            assert invalid.status_code == 400
            assert calls == []

            accepted = await client.post(
                "/api/rooms_close_session", json={"session_id": " session-a "},
                # The bundled SessionEnd hook is a non-browser client and
                # therefore intentionally sends no Origin/Sec-Fetch headers.
                headers={"X-Huddle-Token": token},
            )
            assert accepted.status_code == 200
            assert accepted.json() == {"closed": ["room_one"]}
            assert calls == ["session-a"]

    asyncio.run(scenario())


def test_derived_header_has_no_cookie_or_cross_port_ambient_leak(monkeypatch):
    token = "integration-secret"
    monkeypatch.setenv("MCP_HUDDLE_TOKEN", token)
    monkeypatch.setattr(server.bus, "list_rooms", lambda: [])
    app = server.build_app()

    async def scenario():
        async with _client(app) as client:
            assert (await client.get("/dashboard")).status_code == 200
            assert (await client.get("/static/dashboard.js")).status_code == 200
            assert (await client.get("/api/rooms")).status_code == 401
            assert (await client.get("/missing-route")).status_code == 401
            assert (await client.get(
                "/api/rooms", headers={"Authorization": f"Bearer {token}"}
            )).status_code == 200

            state = await client.get("/api/auth", headers=SAME_ORIGIN)
            assert state.json() == {"required": True}
            assert (await client.post(
                "/api/auth", json={"token": "wrong"}, headers=SAME_ORIGIN
            )).status_code == 401
            assert (await client.post(
                "/api/auth", content=b'{"token":"\\ud800"}',
                headers={**SAME_ORIGIN, "Content-Type": "application/json"},
            )).status_code == 401

            logged_in = await client.post("/api/auth", json={"token": token}, headers=SAME_ORIGIN)
            assert logged_in.status_code == 200
            data = logged_in.json()
            credential = data["credential"]
            assert data["required"] is True
            assert token not in credential
            assert "set-cookie" not in logged_in.headers
            assert len(client.cookies) == 0

            assert (await client.get("/api/rooms", headers=SAME_ORIGIN)).status_code == 401
            client.cookies.set("mcp_huddle_auth", credential)
            assert (await client.get("/api/rooms", headers=SAME_ORIGIN)).status_code == 401
            client.cookies.clear()
            assert (await client.get(
                "/api/rooms",
                headers={**SAME_ORIGIN, "X-Huddle-Credential": credential},
            )).status_code == 200

        async with _client(app, base_url="http://127.0.0.1:8015") as other_port:
            assert len(other_port.cookies) == 0
            assert (await other_port.get("/api/rooms")).status_code == 401

    asyncio.run(scenario())


def test_raw_header_authenticates_real_mcp_initialize(monkeypatch):
    token = "integration-secret"
    monkeypatch.setenv("MCP_HUDDLE_TOKEN", token)
    monkeypatch.setattr(server.mcp, "_session_manager", None)

    async def quiet_watchdog():
        while True:
            await asyncio.sleep(3600)

    # This test validates HTTP/MCP transport, not the process reaper. Keeping
    # the watchdog inert prevents it observing another test's reloaded BUS_DIR.
    monkeypatch.setattr(server, "_background_watchdog", quiet_watchdog)
    app = server.build_app()

    async def scenario():
        async with app.router.lifespan_context(app):
            async with _client(app) as client:
                no_auth = await client.post(
                    "/mcp", json=_initialize_body(), headers=_initialize_headers())
                assert no_auth.status_code == 401
                authenticated = await client.post(
                    "/mcp", json=_initialize_body(),
                    headers=_initialize_headers(**{"X-Huddle-Token": token}),
                )
                assert authenticated.status_code == 200
                assert "mcp-session-id" in authenticated.headers

    asyncio.run(scenario())


def test_root_path_public_classification_is_narrow(monkeypatch):
    monkeypatch.setenv("MCP_HUDDLE_TOKEN", "secret")
    monkeypatch.setattr(server.bus, "list_rooms", lambda: [])
    app = server.build_app()

    async def scenario():
        async with _client(
            app, base_url=f"{BASE}/prefix", root_path="/prefix",
        ) as client:
            assert (await client.get("/dashboard")).status_code == 200
            assert (await client.get("/static/dashboard.js")).status_code == 200
            assert (await client.get("/api/auth")).status_code == 200
            assert (await client.post(
                "/api/auth", content="{}", headers={"Content-Type": "text/plain"},
            )).status_code == 415
            assert (await client.get("/api/rooms")).status_code == 401
            assert (await client.get("/anything-else")).status_code == 401

    asyncio.run(scenario())


def test_dashboard_keeps_credential_in_memory_and_fetches_sse():
    source = (Path(server.__file__).parent / "static" / "dashboard.js").read_text()
    assert "X-Huddle-Credential" in source
    assert "credentials: 'omit'" in source
    assert "new EventSource" not in source
    assert "new AbortController" in source
    assert "parseSSEText" in source
    assert "Enter token" in source
    assert "localStorage.setItem('MCP_HUDDLE_TOKEN'" not in source
    assert "sessionStorage" not in source


def test_public_path_normalization_rejects_ambiguous_root_path():
    assert server._public_route_path({
        "path": "/prefix/dashboard", "root_path": "/prefix",
    }) == "/dashboard"
    assert server._public_route_path({
        "path": "/../dashboard", "root_path": "/..",
    }) is None
    assert not server._is_public_http_request({
        "path": "/prefix/api/rooms", "root_path": "/prefix", "method": "GET",
    })


def test_malformed_host_origin_and_credential_fail_closed(monkeypatch):
    monkeypatch.setenv("MCP_HUDDLE_TOKEN", "secret")
    app = server.build_app()
    huge_port = "9" * 5000

    async def scenario():
        async with _client(app) as client:
            bad_host = await client.get("/api/rooms", headers={"Host": f"localhost:{huge_port}"})
            assert bad_host.status_code == 403
            bad_origin = await client.get(
                "/api/rooms", headers={"Origin": f"http://localhost:{huge_port}"})
            assert bad_origin.status_code == 403
            bad_credential = await client.get(
                "/api/rooms", headers=[(b"X-Huddle-Credential", b"\xff")])
            assert bad_credential.status_code == 401

    asyncio.run(scenario())


def test_json_body_size_cap_prevents_mutator(monkeypatch):
    monkeypatch.delenv("MCP_HUDDLE_TOKEN", raising=False)
    calls = []
    monkeypatch.setattr(server.bus, "nuke_all_rooms", lambda: calls.append(True) or {})
    app = server.build_app()

    async def scenario():
        async with _client(app) as client:
            response = await client.post(
                "/api/rooms_nuke",
                content=b'{' + b' ' * (server._MAX_HTTP_JSON_BYTES + 1) + b'}',
                headers={**SAME_ORIGIN, "Content-Type": "application/json"},
            )
            assert response.status_code == 413
            assert calls == []

    asyncio.run(scenario())


def test_agent_event_stream_rejects_symlink_escape_and_open_race(
    monkeypatch, tmp_path: Path,
):
    monkeypatch.delenv("MCP_HUDDLE_TOKEN", raising=False)
    rooms_root = server.bus.BUS_DIR
    rooms_root.mkdir(parents=True)
    outside_file = tmp_path / "outside-events.jsonl"
    secret = "EXTERNAL-SSE-SECRET"
    outside_file.write_text(secret + "\n")

    final_room = rooms_root / "room_final_link"
    (final_room / "agents").mkdir(parents=True)
    (final_room / "agents" / "codex.events.jsonl").symlink_to(outside_file)

    parent_room = rooms_root / "room_parent_link"
    parent_room.mkdir()
    outside_agents = tmp_path / "outside-agents"
    outside_agents.mkdir()
    (outside_agents / "codex.events.jsonl").write_text(secret + "-PARENT\n")
    (parent_room / "agents").symlink_to(outside_agents, target_is_directory=True)

    directory_room = rooms_root / "room_directory_log"
    (directory_room / "agents" / "codex.events.jsonl").mkdir(parents=True)

    hardlink_room = rooms_root / "room_hardlink_log"
    (hardlink_room / "agents").mkdir(parents=True)
    (hardlink_room / "agents" / "codex.events.jsonl").hardlink_to(outside_file)

    race_room = rooms_root / "room_open_race"
    (race_room / "agents").mkdir(parents=True)
    race_log = race_room / "agents" / "codex.events.jsonl"
    race_log.write_text('{"safe": true}\n')
    real_agent_paths = server.bus._agent_paths
    swapped = False

    def swap_after_validation(room_id, agent_name, *, create=False):
        nonlocal swapped
        result = real_agent_paths(room_id, agent_name, create=create)
        if room_id == race_room.name and not swapped:
            swapped = True
            race_log.unlink()
            race_log.symlink_to(outside_file)
        return result

    monkeypatch.setattr(server.bus, "_agent_paths", swap_after_validation)
    app = server.build_app()

    async def scenario():
        async with _client(app) as client:
            final = await client.get(f"/agents/{final_room.name}/Codex/events")
            assert final.status_code == 400
            assert secret not in final.text

            parent = await client.get(f"/agents/{parent_room.name}/Codex/events")
            assert parent.status_code == 400
            assert secret not in parent.text

            directory = await client.get(f"/agents/{directory_room.name}/Codex/events")
            assert directory.status_code == 200
            assert "event log unavailable" in directory.text
            assert secret not in directory.text

            hardlink = await client.get(f"/agents/{hardlink_room.name}/Codex/events")
            assert hardlink.status_code == 200
            assert "event log unavailable" in hardlink.text
            assert secret not in hardlink.text

            raced = await client.get(f"/agents/{race_room.name}/Codex/events")
            assert raced.status_code == 200
            assert "event log unavailable" in raced.text
            assert secret not in raced.text

    asyncio.run(scenario())


def test_agent_event_stream_offset_resumes_at_next_complete_line(monkeypatch):
    monkeypatch.delenv("MCP_HUDDLE_TOKEN", raising=False)
    room_id = "room_offset_resume"
    agents = server.bus.BUS_DIR / room_id / "agents"
    agents.mkdir(parents=True)
    first = b'{"n":1}\n'
    second = b'{"n":2}\n'
    log_path = agents / "codex.events.jsonl"
    log_path.write_bytes(first + second)

    async def make_request(offset, generation="", cursor=""):
        async def receive():
            await asyncio.sleep(3600)

        scope = {
            "type": "http", "method": "GET", "scheme": "http",
            "path": f"/agents/{room_id}/Codex/events",
            "query_string": (
                f"offset={offset}&generation={generation}&cursor={cursor}"
            ).encode(),
            "headers": [(b"host", b"127.0.0.1:8014")],
            "client": ("127.0.0.1", 40123), "server": ("127.0.0.1", 8014),
            "path_params": {"room_id": room_id, "agent_name": "Codex"},
        }
        return await server.api_agent_events(Request(scope, receive))

    async def scenario():
        response = await make_request(0)
        iterator = response.body_iterator
        opened = await iterator.__anext__()
        line_one = await iterator.__anext__()
        line_two = await iterator.__anext__()
        await iterator.aclose()
        assert f"id: 0\n" in opened
        generation = opened.split("generation: ", 1)[1].split("\n", 1)[0]
        first_cursor = line_one.split("cursor: ", 1)[1].split("\n", 1)[0]
        second_cursor = line_two.split("cursor: ", 1)[1].split("\n", 1)[0]
        assert len(generation) == 64
        assert f"id: {len(first)}\n" in line_one
        assert f"id: {len(first) + len(second)}\n" in line_two

        resumed = await make_request(len(first), generation, first_cursor)
        iterator = resumed.body_iterator
        opened = await iterator.__anext__()
        line_two = await iterator.__anext__()
        await iterator.aclose()
        assert f"id: {len(first)}\n" in opened
        assert '{"n":1}' not in line_two
        assert '{"n":2}' in line_two

        same_inode_line = b"SAME_INODE_FIRST_EVENT\n"
        original_inode = log_path.stat().st_ino
        log_path.write_bytes(same_inode_line)
        assert log_path.stat().st_ino == original_inode
        rewritten = await make_request(
            len(first) + len(second), generation, second_cursor)
        iterator = rewritten.body_iterator
        rewritten_open = await iterator.__anext__()
        rewritten_line = await iterator.__anext__()
        await iterator.aclose()
        assert "id: 0\n" in rewritten_open
        assert "SAME_INODE_FIRST_EVENT" in rewritten_line
        rewritten_generation = rewritten_open.split(
            "generation: ", 1)[1].split("\n", 1)[0]
        rewritten_cursor = rewritten_line.split("cursor: ", 1)[1].split("\n", 1)[0]

        replacement_line = b"NEW_LOG_FIRST_EVENT\n"
        replacement = agents / "replacement.tmp"
        replacement.write_bytes(replacement_line)
        replacement.replace(log_path)
        rotated = await make_request(
            len(same_inode_line), rewritten_generation, rewritten_cursor)
        iterator = rotated.body_iterator
        rotated_open = await iterator.__anext__()
        rotated_line = await iterator.__anext__()
        await iterator.aclose()
        new_generation = rotated_open.split("generation: ", 1)[1].split("\n", 1)[0]
        assert new_generation != generation
        assert "id: 0\n" in rotated_open
        assert "NEW_LOG_FIRST_EVENT" in rotated_line
        assert f"id: {len(replacement_line)}\n" in rotated_line

    asyncio.run(scenario())


def test_agent_event_stream_caps_unterminated_line(monkeypatch):
    monkeypatch.delenv("MCP_HUDDLE_TOKEN", raising=False)
    room_id = "room_oversize_event"
    agents = server.bus.BUS_DIR / room_id / "agents"
    agents.mkdir(parents=True)
    (agents / "codex.events.jsonl").write_bytes(
        b"x" * (server._MAX_AGENT_EVENT_LINE_BYTES + 1))

    async def receive():
        await asyncio.sleep(3600)

    scope = {
        "type": "http", "method": "GET", "scheme": "http",
        "path": f"/agents/{room_id}/Codex/events", "query_string": b"offset=0",
        "headers": [(b"host", b"127.0.0.1:8014")],
        "client": ("127.0.0.1", 40123), "server": ("127.0.0.1", 8014),
        "path_params": {"room_id": room_id, "agent_name": "Codex"},
    }

    async def scenario():
        response = await server.api_agent_events(Request(scope, receive))
        iterator = response.body_iterator
        opened = await asyncio.wait_for(iterator.__anext__(), timeout=1)
        rejected = await asyncio.wait_for(iterator.__anext__(), timeout=1)
        await iterator.aclose()
        assert "event: open" in opened
        assert "event log line too large" in rejected
        assert len(rejected) < 128

    asyncio.run(scenario())
