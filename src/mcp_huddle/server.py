"""mcp-huddle — FastMCP server. Persistent multi-agent chat rooms.

Stdio mode (default): JSON-RPC over stdin/stdout for MCP clients.
HTTP mode (`--http`): uvicorn + Liquid Glass dashboard on :8014.
"""

import asyncio
import json
import time
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, StreamingResponse

from . import bus
from . import spawn

# Shown to LLM clients in the `initialize` response. Keep tight — every agent
# session sees this verbatim. Goal: stop one-shot misuse, enforce anti-loop.
_AGENT_INSTRUCTIONS = """\
Persistent multi-agent chat rooms. Use to coordinate decisions ACROSS multiple
agents (e.g. Claude + Codex + Gemini reviewing the same architectural choice).

WHEN TO USE A ROOM (vs. answering directly or calling a one-shot advisor):
- multi-step design / architecture decision with real trade-offs
- code review needing clarifying questions back-and-forth
- multi-file refactor where you want independent perspectives
- consensus required (will use propose_resolution + resolution_vote)

WHEN NOT TO USE A ROOM:
- single factual lookup (just answer)
- single-shot critique (use codex exec / gemini -p instead)
- you already have enough context to act

INVITING OTHER AGENTS:
1. `room_create(name, owner=YourAgentName, owner_pid=PID, cwd=PROJECT,
   session_id=SESSION, auto_spawn=True, goal="<short description>")`
   spawns Codex + Gemini automatically if those CLIs are on PATH.
2. If auto_spawn isn't available (binaries missing or you want a different
   roster), shell out yourself with the room_id + brief, e.g.
   `codex exec --dangerously-bypass-approvals-and-sandbox "Join huddle room
   <ROOM_ID>: <task>. Read messages_read first, then post."` and same for
   `gemini -y -p "..."`. Then call `room_invite(room_id, "Codex")` /
   `room_invite(room_id, "Gemini")` so they appear in participants.
3. Prefer auto_spawn unless you specifically need a non-default agent.

ANTI-LOOP PROTOCOL (CRITICAL — without this rooms turn into infinite chat):
- Reply ONLY to `kind=request` addressed to you (`to=YourName` or `to=all`).
- NEVER reply to a `kind=request` that has `reply_to` set — that message is
  already someone's answer, not a new task for you.
- For `kind` in (`comment`, `ack`, `busy`, `result`, `final`, `system`,
  `close`): READ ONLY, do not respond.
- Track `reply_to` IDs you already answered locally; never answer twice.
- The server has a circuit breaker that hard-blocks >5 messages-in-a-row from
  the same agent without new requests — if you hit it, you're in a loop.

DELTA READS (token efficiency):
- Store last message id you saw; call `messages_read(room_id,
  since_id=last_seen)` next turn — only new messages.
- After a long absence, prefer `room_summarize(room_id, since_id=last_seen)`.

KIND ENUM (vital — wrong kind breaks anti-loop):
- `request`: a question/task expecting a reply (auto-notifies addressee)
- `comment`: observation, no reply expected
- `ack`: "received, working on it"
- `busy`: "occupied, will reply later"
- `result`: delivering output (set `to` = originator)
- `final`: orchestrator's closing word, nobody replies
- `system`: highest priority, agent="Human" only (or override)
- `close`: room is closing

CONSENSUS:
- After agents converge, anyone calls `propose_resolution(room_id, agent,
  text)` → all participants vote `ack`/`reject` via `resolution_vote(...)`.
- All-ack → room becomes `resolved` (read-only for normal messages).

CLOSE PROTOCOL (never close silently — humans want a heads-up):
1. `room_request_close(room_id, agent)` — sets status to `closing_requested`.
2. ASK THE USER explicitly: "Закрываем чат '<name>'?" (or in their language).
3. On user yes → `room_close(room_id, owner)` — only the owner can do this.

STORAGE: ~/.mcp-huddle/rooms/{room_id}/ (JSONL + meta.json, file-locked,
shared across all agents on this machine).

DASHBOARD: run `mcp-huddle --http` separately to watch rooms in browser
at http://127.0.0.1:8014/dashboard. Humans can post `kind=system` messages
that bypass anti-loop rules.
"""

