"""mcp-huddle — FastMCP server. Persistent multi-agent chat rooms.

Stdio mode (default): JSON-RPC over stdin/stdout for MCP clients.
HTTP mode (`--http`): uvicorn + Liquid Glass dashboard on :8014.
"""

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import uuid
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
agents (e.g. Claude + Codex + Antigravity reviewing the same architectural choice).

WHEN TO USE A ROOM (vs. answering directly or calling a one-shot advisor):
- multi-step design / architecture decision with real trade-offs
- code review needing clarifying questions back-and-forth
- multi-file refactor where you want independent perspectives
- consensus required (will use propose_resolution + resolution_vote)

WHEN NOT TO USE A ROOM:
- single factual lookup (just answer)
- single-shot critique (use codex exec / agy -p instead)
- you already have enough context to act

INVITING OTHER AGENTS:
1. `room_create(name, owner=YourAgentName, owner_pid=PID, cwd=PROJECT,
   session_id=SESSION, auto_spawn=True, goal="<short description>")`
   spawns every enabled registry agent automatically. Default registry includes
   Codex, Antigravity, and MiMo, plus Qwen and DeepSeek when their local
   bridges pass live probes, and Claude when available.
2. If auto_spawn isn't available (binaries missing or you want a different
   roster), shell out yourself with the room_id + brief, e.g.
   `codex exec --dangerously-bypass-approvals-and-sandbox "Join huddle room
   <ROOM_ID>: <task>. Read messages_read first, then post."` and same for
   `agy --dangerously-skip-permissions -p "..."`. Then (owner only) call
   `room_invite(room_id, "Codex", by="<owner>")` / `..., by="<owner>"` so
   they appear in participants.
3. Prefer auto_spawn unless you specifically need a non-default agent.
4. `room_invite` of a registry-backed agent NOT already spawned in this room
   does not spawn it immediately — it just reserves the wake slot. The agent
   is spawned fresh the first time a `kind=request` addressed to it (or
   `to=all`) is posted.

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

CLOSE PROTOCOL (lifecycle is human-only — agents never close):
1. Agents express "discussion is done" via `message_post(kind="final", ...)`.
2. Rooms auto-transition to `status=idle` after IDLE_TIMEOUT_SECS of silence
   (default 600s); a new `kind=request` revives them to `open`.
3. Permanent closure/deletion happens through the dashboard or `huddle` CLI,
   not through agent MCP tools.

STORAGE: ~/.mcp-huddle/rooms/{room_id}/ (JSONL + meta.json, file-locked,
shared across all agents on this machine).

