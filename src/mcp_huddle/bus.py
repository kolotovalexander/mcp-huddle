"""Agent Bus — core file I/O layer.

All writes go through file-locked atomic append to prevent corruption
when multiple agents post simultaneously.
"""

import fcntl
import json
import os
import signal
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

HUDDLE_HOME = Path(os.environ.get("MCP_HUDDLE_HOME", Path.home() / ".mcp-huddle"))
BUS_DIR = HUDDLE_HOME / "rooms"


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

VALID_KINDS = {"request", "comment", "ack", "busy", "result", "final", "system", "close"}
VALID_STATUSES = {"open", "idle", "closing_requested", "closed", "resolved"}


# ── Rooms ────────────────────────────────────────────────────────────────────

def _room_dir(room_id: str) -> Path:
    return BUS_DIR / room_id


def create_room(name: str, owner: str, owner_pid: int, cwd: str = "",
                session_id: str = "") -> str:
    room_id = f"room_{uuid.uuid4().hex[:8]}"
    rdir = _room_dir(room_id)
    rdir.mkdir(parents=True, exist_ok=True, mode=0o700)
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
        owner: {"status": "online", "expires_at": 0, "session_id": session_id}
    })
    return room_id


def invite_agent(room_id: str, agent_name: str) -> None:
    def _update(meta: dict) -> dict:
        if agent_name not in meta["participants"]:
            meta["participants"].append(agent_name)
        return meta
    _update_meta_locked(room_id, _update)
    _patch_status(room_id, agent_name, "online", 0, "")


def register_external_agent(room_id: str, agent_name: str) -> dict:
    """Reserve <name>.events.jsonl + last_message.txt for an agent that wasn't
    auto-spawned by huddle (e.g. orchestrator-launched CLIs). Adds an entry to
    room_meta.agent_meta so /api/room_agents and the dashboard activity panel
    pick it up. Returns {log_path, last_message_path}."""
    rdir = _room_dir(room_id)
    agents_dir = rdir / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    log_path = agents_dir / f"{agent_name.lower()}.events.jsonl"
    last_path = agents_dir / f"{agent_name.lower()}.last_message.txt"
    log_path.touch(exist_ok=True)

    def _update(meta: dict) -> dict:
        am = meta.setdefault("agent_meta", {})
        am[agent_name] = {
            "log_path": str(log_path),
            "last_message_path": str(last_path),
            "thread_id": am.get(agent_name, {}).get("thread_id", ""),
            "external": True,
        }
        return meta

    # Locked RMW: a concurrent wake-thread agent_meta update must not be lost.
    _update_meta_locked(room_id, _update)
    return {"log_path": str(log_path), "last_message_path": str(last_path)}


def append_agent_event(room_id: str, agent_name: str, event: dict) -> None:
    """Append a JSONL event line that the SSE handler tails into the dashboard's
    Agent-activity panel. Persists, so re-opening a closed room replays history."""
    log_path = _room_dir(room_id) / "agents" / f"{agent_name.lower()}.events.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = json.dumps(event, ensure_ascii=False)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(payload + "\n")


def get_room_info(room_id: str) -> dict:
    return _read_meta(room_id)


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


def close_room(room_id: str, owner: str) -> None:
    meta = _read_meta(room_id)
    if meta["status"] == "closed":
        # Idempotent: уже закрыта. Не добавлять повторный system-message и не
        # перезаписывать meta.json лишний раз. Дашборд / агенты могут безопасно
        # дёрнуть room_close ещё раз без побочных эффектов.
        return
    _kill_spawned(meta)
    _append_system(room_id, "Чат закрыт.")
    # Locked flip preserves any agent_meta a wake thread wrote concurrently.
    _update_meta_locked(room_id, lambda m: {**m, "status": "closed"})


def close_session_rooms(session_id: str) -> list[str]:
    closed = []
    for meta in list_rooms():
        if meta.get("session_id") == session_id and meta["status"] in ("open", "closing_requested"):
            close_room(meta["id"], meta["owner"])
            closed.append(meta["id"])
    return closed