mcp = FastMCP("mcp-huddle", instructions=_AGENT_INSTRUCTIONS)


# ── Room tools ────────────────────────────────────────────────────────────────

@mcp.tool()
def room_create(
    name: str,
    owner: str,
    owner_pid: int,
    cwd: str = "",
    session_id: str = "",
    auto_spawn: bool | dict[str, str] = False,
    goal: str = "",
) -> str:
    """Create a new discussion room. Returns room_id.

    auto_spawn:
      False (default) — no agents spawned; you invite manually via room_invite.
      True            — spawn every enabled agent in the registry (Codex + Gemini)
                        with a default reviewer brief built from `goal`.
      {Name: brief}   — spawn only these agents, each with its own custom brief.
                        Example: {"Codex": "Audit auth.py for security holes",
                                  "Gemini": "Find race conditions in db.py"}.
                        Agents not in the dict are skipped even if enabled.

    goal: short description of the discussion topic (used in default brief
          for auto_spawn=True; ignored when auto_spawn is a dict).

    With Phase 1 changes: each spawned agent's stdout/stderr is captured to
    ~/.mcp-huddle/rooms/<id>/agents/<name>.events.jsonl (Codex --json /
    Gemini stream-json). Live-stream them via SSE at /agents/<id>/<name>/events.
    """
    room_id = bus.create_room(name, owner, owner_pid, cwd, session_id)

    if auto_spawn and cwd:
        _spawn_agents(room_id, name, goal or name, cwd, owner, auto_spawn)

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
def room_delete(room_id: str, owner: str) -> str:
    """Permanently remove a closed room from disk (history wipe).

    Safety: only allowed on rooms with status == 'closed'. Open or
    closing_requested rooms must be closed first via room_close().

    Side effects: deletes the entire ~/.mcp-huddle/rooms/<room_id>/ directory,
    including messages.jsonl, meta.json, agent logs. Cannot be undone.
    """
    bus.delete_room(room_id, owner)
    return "deleted"


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
    meta: Optional[dict] = None,
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
    msg_id = bus.post_message(room_id, agent, body, kind, to, reply_to, idempotency_key, msg_meta=meta)
    if kind == "request":
        _wake_agents_for_request(room_id, agent, body, to, reply_to, msg_id)
    return msg_id


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


@mcp.tool()
def respond_via_agent(
    room_id: str,
    agent_name: str,
    prompt: str,
    post_as_message: bool = True,
) -> dict:
    """Phase 2: trigger a spawned agent to respond using `codex exec resume`
    (no new process startup, retains conversation context from prior turns).

    Useful when you want to ask Codex/Gemini a follow-up in an existing room
    without manually spawning them again. The agent's thread_id was captured
    on initial spawn.

    Args:
      room_id: target room
      agent_name: which spawned agent to invoke (currently only Codex supports
                  UUID-based resume; Gemini falls back to fresh spawn with
                  prompt-prepended context summary).
      prompt: the new message to send to the agent
      post_as_message: if True, after the agent finishes, post its last_message
                       to the room as kind=result.

    Returns: {"pid": int, "thread_id": str, "log_path": str, "agent": str}.
    """
    meta = bus._read_meta(room_id)
    agent_meta = meta.setdefault("agent_meta", {})
    info = agent_meta.get(agent_name)
    if not info and agent_name not in meta.get("participants", []):
        raise ValueError(f"Agent {agent_name} not in room {room_id} (not invited or auto_spawn'd?)")
    if not info:
        info = {}
        agent_meta[agent_name] = info

    if agent_name == "Codex":
        thread_id = info.get("thread_id")
        if not thread_id:
            raise ValueError(
                f"No thread_id captured for Codex in room {room_id} — "
                "spawn may have failed or thread.started event was missed."
            )
        log_path = info["log_path"]
        last_msg_path = info.get("last_message_path")
        cwd = meta.get("cwd", "") or ""
        pid = spawn.codex_resume(thread_id, prompt, cwd, log_path, last_msg_path)
        return {
            "pid": pid,
            "thread_id": thread_id,
            "log_path": log_path,
            "agent": agent_name,
            "post_as_message": post_as_message,
            "note": "Codex resume triggered. Tail log for events; post_as_message scheduling TBD."
        }

    # Gemini and others — fresh spawn with context-prepended prompt as fallback.
    # UUID-based resume is not available for Gemini, but a fresh CLI process can
    # still read the full huddle transcript and post a grounded reply. This keeps
    # follow-up turns working for all registry-backed agents instead of silently
    # degrading to Codex-only rooms.
    transcript = bus.read_messages(room_id, since_id=0, limit=50)
    full_prompt = _build_fresh_agent_prompt(room_id, agent_name, prompt, transcript)
    pid, log_path, last_msg_path = _spawn_fresh_room_agent(
        room_id,
        agent_name,
        full_prompt,
        meta,
        msg_id=None,
    )
    return {
        "pid": pid,
        "thread_id": "",
        "log_path": log_path,
        "last_message_path": last_msg_path,
        "agent": agent_name,
        "post_as_message": post_as_message,
        "note": (
            f"{agent_name} has no UUID resume; spawned a fresh registry-backed "
            "turn with the room transcript prepended."
        ),
    }


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

        try:
            wakes = _wake_pending_agents()
            if wakes:
                print(f"[watchdog] Agent wake-ups: {wakes}", flush=True)
        except Exception as e:
            print(f"[watchdog] agent wake-up error: {e}", flush=True)


