"""Agent Bus — core file I/O layer.

All writes go through file-locked atomic append to prevent corruption
when multiple agents post simultaneously.
"""

import fcntl
import json
import os
import signal
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

HUDDLE_HOME = Path(os.environ.get("MCP_HUDDLE_HOME", Path.home() / ".mcp-huddle"))
BUS_DIR = HUDDLE_HOME / "rooms"
CIRCUIT_BREAKER_WINDOW = 10   # last N messages to check
CIRCUIT_BREAKER_LIMIT = 5     # max consecutive from same agent (non-request kinds)
DEADLOCK_TIMEOUT_SECS = 180   # 3 minutes of silence → system message
ZOMBIE_CHECK_SECS = 30        # how often to check owner_pid liveness

VALID_KINDS = {"request", "comment", "ack", "busy", "result", "final", "system", "close"}


# ── Rooms ────────────────────────────────────────────────────────────────────

def _room_dir(room_id: str) -> Path:
    return BUS_DIR / room_id


def create_room(name: str, owner: str, owner_pid: int, cwd: str = "",
                session_id: str = "") -> str:
    room_id = f"room_{uuid.uuid4().hex[:8]}"
    rdir = _room_dir(room_id)
    rdir.mkdir(parents=True, exist_ok=True)

    meta = {
        "id": room_id,
        "name": name,
        "owner": owner,
        "owner_pid": owner_pid,
        "session_id": session_id,
        "participants": [owner],
        "spawned_pids": [],
        "created_at": int(time.time()),
        "status": "open",
        "cwd": cwd,
        "last_activity": int(time.time()),
        "resolution": None,
    }
    _write_json(rdir / "meta.json", meta)
    _write_json(rdir / "status.json", {
        owner: {"status": "online", "expires_at": 0, "session_id": session_id}
    })
    return room_id


def invite_agent(room_id: str, agent_name: str) -> None:
    meta = _read_meta(room_id)
    if agent_name not in meta["participants"]:
        meta["participants"].append(agent_name)
    _write_json(_room_dir(room_id) / "meta.json", meta)
    _patch_status(room_id, agent_name, "online", 0, "")


def get_room_info(room_id: str) -> dict:
    return _read_meta(room_id)


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
    meta = _read_meta(room_id)
    if meta["status"] != "open":
        return meta["status"]
    meta["status"] = "closing_requested"
    _write_json(_room_dir(room_id) / "meta.json", meta)
    _append_system(room_id, f"[{agent}] запросил закрытие комнаты. Подтверди: room_close('{room_id}')")
    return "closing_requested"


def close_room(room_id: str, owner: str) -> None:
    meta = _read_meta(room_id)
    if meta["status"] == "closed":
        # Idempotent: уже закрыта. Не добавлять повторный system-message и не
        # перезаписывать meta.json лишний раз. Дашборд / агенты могут безопасно
        # дёрнуть room_close ещё раз без побочных эффектов.
        return
    _kill_spawned(meta)
    _append_system(room_id, "Чат закрыт.")
    meta["status"] = "closed"
    _write_json(_room_dir(room_id) / "meta.json", meta)


def close_session_rooms(session_id: str) -> list[str]:
    closed = []
    for meta in list_rooms():
        if meta.get("session_id") == session_id and meta["status"] in ("open", "closing_requested"):
            close_room(meta["id"], meta["owner"])
            closed.append(meta["id"])
    return closed


# ── Messages ─────────────────────────────────────────────────────────────────

def post_message(room_id: str, agent: str, body: str, kind: str,
                 to: Optional[str] = None, reply_to: Optional[int] = None,
                 idempotency_key: Optional[str] = None) -> int:
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

        f.seek(0, 2)  # EOF
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # Update last_activity in meta
    meta["last_activity"] = int(time.time())
    _write_json(rdir / "meta.json", meta)

    # Notify relevant agents (only for kind=request)
    if kind == "request":
        _notify_agents(room_id, meta["participants"], agent, to, msg_id)

    return msg_id


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
    data = json.loads(status_file.read_text())
    now = int(time.time())
    result = {}
    changed = False
    for agent, info in data.items():
        expires = info.get("expires_at", 0)
        if expires > 0 and now > expires:
            info["status"] = "online"
            info["expires_at"] = 0
            changed = True
        result[agent] = info["status"]
    if changed:
        _write_json(status_file, data)
    return result


