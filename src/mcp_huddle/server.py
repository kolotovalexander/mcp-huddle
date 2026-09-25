"""mcp-huddle — FastMCP server. Persistent multi-agent chat rooms.

Stdio mode (default): JSON-RPC over stdin/stdout for MCP clients.
HTTP mode (`--http`): uvicorn + Liquid Glass dashboard on :8014.
"""

import asyncio
import contextlib
from functools import wraps
import hashlib
import hmac
import json
import os
import re
import shutil
import signal
import stat
import sys
import tempfile
import threading
import time
import uuid
import weakref
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, StreamingResponse

from . import bus
from . import child_processes
from .claude_model_receipt import parse_claude_model_receipt
from . import room_workspace
from . import spawn
from . import swarm_pilot
from . import swarm_jev
from . import swarm_replacement
from . import swarm_planner

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

LIFECYCLE / WAITING:
- Spawned participants move through `queued` → `starting` → `thinking`/`working`
  → `responding` → `completed`; failures are `unavailable`, `rate_limited`, or
  `stuck`.
- A live process, quiet log, or `busy` status is NOT a completed answer. Call
  `room_status(room_id)` to see `process_alive`, phase, pending request ids,
  and `wait_recommended`; wait while a participant is in an active phase.
- Agents should report active work with `status_set` and publish the final
  answer with `message_post(kind="result", ...)`.

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
- Consensus records agreement, not correctness. Verify factual claims against
  evidence before voting.

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

# ── Watchdog lifespan (shared by stdio and HTTP transports) ─────────────────
#
# FastMCP's `lifespan=` constructor arg wraps the low-level Server's lifespan,
# which fires around *every* `Server.run()` call: once for the whole process
# in stdio mode (mcp.run() -> run_stdio_async() -> one Server.run()), but once
# PER SESSION in HTTP mode (StreamableHTTPSessionManager calls
# `self.app.run(...)` — i.e. `Server.run()` — for each new HTTP connection).
# build_app()'s combined_lifespan ALSO enters this (wrapping the whole HTTP
# app's lifetime), concurrently with whatever per-session entries are live at
# the time.
#
# So this must tolerate an arbitrary number of overlapping entries in any
# order, not just "the first one wins": it's reference-counted rather than
# ownership-based — the watchdog task starts on the 1st entry (or restarts if
# it isn't alive, e.g. it crashed) and is cancelled only when the LAST live
# entry exits (refcount back to 0), regardless of entry/exit order. This
# avoids a real bug an ownership model would have: if a per-session lifespan
# happened to be first in and combined_lifespan's app-level entry outlived
# it, an "only the starter cancels" model would kill the watchdog the moment
# that one session closed, silently starving zombie-room cleanup for every
# other still-open session.
_watchdog_task: "asyncio.Task | None" = None
_watchdog_refcount = 0


@contextlib.asynccontextmanager
async def _watchdog_lifespan(_server=None):
    """Keep `_background_watchdog()` running while at least one caller has
    this context manager open; tear it down only once the last caller exits.
    Safe to enter concurrently/re-entrantly in any order (HTTP: once per
    session plus once for the whole app; stdio: once total)."""
    global _watchdog_task, _watchdog_refcount
    _watchdog_refcount += 1
    if _watchdog_task is None or _watchdog_task.done():
        _watchdog_task = asyncio.create_task(_background_watchdog())
    try:
        yield
    finally:
        _watchdog_refcount = max(0, _watchdog_refcount - 1)
        if _watchdog_refcount == 0:
            task, _watchdog_task = _watchdog_task, None
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task


mcp = FastMCP("mcp-huddle", instructions=_AGENT_INSTRUCTIONS, lifespan=_watchdog_lifespan)


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
# When _check_stuck_wakes announces a hung wake, also SIGTERM the still-alive
# process instead of only announcing — a leaked/hung CLI otherwise sits
# forever holding the busy lease past WAKE_STUCK_SECS. Default ON (owner
# decision). Set to 0/false/no for announce-only (legacy behavior).
STUCK_KILL_ENABLED = os.environ.get("MCP_HUDDLE_STUCK_KILL", "1").lower() not in (
    "0", "false", "no",
)


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
      True            — spawn every enabled registry agent not marked
                        "auto": false, with a default reviewer brief built
                        from `goal`. The per-spec "auto" flag curates the
                        auto-spawn roster without disabling an agent outright
                        — it stays reachable via an explicit dict auto_spawn,
                        room_invite, or a wake-path request (all three ignore
                        "auto": false; a deliberately named agent always works).
      {Name: brief}   — spawn only these agents, each with its own custom brief.
                        Example: {"Codex": "Audit auth.py for security holes",
                                  "Antigravity": "Find race conditions in db.py"}.
                        Agents not in the dict are skipped even if enabled, and
                        each listed agent's "auto" flag is ignored.

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
    """Permanently close a room (owner only).

    The current server terminates only its exact locally owned child processes;
    persisted PIDs remain diagnostic and are never signalled directly.
    """
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
    """Close all open rooms belonging to a session (called at SessionEnd)."""
    return bus.close_session_rooms(session_id)


@mcp.tool()
def room_info(room_id: str) -> dict:
    """Get room metadata (participants, status, cwd, etc.)."""
    return bus.get_room_info(room_id)


@mcp.tool()
def room_rename(room_id: str, name: str, owner: str) -> dict:
    """Rename a room while preserving its ID, messages, status, and access.

    Only the recorded room owner may rename it. Names are trimmed and limited
    to 160 characters.
    """
    return bus.rename_room(room_id, name, owner)


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


# ── Bounded four-mode swarm pilot ─────────────────────────────────────────────

_SWARM_COST_CLASSES = {"free", "cheap", "paid", "unknown"}
_SWARM_CLIENT_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SWARM_HASH_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SWARM_JEV_HARNESSES = {
    "agy": "antigravity", "antigravity": "antigravity",
    "claude": "claude", "codex": "codex", "opencode": "opencode",
}


def _swarm_client_request_fingerprint(
    name: str, organizer: str, goal: str, mode: str, members: list[str],
    cwd: str, workspace_strategy: str, start_requested: bool,
    write_policy: str = room_workspace.READ_ONLY,
) -> str:
    payload = {
        "name": name,
        "organizer": organizer,
        "goal": goal,
        "mode": mode,
        "members": list(members),
        "cwd": cwd,
        "workspace_strategy": workspace_strategy,
        "start_requested": start_requested,
    }
    if write_policy != room_workspace.READ_ONLY:
        # Omitted for read-only so existing request fingerprints stay stable.
        payload["write_policy"] = write_policy
    try:
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, UnicodeError):
        raise ValueError("request fields cannot be encoded safely") from None
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _swarm_request_room_id(organizer: str, client_request_id: str) -> str:
    digest = hashlib.sha256(
        (organizer + "\0" + client_request_id).encode("utf-8")
    ).hexdigest()
    return "room_" + digest[:8]


def _swarm_validate_expected_specs(
    members: list[str], expected_specs: dict[str, str] | None,
    *, check_registry: bool = True,
) -> dict[str, str] | None:
    if expected_specs is None:
        return None
    if not isinstance(members, list) or not all(isinstance(item, str) for item in members):
        raise ValueError("members must be a list of participant names")
    if not isinstance(expected_specs, dict):
        raise ValueError("expected_specs must map every member to a sha256 fingerprint")
    if len(members) != len(set(members)):
        raise ValueError("members must be unique when expected_specs is supplied")
    if set(expected_specs) != set(members):
        raise ValueError("expected_specs keys must exactly match members")
    for member, fingerprint in expected_specs.items():
        if not isinstance(member, str) or not isinstance(fingerprint, str) or not _SWARM_HASH_RE.fullmatch(fingerprint):
            raise ValueError("expected_specs values must be sha256 fingerprints")

    if check_registry:
        registry = {item["id"]: item for item in _swarm_plan_candidates()}
        for member in members:
            current = registry.get(member)
            if current is None or not current["enabled"] or not current["static_ok"]:
                raise ValueError(f"participant is not statically available: {member}")
            if current["spec_fingerprint"] != expected_specs[member]:
                raise ValueError(f"registry spec drift for participant: {member}")
    return dict(expected_specs)


def _swarm_mark_create_state(
    room_id: str, state: str, expected_specs: dict[str, str] | None,
    start_requested: bool, registry_checked: bool,
    initial_dispatch: list[dict] | None = None,
) -> None:
    def update(meta: dict) -> dict:
        pilot = meta.get("swarm_pilot")
        if not isinstance(pilot, dict):
            raise ValueError("partial_room: swarm pilot state is missing")
        pilot["server_create_state"] = state
        pilot["start_requested"] = start_requested
        pilot["registry_availability_checked"] = registry_checked
        if expected_specs is not None:
            pilot["expected_specs"] = dict(expected_specs)
        if state == "ready":
            pilot["initial_dispatch"] = list(initial_dispatch or [])
        return meta
    bus._update_meta_locked(room_id, update)


def _swarm_existing_request(
    room_id: str, fingerprint: str, organizer: str, expected_specs: dict[str, str] | None,
) -> dict | None:
    try:
        meta = bus.get_room_info(room_id)
    except ValueError:
        try:
            room_path = bus._room_dir(room_id)
        except ValueError:
            raise ValueError("partial_room: deterministic room path is invalid") from None
        if room_path.exists():
            raise ValueError("partial_room: room exists without readable initialized metadata") from None
        return None

    pilot = meta.get("swarm_pilot")
    if not isinstance(pilot, dict):
        raise ValueError("partial_room: deterministic room ID is occupied by a non-pilot room")
    stored_fingerprint = pilot.get("client_request_fingerprint")
    if not isinstance(stored_fingerprint, str) or not _SWARM_HASH_RE.fullmatch(stored_fingerprint):
        raise ValueError("partial_room: room has no valid client request fingerprint")
    if stored_fingerprint != fingerprint:
        raise ValueError("client_request_id conflict: the room belongs to a different request")
    if meta.get("owner") != organizer or pilot.get("organizer") != organizer:
        raise ValueError("client_request_id conflict: organizer does not match the stored room")
    if not isinstance(pilot.get("start_requested"), bool):
        raise ValueError("partial_room: original start request state is missing")

    try:
        recomputed = _swarm_client_request_fingerprint(
            meta["name"], pilot["organizer"], pilot["goal"], pilot["mode"],
            pilot["members"], meta.get("cwd", ""), pilot["workspace_strategy"],
            pilot["start_requested"], room_workspace.write_policy(meta),
        )
    except (KeyError, TypeError, ValueError):
        raise ValueError("partial_room: stored request data is incomplete") from None
    if recomputed != stored_fingerprint:
        raise ValueError("partial_room: stored request fingerprint does not match room data")
    create_state = pilot.get("server_create_state")
    if create_state not in {"ready", "preparing"}:
        raise ValueError("partial_room: room initialization state is missing or invalid")
    if create_state == "ready" and not isinstance(pilot.get("initial_dispatch"), list):
        raise ValueError("partial_room: initial dispatch result is missing")
    members = pilot.get("members")
    participants = meta.get("participants")
    if (not isinstance(members, list) or not isinstance(participants, list)
            or any(member not in participants for member in members)):
        raise ValueError("partial_room: room roster initialization is incomplete")
    if expected_specs is not None:
        stored_specs = pilot.get("expected_specs")
        if stored_specs is not None and stored_specs != expected_specs:
            raise ValueError("client_request_id conflict: expected agent specs differ from stored request")
        _swarm_validate_expected_specs(members, expected_specs)
    result = {
        "room_id": room_id,
        "mode": pilot.get("mode"),
        "dispatched": [],
        "started": pilot["start_requested"],
        "start_requested": pilot["start_requested"],
        "previous_dispatch": (
            list(pilot["initial_dispatch"])
            if create_state == "ready" else _swarm_initial_dispatch(room_id)
        ),
        "reused": True,
        "availability": {
            "static_only": not bool(pilot.get("registry_availability_checked")),
            "registry_availability_checked": bool(pilot.get("registry_availability_checked")),
            "provider_response_verified": False,
        },
    }
    if create_state == "preparing":
        result["_resume_preparing"] = True
    return result


def _swarm_initial_dispatch(room_id: str) -> list[dict]:
    state = swarm_pilot.status(room_id)
    dispatched = state.get("dispatched")
    if not isinstance(dispatched, dict):
        return []
    return [
        {"member": member, "request_id": dispatched[member], "status": "dispatched"}
        for member in state.get("members", []) if member in dispatched
    ]


def _serialize_swarm_create_by_request_id(function):
    """Serialize same-process retries for one deterministic room identity."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        request_id = kwargs.get("client_request_id", args[8] if len(args) > 8 else "")
        organizer = kwargs.get("organizer", args[1] if len(args) > 1 else "")
        if (not isinstance(request_id, str) or not _SWARM_CLIENT_REQUEST_ID_RE.fullmatch(request_id)
                or not isinstance(organizer, str)):
            return function(*args, **kwargs)
        room_id = _swarm_request_room_id(organizer, request_id)
        # Reuse Huddle's weak in-process per-room lock. It is held only in
        # memory; bus metadata locks remain sequential and are never nested.
        with _wake_lock(room_id, "__swarm_create__"):
            return function(*args, **kwargs)
    return wrapped


def _swarm_resume_preparing(
    room_id: str, organizer: str, members: list[str], expected_specs: dict[str, str] | None,
    start_requested: bool, registry_checked: bool,
) -> dict:
    """Idempotently finish invites and dispatch for a deterministic partial room."""
    if start_requested:
        unavailable = [member for member in members if spawn.get_enabled_spec(member) is None]
        if unavailable:
            raise ValueError(f"unavailable registry members while resuming partial room: {unavailable}")
    for member in members:
        room_invite(room_id, member, by=organizer)
    new_dispatch = swarm_pilot_pump(room_id) if start_requested else []
    if start_requested and swarm_pilot.due_members(room_id):
        blocked = [
            item for item in new_dispatch
            if item.get("status") in {"spec_drift", "unavailable"}
        ]
        reason = ", ".join(item.get("status", "blocked") for item in blocked) or "pending members"
        raise ValueError(f"partial_room: swarm dispatch remains incomplete ({reason})")
    all_dispatch = _swarm_initial_dispatch(room_id) if start_requested else []
    _swarm_mark_create_state(
        room_id, "ready", expected_specs, start_requested, registry_checked, all_dispatch,
    )
    return {
        "room_id": room_id,
        "mode": swarm_pilot.status(room_id).get("mode"),
        "dispatched": new_dispatch,
        "started": start_requested,
        "start_requested": start_requested,
        "previous_dispatch": all_dispatch,
        "reused": True,
        "availability": {
            "static_only": not registry_checked,
            "registry_availability_checked": registry_checked,
            "provider_response_verified": False,
        },
    }


def _swarm_cli_exists(command: str) -> bool:
    """Check only the executable named by a registry command; never launch it."""
    if not isinstance(command, str) or not command.strip():
        return False
    if os.path.isabs(command):
        path = Path(command)
        return path.is_file() and os.access(path, os.X_OK)
    if os.sep in command:
        path = Path(command)
        return path.is_file() and os.access(path, os.X_OK)
    return shutil.which(command) is not None


def _swarm_display_model(value: object) -> str:
    """Keep model IDs useful while hiding values that look like credentials."""
    if not isinstance(value, str) or not value.strip():
        return "harness default"
    model = value.strip()
    lowered = model.lower()
    if (
        len(model) > 160
        or any(marker in lowered for marker in ("api_key", "token=", "secret=", "password=", "bearer "))
        or lowered.startswith("sk-")
        or re.fullmatch(r"[A-Za-z0-9_-]{40,}", model)
    ):
        return "configured model (redacted)"
    return model


def _swarm_readonly_enforced(spec: dict) -> bool:
    """Delegate CLI permission truth to spawn's canonical effective check."""
    try:
        return spawn.readonly_enforced(spec)
    except Exception:
        return False


def _swarm_plan_candidates() -> list[dict]:
    """Build closed preview facts from the raw registry without live probes."""
    candidates = []
    for spec in spawn._raw_registry():
        name = spec.get("name")
        command = spec.get("cmd")
        enabled = spec.get("enabled") is True
        reasons: list[str] = []
        cli_kind = "unsupported"
        model = "harness default"
        effort = variant = None
        static_ok = True
        settings: dict[str, str] = {}
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(command, list) or not command or not all(
            isinstance(item, str) for item in command
        ):
            static_ok = False
            reasons.append("invalid command template")
            executable = ""
        else:
            cli_index = spawn._effective_binary_index(command)
            executable = command[cli_index] if cli_index is not None else ""
            binary = spawn._effective_binary(command)
            cli_kind = binary or "unsupported"
            if cli_index and Path(command[0]).name == "timeout" and not _swarm_cli_exists(command[0]):
                static_ok = False
                reasons.append("timeout wrapper executable not found")
            if cli_index is None or not _swarm_cli_exists(executable):
                static_ok = False
                reasons.append("CLI executable not found")
            try:
                settings = spawn.model_settings_for_spec(spec)
                if not isinstance(settings, dict) or any(
                    key not in {"model", "effort", "variant"}
                    or not isinstance(value, str) or not value.strip()
                    for key, value in settings.items()
                ):
                    raise ValueError("invalid effective model settings")
            except Exception:
                settings = {}
                static_ok = False
                reasons.append("model settings are invalid")
            model = settings.get("model") or spec.get("model") or model
            effort = settings.get("effort") or spec.get("effort")
            variant = settings.get("variant") or spec.get("variant")
        # Special typed profiles have a different fixed runner/auth contract;
        # the static pilot planner does not claim to preflight those yet.
        if spec.get("profile"):
            static_ok = False
            reasons.append("typed runner profile is not covered by static preflight")
        try:
            fingerprint = spawn.spec_fingerprint(spec)
        except Exception:
            fingerprint = "sha256:invalid"
            static_ok = False
            reasons.append("registry contract could not be fingerprinted")
        raw_cost = spec.get("cost_class")
        cost_class = (
            raw_cost if isinstance(raw_cost, str) and raw_cost in _SWARM_COST_CLASSES
            else "unknown"
        )
        if cost_class == "unknown" and isinstance(model, str) and model.lower().endswith(":free"):
            cost_class = "free"
            reasons.append("configured model route is explicitly marked free")
        if cost_class == "unknown":
            reasons.append("cost class is not declared in registry")
        candidates.append({
            "id": name,
            "name": name,
            "cli_kind": cli_kind,
            "model": _swarm_display_model(model),
            "effort": effort if isinstance(effort, str) and effort.strip() else None,
            "variant": variant if isinstance(variant, str) and variant.strip() else None,
            "readonly_enforced": _swarm_readonly_enforced(spec),
            "enabled": enabled,
            "static_ok": static_ok,
            "cost_class": cost_class,
            "spec_fingerprint": fingerprint,
            "reasons": reasons,
        })
    return candidates


def _swarm_jev_facts(profile: dict) -> swarm_jev.ModeFacts:
    return swarm_jev.ModeFacts(
        task_type=profile["task_type"],
        needs_files=profile["needs_files"],
        parts=profile["parts"],
        sequential_dependency=profile["sequential_dependency"],
        diverse_opinions=profile["diverse_opinions"],
        max_members=profile["max_members"],
        budget=profile["budget"],
    )