DASHBOARD: run `mcp-huddle --http` separately to watch rooms in browser
at http://127.0.0.1:8014/dashboard. Humans can post `kind=system` messages
that bypass anti-loop rules.
"""

mcp = FastMCP("mcp-huddle", instructions=_AGENT_INSTRUCTIONS)


def _env_num(name: str, default, cast):
    """Read a numeric env var, falling back to `default` on missing/garbage.

    A malformed value (e.g. IDLE_TIMEOUT_SECS="abc") must not crash startup —
    we warn to stderr and use the documented default instead.
    """
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        print(
            f"[mcp-huddle] WARNING: env {name}={raw!r} is not a valid "
            f"{cast.__name__}; using default {default!r}",
            file=sys.stderr,
        )
        return default


IDLE_TIMEOUT_SECS = _env_num("IDLE_TIMEOUT_SECS", 600, int)
# Retention: terminal rooms (closed/resolved) older than this are purged by the
# background sweep. 0 disables. Sweep runs at most once per RETENTION_SWEEP_SECS
# (not every zombie-check tick) — deletion is cheap but no need to scan hourly.
RETENTION_DAYS = _env_num("HUDDLE_RETENTION_DAYS", 7.0, float)
RETENTION_SWEEP_SECS = _env_num("HUDDLE_RETENTION_SWEEP_SECS", 3600, int)
# When a spawned agent exits because it hit its provider usage/rate-limit, do
# not re-spawn it for this many seconds — a fresh spawn would instantly fail
# again, post nothing, and burn a wake. 0 disables the cooldown gate.
RATE_LIMIT_COOLDOWN_SECS = _env_num("MCP_HUDDLE_RATE_LIMIT_COOLDOWN_SEC", 900, int)
# A wake ('busy' lease) held longer than this with no message posted by that
# agent is presumed hung — the watchdog announces it once so the organizer
# stops waiting. 0 disables the check. Does not kill the process.
WAKE_STUCK_SECS = _env_num("MCP_HUDDLE_WAKE_STUCK_SEC", 1200, int)
# A 'busy' lease whose last_wake_pid has DIED (a certain fact, unlike a hang)
# is announced fast instead of waiting out WAKE_STUCK_SECS — but only after
# this grace window, so the sweep doesn't race a reaper on_exit callback that
# is about to fire and clear the lease on its own. 0 disables the check.
DEAD_WAKE_GRACE_SECS = _env_num("MCP_HUDDLE_DEAD_WAKE_GRACE_SEC", 60, int)


# Agents whose CLI sessions can be resumed by a stable thread/session id
# (vs. re-spawned fresh each turn). Only Codex currently exposes a UUID-based
# `exec resume`; Antigravity and the rest have no resumable thread handle.
_THREAD_RESUMABLE_AGENTS = frozenset({"Codex"})


def _is_thread_resumable(agent_name: str) -> bool:
    """True if the agent supports id-based session resume (vs. fresh spawn)."""
    return agent_name in _THREAD_RESUMABLE_AGENTS


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
      True            — spawn every enabled agent in the registry
                        with a default reviewer brief built from `goal`.
      {Name: brief}   — spawn only these agents, each with its own custom brief.
                        Example: {"Codex": "Audit auth.py for security holes",
                                  "Antigravity": "Find race conditions in db.py"}.
                        Agents not in the dict are skipped even if enabled.

    goal: short description of the discussion topic (used in default brief
          for auto_spawn=True; ignored when auto_spawn is a dict).

    With Phase 1 changes: each spawned agent's stdout/stderr is captured to
    ~/.mcp-huddle/rooms/<id>/agents/<name>.events.jsonl (Codex --json /
    Antigravity plain text). Live-stream them via SSE at /agents/<id>/<name>/events.
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("room_create requires a non-empty room name")
    if not isinstance(owner, str) or not owner.strip():
        raise ValueError("room_create requires a non-empty owner")

    room_id = bus.create_room(name, owner, owner_pid, cwd, session_id)

    if auto_spawn and cwd:
        _spawn_agents(room_id, name, goal or name, cwd, owner, auto_spawn)

    return room_id


@mcp.tool()
def room_invite(room_id: str, agent_name: str, by: str = "") -> str:
    """Owner-only roster escape hatch — add an agent to an existing room.

    Requires `by` to equal the room's owner. Regular participants must not
    expand the roster; orchestration is the owner's responsibility.

    If `agent_name` matches an enabled spawn-registry entry, this also seeds
    its `agent_meta` (via `bus.register_external_agent`) so a later
    `kind=request` addressed to it is picked up by `_wake_agents_for_request`
    and triggers a fresh spawn. It is NOT spawned immediately — invite only
    reserves the wake slot. Non-registry (external/human) names are added to
    `participants` only, unchanged from prior behavior.
    """
    info = bus.get_room_info(room_id)
    if not info:
        raise ValueError(f"Room {room_id} not found")
    owner = info.get("owner", "")
    if not by or by != owner:
        raise PermissionError(
            f"room_invite is owner-only. by={by!r} does not match owner={owner!r}"
        )
    bus.invite_agent(room_id, agent_name)
    if spawn.get_enabled_spec(agent_name):
        bus.register_external_agent(room_id, agent_name)
    return "ok"


def room_request_close(room_id: str, agent: str) -> str:
    """Signal intent to close. Returns 'closing_requested'.
    Human must confirm by calling room_close().
    """
    return bus.request_close(room_id, agent)


def room_close(room_id: str, owner: str) -> str:
    """Permanently close a room (owner only). Kills spawned agents."""
    bus.close_room(room_id, owner)
    return "closed"


def room_delete(room_id: str, owner: str) -> str:
    """Permanently remove a closed room from disk (history wipe).

    Safety: only allowed on rooms with status == 'closed'. Open or
    closing_requested rooms must be closed first via room_close().

    Side effects: deletes the entire ~/.mcp-huddle/rooms/<room_id>/ directory,
    including messages.jsonl, meta.json, agent logs. Cannot be undone.
    """
    bus.delete_room(room_id, owner)
    return "deleted"


def room_close_session(session_id: str) -> list:
    """Close all open rooms belonging to a session (called by Stop hook)."""
    return bus.close_session_rooms(session_id)


@mcp.tool()
def room_info(room_id: str) -> dict:
    """Get room metadata (participants, status, cwd, etc.)."""
    return bus.get_room_info(room_id)


@mcp.tool()
def room_reclaim(room_id: str, owner: str, owner_pid: int,
                 session_id: str = "") -> str:
    """Re-stamp the room's owner_pid after your session resumed with a new PID.

    On session resume the OS PID changes; the zombie-watchdog would otherwise
    auto-close the room once the old owner_pid no longer exists. Call this with
    your current PID (and session_id) to keep ownership. Owner-only.
    """
    bus.reclaim_room(room_id, owner, owner_pid, session_id)
    return f"reclaimed {room_id} → owner_pid={owner_pid}"


@mcp.tool()
def room_round_advance(room_id: str, owner: str, label: str = "") -> str:
    """Open a new discussion round (owner-only).

    Bumps the room's round counter, posts a visible "Round N" divider so every
    agent sees the boundary, and stamps subsequent messages with the new round.
    Read just that round later with messages_read(round=N) / summarize(round=N).
    Drive rounds by: advance → dispatch fresh workers seeded with round N-1 state
    → collect their kind=result posts → advance again.
    """
    n = bus.advance_round(room_id, owner, label)
    return f"round {n} opened"


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
    msg_id = _post_message_checked(room_id, agent, body, kind, to, reply_to, idempotency_key, meta)
    if kind == "request":
        _wake_agents_for_request(room_id, agent, body, to, reply_to, msg_id)
    return msg_id


@mcp.tool()
def messages_read(room_id: str, since_id: int = 0, limit: int = 20,
                  until_id: int = 0, max_chars: int = bus.MAX_BODY_CHARS,
                  round: int = 0, kind: str = "") -> str:
    """Read chat history as plain text (token-efficient for LLMs).

    since_id: only return messages with id > since_id (delta read).
    until_id: only return messages with id <= until_id (0 = up to newest).
              since_id+until_id give a fixed window for paging a large room.
    limit: max messages to return (default 20 = fresh context window).
    max_chars: truncate each body to this many chars (0 = full). Default caps fat
               summaries so a read can't overflow you; truncation is head+tail
               (keeps the conclusion). Re-read one msg with limit=1&max_chars=0.
    round: 0 = ignore rounds, N = only round N, -1 = current round.
    kind: comma-separated kinds to keep (e.g. "result,final") — grab just deliverables.

    Store last seen id locally and pass it on next call to avoid re-reading history.
    """
    return bus.read_messages(room_id, since_id, limit, until_id, max_chars,
                             round, kind)


@mcp.tool()
def room_summarize(room_id: str, since_id: int = 0, round: int = 0) -> str:
    """Get a cheap (no-LLM) digest: counts, open requests, and each agent's
    LATEST position — the "where does everyone stand" view between rounds.

    Scope: round=N → that round, round=-1 → current round, else since_id (0=all).
    Use instead of messages_read to catch up without re-reading everything.
    """
    return bus.summarize_messages(room_id, since_id, round)


def respond_via_agent(
    room_id: str,
    agent_name: str,
    prompt: str,
    post_as_message: bool = True,
) -> dict:
    """Phase 2: trigger a spawned agent to respond using `codex exec resume`
    (no new process startup, retains conversation context from prior turns).

    Useful when you want to ask Codex/Antigravity a follow-up in an existing room
    without manually spawning them again. The agent's thread_id was captured
    on initial spawn.

    Args:
      room_id: target room
      agent_name: which spawned agent to invoke (currently only Codex supports
                  UUID-based resume; Antigravity falls back to fresh spawn with
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

    if _is_thread_resumable(agent_name):
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

    # Antigravity and others — fresh spawn with context-prepended prompt as fallback.
    # UUID-based resume is not available for Antigravity, but a fresh CLI process can
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


def status_get(room_id: str) -> dict:
    """Get all agent statuses in a room (expired leases auto-reset to online)."""
    return bus.get_status(room_id)


def _post_message_checked(
    room_id: str,
    agent: str,
    body: str,
    kind: str,
    to: Optional[str] = None,
    reply_to: Optional[int] = None,
    idempotency_key: Optional[str] = None,
    meta: Optional[dict] = None,
) -> int:
    info = bus.get_room_info(room_id)
    if info.get("status") == "idle" and kind == "request":
        bus.revive(room_id)
    # reply_to is validated inside bus.post_message under the messages lock —
    # atomic with the append, so a duplicate reply cannot race through the gap.
    return bus.post_message(room_id, agent, body, kind, to, reply_to, idempotency_key, msg_meta=meta)


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
    """Register a file path to be notified when a kind=request is addressed to
    you. Useful for externally-launched agents (not auto_spawn'd by huddle):
    register a path, then have a hook poll it.

    On every matching request huddle writes JSON to notify_file_path:
      {"room_id", "from_agent", "kind": "request", "msg_id"}
    """
    bus.register_notify(room_id, agent, notify_file_path)
    return "ok"


# ── Background tasks ──────────────────────────────────────────────────────────

async def _background_watchdog():
    """Periodically check for zombie rooms and deadlocks."""
    last_retention_sweep = 0.0
    while True:
        await asyncio.sleep(bus.ZOMBIE_CHECK_SECS)
        try:
            closed = bus.check_zombie_rooms()
            if closed:
                print(f"[watchdog] Zombie-closed rooms: {closed}", flush=True)
        except Exception as e:
            print(f"[watchdog] zombie check error: {e}", flush=True)

        try:
            now = time.time()
            if RETENTION_DAYS > 0 and now - last_retention_sweep >= RETENTION_SWEEP_SECS:
                last_retention_sweep = now
                purged = bus.delete_old_terminal_rooms(RETENTION_DAYS)
                if purged.get("deleted"):
                    print(f"[watchdog] Retention-purged {len(purged['deleted'])} "
                          f"terminal rooms (>{RETENTION_DAYS}d)", flush=True)
        except Exception as e:
            print(f"[watchdog] retention sweep error: {e}", flush=True)

        try:
            idled = _mark_idle_rooms()
            if idled:
                print(f"[watchdog] Idle rooms: {idled}", flush=True)
        except Exception as e:
            print(f"[watchdog] idle check error: {e}", flush=True)

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

        try:
            dead = _check_dead_wakes()
            if dead:
                print(f"[watchdog] Dead-wake notices: {dead}", flush=True)
        except Exception as e:
            print(f"[watchdog] dead-wake check error: {e}", flush=True)

        try:
            stuck = _check_stuck_wakes()
            if stuck:
                print(f"[watchdog] Stuck-wake notices: {stuck}", flush=True)
        except Exception as e:
            print(f"[watchdog] stuck-wake check error: {e}", flush=True)


def _mark_idle_rooms() -> list[str]:
    idled = []
    now = int(time.time())
    for meta in bus.list_rooms():
        if meta.get("status") != "open":
            continue
        last = int(meta.get("last_activity_at") or meta.get("last_activity") or meta.get("created_at") or now)
        if now - last > IDLE_TIMEOUT_SECS:
            bus.mark_idle(meta["id"])
            idled.append(meta["id"])
    return idled


# ── Web Dashboard ─────────────────────────────────────────────────────────────

_STATIC_DIR = Path(__file__).parent / "static"


_NO_CACHE_HDRS = {"Cache-Control": "no-cache, no-store, must-revalidate",
                  "Pragma": "no-cache", "Expires": "0"}


# Hosts treated as loopback for the local-only guard below.
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _require_local(request: Request) -> Optional[JSONResponse]:
    """Guard for mutating / log-streaming HTTP endpoints.

    The server binds 127.0.0.1 and the dashboard is served from loopback, so
    this is non-breaking for normal local use. Returns a JSONResponse to
    short-circuit the calling handler when the request must be rejected, or
    None when the handler may proceed.

    1. Loopback only — reject any non-loopback client with HTTP 403.
    2. Optional shared secret — if MCP_HUDDLE_TOKEN is set in the environment,
       additionally require ``Authorization: Bearer <token>`` (or the
       ``X-Huddle-Token: <token>`` header); HTTP 401 if missing/wrong. When the
       env var is unset (the default) no token is required, so there is no
       behavior change.

    NOTE: when MCP_HUDDLE_TOKEN is set, the dashboard's own fetch() calls must
    send the matching header. That lives in dashboard.js (owned elsewhere); the
    server side stays correct and tolerant when the token is unset.
    """
    client = request.client
    host = client.host if client else None
    if host not in _LOOPBACK_HOSTS:
        return JSONResponse({"error": "forbidden: loopback only"}, status_code=403)

    token = os.environ.get("MCP_HUDDLE_TOKEN")
    if token:
        provided = request.headers.get("x-huddle-token")
        if not provided:
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                provided = auth[7:].strip()
        if provided != token:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
    return None


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
    denied = _require_local(request)
    if denied is not None:
        return denied
    try:
        data = await request.json()
        msg_id = _post_message_checked(
            data["room_id"], data["agent"], data["body"], data["kind"],
            data.get("to"), data.get("reply_to"), data.get("idempotency_key"),
            data.get("meta"),
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
    denied = _require_local(request)
    if denied is not None:
        return denied
    try:
        data = await request.json()
        bus.close_room(data["room_id"], data["owner"])
        return JSONResponse({"status": "closed"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@mcp.custom_route("/api/room_delete", methods=["POST"])
async def api_room_delete(request: Request) -> JSONResponse:
    """Wipe a closed room from disk. Backed by bus.delete_room() — only works
    on status='closed' rooms (raises ValueError otherwise)."""
    denied = _require_local(request)
    if denied is not None:
        return denied
    try:
        data = await request.json()
        bus.delete_room(data["room_id"], data["owner"])
        return JSONResponse({"status": "deleted"})
    except json.JSONDecodeError as e:
        # JSONDecodeError subclasses ValueError — catch it first so a malformed
        # body is a 400 (bad request), not a 409 (room-not-closed conflict).
        return JSONResponse({"error": f"invalid JSON: {e}"}, status_code=400)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@mcp.custom_route("/api/rooms_close_all", methods=["POST"])
async def api_rooms_close_all(request: Request) -> JSONResponse:
    """Bulk close: every non-closed room. Kills alive spawned PIDs only,
    excluding owner PIDs of any room. Dead PIDs skipped."""
    denied = _require_local(request)
    if denied is not None:
        return denied
    try:
        return JSONResponse(bus.close_all_rooms())
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/rooms_delete_closed", methods=["POST"])
async def api_rooms_delete_closed(request: Request) -> JSONResponse:
    """Wipe every room with status=closed from disk. Open rooms untouched."""
    denied = _require_local(request)
    if denied is not None:
        return denied
    try:
        return JSONResponse(bus.delete_closed_rooms())
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/rooms_nuke", methods=["POST"])
async def api_rooms_nuke(request: Request) -> JSONResponse:
    """Hard reset: close all + delete all. Owner PIDs preserved."""
    denied = _require_local(request)
    if denied is not None:
        return denied
    try:
        return JSONResponse(bus.nuke_all_rooms())
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Agent live event streaming (Phase 1) ──────────────────────────────────────

@mcp.custom_route("/api/room_agents", methods=["GET"])
async def api_room_agents(request: Request) -> JSONResponse:
    """List spawned agents for a room + computed wake-health per agent
    (stale leases, failed wakes)."""
    room_id = request.query_params.get("room_id", "")
    try:
        meta = bus._read_meta(room_id)
        agent_meta = meta.get("agent_meta", {})
        statuses = bus.get_status(room_id)
        health = {name: _agent_wake_health(info, statuses.get(name))
                  for name, info in agent_meta.items()}
        return JSONResponse({"agents": agent_meta, "health": health})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@mcp.custom_route("/api/health", methods=["GET"])
async def api_health(request: Request) -> JSONResponse:
    """Wake-health across all open/idle rooms — stale leases and failed wakes.
    Powers the dashboard health indicator."""
    try:
        rooms_health = []
        for meta in bus.list_rooms():
            if meta.get("status") not in ("open", "idle"):
                continue
            room_id = meta["id"]
            agent_meta = meta.get("agent_meta", {})
            if not agent_meta:
                continue
            statuses = bus.get_status(room_id)
            agents = {name: _agent_wake_health(info, statuses.get(name))
                      for name, info in agent_meta.items()}
            rooms_health.append({
                "room_id": room_id,
                "name": meta.get("name", ""),
                "status": meta.get("status"),
                "agents": agents,
            })
        stale = sum(1 for r in rooms_health for h in r["agents"].values()
                    if h["stale_lease"])
        failed = sum(1 for r in rooms_health for h in r["agents"].values()
                     if h["last_wake_failed"])
        return JSONResponse({
            "rooms": rooms_health,
            "stale_leases": stale,
            "failed_wakes": failed,
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/agents/{room_id}/{agent_name}/events", methods=["GET"])
async def api_agent_events(request: Request) -> StreamingResponse:
    """Server-Sent Events stream of an agent's stdout (Codex --json /
    Antigravity plain text).

    Tails ~/.mcp-huddle/rooms/<room_id>/agents/<name>.events.jsonl.
    Each line in the file becomes one SSE `data:` event.
    Closes when the file is gone (room deleted) or client disconnects.
    """
    denied = _require_local(request)
    if denied is not None:
        return denied
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

def _announce_spawn_failure(room_id: str, agent_name: str, exc: BaseException,
                            context_id: str) -> None:
    """Best-effort room notice for an outright spawn/resume failure (the
    process never started or the spawn attempt raised) — as opposed to a
    detected provider rate-limit, which _handle_rate_limit_on_exit announces
    separately. Without this, a spawn exception was only `print(...)`ed to
    the daemon's own stdout and the room saw silence with no explanation.

    Idempotent per (room, agent, context_id) so a repeated wake/retry for the
    same event doesn't spam the room with duplicate notices.
    """
    short = f"{type(exc).__name__}: {exc}"
    if len(short) > 200:
        short = short[:197] + "..."
    try:
        _post_message_checked(
            room_id, agent_name,
            f"⚠️ {agent_name} не заспавнился: {short}",
            kind="comment",
            idempotency_key=f"spawnfail:{room_id}:{agent_name}:{context_id}",
        )
    except Exception as post_exc:
        print(f"[huddle] spawn-fail notice post failed "
              f"({agent_name}@{room_id}): {post_exc}", flush=True)


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
      * Builds a default brief and writes it to a secure temp file
        (huddle-room-<id>-*-brief.md in the system temp dir) so users can
        `cat` it for debugging (always written, also when dict is used).
      * Creates ~/.mcp-huddle/rooms/<id>/agents/ for log files.
      * Updates meta.json: spawned_pids + agent_meta {name: {log_path, last_message_path}}.
      * Adds each spawned agent to participants.
    """
    default_brief = _build_default_brief(room_id, name, goal, cwd)
    # Secure unique temp file instead of a predictable /tmp/room-<id>-brief.md
    # (guessable + symlink/TOCTOU-attackable in a shared /tmp). mkstemp creates
    # the file atomically with 0600 perms.
    fd, brief_path = tempfile.mkstemp(
        prefix=f"huddle-room-{room_id}-", suffix="-brief.md")
    with os.fdopen(fd, "w") as fh:
        fh.write(default_brief)

    log_dir = bus._room_dir(room_id) / "agents"

    # Owner is already present as the calling session — never spawn a
    # duplicate of them. Match by exact registry name (canonical: "Claude",
    # "Codex", "Antigravity"). Caller is expected to pass canonical owner.
    skip_owner = {owner} if owner else set()

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
            if spec["name"] not in auto_spawn or spec["name"] in skip_owner:
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
                pid, log_path, last_msg = spawn.spawn_agent(
                    spec, agent_brief, cwd, log_dir,
                    on_exit=_make_initial_spawn_callback(room_id, spec["name"]))
                pids.append(pid)
                names.append(spec["name"])
                agent_meta[spec["name"]] = {
                    "log_path": log_path,
                    "last_message_path": last_msg,
                }
            except (FileNotFoundError, PermissionError) as exc:
                spawn.log_spawn_failure(spec, agent_brief, cwd, log_dir, exc)
                _announce_spawn_failure(room_id, spec["name"], exc, "init")
            except spawn.AgentSpawnError as exc:
                _announce_spawn_failure(room_id, spec["name"], exc, "init")
            except OSError as exc:
                spawn.log_spawn_failure(spec, agent_brief, cwd, log_dir, exc)
                _announce_spawn_failure(room_id, spec["name"], exc, "init")
                raise
    else:
        names, pids, agent_meta = spawn.spawn_all(
            default_brief, cwd, log_dir,
            on_exit_factory=lambda n: _make_initial_spawn_callback(room_id, n),
            skip_names=skip_owner,
            on_spawn_fail=lambda n, exc: _announce_spawn_failure(room_id, n, exc, "init"))

    for n in names:
        bus.invite_agent(room_id, n)

    # Phase 2: capture Codex thread_id from "thread.started" event in log,
    # so we can do `codex exec resume <id>` for follow-ups instead of spawning fresh.
    # Run blocking parse in a thread to avoid stalling room_create — but small
    # timeout so it usually returns within ~1s.
    for agent_name, info in agent_meta.items():
        if not _is_thread_resumable(agent_name):
            continue  # Only Codex has UUID-based resume; Antigravity has none.
        log_path = info.get("log_path")
        if log_path:
            tid = spawn.parse_codex_thread_id(log_path, timeout=10.0)
            if tid:
                info["thread_id"] = tid

    # Save PIDs + log paths for dashboard / zombie cleanup — locked
    # read-modify-write so a concurrent meta.json update isn't clobbered.
    # Merge (don't overwrite): extend any pre-existing spawned_pids and
    # deep-merge per-agent meta so a concurrent wake-thread update survives.
    def _save_spawn_meta(m: dict) -> dict:
        existing_pids = m.get("spawned_pids") or []
        m["spawned_pids"] = existing_pids + [p for p in pids if p not in existing_pids]
        merged = m.get("agent_meta") or {}
        for name_, info_ in agent_meta.items():
            slot = merged.get(name_)
            if isinstance(slot, dict):
                slot.update(info_)
            else:
                merged[name_] = dict(info_)
        m["agent_meta"] = merged
        return m
    bus._update_meta_locked(room_id, _save_spawn_meta)


# ── Wake lease helpers ────────────────────────────────────────────────────────
#
# A wake records a `wake_id` (generation token) + `last_wake_pid` in agent_meta
# and sets status=busy. The agent counts as busy ONLY while that pid is alive —
# a 'busy' whose pid is dead is a stale lease and must not block the next
# request. When the wake process exits, its reaper callback releases the lease
# and drains the next queued request (event-driven); the watchdog is a fallback.

# Per-(room, agent) in-process lock: serialises wake attempts coming from this
# process's own threads (MCP request handler, watchdog, reaper callbacks).
_wake_locks: dict = {}
_wake_locks_guard = threading.Lock()


def _wake_lock(room_id: str, agent_name: str) -> threading.Lock:
    key = (room_id, agent_name)
    with _wake_locks_guard:
        lock = _wake_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _wake_locks[key] = lock
    return lock


def _merge_agent_meta(room_id: str, agent_name: str, fields: dict) -> None:
    """Locked read-modify-write of one agent's agent_meta entry — never clobbers
    a concurrent meta.json update (last_activity, another agent's wake state)."""
    def _update(meta: dict) -> dict:
        am = meta.setdefault("agent_meta", {})
        info = am.get(agent_name)
        if not isinstance(info, dict):
            info = {}
        info.update(fields)
        am[agent_name] = info
        return meta
    bus._update_meta_locked(room_id, _update)


def _wake_in_progress(info: dict, status: Optional[str]) -> bool:
    """True if the agent has a LIVE wake process — do not wake it again. A
    'busy' status whose last_wake_pid is dead is a stale lease (the process
    already exited) and must NOT block a fresh wake."""
    if status != "busy":
        return False
    pid = info.get("last_wake_pid")
    return bool(pid) and bus._pid_alive(pid)


def _agent_wake_health(info: dict, status: Optional[str]) -> dict:
    """Computed wake-health for one agent — surfaces stale leases / failed
    wakes for the dashboard health view."""
    pid = info.get("last_wake_pid")
    pid_alive = bool(pid) and bus._pid_alive(pid)
    rc = info.get("last_wake_rc")
    return {
        "status": status or "offline",
        "wake_id": info.get("wake_id"),
        "last_wake_pid": pid,
        "pid_alive": pid_alive,
        "stale_lease": status == "busy" and not pid_alive,
        "last_wake_msg_id": info.get("last_wake_msg_id"),
        "last_wake_at": info.get("last_wake_at"),
        "last_wake_rc": rc,
        "last_wake_failed": rc is not None and rc != 0,
        "wake_fail_count": int(info.get("wake_fail_count", 0) or 0),
        "rate_limited": _agent_in_rate_limit_cooldown(info),
        "rate_limited_until": int(info.get("rate_limited_until", 0) or 0),
        "rate_limit_reason": info.get("rate_limit_reason"),
    }


def _make_wake_done_callback(room_id: str, agent_name: str, wake_id: str):
    """Reaper on_exit callback for a wake: release the busy lease + drain the
    next queued request the moment the agent turn ends."""
    def _callback(returncode) -> None:
        try:
            _on_wake_exit(room_id, agent_name, wake_id, returncode)
        except Exception as exc:  # never let a callback kill the reaper thread
            print(f"[huddle] wake-exit callback error "
                  f"({agent_name}@{room_id}): {exc}", flush=True)
    return _callback


def _make_initial_spawn_callback(room_id: str, agent_name: str):
    """Reaper on_exit callback for the room_create spawn: no busy lease to
    release — just announce a rate-limit (if any) and drain any request queued
    during the agent's first turn."""
    def _callback(returncode) -> None:
        try:
            if returncode not in (0, None):
                _handle_rate_limit_on_exit(room_id, agent_name)
            _drain_pending_wakes(room_id, agent_name)
        except Exception as exc:
            print(f"[huddle] initial-spawn drain error "
                  f"({agent_name}@{room_id}): {exc}", flush=True)
    return _callback


def _agent_in_rate_limit_cooldown(info: dict) -> bool:
    """True if the agent is inside an active usage/rate-limit cooldown window."""
    until = int(info.get("rate_limited_until", 0) or 0)
    return until > 0 and time.time() < until


def _handle_rate_limit_on_exit(room_id: str, agent_name: str) -> bool:
    """Inspect a just-exited agent's log for a provider usage/rate-limit refusal.

    On detection: record a cooldown window in agent_meta and post ONE comment
    to the room so the organizer knows no reply is coming (instead of silent
    death). Returns True if a rate-limit was detected.

    Idempotent per episode: while the cooldown is still active we neither
    re-stamp nor re-post, so repeated wakes don't spam the room.
    """
    if RATE_LIMIT_COOLDOWN_SECS <= 0:
        return False
    try:
        meta = bus.get_room_info(room_id)
    except Exception:
        return False
    info = (meta.get("agent_meta") or {}).get(agent_name) or {}
    log_path = info.get("log_path")
    if not log_path:
        return False
    reason = spawn.detect_rate_limit(log_path)
    if not reason:
        return False
    if _agent_in_rate_limit_cooldown(info):
        return True  # episode already recorded + announced

    now = int(time.time())
    until = now + RATE_LIMIT_COOLDOWN_SECS
    _merge_agent_meta(room_id, agent_name, {
        "rate_limited_until": until,
        "rate_limited_at": now,
        "rate_limit_reason": reason[:500],
    })
    mins = max(1, RATE_LIMIT_COOLDOWN_SECS // 60)
    short = reason if len(reason) <= 200 else reason[:197] + "..."
    try:
        _post_message_checked(
            room_id, agent_name,
            f"⚠️ {agent_name} недоступен: исчерпан лимит провайдера — "
            f"ответа не будет. Не буду повторять попытки ~{mins} мин. "
            f"Причина: {short}",
            kind="comment",
            idempotency_key=f"ratelimit:{room_id}:{agent_name}:{until}",
        )
    except Exception as exc:
        print(f"[huddle] rate-limit notice post failed "
              f"({agent_name}@{room_id}): {exc}", flush=True)
    return True


def _agent_posted_after(room_id: str, agent_name: str, msg_id: int) -> bool:
    """True if the agent posted ANY message (reply, comment, ack, ...) with an
    id greater than msg_id. Broader than _agent_replied_to_request (which only
    matches a direct reply_to): a woken agent that posts a plain comment/ack
    instead of a formal reply should NOT be flagged as silent."""
    for msg in bus._load_messages(room_id):
        if msg.get("agent") == agent_name and msg.get("id", 0) > msg_id:
            return True
    return False


def _log_tail(log_path: Optional[str], max_len: int = 200) -> str:
    """Short ANSI-stripped tail of an agent log, for a failure notice."""
    if not log_path:
        return ""
    try:
        p = Path(log_path)
        if not p.exists():
            return ""
        with open(p, "rb") as f:
            text = f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    lines = [spawn._strip_ansi(line).strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    tail = " ".join(lines[-3:])
    if len(tail) > max_len:
        tail = tail[: max_len - 3] + "..."
    return tail


def _announce_noreply_on_exit(room_id: str, agent_name: str, msg_id: int,
                              rc: int, log_path: Optional[str]) -> None:
    """Best-effort room notice when a woken agent's turn ended without it
    posting anything back to the room — without this the organizer just sees
    silence with no explanation. Only called when the exit was NOT already
    explained by a detected rate-limit (that path posts its own notice).

    msg_id is the request that triggered this wake (last_wake_msg_id); if
    falsy there is nothing to check a reply against, so this is a no-op.
    Idempotent per (room, agent, msg_id) — a duplicate exit callback for the
    same wake will not double-post (see _post_message_checked's
    idempotency_key handling in bus.post_message).
    """
    if not msg_id:
        return
    if _agent_posted_after(room_id, agent_name, msg_id):
        return
    if rc == 0:
        body = (f"⚠️ {agent_name} завершился без ответа в комнату "
                 f"(exit 0) — не ждите ответа.")
    else:
        tail = _log_tail(log_path)
        suffix = f" {tail}" if tail else ""
        body = (f"⚠️ {agent_name} завершился с ошибкой (exit {rc}) и не "
                 f"ответил — не ждите ответа.{suffix}")
    try:
        _post_message_checked(
            room_id, agent_name, body,
            kind="comment",
            idempotency_key=f"noreply:{room_id}:{agent_name}:{msg_id}",
        )
    except Exception as exc:
        print(f"[huddle] noreply notice post failed "
              f"({agent_name}@{room_id}): {exc}", flush=True)


def _on_wake_exit(room_id: str, agent_name: str, wake_id: str,
                  returncode, already_announced: bool = False) -> None:
    """Release a wake's busy lease and drain the next queued request.

    already_announced: True when the caller (currently only the dead-wake
    watchdog check, _check_dead_wakes) already posted the room notice
    explaining the silence — skips the rate-limit/noreply announcement paths
    below so the room doesn't get a second, redundant comment for the same
    wake_id.
    """
    try:
        meta = bus.get_room_info(room_id)
    except Exception:
        return
    info = (meta.get("agent_meta") or {}).get(agent_name) or {}
    # Act only if this wake still owns the lease — a newer wake may have
    # superseded us (then it owns the busy state and the drain).
    if info.get("wake_id") != wake_id:
        return
    rc = -999 if returncode is None else int(returncode)
    fail_count = int(info.get("wake_fail_count", 0) or 0)
    updates = {
        "last_wake_rc": rc,
        "last_wake_exit_at": int(time.time()),
        "wake_fail_count": (fail_count + 1) if rc != 0 else 0,
    }
    if rc == 0:
        # A clean turn clears any prior rate-limit cooldown so the agent can be
        # woken again immediately.
        updates["rate_limited_until"] = 0
    _merge_agent_meta(room_id, agent_name, updates)
    rate_limit_announced = already_announced
    if not already_announced and rc != 0:
        try:
            rate_limit_announced = _handle_rate_limit_on_exit(room_id, agent_name)
        except Exception as exc:
            print(f"[huddle] rate-limit check error "
                  f"({agent_name}@{room_id}): {exc}", flush=True)
    bus.set_status(room_id, agent_name, "online", 0, meta.get("session_id", ""))
    if not rate_limit_announced:
        try:
            _announce_noreply_on_exit(
                room_id, agent_name,
                int(info.get("last_wake_msg_id", 0) or 0),
                rc, info.get("log_path"))
        except Exception as exc:
            print(f"[huddle] noreply check error "
                  f"({agent_name}@{room_id}): {exc}", flush=True)
    try:
        _drain_pending_wakes(room_id, agent_name)
    except Exception as exc:
        print(f"[huddle] wake drain error ({agent_name}@{room_id}): {exc}",
              flush=True)


def _next_pending_request(room_id: str, agent_name: str,
                          info: dict) -> Optional[dict]:
    """Oldest request addressed to agent_name it has not been woken for yet.
    The message log itself is the per-agent wake queue."""
    last_wake = int(info.get("last_wake_msg_id", 0) or 0)
    for msg in bus._load_messages(room_id):
        if msg.get("id", 0) <= last_wake:
            continue
        if msg.get("kind") != "request" or msg.get("reply_to") is not None:
            continue
        if msg.get("agent") == agent_name:
            continue
        to = msg.get("to")
        if to and to not in (agent_name, "all"):
            continue
        return msg
    return None


def _drain_pending_wakes(room_id: str, agent_name: str) -> None:
    """Wake the agent for the next request that queued while it was busy."""
    try:
        meta = bus.get_room_info(room_id)
    except Exception:
        return
    if meta.get("status") not in ("open", "idle"):
        return
    info = (meta.get("agent_meta") or {}).get(agent_name) or {}
    pending = _next_pending_request(room_id, agent_name, info)
    if pending is None:
        return
    _wake_agents_for_request(
        room_id, pending.get("agent", ""), pending.get("body", ""),
        pending.get("to"), None, pending["id"])


def _wake_agents_for_request(
    room_id: str,
    sender: str,
    body: str,
    to: Optional[str],
    reply_to: Optional[int],
    msg_id: int,
) -> list[dict]:
    """Wake room agents for a newly posted request.

    Codex is resumed via its captured thread_id (one logical session per room);
    other registry agents get a fresh spawn. A request that finds an agent
    mid-turn is left queued — the message log is the queue, drained by the
    agent's reaper callback (the watchdog is only a fallback). Requests that
    carry reply_to are answers, not new tasks → ignored.
    """
    if reply_to is not None:
        return []

    meta = bus.get_room_info(room_id)
    agent_meta = meta.get("agent_meta", {})
    cwd = meta.get("cwd", "") or ""
    session_id = meta.get("session_id", "")
    wakes: list[dict] = []

    for agent_name in list(agent_meta.keys()):
        if agent_name == sender:
            continue
        if to and to not in (agent_name, "all"):
            continue

        with _wake_lock(room_id, agent_name):
            # Re-read under the lock — another thread may have just woken it.
            fresh = bus.get_room_info(room_id)
            info = (fresh.get("agent_meta") or {}).get(agent_name) or {}
            status = bus.get_status(room_id).get(agent_name)

            if _wake_in_progress(info, status):
                continue  # live wake → request stays queued, drained on exit
            if _agent_in_rate_limit_cooldown(info):
                continue  # provider limit hit → a fresh spawn would instantly fail
            last_wake = int(info.get("last_wake_msg_id", 0) or 0)
            if last_wake >= msg_id:
                continue
            if _agent_replied_to_request(room_id, agent_name, msg_id):
                _merge_agent_meta(room_id, agent_name, {
                    "last_wake_msg_id": msg_id,
                    "last_seen_id": max(int(info.get("last_seen_id", 0) or 0), msg_id),
                })
                continue

            log_path = info.get("log_path")
            last_seen = int(info.get("last_seen_id", 0) or 0)
            wake_id = uuid.uuid4().hex[:12]

            if _is_thread_resumable(agent_name):
                thread_id = info.get("thread_id")
                if not log_path:
                    continue
                if not thread_id:
                    thread_id = spawn.parse_codex_thread_id(log_path, timeout=1.0)
                    if not thread_id:
                        continue
                    _merge_agent_meta(room_id, agent_name, {"thread_id": thread_id})
                if not spawn.codex_log_has_completed_turn(log_path):
                    continue
                prompt = _build_codex_wakeup_prompt(
                    room_id, sender, body, to, msg_id, last_seen)
                bus.set_status(room_id, agent_name, "busy", 300, session_id)
                try:
                    pid = spawn.codex_resume(
                        thread_id, prompt, cwd, log_path,
                        info.get("last_message_path"),
                        on_exit=_make_wake_done_callback(room_id, agent_name, wake_id),
                    )
                except Exception as exc:
                    bus.set_status(room_id, agent_name, "online", 0, session_id)
                    print(f"[huddle] codex_resume failed ({room_id}): {exc}",
                          flush=True)
                    _announce_spawn_failure(room_id, agent_name, exc, str(msg_id))
                    continue
                _merge_agent_meta(room_id, agent_name, {
                    "last_wake_msg_id": msg_id, "last_seen_id": msg_id,
                    "last_wake_pid": pid, "last_wake_at": int(time.time()),
                    "wake_id": wake_id,
                })
                wakes.append({"agent": agent_name, "pid": pid,
                              "thread_id": thread_id})
                continue

            # Registry agents without UUID resume — fresh spawn each turn.
            transcript = bus.read_messages(room_id, since_id=0, limit=50)
            prompt = _build_registry_agent_wakeup_prompt(
                room_id, agent_name, sender, body, to, msg_id, last_seen,
                transcript)
            try:
                pid, _, _ = _spawn_fresh_room_agent(
                    room_id, agent_name, prompt, fresh, msg_id=msg_id,
                    wake_id=wake_id)
            except Exception as exc:
                print(f"[huddle] fresh spawn failed for {agent_name} "
                      f"({room_id}): {exc}", flush=True)
                _announce_spawn_failure(room_id, agent_name, exc, str(msg_id))
                continue
            wakes.append({"agent": agent_name, "pid": pid, "thread_id": ""})

    return wakes


def _agent_replied_to_request(room_id: str, agent_name: str, msg_id: int) -> bool:
    """Return True if this agent already posted a visible reply to a request."""
    for msg in bus._load_messages(room_id):
        if msg.get("agent") == agent_name and msg.get("reply_to") == msg_id:
            return True
    return False


def _wake_pending_agents() -> list[dict]:
    """Fallback retry for wakes the event-driven path missed (e.g. a reaper
    thread that died together with a short-lived stdio huddle process). The
    primary drain is the reaper on_exit callback — this is belt-and-suspenders.
    """
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
            if _wake_in_progress(info, statuses.get(agent_name)):
                continue
            pending = _next_pending_request(room_id, agent_name, info)
            if pending is None:
                continue
            wakes.extend(_wake_agents_for_request(
                room_id, pending.get("agent", ""), pending.get("body", ""),
                pending.get("to"), None, pending["id"]))
    return wakes


def _check_dead_wakes() -> list[str]:
    """Watchdog sweep: fast-path for a 'busy' lease whose last_wake_pid has
    already DIED without its reaper on_exit callback ever running (e.g. a
    server restart killed the reaper thread, or the callback raised before
    reaching bus.set_status). A dead pid is a certain fact — unlike a hang,
    there is no need to wait out WAKE_STUCK_SECS to know no reply is coming.

    Runs before _check_stuck_wakes in the sweep so a dead pid is announced
    here, fast, instead of by the slow stuck-wake path; _check_stuck_wakes
    only ever considers leases with a LIVE pid (_wake_in_progress requires
    it), so the two checks never double-announce the same wake.

    Returns the list of (agent, room) leases this sweep released — a lease
    is always released once its pid is confirmed dead, even when the agent
    had already posted something before dying (then no notice is posted,
    since the room already has an explanation, but the lease still must not
    leak forever).
    """
    if DEAD_WAKE_GRACE_SECS <= 0:
        return []
    announced: list[str] = []
    now = int(time.time())
    for meta in bus.list_rooms():
        if meta.get("status") not in ("open", "idle"):
            continue
        room_id = meta["id"]
        agent_meta = meta.get("agent_meta", {})
        if not agent_meta:
            continue
        statuses = bus.get_status(room_id)
        for agent_name, info in agent_meta.items():
            if statuses.get(agent_name) != "busy":
                continue
            pid = info.get("last_wake_pid")
            if not pid or bus._pid_alive(pid):
                continue  # alive, or nothing to check — not this sweep's job
            last_wake_at = int(info.get("last_wake_at", 0) or 0)
            if not last_wake_at or now - last_wake_at < DEAD_WAKE_GRACE_SECS:
                continue  # give the reaper callback a chance to fire first
            wake_id = info.get("wake_id")
            if not wake_id:
                continue
            # A dead pid is a certain fact and the lease must be cleared
            # regardless — an agent that posted something before dying (e.g.
            # a reply that raced its own crash) already explained the
            # silence, so skip only the redundant notice, not the release.
            msg_id = int(info.get("last_wake_msg_id", 0) or 0)
            already_explained = bool(msg_id) and _agent_posted_after(
                room_id, agent_name, msg_id)
            if not already_explained:
                try:
                    _post_message_checked(
                        room_id, agent_name,
                        f"⚠️ {agent_name}: процесс {pid} умер, не ответив — "
                        f"ответа не будет.",
                        kind="comment",
                        idempotency_key=f"deadwake:{room_id}:{agent_name}:{wake_id}",
                    )
                except Exception as exc:
                    print(f"[huddle] dead-wake notice post failed "
                          f"({agent_name}@{room_id}): {exc}", flush=True)
            try:
                _on_wake_exit(room_id, agent_name, wake_id, None,
                               already_announced=True)
            except Exception as exc:
                print(f"[huddle] dead-wake lease-clear error "
                      f"({agent_name}@{room_id}): {exc}", flush=True)
            announced.append(f"{agent_name}@{room_id}")
    return announced


def _check_stuck_wakes() -> list[str]:
    """Watchdog sweep: announce (once per wake) a 'busy' lease held longer
    than WAKE_STUCK_SECS with no message posted by that agent since the wake
    started — a live-but-silent (or hung) agent process. This is the third
    silent-exit path: unlike _on_wake_exit / _handle_rate_limit_on_exit
    (which fire when the process exits), a genuinely hung process never
    exits, so nothing else in this file will ever tell the organizer to stop
    waiting on it. Announce-only — never kills the process.
    """
    if WAKE_STUCK_SECS <= 0:
        return []
    announced: list[str] = []
    now = int(time.time())
    for meta in bus.list_rooms():
        if meta.get("status") not in ("open", "idle"):
            continue
        room_id = meta["id"]
        agent_meta = meta.get("agent_meta", {})
        if not agent_meta:
            continue
        statuses = bus.get_status(room_id)
        for agent_name, info in agent_meta.items():
            if not _wake_in_progress(info, statuses.get(agent_name)):
                continue
            last_wake_at = int(info.get("last_wake_at", 0) or 0)
            if not last_wake_at or now - last_wake_at < WAKE_STUCK_SECS:
                continue
            wake_id = info.get("wake_id")
            if not wake_id or info.get("stuck_announced_wake_id") == wake_id:
                continue  # already announced for this exact wake
            msg_id = int(info.get("last_wake_msg_id", 0) or 0)
            if msg_id and _agent_posted_after(room_id, agent_name, msg_id):
                continue  # it has been talking — a slow lease release, not a hang
            pid = info.get("last_wake_pid")
            alive = bool(pid) and bus._pid_alive(pid)
            mins = max(1, (now - last_wake_at) // 60)
            body = (f"⏳ {agent_name} не отвечает уже ~{mins} мин "
                    f"(процесс {pid} {'жив' if alive else 'мёртв'}) — "
                    f"возможно завис; не ждите ответа.")
            try:
                _post_message_checked(
                    room_id, agent_name, body,
                    kind="comment",
                    idempotency_key=f"stuck:{room_id}:{agent_name}:{wake_id}",
                )
                _merge_agent_meta(room_id, agent_name,
                                   {"stuck_announced_wake_id": wake_id})
                announced.append(f"{agent_name}@{room_id}")
            except Exception as exc:
                print(f"[huddle] stuck-wake notice post failed "
                      f"({agent_name}@{room_id}): {exc}", flush=True)
    return announced


def _spawn_fresh_room_agent(
    room_id: str,
    agent_name: str,
    prompt: str,
    meta: dict,
    msg_id: Optional[int] = None,
    wake_id: Optional[str] = None,
) -> tuple[int, str, str | None]:
    """Spawn a registry-backed one-shot turn for an agent without UUID resume.

    Records a wake lease (wake_id + last_wake_pid) and wires a reaper callback
    so the busy lease is released — and the next request drained — when the
    process exits."""
    spec = spawn.get_enabled_spec(agent_name)
    if not spec:
        raise ValueError(f"Agent {agent_name} has no enabled spawn registry entry")

    if agent_name not in meta.get("participants", []):
        bus.invite_agent(room_id, agent_name)

    if wake_id is None:
        wake_id = uuid.uuid4().hex[:12]
    session_id = meta.get("session_id", "")
    bus.set_status(room_id, agent_name, "busy", 300, session_id)
    try:
        pid, log_path, last_msg_path = spawn.spawn_agent(
            spec,
            prompt,
            meta.get("cwd", "") or "",
            bus._room_dir(room_id) / "agents",
            on_exit=_make_wake_done_callback(room_id, agent_name, wake_id),
        )
    except Exception:
        bus.set_status(room_id, agent_name, "online", 0, session_id)
        raise

    fields = {
        "log_path": log_path,
        "last_message_path": last_msg_path,
        "last_wake_pid": pid,
        "last_wake_at": int(time.time()),
        "wake_id": wake_id,
    }
    if msg_id is not None:
        fields["last_wake_msg_id"] = msg_id
        fields["last_seen_id"] = msg_id
    _merge_agent_meta(room_id, agent_name, fields)
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
1. Call messages_read(room_id="{room_id}", since_id=0, limit=50).
2. If message #{msg_id} is kind=request addressed to {agent_name} or all and has no reply_to, answer it exactly once.
3. Post your answer with message_post(room_id="{room_id}", agent="{agent_name}", kind="result", to="{sender}", reply_to={msg_id}, idempotency_key="{agent_name.lower()}-wake:{room_id}:{msg_id}").

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
1. Call messages_read(room_id="{room_id}", since_id=0, limit=50).
2. If message #{msg_id} is a kind=request addressed to Codex or all and has no reply_to, answer it exactly once.
3. Post your answer with message_post(room_id="{room_id}", agent="Codex", kind="result", to="{sender}", reply_to={msg_id}, idempotency_key="codex-wake:{room_id}:{msg_id}").

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
  room_summarize("{room_id}", since_id=N) — token-efficient digest after absence

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