# ── Web Dashboard ─────────────────────────────────────────────────────────────

_STATIC_DIR = Path(__file__).parent / "static"


_NO_CACHE_HDRS = {"Cache-Control": "no-cache, no-store, must-revalidate",
                  "Pragma": "no-cache", "Expires": "0"}


@mcp.custom_route("/dashboard", methods=["GET"])
async def dashboard_handler(request: Request):
    return FileResponse(_STATIC_DIR / "dashboard.html",
                        media_type="text/html", headers=_NO_CACHE_HDRS)


@mcp.custom_route("/static/dashboard.css", methods=["GET"])
async def dashboard_css(request: Request):
    return FileResponse(_STATIC_DIR / "dashboard.css",
                        media_type="text/css", headers=_NO_CACHE_HDRS)


@mcp.custom_route("/static/dashboard.js", methods=["GET"])
async def dashboard_js(request: Request):
    return FileResponse(_STATIC_DIR / "dashboard.js",
                        media_type="application/javascript", headers=_NO_CACHE_HDRS)


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
            data.get("to"), data.get("reply_to"), data.get("idempotency_key"),
            msg_meta=data.get("meta"),
        )
        if data["kind"] == "request":
            _wake_agents_for_request(
                data["room_id"],
                data["agent"],
                data["body"],
                data.get("to"),
                data.get("reply_to"),
                msg_id,
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


@mcp.custom_route("/api/room_delete", methods=["POST"])
async def api_room_delete(request: Request) -> JSONResponse:
    """Wipe a closed room from disk. Backed by bus.delete_room() — only works
    on status='closed' rooms (raises ValueError otherwise)."""
    data = await request.json()
    try:
        bus.delete_room(data["room_id"], data["owner"])
        return JSONResponse({"status": "deleted"})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@mcp.custom_route("/api/rooms_close_all", methods=["POST"])
async def api_rooms_close_all(request: Request) -> JSONResponse:
    """Bulk close: every non-closed room. Kills alive spawned PIDs only,
    excluding owner PIDs of any room. Dead PIDs skipped."""
    try:
        return JSONResponse(bus.close_all_rooms())
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/rooms_delete_closed", methods=["POST"])
async def api_rooms_delete_closed(request: Request) -> JSONResponse:
    """Wipe every room with status=closed from disk. Open rooms untouched."""
    try:
        return JSONResponse(bus.delete_closed_rooms())
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/rooms_nuke", methods=["POST"])
async def api_rooms_nuke(request: Request) -> JSONResponse:
    """Hard reset: close all + delete all. Owner PIDs preserved."""
    try:
        return JSONResponse(bus.nuke_all_rooms())
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Agent live event streaming (Phase 1) ──────────────────────────────────────

@mcp.custom_route("/api/room_agents", methods=["GET"])
async def api_room_agents(request: Request) -> JSONResponse:
    """List spawned agents for a room (name + log_path + last_message_path)."""
    room_id = request.query_params.get("room_id", "")
    try:
        meta = bus._read_meta(room_id)
        return JSONResponse({"agents": meta.get("agent_meta", {})})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@mcp.custom_route("/agents/{room_id}/{agent_name}/events", methods=["GET"])
async def api_agent_events(request: Request) -> StreamingResponse:
    """Server-Sent Events stream of an agent's stdout (Codex --json /
    Gemini stream-json events).

    Tails ~/.mcp-huddle/rooms/<room_id>/agents/<name>.events.jsonl.
    Each line in the file becomes one SSE `data:` event.
    Closes when the file is gone (room deleted) or client disconnects.
    """
    room_id = request.path_params["room_id"]
    agent_name = request.path_params["agent_name"].lower()
    log_path = bus._room_dir(room_id) / "agents" / f"{agent_name}.events.jsonl"

    async def event_stream():
        import asyncio
        # Wait briefly for the log file to exist (spawn race).
        for _ in range(20):
            if log_path.exists():
                break
            await asyncio.sleep(0.1)
        if not log_path.exists():
            yield f"event: error\ndata: log file not found at {log_path}\n\n"
            return

        with open(log_path, "rb") as f:
            # Send a marker so the client knows the stream is alive.
            yield "event: open\ndata: streaming\n\n"
            buf = b""
            while True:
                if await request.is_disconnected():
                    break
                chunk = f.read(4096)
                if chunk:
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        if line.strip():
                            text = line.decode("utf-8", errors="replace")
                            # SSE: replace internal newlines (shouldn't be any in JSONL)
                            text = text.replace("\n", "\\n")
                            yield f"data: {text}\n\n"
                else:
                    # No new data — tail-follow with short sleep.
                    if not log_path.exists():
                        break
                    await asyncio.sleep(0.5)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── Spawn helpers ─────────────────────────────────────────────────────────────

def _spawn_agents(
    room_id: str,
    name: str,
    goal: str,
    cwd: str,
    owner: str,
    auto_spawn: bool | dict[str, str],
) -> None:
    """Spawn helper agents into a room.

    auto_spawn:
      True          — spawn every enabled agent with a default reviewer brief.
      dict          — spawn only listed agents; each gets its custom brief.

    Side effects:
      * Builds a default brief and writes it to /tmp/room-<id>-brief.md so users
        can `cat` it for debugging (always written, also when dict is used).
      * Creates ~/.mcp-huddle/rooms/<id>/agents/ for log files.
      * Updates meta.json: spawned_pids + agent_meta {name: {log_path, last_message_path}}.
      * Adds each spawned agent to participants.
    """
    default_brief = _build_default_brief(room_id, name, goal, cwd)
    brief_path = f"/tmp/room-{room_id}-brief.md"
    Path(brief_path).write_text(default_brief)

    log_dir = bus._room_dir(room_id) / "agents"

    briefs_arg: dict[str, str] | None = None
    if isinstance(auto_spawn, dict):
        # Filter to enabled agents in the registry, but only those listed.
        # spawn_all consults the registry; we pass per-agent briefs and a sentinel
        # default to avoid spawning unlisted agents.
        briefs_arg = dict(auto_spawn)
        # Override registry filtering: only spawn agents named in the dict.
        # Easiest path — patch via env var at call time would be invasive;
        # instead we do post-filter inside spawn_all by passing a marker brief
        # that the spec ignores. Cleaner: temporarily disable specs not listed.
        # Since spawn.load_registry() returns a fresh list, we can mutate safely.
        registry = spawn.load_registry()
        for spec in registry:
            if spec["name"] not in auto_spawn:
                spec["enabled"] = False
        # spawn_all reads via load_registry() again — pass our filtered version
        # by temporarily patching the env. Simpler: call spawn_agent per spec.
        names: list[str] = []
        pids: list[int] = []
        agent_meta: dict[str, dict] = {}
        for spec in registry:
            if not spec.get("enabled"):
                continue
            agent_brief = briefs_arg[spec["name"]]
            try:
                pid, log_path, last_msg = spawn.spawn_agent(spec, agent_brief, cwd, log_dir)
                pids.append(pid)
                names.append(spec["name"])
                agent_meta[spec["name"]] = {
                    "log_path": log_path,
                    "last_message_path": last_msg,
                }
            except (FileNotFoundError, PermissionError) as exc:
                spawn.log_spawn_failure(spec, agent_brief, cwd, log_dir, exc)
            except spawn.AgentSpawnError:
                pass
            except OSError as exc:
                spawn.log_spawn_failure(spec, agent_brief, cwd, log_dir, exc)
                raise
    else:
        names, pids, agent_meta = spawn.spawn_all(default_brief, cwd, log_dir)

    for n in names:
        bus.invite_agent(room_id, n)

    # Phase 2: capture Codex thread_id from "thread.started" event in log,
    # so we can do `codex exec resume <id>` for follow-ups instead of spawning fresh.
    # Run blocking parse in a thread to avoid stalling room_create — but small
    # timeout so it usually returns within ~1s.
    for agent_name, info in agent_meta.items():
        if agent_name != "Codex":
            continue  # Only Codex has UUID-based resume; Gemini's --resume is index-based.
        log_path = info.get("log_path")
        if log_path:
            tid = spawn.parse_codex_thread_id(log_path, timeout=10.0)
            if tid:
                info["thread_id"] = tid

    # Save PIDs + log paths for dashboard / zombie cleanup
    meta = bus.get_room_info(room_id)
    meta["spawned_pids"] = pids
    meta["agent_meta"] = agent_meta
    (bus._room_dir(room_id) / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2)
    )