@mcp.tool()
def swarm_plan_preview(
    profile: dict,
    explicit_mode: str = "",
    explicit_members: list[str] | None = None,
    allow_unenforced_read: bool = False,
) -> dict:
    """Recommend a four-mode plan from closed task facts; creates no room/process.

    This is a static advisory preview. It checks the configured CLI executable,
    enabled flag, model settings, and enforced read-only argv. It does not test
    provider authentication or ask a model to answer the task. Jev receives
    only the closed profile and safe candidate capability labels. The model
    class sent to Jev is a rough label inferred from configured reasoning
    effort; it is not a benchmark of the model's actual capability.
    """
    if not isinstance(profile, dict):
        raise ValueError("profile must be an object")
    if explicit_mode == "":
        explicit_mode = None
    candidates = _swarm_plan_candidates()
    explicit_kwargs = {
        "explicit_mode": explicit_mode,
        "explicit_members": explicit_members,
        "explicit_allow_unenforced_read": allow_unenforced_read,
    }
    # Validate the closed input before contacting Jev. This call is pure and
    # deliberately uses no room, process, or live registry-availability APIs.
    base = swarm_planner.build_plan(profile, candidates, **explicit_kwargs)
    eligible = [
        item for item in candidates
        if item["enabled"] and item["static_ok"]
        and (item["readonly_enforced"] or (
            allow_unenforced_read and profile.get("needs_files") == "read"
        ))
        and (
            profile["budget"] == "any"
            or (profile["budget"] == "cheap" and item["cost_class"] in {"free", "cheap"})
            or (profile["budget"] == "free" and item["cost_class"] == "free")
        )
    ]
    jev_facts = _swarm_jev_facts(profile)
    jev_mode = None
    jev_mode_confidence = None
    mode_reason = "organizer supplied the mode" if explicit_mode else "Jev not consulted"
    if explicit_mode is None and eligible and profile["needs_files"] != "write":
        try:
            result = swarm_jev.choose_mode(jev_facts)
        except Exception:
            result = None
        if result is not None:
            mode_reason = result.reason
            if result.status == "ok" and result.choice == "none":
                mode_reason = f"Jev chose none; deterministic profile rule used. {result.reason}"
            if result.status == "ok" and result.choice in swarm_planner.MODES:
                jev_mode = result.choice
                jev_mode_confidence = result.confidence
        else:
            mode_reason = "Jev request failed; deterministic mode fallback"

    effective_mode = explicit_mode or jev_mode or base["mode"]
    ordered_candidates = list(candidates)
    roster_lead = None
    roster_reason = "organizer supplied the participant list" if explicit_members is not None else "Jev not consulted"
    if explicit_members is None and len(eligible) >= 2 and profile["needs_files"] != "write":
        jev_candidates = []
        for item in eligible:
            harness = _SWARM_JEV_HARNESSES.get(item["cli_kind"].lower())
            if not harness:
                continue
            effort = (item["effort"] or "").lower()
            model_class = "fast" if effort in {"minimal", "low", "none"} else (
                "strong" if effort in {"high", "xhigh", "max", "ultra"} else "balanced"
            )
            jev_candidates.append(swarm_jev.VerifiedCandidate(
                candidate_id=item["id"],
                harness=harness,
                model_class=model_class,
                cost_class=item["cost_class"],
                readonly_enforced=item["readonly_enforced"],
            ))
            if len(jev_candidates) == 9:
                break
        if len(jev_candidates) >= 2:
            try:
                result = swarm_jev.choose_candidate(jev_facts, jev_candidates)
            except Exception:
                result = None
            if result is not None:
                roster_reason = result.reason
                if result.status == "ok" and result.choice == "none":
                    roster_reason = (
                        f"Jev chose none; eligible registry order used. {result.reason}"
                    )
                if result.status == "ok" and result.choice in {item.candidate_id for item in jev_candidates}:
                    roster_lead = result.choice
                    ordered_candidates.sort(key=lambda item: item["id"] != roster_lead)
            else:
                roster_reason = "Jev request failed; eligible registry order used"

    # A single Jev pick is the lead participant; Huddle fills the rest using
    # the verified registry order, bounded to two for a one-part council.
    effective_profile = dict(profile)
    roster_cap = profile["max_members"]
    if (explicit_members is None and effective_mode == "council"
            and profile["parts"] == "one"):
        roster_cap = min(roster_cap, 2)
        effective_profile["max_members"] = roster_cap
    jev_advice = {
        "confidence": jev_mode_confidence,
        "mode": jev_mode,
        "member_ids": [],
    } if jev_mode else None
    plan = swarm_planner.build_plan(
        effective_profile,
        ordered_candidates,
        jev_advice=jev_advice,
        **explicit_kwargs,
    )
    roster_note = ""
    if roster_lead:
        roster_note = (
            "Jev selected the first participant; Huddle filled the remaining places "
            "in eligible registry order."
        )
    if roster_cap < profile["max_members"]:
        cap_note = f"Roster capped at {roster_cap} for a one-participant council profile."
        roster_note = f"{roster_note} {cap_note}".strip()
    plan["jev"] = {
        "mode": {"choice": jev_mode, "reason": mode_reason},
        "first_participant": {"choice": roster_lead, "reason": roster_reason},
        "roster_note": roster_note,
        "model_class_note": (
            "Roughly inferred from configured reasoning effort; actual model "
            "capability and provider route were not tested."
        ),
    }
    plan["readiness"] = "static advisory only; provider authentication and response not verified"
    plan["side_effects"] = {"room_created": False, "child_processes_started": False}
    return plan


@mcp.tool()
def swarm_room_proposal(
    name: str,
    organizer: str,
    goal: str,
    requirements: dict,
    explicit_mode: str = "",
    explicit_members: list[str] | None = None,
    allow_unenforced_read: bool = False,
    cwd: str = "",
    check_cli_login: bool = True,
    check_exact_model: bool = False,
) -> dict:
    """Build a reviewable room proposal from a goal and closed requirements.

    The exact goal and requirements are retained in the local response. Only
    ``requirements`` and sanitized candidate facts reach Jev through
    ``swarm_plan_preview``; the goal, room name, organizer, and path do not.
    This tool never creates a room or starts a process. ``create_args`` is
    present only when the static plan is ready and contains ``start=False``.
    Login probing checks only native Claude/Codex login status; it does not
    verify the selected model, provider route, quota, or model response. Login
    probing is enabled by default and can be skipped with ``False``. The
    opt-in ``check_exact_model`` makes one short fixed sentinel request for
    each selected Claude/Codex model+effort route. It sends no room goal or
    repository data and fails closed when the route is not explicitly pinned.
    """
    for label, value, limit in (
        ("name", name, 200),
        ("organizer", organizer, 120),
        ("goal", goal, 5000),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} must be a non-empty string")
        if len(value) > limit:
            raise ValueError(f"{label} is too long (maximum {limit} characters)")
    if not isinstance(cwd, str) or len(cwd) > 4096:
        raise ValueError("cwd must be a string of at most 4096 characters")
    if not isinstance(check_cli_login, bool):
        raise ValueError("check_cli_login must be a boolean")
    if not isinstance(check_exact_model, bool):
        raise ValueError("check_exact_model must be a boolean")

    plan = swarm_plan_preview(
        requirements,
        explicit_mode=explicit_mode,
        explicit_members=explicit_members,
        allow_unenforced_read=allow_unenforced_read,
    )
    login_checks = {}
    exact_model_checks = {}
    if plan.get("status") == "planned":
        selected_ids = [member["id"] for member in plan.get("members", [])]
        if check_cli_login:
            specs_by_name = {
                spec.get("name"): spec for spec in spawn._raw_registry()
                if isinstance(spec.get("name"), str)
            }
            probe_cwd = cwd or tempfile.gettempdir()
            probed_routes = {}
            for member_id in selected_ids:
                spec = specs_by_name.get(member_id)
                command = spec.get("cmd", []) if spec else []
                route = spawn._effective_binary(command) if isinstance(command, list) else None
                route_key = ("binary", route) if route else ("profile", member_id)
                if route_key not in probed_routes:
                    if spec is None:
                        result = {"status": "unknown", "reason": "unsupported_harness"}
                    else:
                        try:
                            probe = spawn.probe_cli_login(spec, probe_cwd)
                        except Exception:
                            # Never expose exceptions that may contain local account or
                            # process details; the login probe contract is closed data.
                            probe = None
                        statuses = {"authenticated", "unauthenticated", "unknown"}
                        reasons = {
                            "cli_login_present", "cli_logged_out", "unsupported_harness",
                            "non_native_profile", "probe_unavailable", "probe_timeout",
                            "unrecognized_output",
                        }
                        if (not isinstance(probe, dict)
                                or probe.get("status") not in statuses
                                or probe.get("reason") not in reasons):
                            result = {"status": "unknown", "reason": "probe_unavailable"}
                        else:
                            result = {
                                "status": probe["status"], "reason": probe["reason"],
                            }
                    probed_routes[route_key] = result
                login_checks[member_id] = dict(probed_routes[route_key])
        else:
            login_checks = {
                member_id: {"status": "skipped", "reason": "caller_disabled"}
                for member_id in selected_ids
            }
        if check_exact_model:
            specs_by_name = {
                spec.get("name"): spec for spec in spawn._raw_registry()
                if isinstance(spec.get("name"), str)
            }
            probe_cwd = tempfile.gettempdir()
            for member_id in selected_ids:
                spec = specs_by_name.get(member_id)
                command = spec.get("cmd", []) if spec else []
                route = spawn._effective_binary(command) if isinstance(command, list) else None
                if route not in {"claude", "codex"}:
                    exact_model_checks[member_id] = {
                        "status": "unsupported", "reason": "unsupported_harness",
                    }
                    continue
                if spec is None:
                    probe = {"status": "unsupported", "reason": "unsupported_harness"}
                else:
                    try:
                        probe = spawn.probe_cli_model_response(spec, probe_cwd)
                    except Exception:
                        probe = None
                statuses = {"passed", "failed", "unsupported", "unknown"}
                reasons = {
                    "sentinel_response_received", "provider_request_failed",
                    "sentinel_response_not_received", "response_timeout",
                    "probe_unavailable", "unsupported_harness",
                    "invalid_model_settings", "model_effort_not_explicit",
                }
                if (not isinstance(probe, dict) or probe.get("status") not in statuses
                        or probe.get("reason") not in reasons):
                    exact_model_checks[member_id] = {
                        "status": "unknown", "reason": "probe_unavailable",
                    }
                else:
                    exact_model_checks[member_id] = {
                        "status": probe["status"], "reason": probe["reason"],
                    }
    create_args = None
    proposal_blockers = []
    if plan.get("status") == "planned":
        members = [member["id"] for member in plan.get("members", [])]
        failed_exact_routes = [
            member_id for member_id, check in exact_model_checks.items()
            if check["status"] != "passed"
        ]
        if failed_exact_routes:
            proposal_blockers.append(
                "exact Claude/Codex model preflight did not pass for: "
                + ", ".join(failed_exact_routes)
                + "; choose an explicitly pinned available route or disable exact-model preflight"
            )
        if organizer in members:
            proposal_blockers.append("organizer must not also be a participant")
        if {"Human", "System"}.intersection(members):
            proposal_blockers.append("reserved names cannot be participants")
        if not proposal_blockers:
            create_args = {
                "name": name,
                "organizer": organizer,
                "goal": goal,
                "mode": plan["mode"],
                "members": members,
                "cwd": cwd,
                "workspace_strategy": "shared_only",
                "start": False,
                "expected_specs": {
                    member["id"]: member["spec_fingerprint"]
                    for member in plan["members"]
                },
                # Audit metadata only; it is not approval or authorization.
                "plan_hash": plan["plan_hash"],
            }
    else:
        proposal_blockers.append("the static plan is not ready for room creation")

    return {
        "status": "ready_for_review" if create_args is not None else "not_ready",
        "name": name,
        "organizer": organizer,
        "goal": goal,
        "requirements": requirements,
        "plan": plan,
        "create_args": create_args,
        "proposal_blockers": proposal_blockers,
        "preflight": {
            "static_plan": plan.get("status", "unknown"),
            "cli_login": login_checks,
            "exact_model_provider_response": exact_model_checks or "not_checked",
        },
        "readiness": (
            "static advisory; native CLI login status checked"
            if check_cli_login else "static advisory; native CLI login status skipped"
        ) + (
            "; exact selected Claude/Codex model response checked with a sentinel"
            if check_exact_model else "; exact selected model response not checked"
        ),
        "side_effects": {"room_created": False, "child_processes_started": False},
    }


@mcp.tool()
@_serialize_swarm_create_by_request_id
def swarm_pilot_create(
    name: str,
    organizer: str,
    goal: str,
    mode: str,
    members: list[str],
    cwd: str = "",
    workspace_strategy: str = "shared_only",
    start: bool = True,
    client_request_id: str = "",
    expected_specs: dict[str, str] | None = None,
    plan_hash: str = "",
    write_policy: str = "read_only",
) -> dict:
    """Create a pilot room; start exact enabled registry members when requested.

    ``start=False`` prepares durable state without launching CLI workers. This
    is useful for a dry run and never implies that model work was performed.
    ``write_policy="read_only"`` (default) keeps members read-only discussants.
    ``write_policy="shared_write"`` requires ``cwd`` to equal the single
    admin-approved root in the server's ``MCP_HUDDLE_WRITE_ROOTS`` and be the
    canonical top of an existing local Git worktree; members may then edit
    files there (and nowhere else) with a bounded write mode derived from this
    room. Only Codex, and Claude with a loopback ``mcp_url`` plus the user's
    Edit/Write PreToolUse Guard hook, are accepted; anything else is rejected
    before any room is created. With
    ``workspace_strategy="allow_subworktrees"`` Huddle also creates one
    detached subworktree per member (under the Huddle home) as that member's
    cwd; the shared worktree stays writable for transferring changes.
    Supplying ``client_request_id`` makes retries idempotent for the exact
    request payload. ``expected_specs`` pins each selected profile to its
    current static registry fingerprint before creating a room. ``plan_hash``
    is stored for audit only; it does not grant permissions. For compatibility,
    response field ``started`` mirrors the ``start`` request; it does not prove
    that a child process launched or that a provider returned a response.
    """
    if not isinstance(client_request_id, str):
        raise ValueError("client_request_id must be a string")
    if client_request_id and not _SWARM_CLIENT_REQUEST_ID_RE.fullmatch(client_request_id):
        raise ValueError("client_request_id must be 1-128 ASCII letters, digits, dot, underscore, colon or hyphen")
    if not isinstance(plan_hash, str) or (plan_hash and not _SWARM_HASH_RE.fullmatch(plan_hash)):
        raise ValueError("plan_hash must be empty or a sha256 fingerprint")
    if not isinstance(start, bool):
        raise ValueError("start must be a boolean")
    if write_policy not in room_workspace.WRITE_POLICIES:
        raise ValueError(f"write_policy must be one of {sorted(room_workspace.WRITE_POLICIES)}")

    expected = _swarm_validate_expected_specs(
        members, expected_specs, check_registry=not bool(client_request_id),
    )
    request_fingerprint = ""
    deterministic_room_id = None
    if client_request_id:
        if not all(isinstance(value, str) for value in (name, organizer, goal, cwd, workspace_strategy, mode)):
            raise ValueError("idempotent request fields must be strings")
        if not isinstance(members, list) or not all(isinstance(member, str) for member in members):
            raise ValueError("members must be a list of participant names")
        request_fingerprint = _swarm_client_request_fingerprint(
            name, organizer, goal, mode, members, cwd, workspace_strategy, start,
            write_policy,
        )
        deterministic_room_id = _swarm_request_room_id(organizer, client_request_id)
        existing = _swarm_existing_request(
            deterministic_room_id, request_fingerprint, organizer, expected,
        )
        if existing is not None:
            if existing.pop("_resume_preparing", False):
                return _swarm_resume_preparing(
                    deterministic_room_id, organizer, members, expected, start,
                    bool(existing["availability"]["registry_availability_checked"]),
                )
            return existing

    # Check expected static registry fingerprints before the old live
    # availability probe or any persistent room creation.
    if client_request_id:
        expected = _swarm_validate_expected_specs(members, expected_specs)
    if start:
        unavailable = [name for name in members if spawn.get_enabled_spec(name) is None]
        if unavailable:
            raise ValueError(f"unavailable registry members: {unavailable}")
    if write_policy == room_workspace.SHARED_WRITE:
        if workspace_strategy not in swarm_pilot.WORKSPACES:
            raise ValueError(f"workspace_strategy must be one of {sorted(swarm_pilot.WORKSPACES)}")
        profiles = {spec.get("name"): spec for spec in spawn._raw_registry()}
        room_workspace.check_request(
            cwd, workspace_strategy, list(members),
            {member: profiles.get(member) for member in members},
        )
    registry_checked = bool(start or expected is not None)
    try:
        room_id = swarm_pilot.create(
            name, organizer, goal, mode, members, cwd, workspace_strategy,
            room_id=deterministic_room_id,
            client_request_fingerprint=request_fingerprint,
            plan_hash=plan_hash,
            start_requested=start if client_request_id else None,
            registry_availability_checked=(registry_checked if client_request_id else None),
            expected_specs=(expected if client_request_id else None),
        )
    except FileExistsError:
        if deterministic_room_id:
            existing = _swarm_existing_request(
                deterministic_room_id, request_fingerprint, organizer, expected,
            )
            if existing is not None:
                if existing.pop("_resume_preparing", False):
                    return _swarm_resume_preparing(
                        deterministic_room_id, organizer, members, expected, start,
                        bool(existing["availability"]["registry_availability_checked"]),
                    )
                return existing
            raise ValueError("partial_room: deterministic room directory already exists") from None
        raise
    if write_policy == room_workspace.SHARED_WRITE:
        # Before any invite or request: until this record exists the room is
        # read-only, so an interruption here can only fail closed.
        room_workspace.install(
            room_id, cwd, workspace_strategy, swarm_pilot.status(room_id)["member_ids"],
        )
    if client_request_id:
        _swarm_mark_create_state(room_id, "preparing", expected, start, registry_checked)
    for name in members:
        room_invite(room_id, name, by=organizer)
    dispatch = swarm_pilot_pump(room_id) if start else []
    if client_request_id and start and swarm_pilot.due_members(room_id):
        blocked = [
            item for item in dispatch
            if item.get("status") in {"spec_drift", "unavailable"}
        ]
        reason = ", ".join(item.get("status", "blocked") for item in blocked) or "pending members"
        raise ValueError(f"partial_room: swarm dispatch remains incomplete ({reason})")
    if client_request_id:
        dispatch = _swarm_initial_dispatch(room_id) if start else []
        _swarm_mark_create_state(
            room_id, "ready", expected, start, registry_checked, dispatch,
        )
    return {
        "room_id": room_id,
        "mode": mode,
        "dispatched": dispatch,
        "started": start,
        "start_requested": start,
        "reused": False,
        "availability": {
            "static_only": not registry_checked,
            "registry_availability_checked": registry_checked,
            "provider_response_verified": False,
        },
    }


@mcp.tool()
def swarm_pilot_status(room_id: str) -> dict:
    """Read pilot state and the current per-member runtime snapshot.

    ``last_seen_id`` is the cursor Huddle places in a wake prompt, not proof
    that a model read every message through that ID. Agent runtime fields stay
    in room ``agent_meta``; no native session is copied into pilot state.
    """
    state = swarm_pilot.status(room_id)
    agent_meta = bus.get_room_info(room_id).get("agent_meta") or {}
    if not isinstance(agent_meta, dict):
        agent_meta = {}
    details = []
    for member in state["members"]:
        info = agent_meta.get(member) or {}
        if not isinstance(info, dict):
            info = {}
        thread_id = info.get("thread_id")
        native_session = (
            {"kind": "codex_thread", "id": thread_id,
             "source": "agent_meta.thread_id"}
            if (_is_thread_resumable(member) and isinstance(thread_id, str)
                and thread_id) else None
        )
        wake_id = info.get("wake_id")
        initial_id = info.get("initial_spawn_id")
        if isinstance(wake_id, str) and wake_id:
            generation = {"id": wake_id, "source": "wake_id",
                          "claim_active": info.get("wake_claim_id") == wake_id}
        elif isinstance(initial_id, str) and initial_id:
            generation = {"id": initial_id, "source": "initial_spawn_id",
                          "claim_active": info.get("initial_spawn_active") is True}
        else:
            generation = None
        # These are server delivery offsets. Neither one is a read receipt.
        def cursor_value(field: str) -> int | None:
            value = info.get(field)
            return value if type(value) is int and value >= 0 else None

        cursor = {
            field: cursor_value(field)
            for field in ("last_wake_msg_id", "last_seen_id")
        }
        cursor["read_receipt"] = False
        raw_receipt = info.get("claude_model_receipt")
        model_receipt = None
        if isinstance(raw_receipt, dict):
            reported = raw_receipt.get("reported_model")
            receipt_generation = raw_receipt.get("generation")
            receipt_source = raw_receipt.get("source")
            if ((reported is None or (isinstance(reported, str)
                                      and re.fullmatch(r"claude-[A-Za-z0-9._-]{1,121}", reported)))
                    and receipt_source in ("assistant", "init", "mixed", "none")
                    and isinstance(receipt_generation, str)
                    and 0 < len(receipt_generation) <= 64):
                model_receipt = {
                    "reported_model": reported,
                    "source": receipt_source,
                    "claim_scope": "cli_reported_identifier",
                    "generation": receipt_generation,
                }
        member_detail = {
            "member_id": state["member_ids"][member],
            "name": member,
            "profile": _swarm_route_profile(info) or member,
            "native_session": native_session,
            "process_generation": generation,
            "delivery_cursor": cursor,
        }
        if model_receipt is not None:
            member_detail["last_model_receipt"] = model_receipt
        route = info.get("swarm_route")
        if isinstance(route, dict):
            member_detail["replacement"] = {
                "attempts": len(route.get("attempts") or []),
                "terminal": route.get("terminal"),
                "reason": route.get("reason"),
            }
        details.append(member_detail)
    return {**state, "members_detail": details}


