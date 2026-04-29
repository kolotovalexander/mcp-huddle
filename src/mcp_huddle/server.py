"""Agent Bus — FastMCP server on :8014.

Exposes MCP tools for room management, messaging, status, and consensus.
Also serves a web dashboard at http://127.0.0.1:8014/dashboard.
"""

import asyncio
import json
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse

from . import bus
from . import spawn

mcp = FastMCP("mcp-huddle")


# ── Room tools ────────────────────────────────────────────────────────────────

@mcp.tool()
def room_create(
    name: str,
    owner: str,
    owner_pid: int,
    cwd: str = "",
    session_id: str = "",
    auto_spawn: bool = False,
    goal: str = "",
) -> str:
    """Create a new discussion room. Returns room_id.

    auto_spawn=True: automatically spawns Codex + Gemini as reviewers.
    goal: short description of the discussion topic (used in join_packet for spawned agents).
    """
    room_id = bus.create_room(name, owner, owner_pid, cwd, session_id)

    if auto_spawn and cwd:
        _spawn_agents(room_id, name, goal or name, cwd, owner)

    return room_id


@mcp.tool()
def room_invite(room_id: str, agent_name: str) -> str:
    """Add an agent to an existing room."""
    bus.invite_agent(room_id, agent_name)
    return "ok"


@mcp.tool()
def room_request_close(room_id: str, agent: str) -> str:
    """Signal intent to close. Returns 'closing_requested'.
    Human must confirm by calling room_close().
    """
    return bus.request_close(room_id, agent)


@mcp.tool()
def room_close(room_id: str, owner: str) -> str:
    """Permanently close a room (owner only). Kills spawned agents."""
    bus.close_room(room_id, owner)
    return "closed"


@mcp.tool()
def room_close_session(session_id: str) -> list:
    """Close all open rooms belonging to a session (called by Stop hook)."""
    return bus.close_session_rooms(session_id)


@mcp.tool()
def room_info(room_id: str) -> dict:
    """Get room metadata (participants, status, cwd, etc.)."""
    return bus.get_room_info(room_id)


@mcp.tool()
def room_list() -> list:
    """List all rooms (open and closed)."""
    return bus.list_rooms()


# ── Message tools ─────────────────────────────────────────────────────────────

@mcp.tool()
def message_post(
    room_id: str,
    agent: str,
    body: str,
    kind: str,
    to: Optional[str] = None,
    reply_to: Optional[int] = None,
    idempotency_key: Optional[str] = None,
) -> int:
    """Post a message to a room. Returns assigned message_id.

    kind values:
      request  — question/task, expects a reply (auto-notifies addressee)
      comment  — observation, no reply expected
      ack      — "received, working on it"
      busy     — "occupied, will reply later"
      result   — delivering output (to=originator)
      final    — orchestrator's closing word, nobody replies
      system   — system/human override (highest priority)
      close    — room is closing

    Anti-loop rule (put this in your agent prompt):
      Reply ONLY to kind=request addressed to you (to=your_name or to=all).
      kind=request with reply_to!=null is someone's answer — NOT a new request to you.
      For all other kinds: read silently, do not reply.
    """
    return bus.post_message(room_id, agent, body, kind, to, reply_to, idempotency_key)


@mcp.tool()
def messages_read(room_id: str, since_id: int = 0, limit: int = 20) -> str:
    """Read chat history as plain text (token-efficient for LLMs).

    since_id: only return messages with id > since_id (delta read).
    limit: max messages to return (default 20 = fresh context window).

    Store last seen id locally and pass it on next call to avoid re-reading history.
    """
    return bus.read_messages(room_id, since_id, limit)


@mcp.tool()
def room_summarize(room_id: str, since_id: int = 0) -> str:
    """Get a digest of messages since since_id.

    Use instead of messages_read when you've been absent for a long time
    and want to catch up cheaply (fewer tokens than reading everything).
    """
    return bus.summarize_messages(room_id, since_id)


# ── Status tools ──────────────────────────────────────────────────────────────

@mcp.tool()
def status_set(
    room_id: str,
    agent: str,
    status: str,
    expires_in_sec: int = 0,
    session_id: str = "",
) -> str:
    """Set agent status. status: online|busy|done|typing.

    expires_in_sec > 0: status auto-resets to 'online' after that many seconds (lease).
    """
    bus.set_status(room_id, agent, status, expires_in_sec, session_id)
    return "ok"


@mcp.tool()
def status_get(room_id: str) -> dict:
    """Get all agent statuses in a room (expired leases auto-reset to online)."""
    return bus.get_status(room_id)


# ── Consensus tools ───────────────────────────────────────────────────────────

@mcp.tool()
def propose_resolution(room_id: str, agent: str, text: str) -> str:
    """Propose a resolution to end the discussion. Returns resolution_id.

    All participants must call resolution_vote(..., 'ack') to accept.
    Any 'reject' vote reopens discussion.
    """
    return bus.propose_resolution(room_id, agent, text)


@mcp.tool()
def resolution_vote(room_id: str, agent: str, resolution_id: str, vote: str) -> str:
    """Vote on a proposed resolution. vote: 'ack'|'reject'.

    All ack → room becomes 'resolved' (read-only for discussion).
    """
    return bus.resolution_vote(room_id, agent, resolution_id, vote)


# ── Notification tools ────────────────────────────────────────────────────────