# ── Resolution / consensus ────────────────────────────────────────────────────

def propose_resolution(room_id: str, agent: str, text: str) -> str:
    meta = _read_meta(room_id)
    res_id = f"res_{uuid.uuid4().hex[:6]}"
    meta["resolution"] = {
        "id": res_id,
        "proposed_by": agent,
        "text": text,
        "votes": {agent: "ack"},
        "status": "voting",
    }
    _write_json(_room_dir(room_id) / "meta.json", meta)
    _append_system(room_id,
        f"[Resolution proposed by {agent}]: {text}\n"
        f"Все участники: вызовите resolution_vote('{room_id}', ..., '{res_id}', 'ack'|'reject')")
    return res_id


def resolution_vote(room_id: str, agent: str, resolution_id: str, vote: str) -> str:
    meta = _read_meta(room_id)
    res = meta.get("resolution")
    if not res or res["id"] != resolution_id:
        raise ValueError(f"Resolution {resolution_id} not found")
    if vote not in ("ack", "reject"):
        raise ValueError("vote must be 'ack' or 'reject'")

    res["votes"][agent] = vote
    participants = [p for p in meta["participants"] if p != "Human"]

    if vote == "reject":
        res["status"] = "rejected"
        _write_json(_room_dir(room_id) / "meta.json", meta)
        _append_system(room_id, f"[{agent}] отклонил резолюцию: {res['text'][:80]}")
    elif all(res["votes"].get(p) == "ack" for p in participants):
        res["status"] = "accepted"
        meta["status"] = "resolved"
        _write_json(_room_dir(room_id) / "meta.json", meta)
        _append_system(room_id,
            f"Консенсус достигнут! Резолюция принята: {res['text']}\n"
            "Чат переведён в read-only. Оркестратор может закрыть чат.")
    else:
        _write_json(_room_dir(room_id) / "meta.json", meta)
    return res["status"]


# ── Notifications ─────────────────────────────────────────────────────────────

def register_notify(room_id: str, agent: str, notify_file: str) -> None:
    rdir = _room_dir(room_id)
    notif_registry = rdir / "notify_registry.json"
    data = {}
    if notif_registry.exists():
        try:
            data = json.loads(notif_registry.read_text())
        except Exception:
            pass
    data[agent] = notify_file
    _write_json(notif_registry, data)


# ── Zombie watchdog (called by server background task) ────────────────────────

def check_zombie_rooms() -> list[str]:
    """Return list of room_ids that were auto-closed due to dead owner."""
    closed = []
    for meta in list_rooms():
        if meta["status"] not in ("open", "closing_requested"):
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
            _append_system(meta["id"],
                f"[System] Timeout: комната молчит {DEADLOCK_TIMEOUT_SECS // 60} мин. "
                "Есть незакрытый вопрос?")
            # reset timer to avoid spam
            m = _read_meta(meta["id"])
            m["last_activity"] = now
            _write_json(_room_dir(meta["id"]) / "meta.json", m)
            notified.append(meta["id"])
    return notified


# ── Internal helpers ──────────────────────────────────────────────────────────

def _read_meta(room_id: str) -> dict:
    p = _room_dir(room_id) / "meta.json"
    if not p.exists():
        raise ValueError(f"Room '{room_id}' not found")
    return json.loads(p.read_text())


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.replace(path)


def _patch_status(room_id: str, agent: str, status: str, expires_at: int, session_id: str) -> None:
    p = _room_dir(room_id) / "status.json"
    data = {}
    if p.exists():
        try:
            data = json.loads(p.read_text())
        except Exception:
            pass
    data[agent] = {"status": status, "expires_at": expires_at, "session_id": session_id}
    _write_json(p, data)


def _load_messages(room_id: str) -> list[dict]:
    p = _room_dir(room_id) / "messages.jsonl"
    if not p.exists():
        return []
    msgs = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                msgs.append(json.loads(line))
            except Exception:
                pass
    return msgs


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
    """Context manager: open file with exclusive lock."""
    def __init__(self, path: Path):
        self._path = path
        self._fh = None

    def __enter__(self):
        self._fh = open(self._path, "a+", encoding="utf-8")
        fcntl.flock(self._fh, fcntl.LOCK_EX)
        return self._fh

    def __exit__(self, *_):
        if self._fh:
            self._fh.flush()
            os.fsync(self._fh.fileno())
            fcntl.flock(self._fh, fcntl.LOCK_UN)
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