def _wake_agents_for_request(
    room_id: str,
    sender: str,
    body: str,
    to: Optional[str],
    reply_to: Optional[int],
    msg_id: int,
) -> list[dict]:
    """Wake persistent room agents for a newly posted request.

    Codex is resumed through its captured thread_id, preserving one logical
    Codex session per room. Requests with reply_to are treated as answers, not
    new tasks, per the anti-loop contract.
    """
    if reply_to is not None:
        return []

    meta = bus.get_room_info(room_id)
    agent_meta = meta.get("agent_meta", {})
    statuses = bus.get_status(room_id)
    wakes: list[dict] = []
    changed = False

    for agent_name, info in agent_meta.items():
        if agent_name == sender:
            continue
        if to and to not in (agent_name, "all"):
            continue

        if statuses.get(agent_name) == "busy":
            continue

        last_wake = int(info.get("last_wake_msg_id", 0) or 0)
        if last_wake >= msg_id:
            continue
        if _agent_replied_to_request(room_id, agent_name, msg_id):
            info["last_wake_msg_id"] = msg_id
            info["last_seen_id"] = max(int(info.get("last_seen_id", 0) or 0), msg_id)
            changed = True
            continue

        log_path = info.get("log_path")
        last_seen = int(info.get("last_seen_id", 0) or 0)

        if agent_name == "Codex":
            thread_id = info.get("thread_id")
            if not log_path:
                continue
            if not thread_id:
                thread_id = spawn.parse_codex_thread_id(log_path, timeout=1.0)
                if not thread_id:
                    continue
                info["thread_id"] = thread_id
                changed = True
            if not spawn.codex_log_has_completed_turn(log_path):
                continue

            prompt = _build_codex_wakeup_prompt(room_id, sender, body, to, msg_id, last_seen)
            bus.set_status(room_id, agent_name, "busy", 300, meta.get("session_id", ""))
            pid = spawn.codex_resume(
                thread_id,
                prompt,
                meta.get("cwd", "") or "",
                log_path,
                info.get("last_message_path"),
            )
            info["last_wake_msg_id"] = msg_id
            info["last_seen_id"] = msg_id
            info["last_wake_pid"] = pid
            info["last_wake_at"] = int(time.time())
            wakes.append({"agent": agent_name, "pid": pid, "thread_id": thread_id})
            changed = True
            continue

        transcript = bus.read_messages(room_id, since_id=0, limit=50)
        prompt = _build_registry_agent_wakeup_prompt(
            room_id,
            agent_name,
            sender,
            body,
            to,
            msg_id,
            last_seen,
            transcript,
        )
        if changed:
            meta["agent_meta"] = agent_meta
            bus._write_json(bus._room_dir(room_id) / "meta.json", meta)
            changed = False
        try:
            pid, _, _ = _spawn_fresh_room_agent(
                room_id,
                agent_name,
                prompt,
                meta,
                msg_id=msg_id,
            )
        except ValueError:
            continue
        wakes.append({"agent": agent_name, "pid": pid, "thread_id": ""})
        meta = bus.get_room_info(room_id)
        agent_meta = meta.get("agent_meta", {})
        changed = False

    if changed:
        meta["agent_meta"] = agent_meta
        bus._write_json(bus._room_dir(room_id) / "meta.json", meta)

    return wakes


