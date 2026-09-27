"""Agent Bus — core file I/O layer.

All writes go through file-locked atomic append to prevent corruption
when multiple agents post simultaneously.
"""

import fcntl
import hashlib
import json
import math
import os
import stat
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

from . import child_processes

HUDDLE_HOME = Path(os.environ.get("MCP_HUDDLE_HOME", Path.home() / ".mcp-huddle"))
BUS_DIR = HUDDLE_HOME / "rooms"
NOTIFICATIONS_DIR = HUDDLE_HOME / "notifications"
NOTIFICATION_LOCKS_DIR = HUDDLE_HOME / "internal" / "notification-locks"


def _secure_dir(path: Path) -> None:
    """Best-effort tighten dir perms to 0o700 so room contents aren't
    world-readable on shared machines. Never crashes on exotic filesystems."""
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
CIRCUIT_BREAKER_WINDOW = 10   # last N messages to check
CIRCUIT_BREAKER_LIMIT = 5     # max consecutive from same agent (non-request kinds)
DEADLOCK_TIMEOUT_SECS = 600   # 10 minutes of silence → system message
ZOMBIE_CHECK_SECS = 30        # how often to check owner_pid liveness
ZOMBIE_GRACE_SECS = 300       # dead owner_pid alone isn't enough — a resumed
                              # session gets a NEW pid; require silence too before
                              # reaping, so room_reclaim / fresh posts spare it
MAX_BODY_CHARS = 2000         # per-message body cap in read_messages — one fat
                              # agent summary must not blow the reader's context
MAX_STORED_BODY_BYTES = 256 * 1024  # hard persistence cap; unlike
                                    # MAX_BODY_CHARS this rejects, not truncates
MAX_STORED_MESSAGE_BYTES = 320 * 1024  # complete serialized JSONL entry cap
# Machine/process-independent admission control. The persisted room log is the
# authority, and check+append happen under the same messages lock.
ROOM_MESSAGE_RATE_LIMIT = 120
ROOM_MESSAGE_RATE_WINDOW_SECS = 60
ROOM_CLOSE_FINALIZE_ATTEMPTS = 3
MAX_ROOM_NAME_CHARS = 160

VALID_KINDS = {"request", "comment", "ack", "busy", "result", "final", "system", "close"}
VALID_ROOM_STATUSES = {
    "open", "idle", "closing_requested", "closing", "closed", "resolved",
}

# `status` remains the operational lease used by the wake machinery. `phase`
# is the human/agent-facing lifecycle detail and is safe to extend without
# breaking callers that still consume get_status() -> {agent: "online"|"busy"}.
VALID_AGENT_PHASES = {
    "online", "queued", "starting", "thinking", "working", "responding",
    "completed", "unavailable", "rate_limited", "stuck",
}
_SERVER_TERMINAL_FAILURE_PHASES = frozenset({"unavailable", "rate_limited", "stuck"})


# ── Rooms ────────────────────────────────────────────────────────────────────

def _safe_path_component(value: str, label: str) -> str:
    """Validate an externally supplied single filesystem component.

    Keep printable historical identifiers (spaces and punctuation included),
    but reject traversal, platform separators and control characters.
    """
    if not isinstance(value, str) or not value or value in (".", ".."):
        raise ValueError(f"Invalid {label}")
    if len(value.encode("utf-8")) > 255:
        raise ValueError(f"Invalid {label}: too long")
    if "/" in value or "\\" in value or Path(value).is_absolute():
        raise ValueError(f"Invalid {label}: path separators are not allowed")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError(f"Invalid {label}: control characters are not allowed")
    return value


def _room_dir(room_id: str) -> Path:
    """Return a contained, non-symlink room directory path.

    ``BUS_DIR`` itself may be configured through a symlink, so containment is
    evaluated against its resolved destination. A room entry must still be a
    real direct child: room-level symlinks are rejected even when they point
    back inside the bus root.
    """
    component = _safe_path_component(room_id, "room_id")
    root = BUS_DIR.resolve(strict=False)
    candidate = BUS_DIR / component
    resolved = candidate.resolve(strict=False)
    if resolved.parent != root or candidate.is_symlink():
        raise ValueError(f"Invalid room_id {room_id!r}: path escapes rooms directory")
    return resolved


def _owned_parent_fd(path: Path) -> tuple[int, str]:
    """Open the stable parent of a managed file and return (dirfd, basename).

    Every path component below the resolved managed roots is opened relative to
    a directory fd with ``O_NOFOLLOW``. This prevents a room/agents directory
    swap from redirecting a later open outside the managed tree.
    """
    absolute = Path(os.path.abspath(path))
    bus_root = BUS_DIR.resolve(strict=False)
    notify_root = NOTIFICATIONS_DIR.resolve(strict=False)
    notify_locks_root = NOTIFICATION_LOCKS_DIR.resolve(strict=False)
    dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)

    try:
        relative = absolute.relative_to(bus_root)
    except ValueError:
        relative = None
    if relative is not None:
        if len(relative.parts) not in (2, 3):
            raise ValueError(f"Unsafe managed room path: {path}")
        room_name = _safe_path_component(relative.parts[0], "room_id")
        if len(relative.parts) == 3 and relative.parts[1] != "agents":
            raise ValueError(f"Unsafe managed room path: {path}")
        root_fd = os.open(bus_root, dir_flags)
        try:
            parent_fd = os.open(room_name, dir_flags, dir_fd=root_fd)
        finally:
            os.close(root_fd)
        if len(relative.parts) == 3:
            try:
                agents_fd = os.open("agents", dir_flags, dir_fd=parent_fd)
            finally:
                os.close(parent_fd)
            parent_fd = agents_fd
        return parent_fd, relative.parts[-1]

    for managed_root in (notify_root, notify_locks_root):
        try:
            relative = absolute.relative_to(managed_root)
        except ValueError:
            continue
        if len(relative.parts) != 1:
            raise ValueError(f"Unsafe managed notification path: {path}")
        return os.open(managed_root, dir_flags), relative.parts[0]
    raise ValueError(f"Path is outside managed storage: {path}")