def delete_room(room_id: str, owner: str) -> None:
    """Permanently remove a room from disk (history wipe).

    Safety: only allowed on rooms with status == 'closed'. Open rooms must be
    closed first via close_room() — это защита от случайной потери активного
    обсуждения.

    Side effects: рекурсивно удаляет ~/.mcp-huddle/rooms/<room_id>/, including
    messages.jsonl, meta.json, status.json, agents/<name>.events.jsonl.
    Не убивает spawned PIDs (это делает close_room).
    """
    import shutil
    meta = _read_meta(room_id)
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

    meta = _read_meta(room_id)
    if meta["status"] == "closed":
        raise ValueError("Room is closed.")
    if meta["status"] == "resolved" and kind not in ("system", "close"):
        raise ValueError("Room is resolved and read-only.")

    # Human override bypasses circuit breaker
    if agent != "Human" and kind not in ("request", "system"):
        _check_circuit_breaker(room_id, agent)

    rdir = _room_dir(room_id)
    msgs_file = rdir / "messages.jsonl"

    with _lock(msgs_file) as f:
        # Re-validate room state under the messages lock: a concurrent
        # close_room / resolution_vote may have transitioned the room between
        # the pre-lock read above and here. Without this, a message can land in
        # an already-closed/resolved room.
        cur = _read_meta(room_id)
        if cur["status"] == "closed":
            raise ValueError("Room is closed.")
        if cur["status"] == "resolved" and kind not in ("system", "close"):
            raise ValueError("Room is resolved and read-only.")

        # Idempotency: scan last 20 lines
        if idempotency_key:
            existing = _read_last_n_raw(msgs_file, 20)
            for line in existing:
                try:
                    msg = json.loads(line)
                    if msg.get("idempotency_key") == idempotency_key:
                        return msg["id"]
                except Exception:
                    pass

        # reply_to validation — done under the messages lock so the check is
        # atomic with the append below: a parallel duplicate reply from the
        # same agent cannot slip through the gap between validate and write.
        if reply_to is not None:
            _validate_reply_to_locked(room_id, int(reply_to), agent)

        # Assign next ID
        msg_id = _next_id(msgs_file)

        entry: dict = {
            "id": msg_id,
            "agent": agent,
            "kind": kind,
            "timestamp": int(time.time()),
            "body": body,
        }
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

        f.seek(0, 2)  # EOF
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

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


def _validate_reply_to_locked(room_id: str, target_id: int, agent: str) -> None:
    """Validate a reply_to target. Call ONLY while holding the messages lock so
    the check is atomic with the append that follows.

    Rules:
      * target must exist and be a `request`;
      * the replying agent must have been an addressee (`to` empty / "all" /
        the agent itself) — Human/System bypass this;
      * a broadcast request (`to=all` / no `to`) expects one reply per
        addressee, so we reject only a SECOND reply from the SAME agent —
        replies from other agents are allowed.
    """
    # Caller holds the messages LOCK_EX; read lock-free to avoid self-deadlock.
    messages = _load_messages_unlocked(room_id)
    target = next((m for m in messages if m.get("id") == target_id), None)
    if target is None:
        raise ValueError("reply_to target not found")
    if target.get("kind") != "request":
        raise ValueError("reply_to target must be a request")
    target_to = target.get("to")
    if (agent not in ("Human", "System")
            and target_to and target_to not in ("all", agent)):
        raise ValueError(
            f"reply_to target #{target_id} was addressed to {target_to!r}, "
            f"not to {agent!r}")
    if any(m.get("reply_to") == target_id and m.get("agent") == agent
           for m in messages):
        raise ValueError(f"{agent} already answered request #{target_id}")


def read_messages(room_id: str, since_id: int = 0, limit: int = 20) -> str:
    """Return plain-text chat log for LLM consumption."""
    meta = _read_meta(room_id)
    msgs = _load_messages(room_id)

    if since_id > 0:
        msgs = [m for m in msgs if m["id"] > since_id]
    if len(msgs) > limit:
        msgs = msgs[-limit:]

    participants = " · ".join(meta["participants"])
    lines = [f"=== Chat: {meta['name']} | {participants} ==="]
    for m in msgs:
        addr = f" → {m['to']}" if m.get("to") else ""
        knd = f"[{m['kind']}]" if m["kind"] not in ("comment",) else ""
        re_tag = f" (re:#{m['reply_to']})" if m.get("reply_to") else ""
        ts = time.strftime("%H:%M", time.localtime(m["timestamp"]))
        lines.append(f"[{m['id']:03d}] {m['agent']}{addr} {knd}  {m['body']}{re_tag}  [{ts}]")

    if not msgs:
        lines.append("(no new messages)")
    return "\n".join(lines)


def summarize_messages(room_id: str, since_id: int = 0) -> str:
    """Digest of recent messages — for agents catching up after a long absence."""
    msgs = _load_messages(room_id)
    if since_id > 0:
        msgs = [m for m in msgs if m["id"] > since_id]
    if not msgs:
        return "No messages since that point."

    total = len(msgs)
    agents_seen = {}
    requests_open = []
    for m in msgs:
        agents_seen[m["agent"]] = agents_seen.get(m["agent"], 0) + 1
        if m["kind"] == "request" and not m.get("reply_to"):
            requests_open.append(f"#{m['id']}: {m['body'][:80]}")

    summary = f"[Digest: {total} messages]\n"
    summary += "Participants: " + ", ".join(f"{a}({c})" for a, c in agents_seen.items()) + "\n"
    if requests_open:
        summary += "Open requests:\n" + "\n".join(f"  {r}" for r in requests_open[-5:]) + "\n"
    # Last 3 messages verbatim
    summary += "\nLast messages:\n"
    for m in msgs[-3:]:
        summary += f"  [{m['id']}] {m['agent']}: {m['body'][:120]}\n"
    return summary