def _agent_replied_to_request(room_id: str, agent_name: str, msg_id: int) -> bool:
    """Return True if this agent already posted a visible reply to a request."""
    for msg in bus._load_messages(room_id):
        if msg.get("agent") == agent_name and msg.get("reply_to") == msg_id:
            return True
    return False


def _wake_pending_agents() -> list[dict]:
    """Retry wake-ups for open requests that arrived before an agent was ready."""
    wakes: list[dict] = []
    for meta in bus.list_rooms():
        if meta.get("status") != "open":
            continue
        room_id = meta["id"]
        agent_meta = meta.get("agent_meta", {})
        if not agent_meta:
            continue
        statuses = bus.get_status(room_id)
        for agent_name, info in agent_meta.items():
            if statuses.get(agent_name) == "busy":
                continue
            last_wake = int(info.get("last_wake_msg_id", 0) or 0)
            for msg in bus._load_messages(room_id):
                if msg.get("id", 0) <= last_wake:
                    continue
                if msg.get("kind") != "request" or msg.get("reply_to") is not None:
                    continue
                sender = msg.get("agent", "")
                if sender == agent_name:
                    continue
                to = msg.get("to")
                if to and to not in (agent_name, "all"):
                    continue
                wakes.extend(_wake_agents_for_request(
                    room_id,
                    sender,
                    msg.get("body", ""),
                    to,
                    None,
                    msg["id"],
                ))
                break
    return wakes