def _swarm_pilot_request(room_id: str, member: str) -> str:
    state = swarm_pilot.status(room_id)
    mode = state["mode"]
    mode_instruction = {
        "council": "Read the WHOLE cumulative room history before answering. "
                   "Add a substantive view to earlier answers. The organizer speaks last.",
        "team": "Work on a distinct part. Declare your responsibility and discuss "
                "interfaces with peers. A member must claim reporter.",
        "relay": "Continue the previous member's result, explicitly accept the "
                 "handoff and your next responsibility. A member must claim reporter.",
        "swarm": "Self-assign a useful part of the shared goal. Announce uncovered "
                 "responsibilities to peers. A member must claim reporter.",
    }[mode]
    read_call = (
        "messages_read(room_id, since_id=0, limit=10000, max_chars=0)"
        if mode == "council" else "messages_read(room_id, since_id=0, limit=50)"
    )
    final_instruction = (
        " To claim reporter, call swarm_pilot_record(room_id, member, "
        "kind='responsibility', key='reporter', value='final reporter'). "
        "After round_done, end this CLI turn; do not poll or wait for peers. "
        "Huddle will wake the chosen reporter with a separate addressed final "
        "request after everyone is done. Only then call swarm_pilot_finish."
        if mode != "council" else
        " After round_done, end this CLI turn. The organizer will publish the final."
    )
    return (
        f"Huddle swarm pilot, mode={mode}, round={state['round']}. "
        f"You are {member}; peers are agents, not the human user.\n"
        f"Goal: {state['goal']}\n{mode_instruction}\n"
        f"Use {read_call} to read room context; "
        "use swarm_pilot_record for responsibility/task/decision/fact. "
        "Discuss disagreements substantively. Post your completed answer as "
        "message_post(kind='result', reply_to=<this request id>), then call "
        "swarm_pilot_round_done(room_id, member, summary). "
        "A message alone does not complete the round." + final_instruction
        + room_workspace.brief_note(bus.get_room_info(room_id), member)
    )


_PILOT_MEMBER_EXIT_DELAY_SECONDS = 1.5


def _stop_completed_pilot_turn(
    room_id: str, member: str, wake_id: str, completion: str,
) -> None:
    """Stop only this room/member wake after its pilot completion is durable.

    The child registry is the authority to signal. ``agent_meta.external`` is
    intentionally ignored: room_invite sets it even for registry-backed agents
    which Huddle later launches itself.
    """
    try:
        state = swarm_pilot.status(room_id)
        if completion == "round_done":
            if member not in state["done"]:
                return
        elif completion == "final":
            final = state.get("final") or {}
            if state.get("phase") != "completed" or final.get("member") != member:
                return
        else:
            return
        info = (bus.get_room_info(room_id).get("agent_meta") or {}).get(member) or {}
    except Exception:
        return
    if info.get("wake_id") != wake_id:
        return
    if info.get("wake_claim_id") not in (None, wake_id):
        return
    if child_processes.state(room_id, wake_id) != "alive":
        return
    _merge_agent_meta(room_id, member, {"intentional_stop_wake_id": wake_id})
    result = child_processes.terminate(room_id, wake_id)
    if result == "denied":
        print(f"[huddle] could not stop completed pilot turn "
              f"({member}@{room_id}, wake={wake_id})", flush=True)
    if result != "sent":
        # A vanished/unowned child was not intentionally signalled. Remove
        # only this generation's marker so a later wake cannot inherit it.
        def clear_marker(meta: dict) -> dict:
            current = (meta.get("agent_meta") or {}).get(member) or {}
            if (current.get("wake_id") == wake_id
                    and current.get("intentional_stop_wake_id") == wake_id):
                current.pop("intentional_stop_wake_id", None)
            return meta
        bus._update_meta_locked(room_id, clear_marker)


def _schedule_completed_pilot_turn_exit(
    room_id: str, member: str, state: dict, completion: str,
) -> None:
    """Defer SIGTERM until after the MCP tool has had time to return its reply.

    The round state and any final request are written before this is called.
    The exact-child reaper callback remains responsible for releasing claims.
    """
    if completion == "round_done" and len(state["done"]) >= len(state["members"]):
        return
    if completion == "final":
        final = state.get("final") or {}
        if state.get("phase") != "completed" or final.get("member") != member:
            return
    try:
        info = (bus.get_room_info(room_id).get("agent_meta") or {}).get(member) or {}
    except Exception:
        return
    wake_id = info.get("wake_id")
    if (not wake_id or child_processes.state(room_id, wake_id) != "alive"
            or info.get("wake_claim_id") not in (None, wake_id)):
        return
    try:
        timer = threading.Timer(
            _PILOT_MEMBER_EXIT_DELAY_SECONDS,
            _stop_completed_pilot_turn,
            args=(room_id, member, wake_id, completion),
        )
        timer.daemon = True
        timer.start()
    except Exception as exc:
        print(f"[huddle] could not schedule completed pilot turn exit "
              f"({member}@{room_id}): {exc}", flush=True)


@mcp.tool()
def swarm_pilot_pump(room_id: str) -> list[dict]:
    """Dispatch the next pilot turn(s) according to the room mode.

    Retries reuse the same idempotency key so a failed state write cannot
    duplicate a request. No room metadata lock is held while posting messages.
    """
    state = swarm_pilot.status(room_id)
    room_state = bus.get_room_info(room_id)
    dispatched: list[dict] = []
    for member in swarm_pilot.due_members(room_id):
        spec, drift = _member_launch_spec(room_state, member)
        if drift:
            dispatched.append({"member": member, "status": "spec_drift"})
            continue
        if spec is None:
            dispatched.append({"member": member, "status": "unavailable"})
            continue
        msg_id = message_post(
            room_id, state["organizer"], _swarm_pilot_request(room_id, member),
            "request", to=member,
            idempotency_key=f"swarm-pilot:{room_id}:{state['round']}:{member}",
        )
        swarm_pilot.mark_dispatched(room_id, member, msg_id)
        dispatched.append({"member": member, "request_id": msg_id,
                           "status": "dispatched"})
    return dispatched


def _swarm_final_request_key(room_id: str, state: dict) -> str:
    """Key of the final request for the current final owner.

    Each reporter transfer gets its own key, so the new reporter receives a
    fresh addressed request while the earlier one stays in history.
    """
    key = f"swarm-pilot:{room_id}:{state.get('round', 1)}:final-request"
    if state.get("mode") != "council":
        transfers = swarm_pilot.reporter_transfer_count(state)
        if transfers:
            key += f":transfer-{transfers}"
    return key


def _swarm_advance(room_id: str) -> dict:
    """Publish missing pilot requests after a tool turn or server restart.

    A completed round is already durable in room metadata. Publishing happens
    separately, so the watchdog must be able to repeat this step safely after
    a process stops between those writes. Message keys are the durable journal;
    an existing request is left to the ordinary pending-wake path.
    """
    room = bus.get_room_info(room_id)
    state = room.get("swarm_pilot")
    if (room.get("status") not in ("open", "idle")
            or not isinstance(state, dict) or state.get("phase") != "working"
            or state.get("server_create_state") == "preparing"):
        return {"next_dispatch": [], "final_request": None,
                "new_final_request": False}

    next_dispatch = swarm_pilot_pump(room_id)
    state = swarm_pilot.status(room_id)
    if len(state["done"]) != len(state["members"]):
        return {"next_dispatch": next_dispatch, "final_request": None,
                "new_final_request": False}

    round_no = state.get("round", 1)
    if state["mode"] == "council":
        recipient = state["organizer"]
        suffix = "final-request"
        body = (
            "All council members have spoken. Read their results and "
            "publish the combined result with swarm_pilot_finish(room_id, "
            "member, result)."
        )
    else:
        reporter = state["responsibilities"].get("reporter", {}).get("member")
        if reporter:
            recipient = reporter
            suffix = "final-request"
            body = (
                "All members have completed the round. Read their results and "
                "publish the combined result with swarm_pilot_finish(room_id, "
                "member, result)."
            )
        else:
            recipient = "all"
            suffix = "final-request-missing-reporter"
            body = (
                "All members have completed the round, but no reporter is claimed. "
                "One member must claim the reporter responsibility with "
                "swarm_pilot_record(room_id, member, 'responsibility', 'reporter', '<description>')."
            )
    key = (_swarm_final_request_key(room_id, state) if suffix == "final-request"
           else f"swarm-pilot:{room_id}:{round_no}:{suffix}")
    existing = next(
        (msg for msg in bus._load_messages(room_id)
         if msg.get("idempotency_key") == key), None,
    )
    if existing is not None:
        if (existing.get("agent") != "System"
                or existing.get("kind") != "request"
                or existing.get("to") != recipient):
            raise ValueError("pilot final-request key belongs to another message")
        return {"next_dispatch": next_dispatch, "final_request": existing["id"],
                "new_final_request": False}
    final_request = message_post(
        room_id, "System", body, "request", to=recipient,
        idempotency_key=key,
    )
    return {"next_dispatch": next_dispatch, "final_request": final_request,
            "new_final_request": True}


def _recover_swarm_pilots() -> list[dict]:
    """Advance started, open pilots whose process stopped before publication."""
    recovered: list[dict] = []
    for room in bus.list_rooms():
        state = room.get("swarm_pilot")
        if (room.get("status") not in ("open", "idle")
                or not isinstance(state, dict)
                or state.get("server_create_state") == "preparing"):
            continue
        room_id = room.get("id")
        if not isinstance(room_id, str):
            continue
        try:
            if state.get("phase") == "completed" and state.get("final"):
                final_message = _post_swarm_final_if_missing(room_id, state)
                if final_message is not None:
                    recovered.append({"room_id": room_id,
                                      "final_message": final_message})
                continue
            if (state.get("phase") != "working"
                    or not (state.get("start_requested") is True
                            or state.get("dispatched") or state.get("done"))):
                continue
            advanced = _swarm_advance(room_id)
            dispatched = [item for item in advanced["next_dispatch"]
                          if item.get("status") == "dispatched"]
            blocked = []
            for item in advanced["next_dispatch"]:
                if item.get("status") not in {"unavailable", "spec_drift"}:
                    continue
                member, reason = item["member"], item["status"]
                key = (f"swarm-pilot:{room_id}:{state.get('round', 1)}:"
                       f"{member}:blocked:{reason}")
                existing = next(
                    (msg for msg in bus._load_messages(room_id)
                     if msg.get("idempotency_key") == key), None,
                )
                if existing is not None:
                    if (existing.get("agent") != "System"
                            or existing.get("kind") != "system"):
                        raise ValueError("pilot blocked key belongs to another message")
                    continue
                detail = (
                    "The pinned member profile changed; dispatch is paused. "
                    "Restore the original profile to continue."
                    if reason == "spec_drift" else
                    "The member profile is unavailable; dispatch is paused. "
                    "Enable the profile to continue."
                )
                event_id = message_post(
                    room_id, "System", f"Swarm member {member}: {detail}",
                    "system", idempotency_key=key,
                )
                blocked.append({"member": member, "status": reason,
                                "event_id": event_id})
            if dispatched or advanced["new_final_request"] or blocked:
                event = {"room_id": room_id, "next_dispatch": dispatched,
                         "final_request": advanced["final_request"]
                         if advanced["new_final_request"] else None}
                if blocked:
                    event["blocked"] = blocked
                recovered.append(event)
        except Exception as exc:
            # One malformed/removed room must not prevent other rooms from
            # recovering on this tick. The next tick can retry this room.
            print(f"[watchdog] swarm advance failed ({room_id}): {exc}",
                  flush=True)
    return recovered


def _post_swarm_final_if_missing(room_id: str, state: dict) -> int | None:
    """Complete the final-request reply after a crash between state and post."""
    final = state.get("final") or {}
    member, result = final.get("member"), final.get("result")
    if not member or not result:
        return None
    messages = bus._load_messages(room_id)
    key = _swarm_final_request_key(room_id, state)
    request = next((msg for msg in messages
                    if msg.get("idempotency_key") == key
                    and msg.get("kind") == "request"
                    and msg.get("to") == member), None)
    if request is None:
        raise ValueError("final request has not been delivered to this member")
    final_key = f"swarm-pilot:{room_id}:final"
    existing = next((msg for msg in messages
                     if msg.get("idempotency_key") == final_key), None)
    if existing is not None:
        if (existing.get("agent") != member or existing.get("kind") != "final"
                or existing.get("body") != result
                or existing.get("reply_to") not in (None, request["id"])):
            raise ValueError("pilot final key belongs to another message")
        # Pilots completed before final replies carried reply_to have an
        # append-only final message. The pending queue recognizes that legacy
        # settlement without rewriting history or publishing a second final.
        return None
    return message_post(
        room_id, member, result, "final", to=state["organizer"],
        reply_to=request["id"], idempotency_key=final_key,
    )


def _swarm_spec_drift(
    room_state: dict, member: str, spec: dict | None,
) -> bool:
    """Fail closed when a pinned participant no longer matches its profile."""
    pilot = room_state.get("swarm_pilot")
    if not isinstance(pilot, dict) and isinstance(room_state.get("expected_specs"), dict):
        # swarm_pilot.status returns the pilot payload, while bus.get_room_info
        # returns the enclosing room metadata object.
        pilot = room_state
    expected_specs = pilot.get("expected_specs") if isinstance(pilot, dict) else None
    if expected_specs is None:
        return False
    expected = expected_specs.get(member) if isinstance(expected_specs, dict) else None
    if not isinstance(expected, str) or spec is None:
        return True
    try:
        return spawn.spec_fingerprint(spec) != expected
    except Exception:
        return True


# ── Swarm member replacement ────────────────────────────────────────────────
# agent_meta[member]["swarm_route"] records which registry profile currently
# runs a pilot member. The member name stays the room identity (messages, log
# path, receipts); only the launched profile changes.


def _swarm_route_profile(info: dict) -> str | None:
    route = info.get("swarm_route") if isinstance(info, dict) else None
    profile = route.get("profile") if isinstance(route, dict) else None
    return profile if isinstance(profile, str) and profile else None


def _member_launch_spec(meta: dict, agent_name: str) -> tuple[dict | None, bool]:
    """Return (enabled spec, drift) for the profile currently running a member."""
    info = (meta.get("agent_meta") or {}).get(agent_name) or {}
    profile = _swarm_route_profile(info)
    if profile is None:
        spec = spawn.get_enabled_spec(agent_name)
        return spec, _swarm_spec_drift(meta, agent_name, spec)
    spec = spawn.get_enabled_spec(profile)
    if spec is None or spec.get("swarm_replacement") is not True:
        return None, True
    try:
        drift = spawn.spec_fingerprint(spec) != info["swarm_route"].get("fingerprint")
    except Exception:
        drift = True
    return spec, drift


def _swarm_route_attempt(name: str, spec: dict | None) -> dict:
    spec = spec or {}
    cmd = spec.get("cmd") or [name]
    return {"route": name, "harness": Path(str(cmd[0])).name,
            "model": str(spec.get("model") or ""), "provider": spec.get("provider")}


def _swarm_replacement_candidates(meta: dict, agent_name: str, writes: bool) -> list[dict]:
    """Enabled registry profiles explicitly marked ``swarm_replacement: true``.

    In a write room a candidate qualifies only when this room's workspace
    policy validates it for the member; otherwise it is excluded.
    """
    candidates = []
    for spec in spawn.load_registry():
        name = spec.get("name")
        if (not spec.get("enabled") or spec.get("swarm_replacement") is not True
                or not isinstance(name, str) or name == agent_name):
            continue
        can_limit = True
        if writes:
            try:
                room_workspace.launch(meta, agent_name, spec)
            except Exception:
                can_limit = False
        candidates.append({
            **_swarm_route_attempt(name, spec), "available": True,
            "quality": spec.get("swarm_quality", 0.5),
            "reliability": spec.get("swarm_reliability", 0.5),
            "cost": spec.get("swarm_cost", 0.5),
            "can_limit_writes": can_limit,
        })
    return candidates


def _swarm_route_cas(room_id: str, agent_name: str, wake_id: str, apply) -> bool:
    """Apply ``apply(info)`` only while ``wake_id`` still owns wake and claim."""
    applied = False

    def _update(meta: dict) -> dict:
        nonlocal applied
        info = (meta.get("agent_meta") or {}).get(agent_name)
        if (not isinstance(info, dict) or info.get("wake_id") != wake_id
                or info.get("wake_claim_id") != wake_id):
            return meta
        apply(info)
        applied = True
        return meta

    bus._update_meta_locked(room_id, _update)
    return applied


def _swarm_replace_failed_member(
    room_id: str, agent_name: str, wake_id: str, info: dict,
    rc: int, final_phase: str, rate_limit_announced: bool,
) -> bool:
    """Continue a failed pilot member's request on one backup profile.

    Runs inside the exact child's exit callback while ``wake_id`` still holds
    the persisted claim. The route change and the claim hand-off to the new
    generation are one meta-lock CAS, so no other server can claim in between.
    Returns True when a replacement generation now owns the member.
    """
    meta = bus.get_room_info(room_id)
    pilot = meta.get("swarm_pilot")
    req = int(info.get("last_wake_msg_id", 0) or 0)
    if (meta.get("status") not in ("open", "idle") or not isinstance(pilot, dict)
            or pilot.get("phase") != "working"
            or agent_name not in pilot.get("members", []) or not req):
        return False
    messages = bus._load_messages(room_id)
    request = next((m for m in messages if m.get("id") == req), None)
    if (not isinstance(request, dict) or request.get("kind") != "request"
            or request.get("reply_to") is not None
            or request.get("to") not in (agent_name, "all")
            or _swarm_pilot_request_superseded(room_id, request, pilot, messages)):
        return False

    route = info.get("swarm_route") if isinstance(info.get("swarm_route"), dict) else {}
    current_profile = _swarm_route_profile(info) or agent_name
    attempts = list(route.get("attempts") or []) or [
        _swarm_route_attempt(agent_name, spawn.get_enabled_spec(agent_name))]
    try:
        writes = room_workspace.write_policy(meta) == room_workspace.SHARED_WRITE
    except ValueError:
        writes = True  # invalid record: only a validated launch may proceed
    member_id = swarm_pilot.status(room_id)["member_ids"].get(agent_name)
    responsibility = ", ".join(
        f"{key}: {item.get('value', '')}"
        for key, item in (pilot.get("responsibilities") or {}).items()
        if isinstance(item, dict) and item.get("member") == agent_name
    )
    failure = {"text": _log_tail(room_id, agent_name),
               "waiting_for": info.get("waiting_for")}
    if rate_limit_announced:
        failure["kind"] = "quota"
    if final_phase == "stuck":
        failure["progress"] = False
    candidates = _swarm_replacement_candidates(meta, agent_name, writes)
    plan = swarm_replacement.plan_replacement(
        failure, attempts,
        {"member_id": member_id, "responsibility": responsibility,
         "harness": attempts[-1].get("harness"), "write_rights": writes},
        candidates,
        child_stopped=child_processes.state(room_id, wake_id) == "exited",
    )
    now = int(time.time())
    base = {"task_id": req, "failed_wake_id": wake_id, "updated_at": now,
            "failure_class": plan["failure_class"], "reason": plan["reason"]}

    if plan["action"] != "replace":
        if not route and (plan["failure_class"] == "unknown" or (
                not candidates and plan["action"] != "needs_user")):
            # Replacement is not configured or not applicable: the ordinary
            # noreply / rate-limit notice already explains this failure.
            return False

        def record_outcome(slot: dict) -> None:
            slot["swarm_route"] = {**route, **base, "profile": route.get("profile"),
                                   "attempts": attempts, "terminal": plan["action"]}
        if _swarm_route_cas(room_id, agent_name, wake_id, record_outcome):
            prefix = ("Ждёт решения человека" if plan["action"] == "needs_user"
                      else "Замена не выполнена")
            _swarm_replacement_notice(
                room_id, agent_name, wake_id,
                f"{agent_name} ({current_profile}): {prefix} — {plan['reason']} "
                f"[{plan['failure_class']}].")
        return False

    profile = plan["candidate"]["route"]
    spec = spawn.get_enabled_spec(profile)
    if spec is None:
        return False
    try:
        fingerprint = spawn.spec_fingerprint(spec)
    except Exception:
        return False
    new_wake = uuid.uuid4().hex[:12]

    def hand_off(slot: dict) -> None:
        slot["swarm_route"] = {
            **base, "profile": profile, "fingerprint": fingerprint,
            "generation": new_wake, "terminal": None,
            "attempts": attempts + [_swarm_route_attempt(profile, spec)],
        }
        # Transfer the claim directly: it is never released in between.
        slot.update({
            "wake_claim_id": new_wake, "wake_claim_msg_id": req,
            "wake_claimed_at": now, "wake_id": new_wake,
            "last_wake_msg_id": req, "last_wake_pid": None,
            "rate_limited_until": 0,
        })
        # The previous profile's native session and settings do not transfer.
        for field in ("thread_id", "model_settings"):
            slot.pop(field, None)

    note = (f"\n\n[Huddle] You continue as member {agent_name} (member_id "
            f"{member_id}) on profile {profile} because the previous route "
            f"{current_profile} failed ({plan['failure_class']}). Keep the same "
            f"responsibility{': ' + responsibility if responsibility else ''}. "
            "Read the room history before answering this same request.")
    prompt = _build_registry_agent_wakeup_prompt(
        room_id, agent_name, request.get("agent", ""),
        request.get("body", "") + note, request.get("to"), req,
        int(info.get("last_seen_id", 0) or 0),
        bus.read_messages(room_id, since_id=0, limit=50),
    )
    # Finish every fallible prompt read before transferring the persisted
    # claim; once transferred, only the exact new generation may release it.
    if not _swarm_route_cas(room_id, agent_name, wake_id, hand_off):
        return False  # a newer generation already owns this member
    try:
        _spawn_fresh_room_agent(room_id, agent_name, prompt,
                                bus.get_room_info(room_id), msg_id=req,
                                wake_id=new_wake)
    except Exception as exc:
        _clear_wake_claim(room_id, agent_name, new_wake, rollback=True)

        def mark_failed(meta: dict) -> dict:
            slot = (meta.get("agent_meta") or {}).get(agent_name)
            current = slot.get("swarm_route") if isinstance(slot, dict) else None
            if isinstance(current, dict) and current.get("generation") == new_wake:
                current.update({"terminal": "terminal",
                                "reason": f"replacement launch failed: {exc}"[:300]})
            return meta
        bus._update_meta_locked(room_id, mark_failed)
        _announce_spawn_failure(room_id, agent_name, exc, f"swarm-replace:{new_wake}")
        return False
    _swarm_replacement_notice(
        room_id, agent_name, new_wake,
        f"{agent_name} продолжает через {profile}: маршрут {current_profile} "
        f"завершился сбоем [{plan['failure_class']}]. Тот же участник "
        f"{member_id}, тот же запрос #{req}.")
    return True