# ── Status ───────────────────────────────────────────────────────────────────

def set_status(room_id: str, agent: str, status: str,
               expires_in_sec: int = 0, session_id: str = "") -> None:
    expires_at = int(time.time()) + expires_in_sec if expires_in_sec > 0 else 0
    _patch_status(room_id, agent, status, expires_at, session_id)


def get_status(room_id: str) -> dict:
    status_file = _room_dir(room_id) / "status.json"
    if not status_file.exists():
        return {}
    try:
        data = json.loads(status_file.read_text())
    except Exception:
        return {}
    now = int(time.time())
    result = {}
    expired = []
    for agent, info in data.items():
        expires = info.get("expires_at", 0)
        if expires > 0 and now > expires:
            result[agent] = "online"
            expired.append(agent)
        else:
            result[agent] = info.get("status", "online")
    # Persist expiry resets under the lock with a fresh read so we never
    # clobber a busy lease another thread wrote between our read and write.
    if expired:
        with _lock(_room_dir(room_id) / "status.lock"):
            if status_file.exists():
                try:
                    data = json.loads(status_file.read_text())
                except Exception:
                    data = {}
                changed = False
                for agent in expired:
                    info = data.get(agent)
                    if (info and info.get("expires_at", 0) > 0
                            and now > info["expires_at"]):
                        info["status"] = "online"
                        info["expires_at"] = 0
                        changed = True
                if changed:
                    _write_json(status_file, data)
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

def register_notify(room_id: str, agent: str, notify_file: str) -> None:
    rdir = _room_dir(room_id)
    notif_registry = rdir / "notify_registry.json"
    # Locked RMW: parallel registrations must not clobber each other.
    with _lock(rdir / "notify.lock"):
        data = {}
        if notif_registry.exists():
            try:
                data = json.loads(notif_registry.read_text())
            except Exception:
                data = {}
        data[agent] = notify_file
        _write_json(notif_registry, data)


# ── Zombie watchdog (called by server background task) ────────────────────────

def check_zombie_rooms() -> list[str]:
    """Return list of room_ids that were auto-closed due to dead owner."""
    closed = []
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
            close_room(meta["id"], meta["owner"])
            closed.append(meta["id"])
        except PermissionError:
            pass  # process exists, we just can't signal it
    return closed


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
    if not p.exists():
        raise ValueError(f"Room '{room_id}' not found")
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, ValueError, OSError) as e:
        # One truncated/corrupt meta.json must not crash the whole request/server.
        print(f"[huddle] WARN: corrupt/unreadable meta.json for "
              f"'{room_id}': {e}", file=sys.stderr)
        return {}


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.replace(path)


def _update_meta_locked(room_id: str, update_fn) -> dict:
    rdir = _room_dir(room_id)
    meta_path = rdir / "meta.json"
    with _lock(rdir / "meta.lock"):
        if not meta_path.exists():
            raise ValueError(f"Room '{room_id}' not found")
        try:
            meta = json.loads(meta_path.read_text())
        except (json.JSONDecodeError, ValueError, OSError) as e:
            # Corrupt/truncated meta under lock: don't crash. Start from an empty
            # base so the update function can repair-by-overwrite rather than
            # propagating a parse exception out of every writer.
            print(f"[huddle] WARN: corrupt/unreadable meta.json (locked) for "
                  f"'{room_id}': {e}", file=sys.stderr)
            meta = {}
        updated = update_fn(meta)
        _write_json(meta_path, updated)
        return updated


def _patch_status(room_id: str, agent: str, status: str, expires_at: int, session_id: str) -> None:
    p = _room_dir(room_id) / "status.json"
    # Locked read-modify-write: concurrent wake threads / reaper callbacks /
    # watchdog all patch status.json. Without the lock, two writers that read
    # the same snapshot and write back their own agent silently lose updates
    # (a dropped busy lease then triggers a spurious duplicate wake).
    with _lock(_room_dir(room_id) / "status.lock"):
        data = {}
        if p.exists():
            try:
                data = json.loads(p.read_text())
            except Exception:
                data = {}
        data[agent] = {"status": status, "expires_at": expires_at, "session_id": session_id}
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
        st = p.stat()
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
        return _parse_messages_text(p.read_text())
    except (FileNotFoundError, NotADirectoryError):
        return []