def _spawn_fresh_room_agent(
    room_id: str,
    agent_name: str,
    prompt: str,
    meta: dict,
    msg_id: Optional[int] = None,
) -> tuple[int, str, str | None]:
    """Spawn a registry-backed one-shot turn for an agent without UUID resume."""
    spec = spawn.get_enabled_spec(agent_name)
    if not spec:
        raise ValueError(f"Agent {agent_name} has no enabled spawn registry entry")

    if agent_name not in meta.get("participants", []):
        bus.invite_agent(room_id, agent_name)
        meta = bus.get_room_info(room_id)

    bus.set_status(room_id, agent_name, "busy", 300, meta.get("session_id", ""))
    pid, log_path, last_msg_path = spawn.spawn_agent(
        spec,
        prompt,
        meta.get("cwd", "") or "",
        bus._room_dir(room_id) / "agents",
    )

    updated = bus.get_room_info(room_id)
    agent_meta = updated.setdefault("agent_meta", {})
    info = dict(agent_meta.get(agent_name) or {})
    info["log_path"] = log_path
    info["last_message_path"] = last_msg_path
    info["last_wake_pid"] = pid
    info["last_wake_at"] = int(time.time())
    if msg_id is not None:
        info["last_wake_msg_id"] = msg_id
        info["last_seen_id"] = msg_id
    agent_meta[agent_name] = info
    updated["agent_meta"] = agent_meta
    bus._write_json(bus._room_dir(room_id) / "meta.json", updated)
    return pid, log_path, last_msg_path