def _swarm_replacement_notice(room_id: str, agent_name: str, generation: str,
                              body: str) -> None:
    try:
        _post_message_checked(
            room_id, "System", body, kind="system",
            idempotency_key=f"swarm-replace:{room_id}:{agent_name}:{generation}",
        )
    except Exception as exc:
        print(f"[huddle] swarm replacement notice failed "
              f"({agent_name}@{room_id}): {exc}", flush=True)


@mcp.tool()
def swarm_pilot_record(
    room_id: str, member: str, kind: str, key: str, value: str,
) -> dict:
    """Record a responsibility, task, decision or fact in the pilot room."""
    updated = swarm_pilot.record(room_id, member, kind, key, value)

    advanced = _swarm_advance(room_id) if (
        kind == "responsibility" and key == "reporter"
        and updated["mode"] != "council"
        and len(updated["done"]) == len(updated["members"])
    ) else None
    return {**updated, "final_request": advanced["final_request"] if advanced else None}


@mcp.tool()
def swarm_pilot_transfer(
    room_id: str, member: str, key: str, to_member: str, reason: str,
) -> dict:
    """Transfer a claimed responsibility (e.g. reporter) to another member.

    The owner may hand it off; another member may take it over only after the
    round is complete and the owner is not running a turn. The move is kept in
    ``transfers``. A reporter transfer sends the new reporter a fresh final
    request. Council keeps the organizer's final word and refuses reporter.
    """
    updated = swarm_pilot.transfer_responsibility(room_id, member, key, to_member, reason)
    transfer = updated["transfers"][-1]
    try:
        _post_message_checked(
            room_id, "System",
            f"Responsibility '{key}' moved from {transfer['from']} to {to_member} "
            f"(by {member}): {reason}",
            kind="system",
            idempotency_key=(f"swarm-pilot:{room_id}:transfer:{key}:"
                             f"{transfer['version']}"),
        )
    except Exception as exc:
        print(f"[huddle] swarm transfer notice failed ({room_id}): {exc}",
              flush=True)
    advanced = _swarm_advance(room_id) if (
        key == "reporter" and len(updated["done"]) == len(updated["members"])
    ) else None
    return {**updated, "final_request": advanced["final_request"] if advanced else None}


@mcp.tool()
def swarm_pilot_round_done(room_id: str, member: str, summary: str) -> dict:
    """Consciously finish this member's turn, then wake the next if sequential."""
    state = swarm_pilot.status(room_id)
    request_id = state["dispatched"].get(member)
    if request_id is None:
        raise ValueError("member has no dispatched request")
    messages = bus._load_messages(room_id)
    valid_request_ids = {request_id}
    # An organizer may issue a direct recovery request after a provider error
    # (for example, retrying a wake that failed before the member could use
    # Huddle tools). The member's result belongs to that retry request, not the
    # original dispatch. Only organizer-authored requests explicitly addressed
    # to this member qualify; peer chatter cannot complete another task.
    valid_request_ids.update(
        int(msg["id"])
        for msg in messages
        if int(msg.get("id", 0) or 0) > int(request_id)
        and msg.get("kind") == "request"
        and msg.get("reply_to") is None
        and msg.get("agent") == state["organizer"]
        and msg.get("to") == member
    )
    delivered = any(
        msg.get("agent") == member and msg.get("kind") == "result"
        and msg.get("reply_to") in valid_request_ids
        for msg in messages
    )
    if not delivered:
        raise ValueError(
            "member must post a result for the pilot request or an organizer's "
            "direct recovery request first"
        )
    updated = swarm_pilot.round_done(room_id, member, summary)
    advanced = _swarm_advance(room_id)
    _schedule_completed_pilot_turn_exit(room_id, member, updated, "round_done")
    return {"state": updated, "next_dispatch": advanced["next_dispatch"],
            "final_request": advanced["final_request"]}


@mcp.tool()
def swarm_pilot_finish(room_id: str, member: str, result: str) -> dict:
    """Record the council organizer's last word or the swarm reporter's final."""
    state = swarm_pilot.status(room_id)
    final_request_key = _swarm_final_request_key(room_id, state)
    if not any(
        msg.get("idempotency_key") == final_request_key
        and msg.get("to") == member
        for msg in bus._load_messages(room_id)
    ):
        raise ValueError("final request has not been delivered to this member")
    updated = swarm_pilot.finish(room_id, member, result)
    _post_swarm_final_if_missing(room_id, updated)
    _schedule_completed_pilot_turn_exit(room_id, member, updated, "final")
    return updated


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
    if meta.get("status") not in ("open", "idle"):
        raise ValueError("Room does not accept agent responses")
    agent_meta = meta.setdefault("agent_meta", {})
    info = agent_meta.get(agent_name)
    if not info and agent_name not in meta.get("participants", []):
        raise ValueError(f"Agent {agent_name} not in room {room_id} (not invited or auto_spawn'd?)")
    if not info:
        info = {}
        agent_meta[agent_name] = info

    status = bus.get_status(room_id).get(agent_name)
    if _wake_in_progress(info, status, room_id):
        raise ValueError(f"Agent {agent_name} already has an active process claim")

    if _is_thread_resumable(agent_name):
        thread_id = info.get("thread_id")
        if not thread_id:
            raise ValueError(
                f"No thread_id captured for Codex in room {room_id} — "
                "spawn may have failed or thread.started event was missed."
            )
        canonical_log, canonical_last = bus._agent_paths(
            room_id, agent_name, create=False,
        )
        log_path = str(canonical_log)
        last_msg_path = str(canonical_last)
        cwd = meta.get("cwd", "") or ""
        wake_id = uuid.uuid4().hex[:12]
        if not _claim_explicit_wake(room_id, agent_name, wake_id):
            raise ValueError(f"Agent {agent_name} already has an active process claim")
        _set_agent_phase(room_id, agent_name, "starting")
        try:
            resume_settings = info.get("model_settings")
            resume_kwargs = (
                {"model_settings": resume_settings}
                if isinstance(resume_settings, dict) else {}
            )
            cwd, write_roots = room_workspace.resume(meta, agent_name)
            if write_roots is not None:
                resume_kwargs["workspace_write_roots"] = write_roots
            pid = spawn.codex_resume(
                thread_id, prompt, cwd, log_path, last_msg_path,
                on_exit=_make_wake_done_callback(
                    room_id, agent_name, wake_id),
                owner_room_id=room_id, process_handle=wake_id,
                **resume_kwargs,
            )
        except Exception:
            _set_agent_phase(room_id, agent_name, "unavailable")
            _clear_wake_claim(room_id, agent_name, wake_id, rollback=True)
            raise
        _publish_wake_started(room_id, agent_name, wake_id, {
            "last_wake_pid": pid,
            "last_wake_at": int(time.time()),
            "wake_id": wake_id,
        })
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
    wake_id = uuid.uuid4().hex[:12]
    if not _claim_explicit_wake(room_id, agent_name, wake_id):
        raise ValueError(f"Agent {agent_name} already has an active process claim")
    _set_agent_phase(room_id, agent_name, "starting")
    try:
        pid, log_path, last_msg_path = _spawn_fresh_room_agent(
            room_id, agent_name, full_prompt, meta,
            msg_id=None, wake_id=wake_id,
        )
    except Exception:
        _clear_wake_claim(room_id, agent_name, wake_id, rollback=True)
        raise
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

_AGENT_REPORTED_PHASES = frozenset({"thinking", "working", "responding"})
_ACTIVE_PHASES = frozenset({"queued", "starting", "thinking", "working", "responding"})
_TERMINAL_PHASES = frozenset({"completed", "unavailable", "rate_limited", "stuck"})
_SERVER_FAILURE_PHASES = frozenset({"unavailable", "rate_limited", "stuck"})

@mcp.tool()
def status_set(
    room_id: str,
    agent: str,
    phase: str,
    task_id: int | str = "",
    detail: str = "",
    expires_in_sec: int = 0,
    session_id: str = "",
) -> str:
    """Report your current lifecycle phase to the room.

    Agents may report thinking, working, or responding. The server owns queued,
    starting, completed, unavailable, rate_limited, and stuck transitions.
    expires_in_sec > 0 keeps the underlying lease behavior for callers that
    want an automatic online reset.
    """
    if phase not in _AGENT_REPORTED_PHASES:
        raise ValueError(
            f"Agent phase must be one of {sorted(_AGENT_REPORTED_PHASES)}, got {phase!r}"
        )
    info = bus.get_room_info(room_id)
    if agent not in info.get("participants", []):
        raise ValueError(f"Agent {agent!r} is not a participant in {room_id}")
    operational = "busy"
    bus.set_status(
        room_id, agent, operational, expires_in_sec, session_id,
        phase=phase, task_id=task_id, detail=detail, source="agent",
    )
    return "ok"


def status_get(room_id: str) -> dict:
    """Get all agent statuses in a room (expired leases auto-reset to online)."""
    return bus.get_status(room_id)


def _agent_phase_snapshot(
    status_info: dict, wake_info: dict, room_id: str = "",
) -> tuple[str, dict]:
    status = status_info.get("status", "offline")
    health = _agent_wake_health(wake_info, status, room_id)
    phase = status_info.get("phase", "online")
    if health.get("claim_active") and phase not in _ACTIVE_PHASES:
        phase = "starting"
    elif health.get("rate_limited"):
        phase = "rate_limited"
    elif (health.get("stale_lease") and phase in _ACTIVE_PHASES
          and status_info.get("source") != "agent"):
        phase = "unavailable"
    return phase, health


def _server_terminal_failure_task_ids(status_info: dict, wake_info: dict) -> set[str]:
    """Return request ids settled by server-owned terminal failure receipts.

    The current failure is included for compatibility with status records that
    predate persisted receipts. Agent-reported status is never treated as a
    failure receipt.
    """
    receipts = status_info.get("terminal_failure_receipts", [])
    task_ids = {
        str(receipt.get("task_id", ""))
        for receipt in receipts if isinstance(receipt, dict)
        if receipt.get("source") == "server" and receipt.get("task_id", "") != ""
    }
    phase, _ = _agent_phase_snapshot(status_info, wake_info)
    if (
        status_info.get("source") == "server"
        and phase in _SERVER_FAILURE_PHASES
        and status_info.get("task_id", "") != ""
    ):
        task_ids.add(str(status_info["task_id"]))
    # A live Swarm replacement generation reopened exactly one failed request.
    # The old receipt stays as diagnostics; it settles again only if the
    # replacement route itself ends terminally.
    route = wake_info.get("swarm_route") if isinstance(wake_info, dict) else None
    if isinstance(route, dict) and not route.get("terminal") and route.get("task_id"):
        task_ids.discard(str(route["task_id"]))
    return task_ids


def _swarm_pilot_request_superseded(
    room_id: str, message: dict, pilot: dict | None,
    messages: list[dict] | None = None,
) -> bool:
    """Whether durable pilot state has replaced this request's work."""
    if not isinstance(pilot, dict) or message.get("kind") != "request":
        return False
    prefix = f"swarm-pilot:{room_id}:{pilot.get('round', 1)}:"
    key = message.get("idempotency_key")
    if not isinstance(key, str) or not key.startswith(prefix):
        return False
    suffix = key[len(prefix):]
    if suffix == "final-request-missing-reporter":
        return bool(pilot.get("responsibilities", {}).get("reporter", {}).get("member"))
    if suffix.startswith("final-request") and key != _swarm_final_request_key(room_id, pilot):
        # An earlier reporter's final request, replaced by a transfer.
        return True
    if suffix.startswith("final-request") and pilot.get("phase") == "completed":
        final_member = (pilot.get("final") or {}).get("member")
        return any(
            msg.get("idempotency_key") == f"swarm-pilot:{room_id}:final"
            and msg.get("agent") == final_member and msg.get("kind") == "final"
            for msg in (messages or [])
        )
    return suffix in pilot.get("members", []) and suffix in pilot.get("done", {})


def _pending_requests(
    room_id: str,
    participants: list[str],
    terminal_tasks: dict[str, set[str]] | None = None,
    pilot: dict | None = None,
) -> list[dict]:
    """Return unanswered request work, including agents still expected.

    A terminal lifecycle phase only settles the request recorded in its
    ``task_id``. Progress messages (ack/busy/comment) are not a receipt;
    a stored result/final reply settles ordinary work. Durable pilot completion
    also supersedes its initial dispatch and the reporter-claim broadcast.
    """
    terminal_tasks = terminal_tasks or {}
    messages = bus._load_messages(room_id)
    replies_by_request: dict[int, set[str]] = {}
    for message in messages:
        reply_to = message.get("reply_to")
        if reply_to is not None and message.get("kind") in {"result", "final"}:
            replies_by_request.setdefault(int(reply_to), set()).add(message.get("agent", ""))

    pending: list[dict] = []
    for message in messages:
        if message.get("kind") != "request" or message.get("reply_to") is not None:
            continue
        if _swarm_pilot_request_superseded(room_id, message, pilot, messages):
            continue
        to = message.get("to")
        if to and to != "all":
            targets = [to] if to in participants else []
        else:
            targets = [name for name in participants if name != message.get("agent")]
        waiting_for = [name for name in targets
                       if str(message["id"]) not in terminal_tasks.get(name, set())
                       and name not in replies_by_request.get(int(message["id"]), set())]
        if not waiting_for:
            continue
        pending.append({
            "id": int(message["id"]),
            "from": message.get("agent", ""),
            "to": to or "all",
            "body": message.get("body", "")[:240],
            "created_at": int(message.get("timestamp", 0) or 0),
            "waiting_for": waiting_for,
        })
    return pending


@mcp.tool()
def room_status(room_id: str) -> dict:
    """Return an actionable lifecycle snapshot for orchestrators.

    ``wait_recommended`` is true while an agent is starting/working/responding
    or while a request still has one or more expected replies. A live process
    is never treated as completed merely because its output log is quiet.
    """
    meta = bus.get_room_info(room_id)
    participants = list(meta.get("participants", []))
    agent_meta = meta.get("agent_meta", {}) or {}
    status_details = bus.get_status_details(room_id)
    terminal_tasks: dict[str, set[str]] = {}
    for name, status_info in status_details.items():
        task_ids = _server_terminal_failure_task_ids(
            status_info, agent_meta.get(name) or {}
        )
        if task_ids:
            terminal_tasks[name] = task_ids
    pending = _pending_requests(
        room_id, participants, terminal_tasks, meta.get("swarm_pilot"),
    )
    pending_by_agent = {
        name: [item["id"] for item in pending if name in item["waiting_for"]]
        for name in set(participants) | set(agent_meta) | set(status_details)
    }

    agents: dict[str, dict] = {}
    for name in pending_by_agent:
        info = agent_meta.get(name) or {}
        status_info = dict(status_details.get(name) or {
            "status": "offline", "phase": "unavailable", "updated_at": 0,
        })
        pid = info.get("last_wake_pid")
        process_state = _owned_process_state(room_id, info) if pid else "unknown"
        process_alive = process_state == "alive"
        phase, health = _agent_phase_snapshot(status_info, info, room_id)
        agents[name] = {
            **status_info,
            "phase": phase,
            "process_alive": process_alive,
            "process_state": process_state,
            "pending_request_ids": pending_by_agent[name],
            "health": health,
        }

    active = any(
        agent.get("phase") in _ACTIVE_PHASES
        or (agent.get("health") or {}).get("claim_active")
        for agent in agents.values()
    )
    waiting = bool(pending) or active
    return {
        "room_id": room_id,
        "room_status": meta.get("status", "unknown"),
        "participants": participants,
        "agents": agents,
        "pending_requests": pending,
        "wait_recommended": waiting,
        "all_terminal": not waiting,
    }


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
    pilot = info.get("swarm_pilot")
    if (kind == "result" and isinstance(pilot, dict)
            and pilot.get("phase") == "completed" and reply_to is not None):
        # Final requests can wake a member just as another member publishes
        # the room final. Discard only a late answer to one of those automatic
        # pilot-final requests; ordinary follow-up requests in a completed
        # room remain usable.
        final_request = next((msg for msg in bus._load_messages(room_id)
                              if msg.get("id") == reply_to), None)
        if (isinstance(final_request, dict)
                and ":final-request" in str(
                    final_request.get("idempotency_key") or "")):
            raise ValueError(
                "pilot is already completed; late final-request result discarded"
            )
    if info.get("status") == "idle" and kind == "request":
        bus.revive(room_id)
    # reply_to is validated inside bus.post_message under the messages lock —
    # atomic with the append, so a duplicate reply cannot race through the gap.
    msg_id = bus.post_message(
        room_id, agent, body, kind, to, reply_to, idempotency_key, msg_meta=meta,
    )
    if kind in ("result", "final"):
        _set_agent_phase(room_id, agent, "completed", task_id=reply_to or "")
    return msg_id


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
            reconciled = _reconcile_owned_children()
            if reconciled:
                print(f"[watchdog] Reconciled terminal-room children: "
                      f"{reconciled}", flush=True)
        except Exception as e:
            print(f"[watchdog] child reconciliation error: {e}", flush=True)

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
            advanced = _recover_swarm_pilots()
            if advanced:
                print(f"[watchdog] Recovered swarm requests: {advanced}",
                      flush=True)
        except Exception as e:
            print(f"[watchdog] swarm recovery error: {e}", flush=True)

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