@mcp.tool()
def notify_register(room_id: str, agent: str, notify_file_path: str) -> str:
    """Register a file path to receive notifications when kind=request arrives.

    Server writes JSON to notify_file_path when a request is addressed to you.
    Your hook script should poll /tmp/mcp-huddle-*-notify.json files.
    """
    bus.register_notify(room_id, agent, notify_file_path)
    return "ok"


# ── Background tasks ──────────────────────────────────────────────────────────

async def _background_watchdog():
    """Periodically check for zombie rooms and deadlocks."""
    while True:
        await asyncio.sleep(bus.ZOMBIE_CHECK_SECS)
        try:
            closed = bus.check_zombie_rooms()
            if closed:
                print(f"[watchdog] Zombie-closed rooms: {closed}", flush=True)
        except Exception as e:
            print(f"[watchdog] zombie check error: {e}", flush=True)

        try:
            notified = bus.check_deadlock_rooms()
            if notified:
                print(f"[watchdog] Deadlock-notified rooms: {notified}", flush=True)
        except Exception as e:
            print(f"[watchdog] deadlock check error: {e}", flush=True)


# ── Web Dashboard ─────────────────────────────────────────────────────────────

_STATIC_DIR = Path(__file__).parent / "static"


@mcp.custom_route("/dashboard", methods=["GET"])
async def dashboard_handler(request: Request):
    return FileResponse(_STATIC_DIR / "dashboard.html", media_type="text/html")


@mcp.custom_route("/static/dashboard.css", methods=["GET"])
async def dashboard_css(request: Request):
    return FileResponse(_STATIC_DIR / "dashboard.css", media_type="text/css")


@mcp.custom_route("/static/dashboard.js", methods=["GET"])
async def dashboard_js(request: Request):
    return FileResponse(_STATIC_DIR / "dashboard.js", media_type="application/javascript")


@mcp.custom_route("/api/rooms", methods=["GET"])
async def api_rooms(request: Request) -> JSONResponse:
    try:
        return JSONResponse(bus.list_rooms())
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/messages_json", methods=["GET"])
async def api_messages_json(request: Request) -> JSONResponse:
    room_id = request.query_params.get("room_id", "")
    try:
        since = int(request.query_params.get("since_id", 0))
        msgs = bus._load_messages(room_id)
        if since > 0:
            msgs = [m for m in msgs if m["id"] > since]
        room_meta = bus._read_meta(room_id)
        statuses = bus.get_status(room_id)
        return JSONResponse({"messages": msgs, "room": room_meta, "statuses": statuses})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@mcp.custom_route("/api/message_post", methods=["POST"])
async def api_message_post(request: Request) -> JSONResponse:
    data = await request.json()
    try:
        msg_id = bus.post_message(
            data["room_id"], data["agent"], data["body"], data["kind"],
            data.get("to"), data.get("reply_to"),
        )
        return JSONResponse({"id": msg_id})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@mcp.custom_route("/api/room_close", methods=["POST"])
async def api_room_close(request: Request) -> JSONResponse:
    data = await request.json()
    try:
        bus.close_room(data["room_id"], data["owner"])
        return JSONResponse({"status": "closed"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


# ── Spawn helpers ─────────────────────────────────────────────────────────────

def _spawn_agents(room_id: str, name: str, goal: str, cwd: str, owner: str) -> None:
    brief_path = f"/tmp/room-{room_id}-brief.md"
    brief = f"""# Agent Bus — Room: {name}

**Room ID:** {room_id}
**Goal:** {goal}
**Project:** {cwd}
**MCP server:** http://127.0.0.1:8014/mcp

## Your role
You are an independent reviewer, NOT an executor.
- Critique architectural decisions, point out bugs
- Propose alternatives with justification
- Ask clarifying questions using kind=request
- NEVER send "Thanks", "Agreed", "Got it" — only technical arguments
- Reply ONLY to kind=request addressed to you (to=your_name or to=all)

## Anti-loop rules
- kind=request with reply_to!=null means it's someone's answer — NOT a new request to you → stay silent
- Keep a local set of reply_to IDs you already responded to — never reply twice to the same request

## How to connect
Use MCP tools from mcp-huddle-mcp:
  messages_read("{room_id}") — read chat
  message_post("{room_id}", "YourName", "...", kind="comment"|"request"|"result", ...) — post
  status_set("{room_id}", "YourName", "busy"|"online") — update status
"""
    Path(brief_path).write_text(brief)

    names, pids = spawn.spawn_all(brief, cwd)

    for n in names:
        bus.invite_agent(room_id, n)

    # Save PIDs for zombie cleanup
    meta = bus.get_room_info(room_id)
    meta["spawned_pids"] = pids
    (bus._room_dir(room_id) / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2)
    )


# ── App build (single MCP app + custom routes + watchdog lifespan) ──────────

import contextlib


def build_app():
    """Build the Starlette app: MCP transport + custom HTTP routes + watchdog.

    All HTTP routes are registered via @mcp.custom_route(), so streamable_http_app()
    returns a Starlette app with everything wired in. Its lifespan runs the MCP
    session_manager — we wrap it to also start/stop the zombie watchdog cleanly.
    """
    app = mcp.streamable_http_app()
    mcp_lifespan = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def combined_lifespan(a):
        async with mcp_lifespan(a):
            watchdog = asyncio.create_task(_background_watchdog())
            try:
                yield
            finally:
                watchdog.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watchdog

    app.router.lifespan_context = combined_lifespan
    return app