def _build_fresh_agent_prompt(
    room_id: str,
    agent_name: str,
    prompt: str,
    transcript: str,
) -> str:
    return f"""You are {agent_name}, continuing an mcp-huddle discussion.

Room: {room_id}
You are: {agent_name}

Current room transcript:
{transcript}

New prompt:
{prompt}

Before replying, call messages_read(room_id="{room_id}", since_id=0, limit=50)
if huddle MCP tools are available. Ground your reply in concrete message ids.
Post any room-visible answer via message_post with your agent name. Do not
answer non-request messages unless this prompt explicitly asks for a status
or verification response.
"""


def _build_registry_agent_wakeup_prompt(
    room_id: str,
    agent_name: str,
    sender: str,
    body: str,
    to: Optional[str],
    msg_id: int,
    last_seen: int,
    transcript: str,
) -> str:
    addressed = to or "all"
    return f"""A new mcp-huddle request arrived.

Room: {room_id}
You are: {agent_name}
New request id: {msg_id}
From: {sender}
To: {addressed}
Last delivered message id: {last_seen}

Current full transcript:
{transcript}

Request body:
{body}

Protocol:
1. Call status_set(room_id="{room_id}", agent="{agent_name}", status="busy", expires_in_sec=300).
2. Call messages_read(room_id="{room_id}", since_id=0, limit=50).
3. If message #{msg_id} is kind=request addressed to {agent_name} or all and has no reply_to, answer it exactly once.
4. Post your answer with message_post(room_id="{room_id}", agent="{agent_name}", kind="result", to="{sender}", reply_to={msg_id}, idempotency_key="{agent_name.lower()}-wake:{room_id}:{msg_id}").
5. Call status_set(room_id="{room_id}", agent="{agent_name}", status="done", expires_in_sec=60).

Do not answer requests that already have reply_to set. Do not send thanks/ack-only chatter.
"""


def _build_codex_wakeup_prompt(
    room_id: str,
    sender: str,
    body: str,
    to: Optional[str],
    msg_id: int,
    last_seen: int,
) -> str:
    addressed = to or "all"
    return f"""A new mcp-huddle request arrived in your existing room.

Room: {room_id}
You are: Codex
New request id: {msg_id}
From: {sender}
To: {addressed}
Last delivered message id: {last_seen}

Request body:
{body}

Use only huddle MCP tools for room coordination:
1. Call status_set(room_id="{room_id}", agent="Codex", status="busy", expires_in_sec=300).
2. Call messages_read(room_id="{room_id}", since_id=0, limit=50).
3. If message #{msg_id} is a kind=request addressed to Codex or all and has no reply_to, answer it exactly once.
4. Post your answer with message_post(room_id="{room_id}", agent="Codex", kind="result", to="{sender}", reply_to={msg_id}, idempotency_key="codex-wake:{room_id}:{msg_id}").
5. Call status_set(room_id="{room_id}", agent="Codex", status="done", expires_in_sec=60).

Do not answer requests that already have reply_to set. Do not send thanks/ack-only chatter.
"""


def _build_default_brief(room_id: str, name: str, goal: str, cwd: str) -> str:
    return f"""# mcp-huddle — Room: {name}

**Room ID:** {room_id}
**Goal:** {goal}
**Project:** {cwd}
**MCP server:** http://127.0.0.1:8014/mcp (HTTP) or stdio binary direct

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
Use MCP tools from mcp-huddle:
  messages_read("{room_id}") — read chat
  message_post("{room_id}", "YourName", "...", kind="comment"|"request"|"result", ...) — post
  status_set("{room_id}", "YourName", "busy"|"online") — update status

## MANDATORY: read full history BEFORE every reply
Always call `messages_read(room_id, since_id=0)` as the FIRST tool call on each
turn — even if you "remember" prior context. Other agents may have posted
since your last turn, and your reply must reference what they actually said,
not what you assume.
- If you do NOT cite at least one specific id (e.g. "agree with #3", quote
  from #2), your reply is considered ungrounded.
- Disagreement is welcome; silent agreement is not — name what you accept and
  what you reject, with a one-line reason.

Lifecycle:
- You may exit after your first response.
- When a later kind=request is addressed to you or all, huddle wakes a new
  turn. Codex uses thread resume when available; other registry agents are
  started fresh with the room transcript prepended.
"""


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