def _reconcile_owned_children() -> list[str]:
    """Stop this instance's exact children when shared room state is terminal.

    Another stdio/HTTP server instance cannot safely signal our children: its
    only shared evidence is a persisted PID, which may have been reused. Each
    live owner therefore cooperatively observes the shared room metadata and
    terminates its own registered ``Popen`` objects. A missing directory is
    terminal too (for example, close+delete completed between watchdog ticks).

    Unreadable/corrupt metadata is not treated as deletion: that is ambiguous,
    so this check fails closed and retries on the next watchdog tick.
    """
    reconciled: list[str] = []
    for room_id in child_processes.owned_room_ids():
        missing = False
        try:
            meta = bus.get_room_info(room_id)
        except Exception as exc:
            try:
                missing = not bus._room_dir(room_id).exists()
            except Exception:
                missing = False
            if not missing:
                print(f"[watchdog] cannot reconcile children for {room_id}: "
                      f"{exc}", flush=True)
                continue
            meta = {}
        status = meta.get("status")
        if missing or status in {"closing", "closed", "resolved"}:
            counts = child_processes.close_room(room_id)
            if counts["sent"] or counts["exited"] or counts["denied"]:
                reconciled.append(room_id)
    return reconciled


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

_AUTH_CREDENTIAL_KEY = os.urandom(32)
_MAX_HTTP_JSON_BYTES = 1024 * 1024
_MAX_AGENT_EVENT_LINE_BYTES = 1024 * 1024
_EVENT_CURSOR_WINDOW_BYTES = 4096


def _split_loopback_host(value: str) -> Optional[tuple[str, Optional[int]]]:
    """Parse a Host/Origin authority and accept loopback names only."""
    value = value.strip().lower()
    if (not value or len(value) > 255
            or any(ch in value for ch in ("/", "\\", "@", ",", "#", "?"))):
        return None
    port: Optional[int] = None
    if value.startswith("["):
        end = value.find("]")
        if end < 0:
            return None
        host = value[1:end]
        suffix = value[end + 1:]
        if suffix:
            raw_port = suffix[1:]
            if (not suffix.startswith(":") or not raw_port.isdigit()
                    or len(raw_port) > 5):
                return None
            port = int(raw_port)
    elif value.count(":") == 1:
        host, raw_port = value.rsplit(":", 1)
        if not raw_port.isdigit() or len(raw_port) > 5:
            return None
        port = int(raw_port)
    else:
        host = value
    if host not in _LOOPBACK_HOSTS or (port is not None and not 0 < port < 65536):
        return None
    return host, port