def _safe_open_fd(path: Path, flags: int, mode: int = 0o600) -> int:
    """Open a managed file relative to a stable, non-symlink parent fd."""
    parent_fd, name = _owned_parent_fd(path)
    try:
        safety_flags = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        if flags & os.O_CREAT:
            # On Darwin concurrent O_CREAT|O_NOFOLLOW opens of the same absent
            # file can spuriously return ENOENT. An exclusive create followed
            # by a no-create open for the loser is deterministic and also
            # refuses a symlink that appears between the two attempts.
            try:
                return os.open(
                    name, flags | os.O_EXCL | safety_flags, mode, dir_fd=parent_fd)
            except FileExistsError:
                flags &= ~(os.O_CREAT | os.O_EXCL)
        return os.open(name, flags | safety_flags, mode, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def _safe_read_text(path: Path) -> str:
    fd = _safe_open_fd(path, os.O_RDONLY)
    with os.fdopen(fd, "r", encoding="utf-8") as fh:
        return fh.read()


def _safe_stat(path: Path) -> os.stat_result:
    parent_fd, name = _owned_parent_fd(path)
    try:
        result = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(result.st_mode):
            raise ValueError(f"Refusing symlink managed file: {path}")
        return result
    finally:
        os.close(parent_fd)


def _safe_exists(path: Path) -> bool:
    try:
        _safe_stat(path)
        return True
    except FileNotFoundError:
        return False


def _atomic_write_text(path: Path, text: str) -> None:
    """Atomically replace a managed regular file without following symlinks."""
    parent_fd, name = _owned_parent_fd(path)
    tmp_name = f".{name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
    tmp_created = False
    try:
        try:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        if current is not None and stat.S_ISLNK(current.st_mode):
            raise ValueError(f"Refusing symlink managed file: {path}")
        fd = os.open(
            tmp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=parent_fd,
        )
        tmp_created = True
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        tmp_created = False
    finally:
        if tmp_created:
            try:
                os.unlink(tmp_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        os.close(parent_fd)


def _ensure_agents_dir(room_id: str) -> None:
    """Create/open ``agents`` relative to a stable room fd, never a symlink."""
    rdir = _room_dir(room_id)
    room_fd, _ = _owned_parent_fd(rdir / "meta.json")
    # _owned_parent_fd returns the room fd for a direct room file.
    try:
        try:
            os.mkdir("agents", mode=0o700, dir_fd=room_fd)
        except FileExistsError:
            pass
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        agents_fd = os.open("agents", flags, dir_fd=room_fd)
        os.close(agents_fd)
    finally:
        os.close(room_fd)


def _agent_paths(room_id: str, agent_name: str, *, create: bool = False) -> tuple[Path, Path]:
    """Build contained, non-symlink event paths for an agent."""
    component = _safe_path_component(agent_name, "agent_name").lower()
    _safe_path_component(component, "normalized agent_name")
    rdir = _room_dir(room_id)
    agents_dir = rdir / "agents"
    if agents_dir.is_symlink():
        raise ValueError("Unsafe agents directory symlink")
    if create:
        _ensure_agents_dir(room_id)
        _secure_dir(agents_dir)
    agents_root = agents_dir.resolve(strict=False)
    if agents_root.parent != rdir.resolve(strict=False):
        raise ValueError("Unsafe agents directory")
    log_path = agents_dir / f"{component}.events.jsonl"
    last_path = agents_dir / f"{component}.last_message.txt"
    for path in (log_path, last_path):
        if path.resolve(strict=False).parent != agents_root or path.is_symlink():
            raise ValueError(f"Unsafe agent path for {agent_name!r}")
    return log_path, last_path


def create_room(name: str, owner: str, owner_pid: int, cwd: str = "",
                session_id: str = "", *, room_id: str | None = None) -> str:
    if room_id is None:
        room_id = f"room_{uuid.uuid4().hex[:8]}"
    elif (
        not isinstance(room_id, str)
        or len(room_id) != 13
        or not room_id.startswith("room_")
        or any(char not in "0123456789abcdef" for char in room_id[5:])
    ):
        # Validate caller-controlled IDs before creating or changing any
        # storage directories. Explicit IDs use the same compact form as the
        # generated IDs, which also keeps them safe as one path component.
        raise ValueError("Invalid room_id: expected room_ followed by 8 lowercase hex digits")
    BUS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    rdir = _room_dir(room_id)
    root_fd = os.open(
        BUS_DIR.resolve(),
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.mkdir(room_id, mode=0o700, dir_fd=root_fd)
    finally:
        os.close(root_fd)
    # parents=True creates HUDDLE_HOME / BUS_DIR with the default umask mode, so
    # tighten the root data dirs explicitly (best-effort).
    _secure_dir(HUDDLE_HOME)
    _secure_dir(BUS_DIR)
    _secure_dir(rdir)

    now = int(time.time())
    meta = {
        "id": room_id,
        "name": name,
        "owner": owner,
        "owner_pid": owner_pid,
        "session_id": session_id,
        "participants": [owner],
        "spawned_pids": [],
        "created_at": now,
        "status": "open",
        "cwd": cwd,
        "last_activity": now,
        "last_activity_at": now,
        "resolution": None,
    }
    _write_json(rdir / "meta.json", meta)
    _write_json(rdir / "status.json", {
        owner: {
            "status": "online", "phase": "online", "expires_at": 0,
            "session_id": session_id, "updated_at": now, "source": "server",
        }
    })
    return room_id


def invite_agent(room_id: str, agent_name: str) -> None:
    def _update(meta: dict) -> dict:
        if agent_name not in meta["participants"]:
            meta["participants"].append(agent_name)
        return meta
    _update_meta_locked(room_id, _update)
    # Re-inviting is idempotent and must not turn a live busy/working lease
    # back into online. The status lock makes the create-if-missing decision
    # atomic with concurrent lifecycle writers.
    _patch_status(
        room_id, agent_name, "online", 0, "", only_if_missing=True,
    )


def register_external_agent(room_id: str, agent_name: str) -> dict:
    """Reserve <name>.events.jsonl + last_message.txt for an agent that wasn't
    auto-spawned by huddle (e.g. orchestrator-launched CLIs). Adds an entry to
    room_meta.agent_meta so /api/room_agents and the dashboard activity panel
    pick it up. Returns {log_path, last_message_path}."""
    log_path, last_path = _agent_paths(room_id, agent_name, create=True)
    # O_NOFOLLOW closes the final-component symlink race left by a plain
    # Path.touch(). The containment checks above protect parent traversal.
    flags = os.O_WRONLY | os.O_CREAT
    fd = _safe_open_fd(log_path, flags, 0o600)
    os.close(fd)

    def _update(meta: dict) -> dict:
        am = meta.setdefault("agent_meta", {})
        current = am.get(agent_name)
        if not isinstance(current, dict):
            current = {}
        current.update({
            "log_path": str(log_path),
            "last_message_path": str(last_path),
            "external": True,
        })
        current.setdefault("thread_id", "")
        am[agent_name] = current
        return meta

    # Locked RMW: a concurrent wake-thread agent_meta update must not be lost.
    _update_meta_locked(room_id, _update)
    return {"log_path": str(log_path), "last_message_path": str(last_path)}


def append_agent_event(room_id: str, agent_name: str, event: dict) -> None:
    """Append a JSONL event line that the SSE handler tails into the dashboard's
    Agent-activity panel. Persists, so re-opening a closed room replays history."""
    log_path, _ = _agent_paths(room_id, agent_name, create=True)
    payload = json.dumps(event, ensure_ascii=False)
    with _lock(log_path) as f:
        f.seek(0, 2)
        f.write(payload + "\n")


def get_room_info(room_id: str) -> dict:
    return _read_meta(room_id)


def rename_room(room_id: str, name: str, owner: str) -> dict:
    """Rename an existing room without changing its identity or history.

    The room owner is checked while holding the same metadata lock used for
    the update, so a rename cannot race a concurrent metadata change.
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("room name must be non-empty")
    name = name.strip()
    if len(name) > MAX_ROOM_NAME_CHARS:
        raise ValueError(f"room name must be at most {MAX_ROOM_NAME_CHARS} characters")
    if not isinstance(owner, str) or not owner.strip():
        raise ValueError("room owner is required")
    owner = owner.strip()

    def _update(meta: dict) -> dict:
        if meta.get("owner") != owner:
            raise PermissionError("only the room owner may rename it")
        meta["name"] = name
        return meta

    try:
        return _update_meta_locked(room_id, _update)
    except FileNotFoundError as e:
        # Opening the per-room lock fails before _update_meta_locked can
        # inspect meta.json when the directory does not exist.
        raise ValueError(f"Room '{room_id}' not found") from e


def mark_idle(room_id: str) -> None:
    """Mark an open room idle under a file lock."""
    def update(meta: dict) -> dict:
        if meta.get("status") == "open":
            meta["status"] = "idle"
        return meta

    _update_meta_locked(room_id, update)


def revive(room_id: str) -> None:
    """Reopen an idle room under a file lock."""
    def update(meta: dict) -> dict:
        if meta.get("status") == "idle":
            meta["status"] = "open"
            now = int(time.time())
            meta["last_activity"] = now
            meta["last_activity_at"] = now
        return meta

    _update_meta_locked(room_id, update)


def list_rooms() -> list[dict]:
    if not BUS_DIR.exists():
        return []
    rooms = []
    for rdir in BUS_DIR.iterdir():
        try:
            rooms.append(_read_meta(rdir.name))
        except Exception:
            pass
    return sorted(rooms, key=lambda r: r.get("created_at", 0), reverse=True)


def request_close(room_id: str, agent: str) -> str:
    outcome: dict = {}

    def _update(meta: dict) -> dict:
        if meta["status"] != "open":
            outcome["status"] = meta["status"]
            return meta
        meta["status"] = "closing_requested"
        outcome["status"] = "closing_requested"
        return meta

    _update_meta_locked(room_id, _update)
    if outcome["status"] == "closing_requested":
        _append_system(room_id, f"[{agent}] запросил закрытие комнаты. Подтверди: room_close('{room_id}')")
    return outcome["status"]


def _close_room_once(
    room_id: str,
    terminal_text: str,
    expected_owner: str | None = None,
    claim_if: Callable[[dict], bool] | None = None,
) -> tuple[bool, dict[str, int], dict]:
    """One shared closing -> terminate -> marker -> closed transition."""
    leader: dict = {}

    def _claim_close(meta: dict) -> dict:
        if expected_owner is not None and meta.get("owner") != expected_owner:
            raise ValueError(f"{expected_owner!r} is not the owner of {room_id}")
        if meta.get("status") in ("closed", "closing"):
            leader["close"] = False
            leader["meta"] = dict(meta)
            return meta
        if claim_if is not None and not claim_if(meta):
            leader["close"] = False
            leader["meta"] = dict(meta)
            return meta
        # The intermediate state is the single-winner claim. delete_room only
        # accepts `closed`, so it cannot remove the directory while termination
        # and the terminal marker are still in flight. No locks are nested.
        leader["close"] = True
        leader["meta"] = dict(meta)
        meta["status"] = "closing"
        return meta

    _update_meta_locked(room_id, _claim_close)
    if not leader.get("close"):
        return False, {"sent": 0, "exited": 0, "denied": 0}, leader.get("meta", {})
    # Only exact Popen objects created by this server instance are authority.
    # Persisted PIDs may have been reused after a restart and are never sent a
    # signal. The room tombstone also terminates a spawn that registers late.
    counts = {"sent": 0, "exited": 0, "denied": 0}
    close_errors: list[BaseException] = []
    try:
        counts = child_processes.close_room(room_id)
    except BaseException as exc:  # preserve the lifecycle invariant on shutdown
        close_errors.append(exc)
    try:
        # Keep the room non-deletable until every close side effect has been
        # attempted. A teardown failure must not suppress the terminal marker.
        _append_terminal_system(room_id, terminal_text)
    except BaseException as exc:  # preserve the lifecycle invariant on shutdown
        close_errors.append(exc)
    try:
        finalize_error: BaseException | None = None
        for _attempt in range(ROOM_CLOSE_FINALIZE_ATTEMPTS):
            try:
                _update_meta_locked(
                    room_id, lambda meta: {**meta, "status": "closed"},
                )
                finalize_error = None
                break
            except Exception as exc:
                # Retry a transient atomic-write/read failure, but keep the
                # room fail-closed if storage remains unavailable.
                finalize_error = exc
        if finalize_error is not None:
            raise finalize_error
    except BaseException as exc:  # do not mask an earlier teardown/marker error
        close_errors.append(exc)
    if close_errors:
        primary = close_errors[0]
        for secondary in close_errors[1:]:
            primary.add_note(
                f"Additional room-close failure: {type(secondary).__name__}: {secondary}"
            )
        raise primary
    return True, counts, leader["meta"]


def close_room(room_id: str, owner: str) -> None:
    _close_room_once(room_id, "Чат закрыт.", expected_owner=owner)


def close_session_rooms(session_id: str) -> list[str]:
    if not isinstance(session_id, str) or not session_id:
        return []
    closed = []
    for meta in list_rooms():
        if meta.get("session_id") != session_id:
            continue

        def _still_owned_by_session(current: dict) -> bool:
            return (current.get("session_id") == session_id
                    and current.get("status") in ("open", "closing_requested", "idle"))

        claimed, _, _ = _close_room_once(
            meta["id"], "Чат закрыт.", expected_owner=meta["owner"],
            claim_if=_still_owned_by_session,
        )
        if claimed:
            closed.append(meta["id"])
    return closed


def delete_room(room_id: str, owner: str) -> None:
    """Permanently remove a room from disk (history wipe).

    Safety: only allowed on rooms with status == 'closed'. Open rooms must be
    closed first via close_room() — это защита от случайной потери активного
    обсуждения.

    Side effects: рекурсивно удаляет ~/.mcp-huddle/rooms/<room_id>/, including
    messages.jsonl, meta.json, status.json, agents/<name>.events.jsonl.
    Exact locally owned child processes were already handled by close_room;
    persisted PIDs are never signalling authority.
    """
    import shutil
    meta = _read_meta(room_id)
    if meta.get("owner") != owner:
        raise ValueError(f"{owner!r} is not the owner of {room_id}")
    if meta["status"] != "closed":
        raise ValueError(
            f"Cannot delete room with status '{meta['status']}'. "
            "Close it first via room_close()."
        )
    rdir = _room_dir(room_id)
    if rdir.exists():
        shutil.rmtree(rdir)
    _evict_msg_cache(room_id)


# ── Messages ─────────────────────────────────────────────────────────────────

def post_message(room_id: str, agent: str, body: str, kind: str,
                 to: Optional[str] = None, reply_to: Optional[int] = None,
                 idempotency_key: Optional[str] = None,
                 msg_meta: Optional[dict] = None) -> int:
    if kind not in VALID_KINDS:
        raise ValueError(f"Invalid kind '{kind}'. Valid: {sorted(VALID_KINDS)}")
    if not isinstance(agent, str) or not agent:
        raise ValueError("Message agent must be a non-empty string")
    if to is not None and not isinstance(to, str):
        raise ValueError("Message to must be a string or null")
    if idempotency_key is not None and not isinstance(idempotency_key, str):
        raise ValueError("idempotency_key must be a string or null")
    if msg_meta is not None and not isinstance(msg_meta, dict):
        raise ValueError("msg_meta must be an object or null")
    if not isinstance(body, str):
        raise ValueError("Message body must be a string")
    body_bytes = len(body.encode("utf-8"))
    if body_bytes > MAX_STORED_BODY_BYTES:
        raise ValueError(
            f"Message body is too large ({body_bytes} bytes; "
            f"maximum {MAX_STORED_BODY_BYTES})"
        )

    meta = _read_meta(room_id)
    if meta["status"] in ("closing", "closed"):
        raise ValueError("Room is closing or closed.")
    if meta["status"] == "resolved" and kind not in ("system", "close"):
        raise ValueError("Room is resolved and read-only.")

    rdir = _room_dir(room_id)
    msgs_file = rdir / "messages.jsonl"

    with _lock(msgs_file) as f:
        # Re-validate room state under the messages lock: a concurrent
        # close_room / resolution_vote may have transitioned the room between
        # the pre-lock read above and here. Without this, a message can land in
        # an already-closed/resolved room.
        cur = _read_meta(room_id)
        if cur["status"] in ("closing", "closed"):
            raise ValueError("Room is closing or closed.")
        if cur["status"] == "resolved" and kind not in ("system", "close"):
            raise ValueError("Room is resolved and read-only.")

        # Load the locked history once. Idempotency keys must remain durable
        # after arbitrarily many later messages and across process restarts.
        persisted_messages = _load_messages_unlocked(room_id)
        if idempotency_key:
            for msg in persisted_messages:
                if (isinstance(msg, dict)
                        and msg.get("idempotency_key") == idempotency_key
                        and "id" in msg):
                    return msg["id"]

        _check_room_rate_locked(persisted_messages, int(time.time()))

        # Check and append are one atomic decision. Previously two concurrent
        # posts could both pass the pre-lock check and exceed the streak cap.
        # Human override retains its documented bypass.
        if agent != "Human" and kind not in ("request", "system"):
            _check_circuit_breaker_messages(persisted_messages, agent)

        # reply_to validation — done under the messages lock so the check is
        # atomic with the append below: a parallel duplicate reply from the
        # same agent cannot slip through the gap between validate and write.
        if reply_to is not None:
            _validate_reply_to_locked(room_id, int(reply_to), agent, kind)

        # Assign next ID
        msg_id = _next_id(msgs_file)

        entry: dict = {
            "id": msg_id,
            "agent": agent,
            "kind": kind,
            "timestamp": int(time.time()),
            "body": body,
        }
        cur_round = int(cur.get("current_round", 0) or 0)
        if cur_round:
            entry["round"] = cur_round
        if to:
            entry["to"] = to
        if reply_to is not None:
            entry["reply_to"] = reply_to
        if idempotency_key:
            entry["idempotency_key"] = idempotency_key
        if msg_meta:
            clean = {k: msg_meta[k] for k in ("model", "reasoning", "tokens_in",
                                               "tokens_out", "tokens_total", "duration_ms")
                     if k in msg_meta and msg_meta[k] is not None}
            if clean:
                entry["meta"] = clean

        serialized = _serialize_message_entry(entry)
        f.seek(0, 2)  # EOF
        f.write(serialized + "\n")

    # Update activity in meta.
    def update_activity(current: dict) -> dict:
        now = int(time.time())
        current["last_activity"] = now
        current["last_activity_at"] = now
        return current

    meta = _update_meta_locked(room_id, update_activity)

    # Notify relevant agents (only for kind=request)
    if kind == "request":
        _notify_agents(room_id, meta["participants"], agent, to, msg_id)

    return msg_id


def _validate_reply_to_locked(room_id: str, target_id: int, agent: str, kind: str) -> None:
    """Validate a reply_to target. Call ONLY while holding the messages lock so
    the check is atomic with the append that follows.

    Rules:
      * target must exist; a `comment` can attach to any message (including
        another annotation), while other kinds retain request-only replies;
      * the replying agent must have been an addressee (`to` empty / "all" /
        the agent itself) — Human/System bypass this;
      * a broadcast request (`to=all` / no `to`) expects one terminal reply
        per addressee, so we reject only a SECOND `result`/`final` from the
        SAME agent — replies from other agents are allowed. Progress messages
        (`ack`, `busy`, `comment`) do not consume that terminal reply.
    """
    # Caller holds the messages LOCK_EX; read lock-free to avoid self-deadlock.
    messages = _load_messages_unlocked(room_id)
    target = next((m for m in messages if m.get("id") == target_id), None)
    if target is None:
        raise ValueError("reply_to target not found")
    if target.get("kind") != "request":
        if kind == "comment":
            return
        raise ValueError("reply_to target must be a request for this message kind")
    target_to = target.get("to")
    if (agent not in ("Human", "System")
            and target_to and target_to not in ("all", agent)):
        raise ValueError(
            f"reply_to target #{target_id} was addressed to {target_to!r}, "
            f"not to {agent!r}")
    if kind in {"result", "final"} and any(
            m.get("reply_to") == target_id
            and m.get("agent") == agent
            and m.get("kind") in {"result", "final"}
            for m in messages):
        raise ValueError(f"{agent} already answered request #{target_id}")


def _truncate_body(body: str, max_chars: int, msg_id: int) -> str:
    """Head+tail truncation: keep the opening AND the end of a long message —
    agent conclusions usually sit at the END, so head-only truncation drops the
    actionable part. Marker says how to fetch the full body."""
    if max_chars <= 0 or len(body) <= max_chars:
        return body
    head = (max_chars * 3) // 5          # 60% opening
    tail = max_chars - head              # 40% closing (where conclusions live)
    dropped = len(body) - max_chars
    return (body[:head] +
            f"\n…[{dropped} chars cut — read since_id={msg_id - 1}&limit=1&max_chars=0 for full]…\n" +
            body[-tail:])


def read_messages(room_id: str, since_id: int = 0, limit: int = 20,
                  until_id: int = 0, max_chars: int = MAX_BODY_CHARS,
                  round: int = 0, kind: str = "") -> str:
    """Return plain-text chat log for LLM consumption.

    since_id / until_id bound the window (id in (since_id, until_id]); until_id<=0
    means "up to newest". round>0 returns only that round's messages, round=-1
    the current round, round=0 (default) ignores rounds. kind (comma-separated,
    e.g. "result,final") keeps only those kinds — handy to grab just the workers'
    deliverables. Bodies longer than max_chars (0 = unlimited) are head+tail
    truncated so one huge message can't overflow the reader.
    """
    meta = _read_meta(room_id)
    msgs = _load_messages(room_id)

    if round != 0:
        target = int(meta.get("current_round", 0) or 0) if round < 0 else round
        msgs = [m for m in msgs if int(m.get("round", 0) or 0) == target]
    if kind:
        wanted = {k.strip() for k in kind.split(",") if k.strip()}
        msgs = [m for m in msgs if m["kind"] in wanted]
    if since_id > 0:
        msgs = [m for m in msgs if m["id"] > since_id]
    if until_id > 0:
        msgs = [m for m in msgs if m["id"] <= until_id]
    if len(msgs) > limit:
        msgs = msgs[-limit:]

    participants = " · ".join(meta["participants"])
    lines = [f"=== Chat: {meta['name']} | {participants} ==="]
    for m in msgs:
        addr = f" → {m['to']}" if m.get("to") else ""
        knd = f"[{m['kind']}]" if m["kind"] not in ("comment",) else ""
        re_tag = f" (re:#{m['reply_to']})" if m.get("reply_to") else ""
        ts = time.strftime("%H:%M", time.localtime(m["timestamp"]))
        body = _truncate_body(m["body"], max_chars, m["id"])
        lines.append(f"[{m['id']:03d}] {m['agent']}{addr} {knd}  {body}{re_tag}  [{ts}]")

    if not msgs:
        lines.append("(no new messages)")
    return "\n".join(lines)


def summarize_messages(room_id: str, since_id: int = 0, round: int = 0) -> str:
    """Digest of recent messages — for catching up cheaply (no LLM call).

    Scope: round>0 → that round, round=-1 → current round, else since_id (0=all).
    Reports message counts per agent, still-open requests, and each agent's
    LATEST position (their most recent message) — the "where does everyone stand"
    view an orchestrator wants between rounds.
    """
    meta = _read_meta(room_id)
    msgs = _load_messages(room_id)

    if round != 0:
        target = int(meta.get("current_round", 0) or 0) if round < 0 else round
        msgs = [m for m in msgs if int(m.get("round", 0) or 0) == target]
        scope = f"round {target}"
    else:
        if since_id > 0:
            msgs = [m for m in msgs if m["id"] > since_id]
        scope = f"since #{since_id}" if since_id else "whole room"
    if not msgs:
        return f"No messages ({scope})."

    total = len(msgs)
    agents_seen: dict[str, int] = {}
    latest: dict[str, dict] = {}
    requests_open = []
    answered = {m.get("reply_to") for m in msgs if m.get("reply_to")}
    for m in msgs:
        agents_seen[m["agent"]] = agents_seen.get(m["agent"], 0) + 1
        if m["agent"] != "System":
            latest[m["agent"]] = m  # last write wins → most recent per agent
        if m["kind"] == "request" and not m.get("reply_to") and m["id"] not in answered:
            requests_open.append(f"#{m['id']} {m['agent']}→{m.get('to', 'all')}: {m['body'][:80]}")

    summary = f"[Digest: {total} messages | {scope}]\n"
    summary += "Counts: " + ", ".join(f"{a}({c})" for a, c in agents_seen.items()) + "\n"
    if requests_open:
        summary += "Open requests:\n" + "\n".join(f"  {r}" for r in requests_open[-5:]) + "\n"
    summary += "Latest per agent:\n"
    for a, m in latest.items():
        body = m["body"].replace("\n", " ")
        summary += f"  {a} [{m['kind']}]: {body[:200]}\n"
    return summary


# ── Status ───────────────────────────────────────────────────────────────────

def set_status(room_id: str, agent: str, status: str,
               expires_in_sec: int = 0, session_id: str = "",
               phase: str = "", task_id: int | str = "", detail: str = "",
               source: str = "server") -> None:
    expires_at = int(time.time()) + expires_in_sec if expires_in_sec > 0 else 0
    _patch_status(room_id, agent, status, expires_at, session_id,
                  phase=phase, task_id=task_id, detail=detail, source=source)


def get_status(room_id: str) -> dict:
    return {agent: info["status"]
            for agent, info in get_status_details(room_id).items()}


def get_status_details(room_id: str) -> dict:
    """Return normalized lifecycle records while preserving legacy leases.

    Readers may observe an old status.json written before ``phase`` existed;
    derive a phase in memory instead of rewriting it. Expired leases are reset
    under the existing status lock so concurrent wake writers are not clobbered.
    """
    status_file = _room_dir(room_id) / "status.json"
    if not _safe_exists(status_file):
        return {}
    try:
        data = json.loads(_safe_read_text(status_file))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}

    now = int(time.time())
    result: dict = {}
    expired: list[str] = []
    fallback_phase = {
        "online": "online", "busy": "working", "done": "completed",
        "typing": "responding",
    }
    for agent, raw in data.items():
        info = dict(raw) if isinstance(raw, dict) else {"status": "online"}
        status = info.get("status", "online")
        expires = int(info.get("expires_at", 0) or 0)
        if expires > 0 and now > expires:
            status = "online"
            info["status"] = status
            info["expires_at"] = 0
            info["phase"] = "online"
            expired.append(agent)
        else:
            info["status"] = status
            info.setdefault("phase", fallback_phase.get(status, status))
        info.setdefault("updated_at", 0)
        result[agent] = info

    if expired:
        with _lock(_room_dir(room_id) / "status.lock"):
            if _safe_exists(status_file):
                try:
                    latest = json.loads(_safe_read_text(status_file))
                except Exception:
                    latest = {}
                changed = False
                for agent in expired:
                    info = latest.get(agent)
                    if (isinstance(info, dict)
                            and int(info.get("expires_at", 0) or 0) > 0
                            and now > int(info["expires_at"])):
                        info["status"] = "online"
                        info["expires_at"] = 0
                        info["phase"] = "online"
                        info["updated_at"] = now
                        changed = True
                if changed:
                    _write_json(status_file, latest)
    return result


# ── Resolution / consensus ────────────────────────────────────────────────────

def propose_resolution(room_id: str, agent: str, text: str) -> str:
    res_id = f"res_{uuid.uuid4().hex[:6]}"

    def _update(meta: dict) -> dict:
        meta["resolution"] = {
            "id": res_id,
            "proposed_by": agent,
            "text": text,
            "votes": {agent: "ack"},
            "status": "voting",
        }
        return meta

    # Locked RMW so a concurrent wake-thread agent_meta update isn't clobbered.
    _update_meta_locked(room_id, _update)
    # System message posted AFTER the lock — post_message re-acquires the meta
    # lock, so doing it inside _update would self-deadlock.
    _append_system(room_id,
        f"[Resolution proposed by {agent}]: {text}\n"
        f"Все участники: вызовите resolution_vote('{room_id}', ..., '{res_id}', 'ack'|'reject')")
    return res_id


def resolution_vote(room_id: str, agent: str, resolution_id: str, vote: str) -> str:
    if vote not in ("ack", "reject"):
        raise ValueError("vote must be 'ack' or 'reject'")

    outcome: dict = {}

    def _update(meta: dict) -> dict:
        res = meta.get("resolution")
        if not res or res["id"] != resolution_id:
            raise ValueError(f"Resolution {resolution_id} not found")
        res["votes"][agent] = vote
        participants = [p for p in meta["participants"] if p != "Human"]
        if vote == "reject":
            res["status"] = "rejected"
            outcome["system_msg"] = f"[{agent}] отклонил резолюцию: {res['text'][:80]}"
        elif all(res["votes"].get(p) == "ack" for p in participants):
            res["status"] = "accepted"
            meta["status"] = "resolved"
            outcome["system_msg"] = (
                f"Консенсус достигнут! Резолюция принята: {res['text']}\n"
                "Чат переведён в read-only. Оркестратор может закрыть чат.")
        outcome["status"] = res["status"]
        return meta

    # ValueError from _update (unknown resolution) propagates with no write.
    _update_meta_locked(room_id, _update)
    if outcome.get("system_msg"):
        _append_system(room_id, outcome["system_msg"])
    return outcome["status"]


# ── Notifications ─────────────────────────────────────────────────────────────

def _notification_path(notify_file: str) -> Path:
    """Resolve a notify target inside the managed notification directory.

    Absolute paths remain accepted for compatibility only when they name a
    direct child of that directory. Relative basenames are interpreted beneath
    it. The deliberately flat namespace removes intermediate-directory symlink
    races; a final-component symlink is also rejected.
    """
    if not isinstance(notify_file, str) or not notify_file or "\x00" in notify_file:
        raise ValueError("Invalid notification path")
    NOTIFICATIONS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    _secure_dir(NOTIFICATIONS_DIR)
    root = NOTIFICATIONS_DIR.resolve(strict=True)

    supplied = Path(notify_file)
    configured_root = Path(os.path.abspath(NOTIFICATIONS_DIR))
    if supplied.is_absolute():
        lexical = Path(os.path.abspath(supplied))
        if lexical.parent not in (configured_root, root):
            raise ValueError("Notification path must be inside HUDDLE_HOME/notifications")
        name = lexical.name
    else:
        if len(supplied.parts) != 1:
            raise ValueError("Notification target must be a direct file in the managed directory")
        name = supplied.name
        lexical = configured_root / name
    _safe_path_component(name, "notification filename")
    if lexical.is_symlink():
        raise ValueError("Notification path must not contain symlinks")
    canonical = root / name
    if canonical.is_symlink():
        raise ValueError("Notification path must not contain symlinks")
    return canonical


def _notification_lock_path(target: Path) -> Path:
    """Return a per-target lock in a private namespace, never beside targets."""
    if NOTIFICATION_LOCKS_DIR.parent.is_symlink() or NOTIFICATION_LOCKS_DIR.is_symlink():
        raise ValueError("Notification lock directory must not be a symlink")
    NOTIFICATION_LOCKS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    _secure_dir(NOTIFICATION_LOCKS_DIR.parent)
    _secure_dir(NOTIFICATION_LOCKS_DIR)
    digest = hashlib.sha256(str(target).encode("utf-8")).hexdigest()
    return NOTIFICATION_LOCKS_DIR.resolve(strict=True) / f"{digest}.lock"


def register_notify(room_id: str, agent: str, notify_file: str) -> None:
    """Register an atomic, last-message-wins notification target.

    Targets are restricted to ``HUDDLE_HOME/notifications``. Delivery uses a
    per-target advisory lock plus atomic replace; if concurrent requests finish
    out of order, the stored ``msg_id`` check prevents an older notification
    from replacing a newer one. The existing one-file signal API cannot retain
    every event, but readers always see one complete JSON object for the newest
    delivered message.
    """
    _safe_path_component(agent, "notification agent")
    rdir = _room_dir(room_id)
    safe_notify_file = _notification_path(notify_file)
    notif_registry = rdir / "notify_registry.json"
    # Locked RMW: parallel registrations must not clobber each other.
    with _lock(rdir / "notify.lock"):
        data = {}
        if _safe_exists(notif_registry):
            try:
                data = json.loads(_safe_read_text(notif_registry))
            except (json.JSONDecodeError, OSError) as e:
                raise ValueError("Corrupt notify registry; refusing to overwrite") from e
            if not isinstance(data, dict):
                raise ValueError("Corrupt notify registry; expected an object")
        data[agent] = str(safe_notify_file)
        _write_json(notif_registry, data)


# ── Zombie watchdog (called by server background task) ────────────────────────

def check_zombie_rooms() -> list[str]:
    """Return room ids atomically claimed closed after a dead-owner probe.

    ``list_rooms`` and ``kill(pid, 0)`` are necessarily snapshots.  The final
    close claim therefore revalidates the observed owner identity, lifecycle
    and activity under the room's meta lock.  A concurrent ``room_reclaim`` or
    fresh message wins and keeps the room open.
    """
    closed = []
    now = int(time.time())
    for meta in list_rooms():
        # idle rooms whose owner has died are dead weight — reap them too, so
        # they don't pile up forever (idle has no auto-close transition otherwise).
        if meta["status"] not in ("open", "closing_requested", "idle"):
            continue
        pid = meta.get("owner_pid", 0)
        if pid <= 0:
            continue
        try:
            os.kill(pid, 0)  # raises if dead
        except ProcessLookupError:
            # A resumed owner session gets a NEW pid, so the old one looks dead
            # while the room is still actively used. Spare an open/closing room
            # until it has ALSO been silent past the grace window — a live/resumed
            # session keeps last_activity fresh (or calls room_reclaim). Idle
            # rooms are already stale → reap immediately to avoid pileup.
            last = int(meta.get("last_activity") or meta.get("created_at") or 0)
            if meta["status"] != "idle" and now - last <= ZOMBIE_GRACE_SECS:
                continue

            observed = {
                key: meta.get(key)
                for key in (
                    "owner", "owner_pid", "session_id", "status",
                    "last_activity", "last_activity_at",
                )
            }

            def _still_same_zombie(current: dict) -> bool:
                if any(current.get(key) != value
                       for key, value in observed.items()):
                    return False
                current_last = int(
                    current.get("last_activity")
                    or current.get("created_at")
                    or 0
                )
                return (current.get("status") == "idle"
                        or now - current_last > ZOMBIE_GRACE_SECS)

            claimed, _, _ = _close_room_once(
                meta["id"],
                "Чат закрыт.",
                expected_owner=meta["owner"],
                claim_if=_still_same_zombie,
            )
            if claimed:
                closed.append(meta["id"])
        except PermissionError:
            pass  # process exists, we just can't signal it
    return closed


def reclaim_room(room_id: str, owner: str, owner_pid: int,
                 session_id: str = "") -> dict:
    """Re-stamp owner_pid (+ session_id) after the owner's session resumed with a
    new PID, so the zombie-watchdog won't reap a live room. Only the recorded
    owner may reclaim."""
    info = get_room_info(room_id)
    if not info:
        raise ValueError(f"unknown room {room_id}")
    if info.get("owner") != owner:
        raise ValueError(f"{owner!r} is not the owner of {room_id}")

    def _update(meta: dict) -> dict:
        meta["owner_pid"] = int(owner_pid)
        if session_id:
            meta["session_id"] = session_id
        return meta

    return _update_meta_locked(room_id, _update)


def advance_round(room_id: str, owner: str, label: str = "") -> int:
    """Open a new round (owner-only). Increments meta.current_round, posts a
    visible divider so every agent sees the boundary, and stamps subsequent
    messages with the new round number. Returns the new round number."""
    info = get_room_info(room_id)
    if not info:
        raise ValueError(f"unknown room {room_id}")
    if info.get("owner") != owner:
        raise ValueError(f"{owner!r} is not the owner of {room_id}")

    def _update(meta: dict) -> dict:
        meta["current_round"] = int(meta.get("current_round", 0) or 0) + 1
        return meta

    meta = _update_meta_locked(room_id, _update)
    n = int(meta["current_round"])
    divider = f"━━━ Round {n}" + (f": {label}" if label else "") + " ━━━"
    _append_system(room_id, divider)
    return n


def check_deadlock_rooms() -> list[str]:
    """Inject timeout system message for rooms silent > DEADLOCK_TIMEOUT_SECS."""
    notified = []
    now = int(time.time())
    for meta in list_rooms():
        if meta["status"] != "open":
            continue
        last = meta.get("last_activity", meta["created_at"])
        if now - last > DEADLOCK_TIMEOUT_SECS:
            # _append_system → post_message bumps last_activity under the meta
            # lock, which resets the timer. No extra (unlocked) write needed —
            # the previous manual rewrite here could clobber a concurrent
            # agent_meta wake update.
            _append_system(meta["id"],
                f"[System] Timeout: комната молчит {DEADLOCK_TIMEOUT_SECS // 60} мин. "
                "Есть незакрытый вопрос?")
            notified.append(meta["id"])
    return notified


# ── Internal helpers ──────────────────────────────────────────────────────────

def _read_meta(room_id: str) -> dict:
    p = _room_dir(room_id) / "meta.json"
    if not _safe_exists(p):
        raise ValueError(f"Room '{room_id}' not found")
    try:
        meta = json.loads(_safe_read_text(p))
    except (json.JSONDecodeError, ValueError, OSError) as e:
        print(f"[huddle] WARN: corrupt/unreadable meta.json for "
              f"'{room_id}': {e}", file=sys.stderr)
        raise ValueError(f"Corrupt/unreadable meta.json for room {room_id!r}") from e
    return _validate_meta(room_id, meta)


def _validate_meta(room_id: str, meta: object) -> dict:
    """Validate the minimum semantic identity/lifecycle contract for a room."""
    if not isinstance(meta, dict):
        raise ValueError(f"Corrupt meta.json for room {room_id!r}: expected an object")
    if meta.get("id") != room_id:
        raise ValueError(f"Corrupt meta.json for room {room_id!r}: id mismatch")
    if meta.get("status") not in VALID_ROOM_STATUSES:
        raise ValueError(f"Corrupt meta.json for room {room_id!r}: invalid status")
    owner = meta.get("owner")
    if not isinstance(owner, str) or not owner:
        raise ValueError(f"Corrupt meta.json for room {room_id!r}: invalid owner")
    participants = meta.get("participants")
    if (not isinstance(participants, list)
            or not participants
            or not all(isinstance(item, str) and item for item in participants)
            or owner not in participants):
        raise ValueError(f"Corrupt meta.json for room {room_id!r}: invalid participants")
    created_at = meta.get("created_at")
    if (isinstance(created_at, bool)
            or not isinstance(created_at, (int, float))
            or not math.isfinite(created_at)
            or created_at < 0):
        raise ValueError(f"Corrupt meta.json for room {room_id!r}: invalid created_at")
    return meta


def _write_json(path: Path, data: dict) -> None:
    _atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def _update_meta_locked(room_id: str, update_fn) -> dict:
    rdir = _room_dir(room_id)
    meta_path = rdir / "meta.json"
    with _lock(rdir / "meta.lock"):
        if not _safe_exists(meta_path):
            raise ValueError(f"Room '{room_id}' not found")
        try:
            meta = json.loads(_safe_read_text(meta_path))
        except (json.JSONDecodeError, ValueError, OSError) as e:
            print(f"[huddle] WARN: corrupt/unreadable meta.json (locked) for "
                  f"'{room_id}': {e}", file=sys.stderr)
            raise ValueError(
                f"Corrupt/unreadable meta.json for room {room_id!r}; refusing to overwrite"
            ) from e
        try:
            meta = _validate_meta(room_id, meta)
        except ValueError as e:
            raise ValueError(
                f"Corrupt meta.json for room {room_id!r}; refusing to overwrite: {e}"
            ) from e
        updated = update_fn(meta)
        _validate_meta(room_id, updated)
        _write_json(meta_path, updated)
        return updated


def _patch_status(room_id: str, agent: str, status: str, expires_at: int,
                  session_id: str, *, phase: str = "", task_id: int | str = "",
                  detail: str = "", source: str = "server",
                  only_if_missing: bool = False) -> None:
    p = _room_dir(room_id) / "status.json"
    # Locked read-modify-write: concurrent wake threads / reaper callbacks /
    # watchdog all patch status.json. Without the lock, two writers that read
    # the same snapshot and write back their own agent silently lose updates
    # (a dropped busy lease then triggers a spurious duplicate wake).
    with _lock(_room_dir(room_id) / "status.lock"):
        data = {}
        if _safe_exists(p):
            try:
                data = json.loads(_safe_read_text(p))
            except Exception:
                data = {}
        if not isinstance(data, dict):
            data = {}
        if only_if_missing and isinstance(data.get(agent), dict):
            return
        previous = data.get(agent) if isinstance(data.get(agent), dict) else {}
        if phase and phase not in VALID_AGENT_PHASES:
            raise ValueError(
                f"Invalid agent phase {phase!r}. Valid: {sorted(VALID_AGENT_PHASES)}"
            )
        record = {
            "status": status,
            "phase": phase or previous.get("phase") or status,
            "expires_at": expires_at,
            "session_id": session_id or previous.get("session_id", ""),
            "updated_at": int(time.time()),
            "source": source or previous.get("source", "server"),
        }
        if task_id != "":
            record["task_id"] = task_id
        elif "task_id" in previous:
            record["task_id"] = previous["task_id"]
        if detail:
            record["detail"] = detail
        elif previous.get("detail"):
            record["detail"] = previous["detail"]
        previous_receipts = previous.get("terminal_failure_receipts", [])
        receipts = list(previous_receipts) if isinstance(previous_receipts, list) else []
        if (
            source == "server"
            and phase in _SERVER_TERMINAL_FAILURE_PHASES
            and task_id != ""
        ):
            receipt = {
                "task_id": task_id,
                "phase": phase,
                "timestamp": record["updated_at"],
                "source": "server",
            }
            if not any(str(item.get("task_id", "")) == str(task_id)
                       for item in receipts if isinstance(item, dict)):
                receipts.append(receipt)
        if receipts:
            record["terminal_failure_receipts"] = receipts
        data[agent] = record
        _write_json(p, data)


# Parsed-message cache keyed by file identity (size, mtime_ns). messages.jsonl
# is append-only under a lock, so any new message grows the file — the key
# changes and the cache self-invalidates. A full rewrite to the same size in
# the same nanosecond is not reachable for this workload. Returned lists are
# treated read-only by every caller (internal `_`-prefixed contract).
_msg_cache: dict[str, tuple] = {}
_msg_cache_lock = threading.Lock()
# Cap the cache so it can't grow unbounded across many rooms. Oldest entries are
# evicted FIFO/LRU-ish on insert (room deletion also evicts via _evict_msg_cache).
_MSG_CACHE_MAX = 256


def _parse_messages_text(raw: str) -> list[dict]:
    msgs = []
    for line in raw.splitlines():
        line = line.strip()
        if line:
            try:
                msgs.append(json.loads(line))
            except Exception:
                pass
    return msgs


def _load_messages(room_id: str) -> list[dict]:
    p = _room_dir(room_id) / "messages.jsonl"
    try:
        st = _safe_stat(p)
    except (FileNotFoundError, NotADirectoryError):
        return []
    pstr = str(p)
    key = (st.st_size, st.st_mtime_ns)
    with _msg_cache_lock:
        cached = _msg_cache.get(pstr)
        if cached is not None and cached[0] == key:
            return cached[1]
    # Cache miss: read under a shared lock so we never parse a half-written
    # final line while a writer is mid-append (it holds LOCK_EX). Recompute the
    # key from the fd we actually read, so the cache reflects that exact state.
    # NB: callers already holding the messages LOCK_EX (e.g.
    # _validate_reply_to_locked) must NOT use this — they would self-deadlock.
    try:
        with _lock(p, shared=True) as fh:
            fh.seek(0)
            raw = fh.read()
            fst = os.fstat(fh.fileno())
            key = (fst.st_size, fst.st_mtime_ns)
    except (FileNotFoundError, NotADirectoryError):
        return []
    msgs = _parse_messages_text(raw)
    with _msg_cache_lock:
        # Re-insert at the tail (LRU-ish) then evict the oldest while over cap.
        _msg_cache.pop(pstr, None)
        _msg_cache[pstr] = (key, msgs)
        while len(_msg_cache) > _MSG_CACHE_MAX:
            oldest = next(iter(_msg_cache))
            _msg_cache.pop(oldest, None)
    return msgs


def _load_messages_unlocked(room_id: str) -> list[dict]:
    """Parse messages WITHOUT taking the messages lock. Only safe to call from
    code that already holds the LOCK_EX on messages.jsonl (re-locking the same
    file from a second fd in the same thread would deadlock)."""
    p = _room_dir(room_id) / "messages.jsonl"
    try:
        return _parse_messages_text(_safe_read_text(p))
    except (FileNotFoundError, NotADirectoryError):
        return []


def _evict_msg_cache(room_id: str) -> None:
    pstr = str(_room_dir(room_id) / "messages.jsonl")
    with _msg_cache_lock:
        _msg_cache.pop(pstr, None)


def _next_id(msgs_file: Path) -> int:
    if not _safe_exists(msgs_file) or _safe_stat(msgs_file).st_size == 0:
        return 1
    lines = _safe_read_text(msgs_file).strip().splitlines()
    for line in reversed(lines):
        try:
            return json.loads(line)["id"] + 1
        except Exception:
            pass
    return 1


class _lock:
    """Context manager: open file with an advisory lock.

    shared=False (default) → exclusive (LOCK_EX) for writers; flush+fsync on
    exit. shared=True → shared (LOCK_SH) for readers: multiple readers proceed
    together but block while any writer holds the exclusive lock, so a reader
    never observes a half-written line. Readers skip flush/fsync."""
    def __init__(self, path: Path, shared: bool = False):
        self._path = path
        self._shared = shared
        self._fh = None

    def __enter__(self):
        # Shared (reader) locks open read-only: requesting write access ("a+")
        # for a pure read needlessly fails on read-only filesystems and inside
        # restrictive sandboxes (e.g. a Codex resume pinned to sandbox_mode=
        # read-only), where O_RDONLY is fine but O_RDWR/append is denied.
        mode = "r" if self._shared else "a+"
        flags = os.O_RDONLY if self._shared else os.O_RDWR | os.O_APPEND | os.O_CREAT
        fd = _safe_open_fd(self._path, flags, 0o600)
        self._fh = os.fdopen(fd, mode, encoding="utf-8")
        fcntl.flock(self._fh, fcntl.LOCK_SH if self._shared else fcntl.LOCK_EX)
        return self._fh

    def __exit__(self, *_):
        if not self._fh:
            return
        try:
            if not self._shared:
                try:
                    self._fh.flush()
                    os.fsync(self._fh.fileno())
                except OSError:
                    # e.g. ENOSPC — durability is best-effort, but we MUST still
                    # release the lock and close the fd below, otherwise every
                    # later writer of this file deadlocks on the held LOCK_EX.
                    pass
        finally:
            try:
                fcntl.flock(self._fh, fcntl.LOCK_UN)
            finally:
                self._fh.close()


def _check_circuit_breaker(room_id: str, agent: str) -> None:
    msgs = _load_messages(room_id)
    _check_circuit_breaker_messages(msgs, agent)


def _check_room_rate_locked(messages: list[dict], now: int) -> None:
    """Enforce the persisted per-room admission cap under messages LOCK_EX.

    The lower boundary is open: a message timestamped exactly one full window
    ago no longer consumes capacity. All public agents and kinds share the same
    budget; only direct internal append helpers bypass this admission path.
    """
    lower_bound = now - ROOM_MESSAGE_RATE_WINDOW_SECS
    recent = sum(
        1 for message in messages
        if isinstance(message.get("timestamp"), (int, float))
        and not isinstance(message.get("timestamp"), bool)
        and message["timestamp"] > lower_bound
    )
    if recent >= ROOM_MESSAGE_RATE_LIMIT:
        raise ValueError(
            f"Room rate limit: {recent} persisted messages in the last "
            f"{ROOM_MESSAGE_RATE_WINDOW_SECS} seconds; "
            f"maximum {ROOM_MESSAGE_RATE_LIMIT}"
        )


def _check_circuit_breaker_locked(room_id: str, agent: str) -> None:
    """Circuit-breaker check for callers holding messages.jsonl LOCK_EX."""
    _check_circuit_breaker_messages(_load_messages_unlocked(room_id), agent)


def _check_circuit_breaker_messages(msgs: list[dict], agent: str) -> None:
    recent = msgs[-CIRCUIT_BREAKER_WINDOW:]
    # count consecutive messages from this agent at the tail
    streak = 0
    for m in reversed(recent):
        if m["agent"] == agent and m["kind"] not in ("request", "system"):
            streak += 1
        else:
            break
    if streak >= CIRCUIT_BREAKER_LIMIT:
        raise ValueError(
            f"Circuit breaker: {agent} sent {streak} consecutive non-request messages. "
            "Post a 'request' or wait for others to respond first."
        )


def _append_system(room_id: str, text: str) -> None:
    post_message(room_id, "System", text, kind="system")


def _serialize_message_entry(entry: dict) -> str:
    try:
        serialized = json.dumps(entry, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as e:
        raise ValueError("Message entry is not JSON-serializable") from e
    entry_bytes = len(serialized.encode("utf-8"))
    if entry_bytes > MAX_STORED_MESSAGE_BYTES:
        raise ValueError(
            f"Serialized message is too large ({entry_bytes} bytes; "
            f"maximum {MAX_STORED_MESSAGE_BYTES})"
        )
    return serialized


def _append_terminal_system(room_id: str, text: str) -> int:
    """Append the one close marker while the atomic closing claim is held.

    This deliberately bypasses post_message's closed-room rejection. Only the
    winner of close_room's meta-lock claim calls it.
    """
    body_bytes = len(text.encode("utf-8"))
    if body_bytes > MAX_STORED_BODY_BYTES:
        raise ValueError("System message body is too large")
    msgs_file = _room_dir(room_id) / "messages.jsonl"
    with _lock(msgs_file) as f:
        msg_id = _next_id(msgs_file)
        entry = {
            "id": msg_id,
            "agent": "System",
            "kind": "system",
            "timestamp": int(time.time()),
            "body": text,
        }
        serialized = _serialize_message_entry(entry)
        f.seek(0, 2)
        f.write(serialized + "\n")
    return msg_id


def _notify_agents(room_id: str, participants: list[str], sender: str,
                   to: Optional[str], msg_id: int) -> None:
    rdir = _room_dir(room_id)
    notif_registry = rdir / "notify_registry.json"
    try:
        if not _safe_exists(notif_registry):
            return
        registry = json.loads(_safe_read_text(notif_registry))
    except Exception:
        return

    for agent, notify_file in registry.items():
        if agent == sender:
            continue
        if to and to not in (agent, "all"):
            continue
        payload = {
            "room_id": room_id,
            "from_agent": sender,
            "kind": "request",
            "msg_id": msg_id,
        }
        try:
            target = _notification_path(notify_file)
            delivery_lock = _notification_lock_path(target)
            with _lock(delivery_lock):
                # Registry and filesystem may have changed since registration;
                # validate again while serializing delivery to this target.
                target = _notification_path(notify_file)
                try:
                    previous = json.loads(_safe_read_text(target)) if _safe_exists(target) else {}
                except (json.JSONDecodeError, OSError):
                    previous = {}
                try:
                    previous_id = int(previous.get("msg_id", 0) or 0)
                except (AttributeError, TypeError, ValueError):
                    previous_id = 0
                if (isinstance(previous, dict)
                        and previous.get("room_id") == room_id
                        and previous_id >= msg_id):
                    continue
                _write_json(target, payload)
        except Exception:
            pass


def close_all_rooms() -> dict:
    """Bulk-close rooms and only their current-instance owned children."""
    result = {
        "closed": [], "already_closed": [],
        "killed": 0, "skipped_dead": 0, "skipped_owner": 0,
        "persisted_pids_ignored": 0,
        "errors": [],
    }
    for meta in list_rooms():
        rid = meta.get("id", "")
        try:
            if meta.get("status") == "closed":
                result["already_closed"].append(rid)
                continue
            closed, counts, claimed_meta = _close_room_once(
                rid, "Чат закрыт (bulk close).",
            )
            if not closed:
                result["already_closed"].append(rid)
                continue
            result["killed"] += counts["sent"]
            result["skipped_dead"] += counts["exited"]
            result["skipped_owner"] += counts["denied"]
            result["persisted_pids_ignored"] += len(
                claimed_meta.get("spawned_pids", []) or []
            )
            result["closed"].append(rid)
        except Exception as e:
            result["errors"].append({"room_id": rid, "error": str(e)})
    return result


def delete_closed_rooms() -> dict:
    """Wipe every room with status=closed from disk. Open rooms untouched."""
    import shutil
    result = {"deleted": [], "skipped_open": [], "errors": []}
    for meta in list_rooms():
        rid = meta.get("id", "")
        try:
            if meta.get("status") != "closed":
                result["skipped_open"].append(rid)
                continue
            rdir = _room_dir(rid)
            if rdir.exists():
                shutil.rmtree(rdir)
            _evict_msg_cache(rid)
            result["deleted"].append(rid)
        except Exception as e:
            result["errors"].append({"room_id": rid, "error": str(e)})
    return result


def delete_old_terminal_rooms(max_age_days: float) -> dict:
    """Delete terminal rooms (closed/resolved) whose dir is older than
    max_age_days. Open/idle rooms and recently-closed rooms are untouched.
    Used by the background retention sweep so terminal rooms don't pile up
    forever (and don't keep inflating the O(N) list_rooms() scan).

    Age = room-dir mtime (a closed room gets no more writes, so mtime ≈ close
    time; reads don't bump mtime). Cache evicted on delete. max_age_days <= 0
    disables (returns empty)."""
    import shutil
    result = {"deleted": [], "skipped": [], "errors": []}
    if max_age_days <= 0:
        return result
    cutoff = time.time() - max_age_days * 86400
    for meta in list_rooms():
        rid = meta.get("id", "")
        try:
            if meta.get("status") not in ("closed", "resolved"):
                result["skipped"].append(rid)
                continue
            rdir = _room_dir(rid)
            if not rdir.exists():
                continue
            if rdir.stat().st_mtime > cutoff:
                result["skipped"].append(rid)
                continue
            # A resolved room is read-only but not necessarily process-free.
            # Tombstone it in the current-instance ownership registry before
            # deletion so both existing and concurrently-late children are
            # terminated instead of surviving their room directory.
            child_processes.close_room(rid)
            shutil.rmtree(rdir)
            _evict_msg_cache(rid)
            result["deleted"].append(rid)
        except Exception as e:
            result["errors"].append({"room_id": rid, "error": str(e)})
    return result


def nuke_all_rooms() -> dict:
    """Hard reset: close_all_rooms() then delete_closed_rooms(). Returns merged
    summary. Only exact current-instance child processes are terminated."""
    close_summary = close_all_rooms()
    delete_summary = delete_closed_rooms()
    return {
        "closed": close_summary["closed"],
        "already_closed": close_summary["already_closed"],
        "killed": close_summary["killed"],
        "skipped_dead": close_summary["skipped_dead"],
        "skipped_owner": close_summary["skipped_owner"],
        "persisted_pids_ignored": close_summary["persisted_pids_ignored"],
        "deleted": delete_summary["deleted"],
        "errors": close_summary["errors"] + delete_summary["errors"],
    }