def _evict_msg_cache(room_id: str) -> None:
    pstr = str(_room_dir(room_id) / "messages.jsonl")
    with _msg_cache_lock:
        _msg_cache.pop(pstr, None)


def _next_id(msgs_file: Path) -> int:
    if not msgs_file.exists() or msgs_file.stat().st_size == 0:
        return 1
    lines = msgs_file.read_text().strip().splitlines()
    for line in reversed(lines):
        try:
            return json.loads(line)["id"] + 1
        except Exception:
            pass
    return 1


def _read_last_n_raw(msgs_file: Path, n: int) -> list[str]:
    if not msgs_file.exists():
        return []
    lines = msgs_file.read_text().strip().splitlines()
    return lines[-n:]


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
        self._fh = open(self._path, mode, encoding="utf-8")
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


def _notify_agents(room_id: str, participants: list[str], sender: str,
                   to: Optional[str], msg_id: int) -> None:
    rdir = _room_dir(room_id)
    notif_registry = rdir / "notify_registry.json"
    if not notif_registry.exists():
        return
    try:
        registry = json.loads(notif_registry.read_text())
    except Exception:
        return

    for agent, notify_file in registry.items():
        if agent == sender:
            continue
        if to and to not in (agent, "all"):
            continue
        payload = json.dumps({
            "room_id": room_id,
            "from_agent": sender,
            "kind": "request",
            "msg_id": msg_id,
        })
        try:
            Path(notify_file).write_text(payload)
        except Exception:
            pass


def _kill_spawned(meta: dict) -> None:
    for pid in meta.get("spawned_pids", []):
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass


def _pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _collect_protected_owner_pids() -> set[int]:
    """Union of owner_pid across all rooms — but only those still alive.
    Dead owner PIDs are not protected (can be reused by unrelated processes)."""
    protected: set[int] = set()
    for meta in list_rooms():
        pid = int(meta.get("owner_pid") or 0)
        if pid > 0 and _pid_alive(pid):
            protected.add(pid)
    return protected


def _kill_spawned_safe(meta: dict, protected: set[int]) -> dict:
    """Kill alive spawned PIDs, skipping any that are dead or protected.
    Returns counts: {killed: int, skipped_dead: int, skipped_owner: int}."""
    counts = {"killed": 0, "skipped_dead": 0, "skipped_owner": 0}
    for pid in meta.get("spawned_pids", []) or []:
        try:
            pid_int = int(pid)
        except (TypeError, ValueError):
            continue
        if pid_int <= 0:
            continue
        if not _pid_alive(pid_int):
            counts["skipped_dead"] += 1
            continue
        if pid_int in protected:
            counts["skipped_owner"] += 1
            continue
        try:
            os.kill(pid_int, signal.SIGTERM)
            counts["killed"] += 1
        except (ProcessLookupError, PermissionError):
            counts["skipped_dead"] += 1
    return counts


def close_all_rooms() -> dict:
    """Bulk close: every non-closed room → kill alive spawned (excluding owner
    PIDs of any room) → status=closed. Owners are never touched."""
    protected = _collect_protected_owner_pids()
    result = {
        "closed": [], "already_closed": [],
        "killed": 0, "skipped_dead": 0, "skipped_owner": 0,
        "errors": [],
    }
    for meta in list_rooms():
        rid = meta.get("id", "")
        try:
            if meta.get("status") == "closed":
                result["already_closed"].append(rid)
                continue
            counts = _kill_spawned_safe(meta, protected)
            result["killed"] += counts["killed"]
            result["skipped_dead"] += counts["skipped_dead"]
            result["skipped_owner"] += counts["skipped_owner"]
            _append_system(rid, "Чат закрыт (bulk close).")
            _update_meta_locked(rid, lambda m: {**m, "status": "closed"})
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
            shutil.rmtree(rdir)
            _evict_msg_cache(rid)
            result["deleted"].append(rid)
        except Exception as e:
            result["errors"].append({"room_id": rid, "error": str(e)})
    return result


def nuke_all_rooms() -> dict:
    """Hard reset: close_all_rooms() then delete_closed_rooms(). Returns merged
    summary. Owner PIDs never killed; dead spawned PIDs skipped."""
    close_summary = close_all_rooms()
    delete_summary = delete_closed_rooms()
    return {
        "closed": close_summary["closed"],
        "already_closed": close_summary["already_closed"],
        "killed": close_summary["killed"],
        "skipped_dead": close_summary["skipped_dead"],
        "skipped_owner": close_summary["skipped_owner"],
        "deleted": delete_summary["deleted"],
        "errors": close_summary["errors"] + delete_summary["errors"],
    }