def _dashboard_credential(token: str) -> str:
    """Create the process-local credential returned to dashboard JavaScript."""
    return hmac.new(
        _AUTH_CREDENTIAL_KEY,
        b"mcp-huddle-dashboard-v1\0" + token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _constant_equal(left: str, right: str) -> bool:
    try:
        return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
    except UnicodeError:
        return False


def _event_file_generation(info: os.stat_result) -> str:
    """Return an opaque process-local identity for one event-log inode.

    macOS birth time distinguishes a rapidly reused inode without changing on
    append. Platforms without birth time fall back to device+inode, whose reuse
    window is much smaller but cannot be eliminated through portable stat().
    """
    birth_ns = getattr(info, "st_birthtime_ns", None)
    if birth_ns is None:
        birth = getattr(info, "st_birthtime", None)
        birth_ns = int(birth * 1_000_000_000) if birth is not None else 0
    identity = f"{info.st_dev}:{info.st_ino}:{birth_ns}".encode("ascii")
    return hmac.new(
        _AUTH_CREDENTIAL_KEY, b"mcp-huddle-event-file-v1\0" + identity,
        hashlib.sha256,
    ).hexdigest()


def _event_file_cursor(fd: int, generation: str, offset: int) -> str:
    """Bind an acknowledged offset to bounded file-content sentinels.

    Validation reads at most 8 KiB regardless of offset. The HMAC reveals no
    log bytes and stays stable on append, while detecting common same-inode
    truncate/rewrite cases at the start or immediately before the cursor.
    """
    head_size = min(offset, _EVENT_CURSOR_WINDOW_BYTES)
    tail_start = max(0, offset - _EVENT_CURSOR_WINDOW_BYTES)
    tail_size = offset - tail_start
    head = os.pread(fd, head_size, 0)
    tail = os.pread(fd, tail_size, tail_start)
    payload = (
        b"mcp-huddle-event-cursor-v1\0" + generation.encode("ascii")
        + b":" + str(offset).encode("ascii") + b":" + head + b"\0" + tail
    )
    return hmac.new(_AUTH_CREDENTIAL_KEY, payload, hashlib.sha256).hexdigest()


def _credential_valid(request: Request, token: str) -> bool:
    derived = request.headers.get("x-huddle-credential", "")
    if derived:
        if len(derived) > 256:
            return False
        return _constant_equal(derived, _dashboard_credential(token))
    provided = request.headers.get("x-huddle-token", "")
    if not provided:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            provided = auth[7:].strip()
    if not provided or len(provided) > max(1024, len(token)):
        return False
    return _constant_equal(provided, token)


def _same_origin(request: Request) -> bool:
    """Reject browser cross-origin requests while allowing non-browser clients."""
    fetch_site = request.headers.get("sec-fetch-site", "").lower()
    if fetch_site and fetch_site not in {"same-origin", "none"}:
        return False
    origin = request.headers.get("origin")
    if not origin:
        return True
    try:
        parsed = urlsplit(origin)
        origin_port = parsed.port
    except ValueError:
        return False
    if (parsed.scheme not in {"http", "https"} or parsed.username or parsed.password
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        return False
    host = _split_loopback_host(request.headers.get("host", ""))
    origin_host = _split_loopback_host(parsed.netloc)
    if host is None or origin_host is None or parsed.scheme != request.url.scheme:
        return False
    request_port = host[1] or (443 if request.url.scheme == "https" else 80)
    source_port = origin_port or (443 if parsed.scheme == "https" else 80)
    return host[0] == origin_host[0] and request_port == source_port


def _require_local(request: Request, *, require_auth: bool = True) -> Optional[JSONResponse]:
    """Apply loopback-client and optional credential policy.

    The server binds 127.0.0.1 and the dashboard is served from loopback, so
    this is non-breaking for normal local use. Returns a JSONResponse to
    short-circuit the calling handler when the request must be rejected, or
    None when the handler may proceed.

    MCP clients may supply the raw token as Bearer/X-Huddle-Token. The dashboard
    supplies only the derived, process-local X-Huddle-Credential from /api/auth.
    """
    client = request.client
    host = client.host if client else None
    if host not in _LOOPBACK_HOSTS:
        return JSONResponse({"error": "forbidden: loopback only"}, status_code=403)

    token = os.environ.get("MCP_HUDDLE_TOKEN")
    if require_auth and token:
        if not _credential_valid(request, token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
    return None


def _require_http_origin(request: Request) -> Optional[JSONResponse]:
    """Validate the authority and browser provenance of an HTTP request."""
    if _split_loopback_host(request.headers.get("host", "")) is None:
        return JSONResponse({"error": "forbidden: invalid Host"}, status_code=403)
    if not _same_origin(request):
        return JSONResponse({"error": "forbidden: cross-origin request"}, status_code=403)
    return None


def _public_route_path(scope: dict) -> Optional[str]:
    """Return an app-relative, traversal-free path for public classification.

    ``root_path`` differs between ASGI servers: some include it in ``path`` and
    some already strip it. Support either representation, but use this result
    only to decide whether a route is public; routing itself keeps the ASGI
    scope untouched.
    """
    path = scope.get("path", "")
    root_path = scope.get("root_path", "") or ""
    if not isinstance(path, str) or not path.startswith("/") or len(path) > 4096:
        return None
    if not isinstance(root_path, str) or len(root_path) > 1024:
        return None
    if root_path and root_path != "/":
        if (not root_path.startswith("/") or root_path.endswith("/")
                or "\\" in root_path or "//" in root_path or "\x00" in root_path
                or any(segment in {".", ".."} for segment in root_path.split("/"))):
            return None
        if path == root_path:
            path = "/"
        elif path.startswith(root_path + "/"):
            path = path[len(root_path):]
    if "\\" in path or "//" in path or "\x00" in path:
        return None
    if any(segment in {".", ".."} for segment in path.split("/")):
        return None
    return path


def _is_public_http_request(scope: dict) -> bool:
    path = _public_route_path(scope)
    method = scope.get("method", "GET").upper()
    if path == "/api/auth":
        return method in {"GET", "POST", "HEAD"}
    if method not in {"GET", "HEAD"}:
        return False
    return path == "/dashboard" or bool(path and path.startswith("/static/"))


class _HTTPGuardMiddleware:
    """Security boundary shared by custom HTTP routes and MCP transport."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        request = Request(scope)
        public = _is_public_http_request(scope)
        denied = _require_http_origin(request)
        if denied is None:
            denied = _require_local(request, require_auth=not public)
        if denied is not None:
            await denied(scope, receive, send)
            return

        method = scope.get("method", "GET").upper()
        # Every valid POST surface in this app (REST auth/mutators and MCP) is JSON.
        # Applying the rule to all POSTs also stays correct behind ASGI root_path.
        json_body = method == "POST"
        if json_body:
            media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if media_type != "application/json":
                response = JSONResponse(
                    {"error": "unsupported media type: application/json required"},
                    status_code=415,
                )
                await response(scope, receive, send)
                return
            raw_length = request.headers.get("content-length")
            if raw_length:
                try:
                    length = int(raw_length)
                except ValueError:
                    length = -1
                if length < 0:
                    response = JSONResponse({"error": "invalid Content-Length"}, status_code=400)
                    await response(scope, receive, send)
                    return
                if length > _MAX_HTTP_JSON_BYTES:
                    response = JSONResponse({"error": "request body too large"}, status_code=413)
                    await response(scope, receive, send)
                    return

            chunks = []
            size = 0
            more = True
            while more:
                message = await receive()
                if message.get("type") == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                size += len(chunk)
                if size > _MAX_HTTP_JSON_BYTES:
                    response = JSONResponse({"error": "request body too large"}, status_code=413)
                    await response(scope, receive, send)
                    return
                chunks.append(chunk)
                more = message.get("more_body", False)
            body = b"".join(chunks)
            delivered = False
            original_receive = receive

            async def replay_receive():
                nonlocal delivered
                if delivered:
                    return await original_receive()
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}

            receive = replay_receive

        await self.app(scope, receive, send)


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


@mcp.custom_route("/api/auth", methods=["GET", "POST"])
async def api_auth(request: Request) -> JSONResponse:
    """Exchange the raw token for a process-local dashboard credential.

    The raw token exists only in the POST body and is never copied into a URL,
    cookie or browser storage. The returned derived credential is kept only in
    dashboard JavaScript memory and becomes invalid when this server restarts.
    """
    denied = _require_local(request, require_auth=False)
    if denied is not None:
        return denied
    token = os.environ.get("MCP_HUDDLE_TOKEN")
    if request.method == "GET":
        return JSONResponse(
            {"required": bool(token)},
            headers=_NO_CACHE_HDRS,
        )
    if not token:
        return JSONResponse({"required": False}, headers=_NO_CACHE_HDRS)
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400, headers=_NO_CACHE_HDRS)
    provided = data.get("token") if isinstance(data, dict) else None
    if not isinstance(provided, str) or not _constant_equal(provided, token):
        return JSONResponse(
            {"error": "unauthorized"}, status_code=401, headers=_NO_CACHE_HDRS)
    return JSONResponse(
        {"required": True, "credential": _dashboard_credential(token)},
        headers=_NO_CACHE_HDRS,
    )


@mcp.custom_route("/api/rooms", methods=["GET"])
async def api_rooms(request: Request) -> JSONResponse:
    try:
        return JSONResponse(bus.list_rooms())
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/rooms_search", methods=["GET"])
async def api_rooms_search(request: Request) -> JSONResponse:
    query = request.query_params.get("q", "").strip()
    if not query or len(query) > 120:
        return JSONResponse({"error": "q must contain 1–120 characters"}, status_code=400)

    def search() -> list[dict]:
        needle = query.casefold()
        found = []
        for room in bus.list_rooms():
            name = str(room.get("name") or room.get("id") or "")
            title_match = needle in name.casefold()
            matching_message = None
            for msg in bus._load_messages(room["id"]):
                body = msg.get("body")
                if isinstance(body, str) and needle in body.casefold():
                    matching_message = msg
                    break
            if not title_match and matching_message is None:
                continue
            snippet = ""
            message_id = None
            if matching_message is not None:
                body = " ".join(matching_message["body"].split())
                at = body.casefold().find(needle)
                start = max(0, at - 72)
                snippet = ("…" if start else "") + body[start:start + 190]
                if start + 190 < len(body):
                    snippet += "…"
                message_id = matching_message.get("id")
            found.append({"id": room["id"], "name": name,
                          "owner": room.get("owner"), "status": room.get("status"),
                          "snippet": snippet, "message_id": message_id,
                          "title_match": title_match,
                          "last_activity": room.get("last_activity") or room.get("created_at")})
        found.sort(key=lambda item: (not item["title_match"], -(item["last_activity"] or 0)))
        return found[:100]

    try:
        return JSONResponse({"results": await asyncio.to_thread(search)})
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
        status_details = bus.get_status_details(room_id)
        statuses = {name: info["status"] for name, info in status_details.items()}
        phases = {name: info.get("phase", "online")
                  for name, info in status_details.items()}
        return JSONResponse({"messages": msgs, "room": room_meta,
                             "statuses": statuses, "phases": phases})
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


@mcp.custom_route("/api/rooms_close_session", methods=["POST"])
async def api_rooms_close_session(request: Request) -> JSONResponse:
    """Close rooms owned by one client session (used by the SessionEnd hook)."""
    denied = _require_local(request)
    if denied is not None:
        return denied
    try:
        data = await request.json()
        session_id = data.get("session_id") if isinstance(data, dict) else None
        if (not isinstance(session_id, str) or not session_id.strip()
                or len(session_id) > 1024 or "\x00" in session_id):
            raise ValueError("invalid session_id")
        closed = bus.close_session_rooms(session_id.strip())
        return JSONResponse({"closed": closed})
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
    """Bulk-close every non-terminal room.

    Only exact child handles owned by this server are eligible for termination;
    persisted or foreign PIDs are reported but never signalled directly.
    """
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
        health = {name: _agent_wake_health(info, statuses.get(name), room_id)
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
            agents = {name: _agent_wake_health(info, statuses.get(name), room_id)
                      for name, info in agent_meta.items()}
            rooms_health.append({
                "room_id": room_id,
                "name": meta.get("name", ""),
                "status": meta.get("status"),
                "agents": agents,
            })
        stale = sum(1 for r in rooms_health for h in r["agents"].values()
                    if h["stale_lease"])
        unowned = sum(1 for r in rooms_health for h in r["agents"].values()
                      if h["unowned_lease"])
        failed = sum(1 for r in rooms_health for h in r["agents"].values()
                     if h["last_wake_failed"])
        return JSONResponse({
            "rooms": rooms_health,
            "stale_leases": stale,
            "unowned_leases": unowned,
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
    agent_name = request.path_params["agent_name"]
    raw_offset = request.query_params.get("offset", "0")
    requested_generation = request.query_params.get("generation", "")
    requested_cursor = request.query_params.get("cursor", "")
    try:
        if len(raw_offset) > 20:
            raise ValueError
        requested_offset = int(raw_offset)
        if requested_offset < 0:
            raise ValueError
        if requested_generation and (
                len(requested_generation) != 64
                or any(ch not in "0123456789abcdef" for ch in requested_generation)):
            raise ValueError
        if requested_cursor and (
                len(requested_cursor) != 64
                or any(ch not in "0123456789abcdef" for ch in requested_cursor)):
            raise ValueError
    except (TypeError, ValueError):
        return JSONResponse({"error": "invalid event offset"}, status_code=400)
    try:
        # Validates both components and rejects existing room/agents/final-file
        # symlinks without creating a room or agents directory.
        log_path, _ = bus._agent_paths(room_id, agent_name, create=False)
    except (ValueError, OSError):
        return JSONResponse({"error": "unsafe or unavailable event log"}, status_code=400)

    async def event_stream():
        import asyncio
        fd = None
        # Wait briefly for the log file to exist (spawn race). Each attempt is
        # relative to stable no-follow directory fds and opens the final file
        # with O_NOFOLLOW, closing validation/open swap races.
        for _ in range(20):
            try:
                fd = bus._safe_open_fd(
                    log_path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
                break
            except FileNotFoundError:
                pass
            except (ValueError, OSError):
                yield "event: error\ndata: event log unavailable\n\n"
                return
            await asyncio.sleep(0.1)
        if fd is None:
            yield "event: error\ndata: event log unavailable\n\n"
            return

        opened_stat = os.fstat(fd)
        if not stat.S_ISREG(opened_stat.st_mode) or opened_stat.st_nlink != 1:
            os.close(fd)
            yield "event: error\ndata: event log unavailable\n\n"
            return
        with os.fdopen(fd, "rb") as f:
            file_generation = _event_file_generation(opened_stat)
            # A stale client offset after truncation starts at the new file's
            # beginning. An offset is meaningful only for the exact inode
            # generation that produced it; atomic replacement forces offset 0.
            same_generation = bool(requested_generation) and _constant_equal(
                requested_generation, file_generation)
            valid_cursor = False
            if (same_generation and requested_cursor
                    and requested_offset <= opened_stat.st_size):
                current_cursor = _event_file_cursor(
                    f.fileno(), file_generation, requested_offset)
                valid_cursor = _constant_equal(requested_cursor, current_cursor)
            stream_offset = (
                requested_offset
                if valid_cursor
                else 0
            )
            f.seek(stream_offset)
            stream_cursor = _event_file_cursor(
                f.fileno(), file_generation, stream_offset)
            # Send a marker so the client knows the stream is alive.
            yield (
                f"event: open\ngeneration: {file_generation}\n"
                f"cursor: {stream_cursor}\nid: {stream_offset}\n"
                "data: streaming\n\n"
            )
            buf = b""
            while True:
                if await request.is_disconnected():
                    break
                chunk = f.read(4096)
                if chunk:
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        if len(line) > _MAX_AGENT_EVENT_LINE_BYTES:
                            yield "event: error\ndata: event log line too large\n\n"
                            return
                        stream_offset += len(line) + 1
                        if line.strip():
                            text = line.decode("utf-8", errors="replace")
                            # SSE: replace internal newlines (shouldn't be any in JSONL)
                            text = text.replace("\n", "\\n")
                            stream_cursor = _event_file_cursor(
                                f.fileno(), file_generation, stream_offset)
                            yield (
                                f"cursor: {stream_cursor}\nid: {stream_offset}\n"
                                f"data: {text}\n\n"
                            )
                    if len(buf) > _MAX_AGENT_EVENT_LINE_BYTES:
                        yield "event: error\ndata: event log line too large\n\n"
                        return
                else:
                    # No new data — tail-follow with short sleep.
                    try:
                        current_stat = bus._safe_stat(log_path)
                    except (FileNotFoundError, ValueError, OSError):
                        break
                    if (current_stat.st_dev, current_stat.st_ino) != (
                            opened_stat.st_dev, opened_stat.st_ino):
                        break
                    if current_stat.st_size < stream_offset:
                        f.seek(0)
                        buf = b""
                        stream_offset = 0
                        stream_cursor = _event_file_cursor(
                            f.fileno(), file_generation, stream_offset)
                        yield (
                            f"event: reset\ngeneration: {file_generation}\n"
                            f"cursor: {stream_cursor}\nid: 0\n"
                            "data: stream reset\n\n"
                        )
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

def _set_agent_phase(
    room_id: str,
    agent_name: str,
    phase: str,
    task_id: int | str = "",
    detail: str = "",
    source: str = "server",
    preserve_busy: bool = False,
) -> None:
    """Persist one lifecycle transition without disturbing wake metadata."""
    if phase not in bus.VALID_AGENT_PHASES:
        return
    try:
        meta = bus.get_room_info(room_id)
        current = (bus.get_status_details(room_id).get(agent_name) or {})
        operational = "busy" if preserve_busy or phase in _ACTIVE_PHASES else "online"
        bus.set_status(
            room_id,
            agent_name,
            operational,
            0,
            meta.get("session_id", ""),
            phase=phase,
            task_id=task_id if task_id != "" else current.get("task_id", ""),
            detail=detail[:500] if detail else "",
            source=source,
        )
    except Exception as exc:
        print(f"[huddle] lifecycle status update failed "
              f"({agent_name}@{room_id}, {phase}): {exc}", flush=True)

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
    _set_agent_phase(room_id, agent_name, "unavailable", detail=short)
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


def _room_open_for_spawn(room_id: str) -> bool:
    """Gate for staggered (delayed) spawns: re-checked when the timer fires.
    The room may have been closed or deleted inside the stagger window — a
    fresh agent must not be spawned into a terminal/missing room."""
    try:
        info = bus.get_room_info(room_id)
    except Exception:
        return False
    return bool(info) and info.get("status") in ("open", "idle")


_SPAWNED_PID_HISTORY_MAX = 64


def _claude_receipt_log_open(
    room_id: str, agent_name: str, generation: str, source: str, spec: dict,
):
    """Bind one subscription Claude stream segment to its owned generation."""
    if spec.get("profile") != spawn._SUBSCRIPTION_OPUS_REVIEW_PROFILE:
        return None

    def _opened(start_offset: int, log_path: str, device: int, inode: int) -> None:
        try:
            canonical, _ = bus._agent_paths(room_id, agent_name, create=True)
        except (OSError, ValueError) as exc:
            raise spawn.AgentSpawnError("Claude room log unavailable") from exc
        if log_path != str(canonical):
            raise spawn.AgentSpawnError("Claude log path does not match room agent path")
        claimed = False

        def _update(meta: dict) -> dict:
            nonlocal claimed
            info = (meta.get("agent_meta") or {}).get(agent_name)
            if not isinstance(info, dict):
                return meta
            if source == "initial_spawn_id":
                claimed = (info.get(source) == generation
                           and info.get("initial_spawn_active") is True)
            else:
                claimed = (info.get(source) == generation
                           and info.get("wake_claim_id") == generation)
            if claimed:
                info["claude_log_segment"] = {
                    "generation": generation, "source": source,
                    "start_offset": start_offset,
                    "device": device, "inode": inode,
                }
            return meta

        bus._update_meta_locked(room_id, _update)
        if not claimed:
            raise spawn.AgentSpawnError("Claude process generation no longer owns room slot")

    return _opened


def _record_claude_model_receipt(
    room_id: str, agent_name: str, generation: str, source: str,
) -> None:
    """Observe only this exited CLI process and publish under the same claim."""
    meta = bus.get_room_info(room_id)
    info = (meta.get("agent_meta") or {}).get(agent_name) or {}
    segment = info.get("claude_log_segment") or {}
    if (segment.get("generation") != generation
            or segment.get("source") != source
            or info.get(source) != generation):
        return
    start = segment.get("start_offset")
    if type(start) is not int:
        return
    canonical, _ = bus._agent_paths(room_id, agent_name, create=False)
    try:
        fd = bus._safe_open_fd(canonical, os.O_RDONLY)
        with os.fdopen(fd, "rb") as stream:
            current_stat = os.fstat(stream.fileno())
            if (current_stat.st_dev != segment.get("device")
                    or current_stat.st_ino != segment.get("inode")
                    or current_stat.st_size < start):
                return
            receipt = parse_claude_model_receipt(
                stream, start_offset=start, end_offset=current_stat.st_size,
                expected_device=segment.get("device"),
                expected_inode=segment.get("inode"),
            )
    except (OSError, ValueError):
        receipt = {"reported_model": None, "source": "none"}

    def _update(current: dict) -> dict:
        slot = (current.get("agent_meta") or {}).get(agent_name)
        if not isinstance(slot, dict):
            return current
        owned_segment = slot.get("claude_log_segment") or {}
        if (slot.get(source) != generation
                or owned_segment.get("generation") != generation
                or owned_segment.get("source") != source
                or owned_segment.get("start_offset") != start):
            return current
        slot["claude_model_receipt"] = {
            "reported_model": receipt["reported_model"],
            "source": receipt["source"],
            "claim_scope": "cli_reported_identifier",
            "generation": generation,
        }
        return current

    bus._update_meta_locked(room_id, _update)


def _bounded_spawned_pids(pids: list, *new_pids: int) -> list[int]:
    values = [int(pid) for pid in pids if isinstance(pid, int) and pid > 0]
    for pid in new_pids:
        if pid > 0 and pid not in values:
            values.append(pid)
    return values[-_SPAWNED_PID_HISTORY_MAX:]


def _record_spawned_pid(
    room_id: str, pid: int, agent_name: str = "",
    initial_spawn_id: str = "",
) -> bool:
    """Merge one late-known pid into the room's spawned_pids (locked RMW).
    Persisted PIDs are diagnostics only. Termination authority remains the
    exact in-memory Popen registered by spawn's child-process registry."""
    published = False

    def _upd(m: dict) -> dict:
        nonlocal published
        if agent_name:
            info = (m.setdefault("agent_meta", {}).get(agent_name) or {})
            if (info.get("initial_spawn_id") != initial_spawn_id
                    or not info.get("initial_spawn_active")):
                return m
            info["last_wake_pid"] = pid
            info["last_wake_at"] = int(time.time())
            m["agent_meta"][agent_name] = info
        m["spawned_pids"] = _bounded_spawned_pids(
            m.get("spawned_pids") or [], pid,
        )
        published = True
        return m
    try:
        bus._update_meta_locked(room_id, _upd)
    except Exception as exc:
        print(f"[huddle] failed to record delayed-spawn pid {pid} "
              f"({room_id}): {exc}", flush=True)
    return published


def _reserve_initial_spawn(
    room_id: str,
    agent_name: str,
    initial_spawn_id: str,
    fields: dict,
) -> bool:
    """Persist an initial generation before its timer/Popen can run.

    The claim is the cross-process authority. A crash after this write is
    deliberately visible as an unowned active lease instead of allowing a
    second server instance to start a duplicate child.
    """
    reserved = False

    def _update(meta: dict) -> dict:
        nonlocal reserved
        if meta.get("status") not in ("open", "idle"):
            return meta
        am = meta.setdefault("agent_meta", {})
        info = am.get(agent_name)
        if not isinstance(info, dict):
            info = {}
        if info.get("wake_claim_id") or info.get("initial_spawn_active"):
            return meta
        info.update(fields)
        info.update({
            "initial_spawn_id": initial_spawn_id,
            "initial_spawn_active": True,
            "last_wake_pid": None,
        })
        am[agent_name] = info
        reserved = True
        return meta

    bus._update_meta_locked(room_id, _update)
    return reserved


def _begin_initial_spawn_exit(
    room_id: str, agent_name: str, initial_spawn_id: str,
) -> bool:
    """Claim the exit/failure transition for one exact initial generation."""
    begun = False

    def _update(meta: dict) -> dict:
        nonlocal begun
        info = (meta.get("agent_meta") or {}).get(agent_name)
        if (not isinstance(info, dict)
                or info.get("initial_spawn_id") != initial_spawn_id
                or not info.get("initial_spawn_active")
                or info.get("initial_spawn_exiting")):
            return meta
        info["initial_spawn_exiting"] = True
        begun = True
        return meta

    bus._update_meta_locked(room_id, _update)
    return begun


def _mark_initial_spawn_finished(
    room_id: str, agent_name: str, initial_spawn_id: str,
) -> None:
    """Clear the persisted initial-spawn guard, including an early-exit race."""
    def _update(meta: dict) -> dict:
        am = meta.setdefault("agent_meta", {})
        info = am.get(agent_name)
        if not isinstance(info, dict):
            info = {}
        if info.get("initial_spawn_id") != initial_spawn_id:
            return meta
        info["initial_spawn_id"] = initial_spawn_id
        info["initial_spawn_active"] = False
        info.pop("initial_spawn_exiting", None)
        am[agent_name] = info
        return meta
    bus._update_meta_locked(room_id, _update)


def _clear_existing_initial_spawn(
    room_id: str, agent_name: str, initial_spawn_id: str,
) -> None:
    """Rollback a delayed initial claim after spawn failure, if persisted."""
    def _update(meta: dict) -> dict:
        info = (meta.get("agent_meta") or {}).get(agent_name)
        if (isinstance(info, dict)
                and info.get("initial_spawn_id") == initial_spawn_id):
            info["initial_spawn_active"] = False
        return meta
    try:
        bus._update_meta_locked(room_id, _update)
    except Exception:
        pass


def _handle_initial_spawn_failure(
    room_id: str, agent_name: str, initial_spawn_id: str, exc: BaseException,
) -> None:
    if not _begin_initial_spawn_exit(room_id, agent_name, initial_spawn_id):
        return
    _announce_spawn_failure(room_id, agent_name, exc, "init")
    _mark_initial_spawn_finished(room_id, agent_name, initial_spawn_id)


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
      True          — spawn every enabled agent not marked "auto": false, with
                      a default reviewer brief.
      dict          — spawn only listed agents; each gets its custom brief.
                      Ignores each spec's "auto" flag.

    Side effects:
      * Creates ~/.mcp-huddle/rooms/<id>/agents/ for log files.
      * Updates meta.json: spawned_pids + agent_meta {name: {log_path, last_message_path}}.
      * Adds each spawned agent to participants.
    """
    default_brief = _build_default_brief(room_id, name, goal, cwd)

    log_dir = bus._room_dir(room_id) / "agents"

    # Owner is already present as the calling session — never spawn a
    # duplicate of them. Match by exact registry name (canonical: "Claude",
    # "Codex", "Antigravity"). Caller is expected to pass canonical owner.
    skip_owner = {owner} if owner else set()

    # Per-agent default briefs (auto_spawn=True path): each spawned agent
    # gets its OWN name baked into the identity/reply-call preamble, instead
    # of every agent receiving the exact same generic brief (room_31d32c82 —
    # a shared brief with no concrete "you are X" let a model post under
    # another participant's name).
    per_agent_default_briefs = {
        spec["name"]: _build_default_brief(room_id, name, goal, cwd, agent_name=spec["name"])
        for spec in spawn.load_registry()
        if (spec.get("enabled") and spec.get("auto", True) is not False
            and spec["name"] not in skip_owner)
    }

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
        enabled_specs = [spec for spec in registry if spec.get("enabled")]
        initial_generations = {
            spec["name"]: child_processes.new_handle()
            for spec in enabled_specs
        }
        # Same-binary stagger (see spawn.compute_stagger_delays): a dict
        # auto_spawn naming two agents that resolve to the same underlying
        # binary (e.g. two OpenCode-backed slots) must not start at the same
        # instant either — spawn_all already does this for auto_spawn=True,
        # this branch mirrors it for the explicit-dict path.
        delays = spawn.compute_stagger_delays(enabled_specs)
        for spec in enabled_specs:
            initial_spawn_id = initial_generations[spec["name"]]
            agent_brief = _wrap_user_brief(room_id, spec["name"], briefs_arg[spec["name"]])
            delay = delays.get(spec["name"], 0.0)
            bus.invite_agent(room_id, spec["name"])
            placeholder = spawn._placeholder_agent_meta(
                spec, agent_brief, log_dir,
            )
            if not _reserve_initial_spawn(
                room_id, spec["name"], initial_spawn_id, placeholder,
            ):
                continue
            if delay > 0:
                names.append(spec["name"])
                agent_meta[spec["name"]] = dict(placeholder)
                agent_meta[spec["name"]]["initial_spawn_active"] = True
                agent_meta[spec["name"]]["initial_spawn_id"] = initial_spawn_id
                _set_agent_phase(room_id, spec["name"], "queued")
                spawn._schedule_delayed_spawn(
                    delay, spec, agent_brief, cwd, log_dir,
                    on_exit=_make_initial_spawn_callback(
                        room_id, spec["name"], initial_spawn_id),
                    on_spawn_fail=(lambda generation: lambda n, exc:
                        _handle_initial_spawn_failure(
                            room_id, n, generation, exc))(initial_spawn_id),
                    should_spawn=lambda: _room_open_for_spawn(room_id),
                    on_spawned=(lambda n, generation: lambda pid:
                        _record_spawned_pid(
                            room_id, pid, n, generation,
                        ))(spec["name"], initial_spawn_id),
                    owner_room_id=room_id,
                    process_handle=initial_spawn_id,
                    on_log_open_identity=_claude_receipt_log_open(
                        room_id, spec["name"], initial_spawn_id,
                        "initial_spawn_id", spec,
                    ),
                )
                continue
            try:
                _set_agent_phase(room_id, spec["name"], "starting")
                pid, log_path, last_msg = spawn.spawn_agent(
                    spec, agent_brief, cwd, log_dir,
                    on_exit=_make_initial_spawn_callback(
                        room_id, spec["name"], initial_spawn_id),
                    owner_room_id=room_id,
                    process_handle=initial_spawn_id,
                    on_log_open_identity=_claude_receipt_log_open(
                        room_id, spec["name"], initial_spawn_id,
                        "initial_spawn_id", spec,
                    ),
                )
                pids.append(pid)
                names.append(spec["name"])
                agent_meta[spec["name"]] = {
                    "log_path": log_path,
                    "last_message_path": last_msg,
                    "last_wake_pid": pid,
                    "last_wake_at": int(time.time()),
                    "initial_spawn_active": True,
                    "initial_spawn_id": initial_spawn_id,
                }
            except (FileNotFoundError, PermissionError) as exc:
                spawn.log_spawn_failure(spec, agent_brief, cwd, log_dir, exc)
                _handle_initial_spawn_failure(
                    room_id, spec["name"], initial_spawn_id, exc,
                )
            except spawn.AgentSpawnError as exc:
                _handle_initial_spawn_failure(
                    room_id, spec["name"], initial_spawn_id, exc,
                )
            except OSError as exc:
                spawn.log_spawn_failure(spec, agent_brief, cwd, log_dir, exc)
                _handle_initial_spawn_failure(
                    room_id, spec["name"], initial_spawn_id, exc,
                )
                raise
    else:
        initial_generations = {
            agent_name: child_processes.new_handle()
            for agent_name in per_agent_default_briefs
        }
        def _prepare_initial_spawn(spec, generation, placeholder, delay):
            agent_name = spec["name"]
            bus.invite_agent(room_id, agent_name)
            if not _reserve_initial_spawn(
                room_id, agent_name, generation, placeholder,
            ):
                return False
            _set_agent_phase(
                room_id, agent_name, "queued" if delay > 0 else "starting",
            )
            return True

        names, pids, agent_meta = spawn.spawn_all(
            default_brief, cwd, log_dir,
            briefs=per_agent_default_briefs,
            on_exit_factory=lambda n: _make_initial_spawn_callback(
                room_id, n, initial_generations[n]),
            skip_names=skip_owner,
            on_spawn_fail=lambda n, exc: _handle_initial_spawn_failure(
                room_id, n, initial_generations[n], exc),
            delayed_spawn_gate=lambda: _room_open_for_spawn(room_id),
            on_delayed_spawn=lambda n, pid: _record_spawned_pid(
                room_id, pid, n, initial_generations[n]),
            owner_room_id=room_id,
            process_handle_factory=lambda n: initial_generations[n],
            prepare_spawn=_prepare_initial_spawn,
            on_log_open_identity_factory=lambda n: _claude_receipt_log_open(
                room_id, n, initial_generations[n], "initial_spawn_id",
                spawn.get_enabled_spec(n) or {},
            ),
        )
        for agent_name, info in agent_meta.items():
            info["initial_spawn_id"] = initial_generations[agent_name]

    # Phase 2: capture Codex thread_id from "thread.started" event in log,
    # so we can do `codex exec resume <id>` for follow-ups instead of spawning fresh.
    # Run blocking parse in a thread to avoid stalling room_create — but small
    # timeout so it usually returns within ~1s.
    for agent_name, info in agent_meta.items():
        if not _is_thread_resumable(agent_name):
            continue  # Only Codex has UUID-based resume; Antigravity has none.
        log_path = info.get("log_path")
        if log_path:
            tid = _parse_owned_codex_thread_id(
                room_id, agent_name, timeout=10.0,
            )
            if tid:
                info["thread_id"] = tid

    # Save diagnostic PIDs + log paths for dashboard / lifecycle visibility — locked
    # read-modify-write so a concurrent meta.json update isn't clobbered.
    # Merge (don't overwrite): extend any pre-existing spawned_pids and
    # deep-merge per-agent meta so a concurrent wake-thread update survives.
    def _save_spawn_meta(m: dict) -> dict:
        merged = m.get("agent_meta") or {}
        published_pids: list[int] = list(pids)
        for name_, info_ in agent_meta.items():
            info_ = dict(info_)
            # Every returned entry represents either a running child or a
            # scheduled delayed child. This persisted guard is authoritative
            # even if invite_agent temporarily reset operational status.
            info_.setdefault("initial_spawn_active", True)
            pid = info_.pop("pid", None)
            if pid:
                info_["last_wake_pid"] = pid
                info_["last_wake_at"] = int(time.time())
                info_["initial_spawn_active"] = True
            slot = merged.get(name_)
            if isinstance(slot, dict):
                expected_id = info_.get("initial_spawn_id")
                current_id = slot.get("initial_spawn_id")
                if slot.get("wake_claim_id"):
                    continue
                if current_id not in (None, expected_id):
                    continue
                # An extremely short-lived child callback may have recorded
                # completion before this final metadata merge. Never revive
                # its initial guard in that race.
                if (current_id == expected_id
                        and slot.get("initial_spawn_active") is False):
                    info_.pop("initial_spawn_active", None)
                    info_.pop("last_wake_pid", None)
                    info_.pop("last_wake_at", None)
                    pid = None
                slot.update(info_)
            else:
                merged[name_] = dict(info_)
            if pid:
                published_pids.append(pid)
        m["spawned_pids"] = _bounded_spawned_pids(
            m.get("spawned_pids") or [], *published_pids,
        )
        m["agent_meta"] = merged
        return m
    bus._update_meta_locked(room_id, _save_spawn_meta)


# ── Wake lease helpers ────────────────────────────────────────────────────────
#
# A wake records a persisted generation claim before Popen and later publishes
# its `wake_id` + diagnostic `last_wake_pid`.  The claim remains authoritative
# until this server observes exact-child exit.  An unknown/foreign child is not
# assumed dead, so another server cannot reuse the lease or signal a recycled
# PID.  The exact child's reaper releases the claim and drains the next queued
# request; the watchdog is a fallback and observability path.

# Per-(room, agent) in-process lock: serialises wake attempts coming from this
# process's own threads (MCP request handler, watchdog, reaper callbacks).
_wake_locks: weakref.WeakValueDictionary[
    tuple[str, str], threading.RLock
] = weakref.WeakValueDictionary()
_wake_locks_guard = threading.Lock()


def _reset_wake_locks_after_fork() -> None:
    """Discard locks inherited from vanished parent threads after fork."""
    global _wake_locks, _wake_locks_guard
    _wake_locks = weakref.WeakValueDictionary()
    _wake_locks_guard = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_wake_locks_after_fork)


def _wake_lock(room_id: str, agent_name: str) -> threading.RLock:
    key = (room_id, agent_name)
    with _wake_locks_guard:
        lock = _wake_locks.get(key)
        if lock is None:
            # Re-entrant because a synchronous test/fake Popen may invoke its
            # exit callback before the spawn call returns on the same thread.
            lock = threading.RLock()
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


def _claim_wake(
    room_id: str, agent_name: str, msg_id: int, wake_id: str,
) -> bool:
    """Atomically reserve one room+agent+message before starting a process.

    This uses only the room meta lock. Status/message reads happen before or
    after it, never while it is held. A process crash after this write leaves
    a persisted claim, deliberately failing closed instead of spawning the
    same turn from a second server instance.
    """
    claimed = False

    def _update(meta: dict) -> dict:
        nonlocal claimed
        if meta.get("status") not in ("open", "idle"):
            return meta
        am = meta.setdefault("agent_meta", {})
        info = am.get(agent_name)
        if not isinstance(info, dict):
            info = {}
        if info.get("wake_claim_id") or info.get("initial_spawn_active"):
            return meta
        if int(info.get("last_wake_msg_id", 0) or 0) >= msg_id:
            return meta
        info.update({
            "wake_claim_id": wake_id,
            "wake_claim_msg_id": msg_id,
            "wake_claimed_at": int(time.time()),
            "wake_id": wake_id,
            "last_wake_msg_id": msg_id,
            "last_seen_id": max(int(info.get("last_seen_id", 0) or 0), msg_id),
            "last_wake_pid": None,
        })
        am[agent_name] = info
        claimed = True
        return meta

    bus._update_meta_locked(room_id, _update)
    return claimed


def _claim_explicit_wake(
    room_id: str, agent_name: str, wake_id: str,
) -> bool:
    """Atomically reserve a public respond_via_agent turn without a msg id."""
    claimed = False

    def _update(meta: dict) -> dict:
        nonlocal claimed
        if meta.get("status") not in ("open", "idle"):
            return meta
        am = meta.setdefault("agent_meta", {})
        info = am.get(agent_name)
        if not isinstance(info, dict):
            info = {}
        if info.get("wake_claim_id") or info.get("initial_spawn_active"):
            return meta
        info.update({
            "wake_claim_id": wake_id,
            "wake_claim_msg_id": None,
            "wake_claimed_at": int(time.time()),
            "wake_id": wake_id,
            "last_wake_pid": None,
        })
        am[agent_name] = info
        claimed = True
        return meta

    bus._update_meta_locked(room_id, _update)
    return claimed


def _clear_wake_claim(
    room_id: str, agent_name: str, wake_id: str, *, rollback: bool = False,
) -> bool:
    """Conditionally release only this generation's persisted wake claim."""
    cleared = False

    def _update(meta: dict) -> dict:
        nonlocal cleared
        info = (meta.get("agent_meta") or {}).get(agent_name)
        if not isinstance(info, dict) or info.get("wake_claim_id") != wake_id:
            return meta
        for field in ("wake_claim_id", "wake_claim_msg_id", "wake_claimed_at"):
            info.pop(field, None)
        if rollback and info.get("wake_id") == wake_id:
            info.pop("wake_id", None)
            info.pop("last_wake_pid", None)
        cleared = True
        return meta

    bus._update_meta_locked(room_id, _update)
    return cleared


def _publish_wake_started(
    room_id: str,
    agent_name: str,
    wake_id: str,
    fields: dict,
    task_id: int | str = "",
) -> bool:
    """Generation-CAS publish after Popen returns.

    A very short child may exit, release its claim and let another server
    claim a newer request before the original Popen call returns. The stale
    caller must then leave both metadata and operational status untouched.
    """
    published = False

    def _update(meta: dict) -> dict:
        nonlocal published
        info = (meta.get("agent_meta") or {}).get(agent_name)
        if not isinstance(info, dict) or info.get("wake_claim_id") != wake_id:
            return meta
        info.update(fields)
        published = True
        return meta

    # The exact child's reaper uses this same process-local lock. Since a
    # foreign process has no Popen authority, this serializes every legitimate
    # terminal callback with the meta-CAS + operational status publication.
    with _wake_lock(room_id, agent_name):
        bus._update_meta_locked(room_id, _update)
        if published:
            _set_agent_phase(
                room_id, agent_name, "working", task_id=task_id,
            )
    return published


def _owned_process_state(room_id: str, info: dict) -> child_processes.ProcessState:
    handle = info.get("wake_id")
    if handle:
        return child_processes.state(room_id, handle)
    return child_processes.state_for_pid(room_id, info.get("last_wake_pid"))


def _wake_in_progress(info: dict, status: Optional[str], room_id: str = "") -> bool:
    """True when a persisted claim or active child makes a wake unsafe.

    Claims are authoritative independently of process-local status. This is
    what closes the pre-Popen cross-process race and also protects an initial
    auto-spawn whose invite/status write raced with its live child.
    """
    if info.get("wake_claim_id") or info.get("initial_spawn_active"):
        return True
    if status != "busy":
        return False
    if not info.get("last_wake_pid"):
        return False
    # An unknown persisted lease is not proof of death. Keep it occupied until
    # the dead-wake grace path reports ownership loss and releases it.
    return _owned_process_state(room_id, info) in {"alive", "unknown"}


def _agent_wake_health(
    info: dict, status: Optional[str], room_id: str = "",
) -> dict:
    """Computed wake-health for one agent — surfaces stale leases / failed
    wakes for the dashboard health view."""
    pid = info.get("last_wake_pid")
    claim_active = bool(
        info.get("wake_claim_id") or info.get("initial_spawn_active")
    )
    process_state = _owned_process_state(room_id, info) if pid else "unknown"
    pid_alive = process_state == "alive"
    rc = info.get("last_wake_rc")
    return {
        "status": status or "offline",
        "wake_id": info.get("wake_id"),
        "last_wake_pid": pid,
        "pid_alive": pid_alive,
        "process_state": process_state,
        "claim_active": claim_active,
        "stale_lease": status == "busy" and process_state == "exited",
        "unowned_lease": process_state == "unknown" and (
            claim_active or (bool(pid) and status == "busy")
        ),
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
            with _wake_lock(room_id, agent_name):
                _on_wake_exit(room_id, agent_name, wake_id, returncode)
        except Exception as exc:  # never let a callback kill the reaper thread
            print(f"[huddle] wake-exit callback error "
                  f"({agent_name}@{room_id}): {exc}", flush=True)
    return _callback


def _make_initial_spawn_callback(
    room_id: str, agent_name: str, initial_spawn_id: str,
):
    """Reaper on_exit callback for the room_create spawn: no busy lease to
    release — explain the silence the same way a wake exit does (rate-limit
    detection, else a noreply notice) and drain any request queued during
    the agent's first turn."""
    def _callback(returncode) -> None:
        try:
            _on_initial_spawn_exit(
                room_id, agent_name, initial_spawn_id, returncode,
            )
        except Exception as exc:
            print(f"[huddle] initial-spawn exit callback error "
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
    if not info.get("log_path"):
        return False
    text = _read_owned_agent_log(room_id, agent_name)
    reason = spawn.detect_rate_limit_text(text) if text is not None else None
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


def _agent_result_posted_after(room_id: str, agent_name: str, msg_id: int) -> bool:
    """True only when the agent stored a deliverable result after the wake."""
    return any(
        msg.get("agent") == agent_name
        and msg.get("id", 0) > msg_id
        and msg.get("kind") in ("result", "final")
        for msg in bus._load_messages(room_id)
    )


_OWNED_LOG_HEAD_BYTES = 256 * 1024
_OWNED_LOG_TAIL_BYTES = 512 * 1024
_OWNED_LOG_NOTICE_BYTES = 64 * 1024


def _read_owned_agent_log(
    room_id: str,
    agent_name: str,
    *,
    limit: int = _OWNED_LOG_TAIL_BYTES,
    tail: bool = True,
) -> str | None:
    """Read only the canonical room-owned log through confined O_NOFOLLOW IO."""
    try:
        log_path, _ = bus._agent_paths(room_id, agent_name, create=False)
        fd = bus._safe_open_fd(log_path, os.O_RDONLY)
        with os.fdopen(fd, "rb") as fh:
            if tail:
                fh.seek(0, os.SEEK_END)
                fh.seek(max(0, fh.tell() - limit), os.SEEK_SET)
            return fh.read(limit).decode("utf-8", errors="replace")
    except (FileNotFoundError, NotADirectoryError, OSError, ValueError):
        return None


def _parse_owned_codex_thread_id(
    room_id: str, agent_name: str, timeout: float,
) -> str | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        text = _read_owned_agent_log(
            room_id, agent_name, limit=_OWNED_LOG_HEAD_BYTES, tail=False,
        )
        if text:
            thread_id = spawn.parse_codex_thread_id_text(text)
            if thread_id:
                return thread_id
        time.sleep(0.1)
    return None


def _owned_codex_log_has_completed_turn(room_id: str, agent_name: str) -> bool:
    text = _read_owned_agent_log(
        room_id, agent_name, limit=_OWNED_LOG_TAIL_BYTES, tail=True,
    )
    return bool(text and spawn.codex_log_has_completed_turn_text(text))


def _log_tail(room_id: str, agent_name: str, max_len: int = 200) -> str:
    """Short ANSI-stripped tail of an agent log, for a failure notice."""
    text = _read_owned_agent_log(
        room_id, agent_name, limit=_OWNED_LOG_NOTICE_BYTES, tail=True,
    )
    if text is None:
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
        tail = _log_tail(room_id, agent_name)
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


def _announce_noreply_on_initial_exit(room_id: str, agent_name: str, rc: int,
                                      log_path: Optional[str]) -> None:
    """Same notice as _announce_noreply_on_exit, for the room_create
    auto_spawn path where there is no wake msg_id to check a reply against —
    "posted anything since the room was created" (message id > 0) is the bar
    instead of "posted since this wake". Only called when the exit was NOT
    already explained by a detected rate-limit.

    Idempotent per (room, agent) via a fixed 'init' key: an agent gets
    exactly one initial spawn per room (_spawn_agents runs once, from
    room_create), so no per-attempt disambiguator is needed beyond that
    constant suffix — a duplicate exit callback for the same initial spawn
    will not double-post.
    """
    if _agent_posted_after(room_id, agent_name, 0):
        return
    if rc == 0:
        body = (f"⚠️ {agent_name} завершился без ответа в комнату "
                 f"(exit 0) — не ждите ответа.")
    else:
        tail = _log_tail(room_id, agent_name)
        suffix = f" {tail}" if tail else ""
        body = (f"⚠️ {agent_name} завершился с ошибкой (exit {rc}) и не "
                 f"ответил — не ждите ответа.{suffix}")
    try:
        _post_message_checked(
            room_id, agent_name, body,
            kind="comment",
            idempotency_key=f"noreply:{room_id}:{agent_name}:init",
        )
    except Exception as exc:
        print(f"[huddle] noreply notice post failed (init) "
              f"({agent_name}@{room_id}): {exc}", flush=True)


def _on_initial_spawn_exit(
    room_id: str, agent_name: str, initial_spawn_id: str, returncode,
) -> None:
    """Reaction to a room_create auto_spawn agent's process exit: unlike a
    wake, there is no busy lease to release — but the same no-silent-failure
    guarantee applies: detect a provider rate-limit, else post a noreply
    notice if the agent exited without posting anything at all, then drain
    any request that queued during the agent's first turn."""
    # Keep the exact generation's persisted guard throughout status/notices.
    # A stale callback must not overwrite a newer wake's phase or metadata.
    if not _begin_initial_spawn_exit(room_id, agent_name, initial_spawn_id):
        return
    try:
        _record_claude_model_receipt(
            room_id, agent_name, initial_spawn_id, "initial_spawn_id",
        )
    except Exception as exc:
        print(f"[huddle] Claude model receipt failed (init) "
              f"({agent_name}@{room_id}): {exc}", flush=True)
    rc = -999 if returncode is None else int(returncode)
    rate_limit_announced = False
    if rc != 0:
        try:
            rate_limit_announced = _handle_rate_limit_on_exit(room_id, agent_name)
        except Exception as exc:
            print(f"[huddle] rate-limit check error (init) "
                  f"({agent_name}@{room_id}): {exc}", flush=True)
    posted_result = _agent_result_posted_after(room_id, agent_name, 0)
    if rate_limit_announced:
        _set_agent_phase(room_id, agent_name, "rate_limited")
    elif posted_result:
        _set_agent_phase(room_id, agent_name, "completed")
    else:
        _set_agent_phase(room_id, agent_name, "unavailable")
    if not rate_limit_announced:
        log_path = None
        try:
            meta = bus.get_room_info(room_id)
            log_path = ((meta.get("agent_meta") or {}).get(agent_name) or {}).get("log_path")
        except Exception:
            pass
        try:
            _announce_noreply_on_initial_exit(room_id, agent_name, rc, log_path)
        except Exception as exc:
            print(f"[huddle] noreply check error (init) "
                  f"({agent_name}@{room_id}): {exc}", flush=True)
    try:
        # Status has already reached its terminal phase. Clear the persisted
        # initial guard last so a concurrent request can only start afterwards.
        _mark_initial_spawn_finished(room_id, agent_name, initial_spawn_id)
    except Exception as exc:
        print(f"[huddle] initial-spawn claim release error "
              f"({agent_name}@{room_id}): {exc}", flush=True)
    try:
        _drain_pending_wakes(room_id, agent_name)
    except Exception as exc:
        print(f"[huddle] wake drain error ({agent_name}@{room_id}): {exc}",
              flush=True)


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
    try:
        _record_claude_model_receipt(room_id, agent_name, wake_id, "wake_id")
    except Exception as exc:
        print(f"[huddle] Claude model receipt failed (wake) "
              f"({agent_name}@{room_id}): {exc}", flush=True)
    if (not already_announced and (
        info.get("stuck_announced_wake_id") == wake_id
        or info.get("stuck_killed_wake_id") == wake_id
    )):
        # The stuck watchdog already explained this wake's silence. Whether
        # SIGTERM was sent, denied, or disabled, don't add a second noreply
        # notice when the exact child eventually exits.
        already_announced = True
    rc = -999 if returncode is None else int(returncode)
    intentional_pilot_stop = (
        info.get("intentional_stop_wake_id") == wake_id
        and rc == -int(signal.SIGTERM)
    )
    fail_count = int(info.get("wake_fail_count", 0) or 0)
    updates = {
        "last_wake_rc": rc,
        "last_wake_exit_at": int(time.time()),
        "wake_fail_count": (
            fail_count if intentional_pilot_stop
            else (fail_count + 1) if rc != 0 else 0
        ),
    }
    if rc == 0 and not intentional_pilot_stop:
        # A clean turn clears any prior rate-limit cooldown so the agent can be
        # woken again immediately.
        updates["rate_limited_until"] = 0
    _merge_agent_meta(room_id, agent_name, updates)
    rate_limit_announced = False
    if not already_announced and rc != 0 and not intentional_pilot_stop:
        try:
            rate_limit_announced = _handle_rate_limit_on_exit(room_id, agent_name)
        except Exception as exc:
            print(f"[huddle] rate-limit check error "
                  f"({agent_name}@{room_id}): {exc}", flush=True)
    posted_result = _agent_result_posted_after(
        room_id, agent_name, int(info.get("last_wake_msg_id", 0) or 0))
    if (info.get("stuck_announced_wake_id") == wake_id
            or info.get("stuck_killed_wake_id") == wake_id):
        final_phase = "stuck"
    elif rate_limit_announced:
        final_phase = "rate_limited"
    elif posted_result:
        final_phase = "completed"
    else:
        final_phase = "unavailable"
    _set_agent_phase(room_id, agent_name, final_phase)
    if (not already_announced and not rate_limit_announced
            and not intentional_pilot_stop):
        try:
            _announce_noreply_on_exit(
                room_id, agent_name,
                int(info.get("last_wake_msg_id", 0) or 0),
                rc, info.get("log_path"))
        except Exception as exc:
            print(f"[huddle] noreply check error "
                  f"({agent_name}@{room_id}): {exc}", flush=True)
    if not posted_result and not intentional_pilot_stop:
        try:
            # Still holding this generation's claim: a replacement takes it
            # over atomically, making the release below a no-op.
            if _swarm_replace_failed_member(
                room_id, agent_name, wake_id, info, rc, final_phase,
                rate_limit_announced,
            ):
                return
        except Exception as exc:
            print(f"[huddle] swarm replacement error "
                  f"({agent_name}@{room_id}): {exc}", flush=True)
    try:
        # Operational status is terminal now. Release only this exact
        # generation, then allow the queued-drain path to claim the next turn.
        _clear_wake_claim(room_id, agent_name, wake_id)
    except Exception as exc:
        print(f"[huddle] wake-claim release error "
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
    pilot = bus.get_room_info(room_id).get("swarm_pilot")
    messages = bus._load_messages(room_id)
    for msg in messages:
        if msg.get("id", 0) <= last_wake:
            continue
        if msg.get("kind") != "request" or msg.get("reply_to") is not None:
            continue
        if _swarm_pilot_request_superseded(room_id, msg, pilot, messages):
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

            if _wake_in_progress(info, status, room_id):
                continue  # live wake → request stays queued, drained on exit
            if _agent_in_rate_limit_cooldown(info):
                continue  # provider limit hit → a fresh spawn would instantly fail
            last_wake = int(info.get("last_wake_msg_id", 0) or 0)
            if last_wake >= msg_id:
                continue
            # Message and meta locks must never be nested. Read the persisted
            # queue first, then let the meta-lock CAS below serialize the
            # claim. A caller for request #2 cannot skip an older eligible #1.
            oldest = _next_pending_request(room_id, agent_name, info)
            if oldest is None or int(oldest.get("id", 0) or 0) != msg_id:
                continue
            if _agent_replied_to_request(room_id, agent_name, msg_id):
                _merge_agent_meta(room_id, agent_name, {
                    "last_wake_msg_id": msg_id,
                    "last_seen_id": max(int(info.get("last_seen_id", 0) or 0), msg_id),
                })
                continue

            pilot_state = fresh.get("swarm_pilot")
            pinned_specs = (
                pilot_state.get("expected_specs")
                if isinstance(pilot_state, dict) else None
            )
            if isinstance(pinned_specs, dict) and _member_launch_spec(fresh, agent_name)[1]:
                wakes.append({"agent": agent_name, "status": "spec_drift"})
                continue

            log_path = info.get("log_path")
            last_seen = int(info.get("last_seen_id", 0) or 0)
            wake_id = uuid.uuid4().hex[:12]

            # A replaced member always launches its route profile fresh.
            resumable = (_is_thread_resumable(agent_name)
                         and _swarm_route_profile(info) is None)
            # A newly invited Codex has no native thread yet. The first pilot
            # turn must start a registry-backed process; later turns resume
            # the captured thread just like an ordinary room.
            if (resumable and fresh.get("swarm_pilot")
                    and not info.get("thread_id")):
                initial_thread = _parse_owned_codex_thread_id(
                    room_id, agent_name, timeout=1.0,
                )
                if initial_thread:
                    _merge_agent_meta(room_id, agent_name,
                                      {"thread_id": initial_thread})
                    info["thread_id"] = initial_thread
                else:
                    resumable = False
            if resumable:
                try:
                    canonical_log, canonical_last = bus._agent_paths(
                        room_id, agent_name, create=False,
                    )
                except (OSError, ValueError):
                    continue
                log_path = str(canonical_log)
                thread_id = info.get("thread_id")
                if not thread_id:
                    thread_id = _parse_owned_codex_thread_id(
                        room_id, agent_name, timeout=1.0,
                    )
                    if not thread_id:
                        continue
                    _merge_agent_meta(room_id, agent_name, {"thread_id": thread_id})
                if not _owned_codex_log_has_completed_turn(room_id, agent_name):
                    continue
                prompt = _build_codex_wakeup_prompt(
                    room_id, sender, body, to, msg_id, last_seen)
                if not _claim_wake(room_id, agent_name, msg_id, wake_id):
                    continue
                _set_agent_phase(room_id, agent_name, "starting", task_id=msg_id)
                try:
                    if isinstance(pinned_specs, dict):
                        latest_spec = spawn.get_enabled_spec(agent_name)
                        if _swarm_spec_drift(
                            bus.get_room_info(room_id), agent_name, latest_spec,
                        ):
                            _clear_wake_claim(
                                room_id, agent_name, wake_id, rollback=True,
                            )
                            _set_agent_phase(
                                room_id, agent_name, "unavailable", msg_id,
                                "spec_drift",
                            )
                            wakes.append({"agent": agent_name, "status": "spec_drift"})
                            continue
                    resume_settings = info.get("model_settings")
                    resume_kwargs = (
                        {"model_settings": resume_settings}
                        if isinstance(resume_settings, dict) else {}
                    )
                    resume_cwd, write_roots = room_workspace.resume(
                        bus.get_room_info(room_id), agent_name,
                    )
                    if write_roots is not None:
                        resume_kwargs["workspace_write_roots"] = write_roots
                    pid = spawn.codex_resume(
                        thread_id, prompt, resume_cwd, log_path,
                        str(canonical_last),
                        on_exit=_make_wake_done_callback(room_id, agent_name, wake_id),
                        owner_room_id=room_id,
                        process_handle=wake_id,
                        **resume_kwargs,
                    )
                except Exception as exc:
                    _set_agent_phase(room_id, agent_name, "unavailable", msg_id, str(exc))
                    try:
                        _clear_wake_claim(
                            room_id, agent_name, wake_id, rollback=True,
                        )
                    except Exception as rollback_exc:
                        print(f"[huddle] wake-claim rollback failed "
                              f"({agent_name}@{room_id}): {rollback_exc}",
                              flush=True)
                    print(f"[huddle] codex_resume failed ({room_id}): {exc}",
                          flush=True)
                    _announce_spawn_failure(room_id, agent_name, exc, str(msg_id))
                    continue
                published = _publish_wake_started(room_id, agent_name, wake_id, {
                    "last_wake_msg_id": msg_id, "last_seen_id": msg_id,
                    "last_wake_pid": pid, "last_wake_at": int(time.time()),
                    "wake_id": wake_id,
                }, task_id=msg_id)
                if not published:
                    continue
                wakes.append({"agent": agent_name, "pid": pid,
                              "thread_id": thread_id})
                continue

            # Registry agents without UUID resume — fresh spawn each turn.
            transcript = bus.read_messages(room_id, since_id=0, limit=50)
            prompt = _build_registry_agent_wakeup_prompt(
                room_id, agent_name, sender, body, to, msg_id, last_seen,
                transcript)
            if not _claim_wake(room_id, agent_name, msg_id, wake_id):
                continue
            try:
                pid, _, _ = _spawn_fresh_room_agent(
                    room_id, agent_name, prompt, fresh, msg_id=msg_id,
                    wake_id=wake_id)
            except Exception as exc:
                try:
                    _clear_wake_claim(
                        room_id, agent_name, wake_id, rollback=True,
                    )
                except Exception as rollback_exc:
                    print(f"[huddle] wake-claim rollback failed "
                          f"({agent_name}@{room_id}): {rollback_exc}",
                          flush=True)
                print(f"[huddle] fresh spawn failed for {agent_name} "
                      f"({room_id}): {exc}", flush=True)
                _announce_spawn_failure(room_id, agent_name, exc, str(msg_id))
                continue
            wakes.append({"agent": agent_name, "pid": pid, "thread_id": ""})

    return wakes


def _agent_replied_to_request(room_id: str, agent_name: str, msg_id: int) -> bool:
    """Return whether a result/final or server failure settled this request.

    A failure settlement remains a failure lifecycle state; this predicate only
    prevents a duplicate wake when wake metadata is missing.
    """
    messages = bus._load_messages(room_id)
    for msg in messages:
        if (
            msg.get("agent") == agent_name
            and msg.get("reply_to") == msg_id
            and msg.get("kind") in {"result", "final"}
        ):
            return True
    try:
        status_info = bus.get_status_details(room_id).get(agent_name) or {}
        meta = bus.get_room_info(room_id)
    except Exception:
        return False
    request = next((msg for msg in messages if msg.get("id") == msg_id), None)
    if request and _swarm_pilot_request_superseded(
        room_id, request, meta.get("swarm_pilot"), messages,
    ):
        return True
    wake_info = (meta.get("agent_meta") or {}).get(agent_name) or {}
    return str(msg_id) in _server_terminal_failure_task_ids(status_info, wake_info)


def _wake_pending_agents() -> list[dict]:
    """Fallback retry for wakes the event-driven path missed (e.g. a reaper
    thread that died together with a short-lived stdio huddle process). The
    primary drain is the reaper on_exit callback — this is belt-and-suspenders.
    """
    wakes: list[dict] = []
    for meta in bus.list_rooms():
        if meta.get("status") not in ("open", "idle"):
            continue
        room_id = meta["id"]
        agent_meta = meta.get("agent_meta", {})
        if not agent_meta:
            continue
        statuses = bus.get_status(room_id)
        for agent_name, info in agent_meta.items():
            if _wake_in_progress(info, statuses.get(agent_name), room_id):
                continue
            pending = _next_pending_request(room_id, agent_name, info)
            if pending is None:
                continue
            wakes.extend(_wake_agents_for_request(
                room_id, pending.get("agent", ""), pending.get("body", ""),
                pending.get("to"), None, pending["id"]))
    return wakes


def _check_dead_wakes() -> list[str]:
    """Release only exact current-instance exited leases after a short grace.

    Ownership state comes only from this server instance's exact Popen
    registry. ``unknown`` may belong to another live stdio server instance, so
    it remains occupied and is never cleared, drained, or signalled.

    Runs before _check_stuck_wakes in the sweep so an exact exited child is
    announced here, fast, instead of by the slow stuck-wake path. That path
    can also observe active claims without a live pid; generation-scoped
    markers keep the two checks from double-announcing the same wake.

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
            process_state = _owned_process_state(room_id, info)
            if not pid or process_state != "exited":
                continue
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
            # Same for a wake _check_stuck_wakes already signalled/announced
            # (e.g. its reaper thread never fired to release the lease on its
            # own) — the room already has the stuck-process notice.
            msg_id = int(info.get("last_wake_msg_id", 0) or 0)
            already_explained = (
                (bool(msg_id) and _agent_posted_after(room_id, agent_name, msg_id))
                or info.get("stuck_announced_wake_id") == wake_id
                or info.get("stuck_killed_wake_id") == wake_id
            )
            if not already_explained:
                try:
                    _post_message_checked(
                        room_id, agent_name,
                        f"⚠️ {agent_name}: процесс {pid} завершился, не ответив — "
                        f"ответа не будет.",
                        kind="comment",
                        idempotency_key=f"deadwake:{room_id}:{agent_name}:{wake_id}",
                    )
                except Exception as exc:
                    print(f"[huddle] dead-wake notice post failed "
                          f"({agent_name}@{room_id}): {exc}", flush=True)
            try:
                with _wake_lock(room_id, agent_name):
                    _on_wake_exit(room_id, agent_name, wake_id, None,
                                  already_announced=True)
            except Exception as exc:
                print(f"[huddle] dead-wake lease-clear error "
                      f"({agent_name}@{room_id}): {exc}", flush=True)
            announced.append(f"{agent_name}@{room_id}")
    return announced


def _terminate_stuck_wake(
    wake_id: str, pid: int, agent_name: str, room_id: str,
) -> bool:
    """Terminate only the exact current-instance child for this wake.

    NB: for a `timeout N <cli> ...`-wrapped spec the tracked pid is the
    timeout wrapper's — we rely on GNU/coreutils timeout forwarding SIGTERM
    to its child (its documented default behavior), so the real CLI dies too.
    """
    result = child_processes.terminate(room_id, wake_id)
    if result == "denied":
        print(f"[huddle] stuck-wake kill denied ({agent_name}@{room_id}, "
              f"pid={pid})", flush=True)
    return result == "sent"


def _mark_wake_generation(
    room_id: str, agent_name: str, wake_id: str, fields: dict,
) -> bool:
    """CAS fields onto an exact still-active wake generation."""
    marked = False

    def _update(meta: dict) -> dict:
        nonlocal marked
        info = (meta.get("agent_meta") or {}).get(agent_name)
        claim_id = info.get("wake_claim_id") if isinstance(info, dict) else None
        if (not isinstance(info, dict)
                or info.get("wake_id") != wake_id
                or claim_id not in (None, wake_id)):
            return meta
        info.update(fields)
        marked = True
        return meta

    bus._update_meta_locked(room_id, _update)
    return marked


def _check_stuck_wakes() -> list[str]:
    """Watchdog sweep: announce (once per wake) a 'busy' lease held longer
    than WAKE_STUCK_SECS with no message posted by that agent since the wake
    started — a live-but-silent (or hung) agent process. This is the third
    silent-exit path: unlike _on_wake_exit / _handle_rate_limit_on_exit
    (which fire when the process exits), a genuinely hung process never
    exits, so nothing else in this file will ever tell the organizer to stop
    waiting on it.

    When STUCK_KILL_ENABLED (default on), it first asks the exact owned Popen
    to terminate and then accurately reports whether SIGTERM was sent. Sending
    a signal is not proof of exit: the operational lease stays busy until the
    exact child is observed exited by its normal reaper
    on_exit → _on_wake_exit, which checks stuck_killed_wake_id and skips its
    own noreply/rate-limit announcement (this notice already explained the
    silence). Set MCP_HUDDLE_STUCK_KILL=0 to fall back to announce-only.
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
            if not _wake_in_progress(info, statuses.get(agent_name), room_id):
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
            pid_available = (
                isinstance(pid, int) and not isinstance(pid, bool) and pid > 0
            )
            process_state = _owned_process_state(room_id, info)
            alive = process_state == "alive"
            mins = max(1, (now - last_wake_at) // 60)
            try:
                # The list_rooms snapshot may already be obsolete: the old
                # child can exit and another server can claim a new request.
                # Mark only the exact still-active generation. Warning alone
                # is not a terminal transition; exact exit/reaper owns the
                # eventual phase=stuck + operational release.
                if not _mark_wake_generation(room_id, agent_name, wake_id, {
                    "stuck_announced_wake_id": wake_id,
                }):
                    continue
                kill_sent = (
                    STUCK_KILL_ENABLED
                    and alive
                    and pid_available
                    and _terminate_stuck_wake(
                        wake_id, pid, agent_name, room_id,
                    )
                )
                if kill_sent:
                    _mark_wake_generation(room_id, agent_name, wake_id, {
                        "stuck_killed_wake_id": wake_id,
                    })
                    body = (f"⏳ {agent_name} не отвечает уже ~{mins} мин "
                            f"(процесс {pid}) — возможно завис; "
                            f"SIGTERM отправлен, ожидается завершение.")
                elif not pid_available:
                    body = (f"⏳ {agent_name} не отвечает уже ~{mins} мин "
                            "(PID ещё не опубликован; запуск остаётся "
                            "зарезервированным) — возможно завис; "
                            "не ждите ответа.")
                else:
                    state_label = {
                        "alive": "жив",
                        "exited": "завершён",
                        "unknown": "не принадлежит текущему серверу",
                    }[process_state]
                    body = (f"⏳ {agent_name} не отвечает уже ~{mins} мин "
                            f"(процесс {pid}: {state_label}) — "
                            f"возможно завис; не ждите ответа.")
                _post_message_checked(
                    room_id, agent_name, body,
                    kind="comment",
                    idempotency_key=f"stuck:{room_id}:{agent_name}:{wake_id}",
                )
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
    process exits. A replaced Swarm member launches its route profile under
    the member's own room/log identity."""
    current_meta = bus.get_room_info(room_id)
    spec, drift = _member_launch_spec(current_meta, agent_name)
    if not spec:
        raise ValueError(f"Agent {agent_name} has no enabled spawn registry entry")
    if drift:
        raise ValueError(
            f"spec_drift: refusing to launch {agent_name} with a changed swarm profile"
        )
    # Permissions come from this room's persisted policy, re-validated per
    # launch; a write room refuses unsupported profiles before Popen.
    launch_spec, launch_cwd = room_workspace.launch(current_meta, agent_name, spec)

    if agent_name not in meta.get("participants", []):
        bus.invite_agent(room_id, agent_name)

    if wake_id is None:
        wake_id = uuid.uuid4().hex[:12]
    session_id = meta.get("session_id", "")
    _set_agent_phase(room_id, agent_name, "starting", task_id=msg_id or "")
    identity = {"log_name": agent_name} if spec.get("name") != agent_name else {}
    try:
        pid, log_path, last_msg_path = spawn.spawn_agent(
            launch_spec,
            prompt,
            launch_cwd,
            bus._room_dir(room_id) / "agents",
            on_exit=_make_wake_done_callback(room_id, agent_name, wake_id),
            owner_room_id=room_id,
            process_handle=wake_id,
            on_log_open_identity=_claude_receipt_log_open(
                room_id, agent_name, wake_id, "wake_id", spec,
            ),
            **identity,
        )
    except Exception as exc:
        _set_agent_phase(room_id, agent_name, "unavailable", detail=str(exc))
        raise

    fields = {
        "log_path": log_path,
        "last_message_path": last_msg_path,
        "last_wake_pid": pid,
        "last_wake_at": int(time.time()),
        "wake_id": wake_id,
    }
    model_settings = spawn.model_settings_for_spec(spec)
    if model_settings:
        fields["model_settings"] = model_settings
    if msg_id is not None:
        fields["last_wake_msg_id"] = msg_id
        fields["last_seen_id"] = msg_id
    _publish_wake_started(
        room_id, agent_name, wake_id, fields, task_id=msg_id or "",
    )
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

{_lifecycle_protocol_block(room_id, agent_name)}

{_evidence_protocol_block()}

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


# ── Shared protocol building-blocks ──────────────────────────────────────
# Used by every agent-facing prompt (cold-spawn default brief, per-agent
# custom brief, and the mid-discussion wake-up prompt) so the delivery
# protocol can't drift between them. Root cause this closes (room_31d32c82):
# a weaker cold-spawn brief let small models answer to stdout instead of
# calling message_post, or post under another participant's name — the same
# models behaved correctly once driven by the wake prompt's explicit
# identity + exact-tool-call framing below.


def _agent_identity_block(room_id: str, agent_name: str) -> str:
    """Room id + own-identity line, shared verbatim by every prompt."""
    return (f"Room: {room_id}\n"
            f'You are: {agent_name} — always post as agent="{agent_name}", '
            f"never another participant's name.")


def _reply_call(room_id: str, agent_name: str, to: str, idempotency_key: str,
                 kind: str = "result", reply_to: Optional[int] = None) -> str:
    """The exact message_post(...) call an agent should make to reply."""
    reply_to_part = f", reply_to={reply_to}" if reply_to is not None else ""
    return (f'message_post(room_id="{room_id}", agent="{agent_name}", '
            f'kind="{kind}", to="{to}"{reply_to_part}, '
            f'idempotency_key="{idempotency_key}")')


def _stdout_not_delivered_note() -> str:
    return ("Only mcp-huddle MCP tool calls are delivered to the room — "
            "anything you print to stdout or return as a plain answer is "
            "invisible to other participants.")


def _anti_loop_block() -> str:
    return ("Anti-loop: reply once per request. Do not answer a request "
            "that already has reply_to set, and never send thanks/ack-only "
            "chatter.")


def _evidence_protocol_block() -> str:
    """Shared evidence floor and lightweight evaluation rubric."""
    return """## Evidence and evaluation (MANDATORY)
Consensus is not correctness. Evaluate proposals against the stated goal and
constraints, evidence quality, risks/unknowns, and reversibility.
- Support every verifiable factual claim with available evidence: a source URL,
  file:line, test/command result, or specific room message id.
- If direct support is unavailable, label the claim as inference or unknown.
  Opinions and trade-offs need reasoning, not fake citations.
"""


def _lifecycle_protocol_block(room_id: str, agent_name: str,
                              task_id: int | str = "") -> str:
    """Shared lifecycle contract for every process-backed agent prompt."""
    task = str(task_id) if task_id != "" else "current-request"
    return f'''## Lifecycle and waiting (MANDATORY)
The server owns the lifecycle: queued, starting, thinking, working, responding,
completed, unavailable, rate_limited, and stuck.
- At the start of work, call `status_set(room_id="{room_id}", agent="{agent_name}", phase="working", task_id="{task}")`.
- During research or generation, use phase `thinking` or `working`; before
  publishing the answer, use phase `responding`.
- Publish the answer with `message_post(..., kind="result", ...)`. The server
  marks the turn `completed` after that result is stored.
- If you create a room or ask other agents, call `room_status(room_id="{room_id}")`.
  Wait while their phase is `queued`, `starting`, `thinking`, `working`, or
  `responding`, or while `wait_recommended` is true. Do not busy-loop.
- `process_alive=true` only means that a process exists; it does not mean that
  generation, research, or the answer is finished. Read completed output with
  `messages_read(..., kind="result")`.
- Treat `completed` as done. Treat `unavailable`, `rate_limited`, and `stuck`
  as terminal failures and report them instead of waiting forever.
'''


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
    idempotency_key = f"{agent_name.lower()}-wake:{room_id}:{msg_id}"
    reply_call = _reply_call(room_id, agent_name, sender, idempotency_key,
                              kind="result", reply_to=msg_id)
    return f"""A new mcp-huddle request arrived.

Room: {room_id}
You are: {agent_name}
New request id: {msg_id}
From: {sender}
To: {addressed}
Last delivered message id: {last_seen}

{_lifecycle_protocol_block(room_id, agent_name, msg_id)}

{_evidence_protocol_block()}

Current full transcript:
{transcript}

Request body:
{body}

Protocol:
1. Call messages_read(room_id="{room_id}", since_id=0, limit=50).
2. If message #{msg_id} is kind=request addressed to {agent_name} or all and has no reply_to, answer it exactly once.
3. Post your answer with {reply_call}.

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
    reply_call = _reply_call(room_id, "Codex", sender,
                              f"codex-wake:{room_id}:{msg_id}",
                              kind="result", reply_to=msg_id)
    return f"""A new mcp-huddle request arrived in your existing room.

Room: {room_id}
You are: Codex
New request id: {msg_id}
From: {sender}
To: {addressed}
Last delivered message id: {last_seen}

{_lifecycle_protocol_block(room_id, "Codex", msg_id)}

{_evidence_protocol_block()}

Request body:
{body}

Use only huddle MCP tools for room coordination:
1. Call messages_read(room_id="{room_id}", since_id=0, limit=50).
2. If message #{msg_id} is a kind=request addressed to Codex or all and has no reply_to, answer it exactly once.
3. Post your answer with {reply_call}.

Do not answer requests that already have reply_to set. Do not send thanks/ack-only chatter.
"""


def _build_default_brief(room_id: str, name: str, goal: str, cwd: str,
                          agent_name: Optional[str] = None) -> str:
    """Default reviewer brief for auto_spawn=True.

    Wrapped in the same identity/reply-call framing as the wake-up prompt
    (room id, own name, exact message_post call, anti-loop rule) so a
    cold-spawned agent behaves the same as one woken mid-discussion —
    without this, small models spawned cold answered to stdout instead of
    calling message_post, or posted under another participant's name
    (observed live in room_31d32c82). `agent_name` is the actual per-agent
    identity to spawn under; omitted only by callers that don't yet know it
    (falls back to a generic "check your invite" placeholder).
    """
    who = agent_name or "the name you were spawned/invited under (see room_info)"
    idem_prefix = (agent_name or "agent").lower()
    reply_call = _reply_call(room_id, who, "<original sender, or 'all'>",
                              f"{idem_prefix}-init:{room_id}", kind="result")
    return f"""# mcp-huddle — Room: {name}

{_agent_identity_block(room_id, who)}
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

{_evidence_protocol_block()}

## How to reply (MANDATORY)
{_stdout_not_delivered_note()}
To reply: {reply_call}

## Anti-loop rules
{_anti_loop_block()}
- Keep a local set of reply_to IDs you already responded to — never reply twice to the same request

{_lifecycle_protocol_block(room_id, who, "initial")}

## How to connect
Use MCP tools from mcp-huddle:
  messages_read("{room_id}") — read chat
  message_post("{room_id}", "{who}", "...", kind="comment"|"request"|"result", ...) — post
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


def _wrap_user_brief(room_id: str, agent_name: str, brief: str) -> str:
    """Wrap a user-supplied per-agent brief (auto_spawn={name: brief}) with
    the same protocol preamble as the default brief/wake prompt.

    The user's text is the task; the preamble is the delivery protocol —
    both are required or small models drift into replying on stdout or
    under the wrong name (room_31d32c82). The user brief is preserved
    verbatim after the preamble.
    """
    idem = f"{agent_name.lower()}-init:{room_id}"
    reply_call = _reply_call(room_id, agent_name, "<original sender, or 'all'>",
                              idem, kind="result")
    return f"""{_agent_identity_block(room_id, agent_name)}

{_stdout_not_delivered_note()}
To reply: {reply_call}

{_anti_loop_block()}

{_evidence_protocol_block()}

{_lifecycle_protocol_block(room_id, agent_name, "initial")}

---

{brief}
"""


# ── App build (single MCP app + custom routes + watchdog lifespan) ──────────


def build_app():
    """Build the Starlette app: MCP transport + custom HTTP routes + watchdog.

    All HTTP routes are registered via @mcp.custom_route(), so streamable_http_app()
    returns a Starlette app with everything wired in. Its lifespan runs the MCP
    session_manager — we wrap it in `_watchdog_lifespan` (the same idempotent
    helper the stdio path gets via FastMCP's `lifespan=` constructor arg) so the
    watchdog starts once for the whole HTTP app's lifetime, not per session.
    """
    app = mcp.streamable_http_app()
    app.add_middleware(_HTTPGuardMiddleware)
    mcp_lifespan = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def combined_lifespan(a):
        async with mcp_lifespan(a):
            async with _watchdog_lifespan():
                yield

    app.router.lifespan_context = combined_lifespan
    return app
