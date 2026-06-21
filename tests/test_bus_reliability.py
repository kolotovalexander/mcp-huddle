"""Reliability tests for the file-backed huddle bus."""

from __future__ import annotations

import asyncio
import importlib
import json
import threading
import time
from pathlib import Path

import pytest

import mcp_huddle.bus as bus


@pytest.fixture()
def isolated_bus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    reloaded = importlib.reload(bus)
    yield reloaded
    monkeypatch.delenv("MCP_HUDDLE_HOME", raising=False)
    importlib.reload(bus)


def _create_room(bus_module=bus) -> str:
    return bus_module.create_room("Reliability", "Codex", 0, "/tmp", "test-session")


def test_storage_root_uses_mcp_huddle_home(isolated_bus, tmp_path: Path) -> None:
    room_id = _create_room(isolated_bus)

    assert isolated_bus.BUS_DIR == tmp_path / "rooms"
    assert (tmp_path / "rooms" / room_id / "meta.json").exists()
    assert not (Path.home() / ".mcp-huddle" / "rooms" / room_id).exists()


def test_concurrent_appends_have_unique_sequential_ids(isolated_bus) -> None:
    room_id = _create_room(isolated_bus)
    count = 10
    barrier = threading.Barrier(count)
    results: list[int] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def post(index: int) -> None:
        try:
            barrier.wait(timeout=5)
            msg_id = isolated_bus.post_message(room_id, f"agent-{index}", f"body-{index}", "comment")
            with lock:
                results.append(msg_id)
        except BaseException as exc:
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=post, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert errors == []
    assert sorted(results) == list(range(1, count + 1))
    messages = isolated_bus._load_messages(room_id)
    assert [m["id"] for m in messages] == list(range(1, count + 1))

    messages_file = isolated_bus._room_dir(room_id) / "messages.jsonl"
    for line in messages_file.read_text().splitlines():
        json.loads(line)


def test_concurrent_status_updates_do_not_clobber(isolated_bus) -> None:
    """status.json read-modify-write must be atomic across threads.

    Wake threads, reaper callbacks and the watchdog all call set_status
    concurrently. Without a file lock, two writers that each read the same
    snapshot and write back their own agent's entry clobber each other —
    a busy lease can be silently lost, which then triggers a duplicate wake.
    """
    room_id = _create_room(isolated_bus)
    count = 16
    barrier = threading.Barrier(count)
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            barrier.wait(timeout=5)
            isolated_bus.set_status(room_id, f"agent-{index}", "busy", 300, "sess")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert errors == []
    statuses = isolated_bus.get_status(room_id)
    missing = [i for i in range(count) if statuses.get(f"agent-{i}") != "busy"]
    assert missing == [], f"lost status updates for agents {missing}: {statuses}"


def test_expired_lease_reset_preserves_concurrent_busy(isolated_bus) -> None:
    """get_status persists expiry resets; doing so must not clobber a busy
    lease written concurrently by another agent."""
    room_id = _create_room(isolated_bus)
    # A: already-expired lease that get_status will reset to online.
    isolated_bus.set_status(room_id, "A", "busy", 0, "sess")
    status_file = isolated_bus._room_dir(room_id) / "status.json"
    data = json.loads(status_file.read_text())
    data["A"] = {"status": "busy", "expires_at": int(time.time()) - 1, "session_id": "sess"}
    status_file.write_text(json.dumps(data))

    errors: list[BaseException] = []
    start = threading.Barrier(2)

    def reader() -> None:
        try:
            start.wait(timeout=5)
            for _ in range(50):
                isolated_bus.get_status(room_id)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def writer() -> None:
        try:
            start.wait(timeout=5)
            for _ in range(50):
                isolated_bus.set_status(room_id, "B", "busy", 300, "sess")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t1, t2 = threading.Thread(target=reader), threading.Thread(target=writer)
    t1.start(); t2.start()
    t1.join(timeout=5); t2.join(timeout=5)

    assert errors == []
    statuses = isolated_bus.get_status(room_id)
    assert statuses.get("A") == "online"  # expired lease reset
    assert statuses.get("B") == "busy"     # concurrent write not clobbered


def test_load_messages_cache_invalidates_on_append(isolated_bus) -> None:
    """_load_messages caches by (size, mtime); a new append must be visible."""
    room_id = _create_room(isolated_bus)
    isolated_bus.post_message(room_id, "Codex", "first", "comment")
    first = isolated_bus._load_messages(room_id)
    assert [m["body"] for m in first] == ["first"]

    # Cache hit returns equal content without a new append.
    assert isolated_bus._load_messages(room_id) == first

    isolated_bus.post_message(room_id, "Codex", "second", "comment")
    second = isolated_bus._load_messages(room_id)
    assert [m["body"] for m in second] == ["first", "second"]


def test_load_messages_cache_reflects_external_rewrite(isolated_bus) -> None:
    """Even a full file rewrite (different content, larger size) is picked up."""
    room_id = _create_room(isolated_bus)
    isolated_bus.post_message(room_id, "Codex", "orig", "comment")
    isolated_bus._load_messages(room_id)  # prime cache

    msgs_file = isolated_bus._room_dir(room_id) / "messages.jsonl"
    rewritten = {"id": 1, "agent": "Codex", "kind": "comment",
                 "timestamp": int(time.time()), "body": "rewritten-longer-body"}
    msgs_file.write_text(json.dumps(rewritten) + "\n")

    reloaded = isolated_bus._load_messages(room_id)
    assert [m["body"] for m in reloaded] == ["rewritten-longer-body"]


def test_meta_writers_do_not_clobber_concurrent_agent_meta(isolated_bus) -> None:
    """Lifecycle meta writers (propose_resolution etc.) must not clobber the
    agent_meta wake state that wake threads write via _update_meta_locked.

    Wake threads record wake_id / last_wake_pid under the meta lock. A meta
    writer that does an unlocked read-modify-write can drop those fields,
    leaving a stale lease the wake system can never release.
    """
    room_id = _create_room(isolated_bus)
    count = 20
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def add_agent_meta() -> None:
        try:
            barrier.wait(timeout=5)
            for i in range(count):
                def _u(meta: dict, i=i) -> dict:
                    am = meta.setdefault("agent_meta", {})
                    am[f"agent-{i}"] = {"wake_id": f"w{i}", "last_wake_pid": 1000 + i}
                    return meta
                isolated_bus._update_meta_locked(room_id, _u)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def churn_resolution() -> None:
        try:
            barrier.wait(timeout=5)
            for i in range(count):
                isolated_bus.propose_resolution(room_id, "Codex", f"proposal {i}")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=add_agent_meta)
    t2 = threading.Thread(target=churn_resolution)
    t1.start(); t2.start()
    t1.join(timeout=5); t2.join(timeout=5)

    assert errors == []
    meta = isolated_bus.get_room_info(room_id)
    am = meta.get("agent_meta", {})
    missing = [f"agent-{i}" for i in range(count) if f"agent-{i}" not in am]
    assert missing == [], f"agent_meta clobbered for {missing}"


def test_concurrent_register_notify_keeps_all_entries(isolated_bus) -> None:
    """register_notify is read-modify-write on notify_registry.json. Without a
    lock, two agents registering at once lose each other's entry."""
    room_id = _create_room(isolated_bus)
    count = 16
    barrier = threading.Barrier(count)
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            barrier.wait(timeout=5)
            isolated_bus.register_notify(room_id, f"agent-{index}", f"/tmp/notify-{index}")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert errors == []
    registry = json.loads((isolated_bus._room_dir(room_id) / "notify_registry.json").read_text())
    missing = [f"agent-{i}" for i in range(count) if f"agent-{i}" not in registry]
    assert missing == [], f"register_notify lost {missing}"


def test_post_message_rejected_after_close(isolated_bus) -> None:
    """A close transition must block subsequent posts — validated under the
    messages lock so a post can't slip into a just-closed room."""
    room_id = _create_room(isolated_bus)
    isolated_bus.close_room(room_id, "Codex")
    with pytest.raises(ValueError, match="closed"):
        isolated_bus.post_message(room_id, "Codex", "late", "comment")


def test_idempotency_key_reuses_message_id(isolated_bus) -> None:
    room_id = _create_room(isolated_bus)

    first = isolated_bus.post_message(room_id, "Codex", "same", "request", idempotency_key="same-key")
    second = isolated_bus.post_message(room_id, "Codex", "same", "request", idempotency_key="same-key")

    assert first == second
    assert len(isolated_bus._load_messages(room_id)) == 1


def test_http_message_post_honors_idempotency_key(isolated_bus) -> None:
    from mcp_huddle.server import api_message_post

    class _Client:
        host = "127.0.0.1"  # loopback so _require_local allows the request

    class RequestStub:
        def __init__(self, data: dict):
            self._data = data
            self.client = _Client()
            self.headers: dict = {}

        async def json(self) -> dict:
            return self._data

    room_id = _create_room(isolated_bus)
    payload = {
        "room_id": room_id,
        "agent": "Codex",
        "body": "same",
        "kind": "request",
        "idempotency_key": "http-same-key",
    }

    first = asyncio.run(api_message_post(RequestStub(payload)))
    second = asyncio.run(api_message_post(RequestStub(payload)))

    assert first.status_code == 200
    assert second.status_code == 200
    assert json.loads(first.body)["id"] == json.loads(second.body)["id"]
    assert len(isolated_bus._load_messages(room_id)) == 1


def test_http_message_post_rejects_non_loopback(isolated_bus) -> None:
    """The mutating /api/message_post route must enforce _require_local, like the
    other write endpoints — a non-loopback client gets 403 (regression guard)."""
    from mcp_huddle.server import api_message_post

    class _RemoteClient:
        host = "203.0.113.7"  # non-loopback

    class RequestStub:
        client = _RemoteClient()
        headers: dict = {}

        async def json(self) -> dict:
            return {"room_id": _create_room(isolated_bus), "agent": "X",
                    "body": "hi", "kind": "comment"}

    resp = asyncio.run(api_message_post(RequestStub()))
    assert resp.status_code == 403


def test_messages_read_applies_since_id_and_limit(isolated_bus) -> None:
    room_id = _create_room(isolated_bus)
    for index in range(6):
        isolated_bus.post_message(room_id, "Codex", f"message-{index + 1}", "request")

    output = isolated_bus.read_messages(room_id, since_id=2, limit=2)

    assert "message-5" in output
    assert "message-6" in output
    assert "message-1" not in output
    assert "message-2" not in output
    assert "message-3" not in output
    assert "message-4" not in output


def test_circuit_breaker_blocks_repeated_non_request_messages(isolated_bus) -> None:
    room_id = _create_room(isolated_bus)
    for index in range(isolated_bus.CIRCUIT_BREAKER_LIMIT):
        isolated_bus.post_message(room_id, "Gemini", f"comment-{index}", "comment")

    with pytest.raises(ValueError, match="Circuit breaker"):
        isolated_bus.post_message(room_id, "Gemini", "one too many", "comment")


def test_deadlock_watchdog_posts_one_timeout_then_resets_timer(isolated_bus) -> None:
    room_id = _create_room(isolated_bus)
    meta_path = isolated_bus._room_dir(room_id) / "meta.json"
    meta = isolated_bus.get_room_info(room_id)
    meta["last_activity"] = int(time.time()) - isolated_bus.DEADLOCK_TIMEOUT_SECS - 1
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))

    first = isolated_bus.check_deadlock_rooms()
    second = isolated_bus.check_deadlock_rooms()

    assert first == [room_id]
    assert second == []
    timeout_messages = [
        m for m in isolated_bus._load_messages(room_id)
        if m["agent"] == "System" and "Timeout" in m["body"]
    ]
    assert len(timeout_messages) == 1


def test_persistence_survives_module_reload(isolated_bus) -> None:
    room_id = _create_room(isolated_bus)
    isolated_bus.post_message(room_id, "Codex", "persist me", "request")

    reloaded = importlib.reload(isolated_bus)

    assert reloaded.get_room_info(room_id)["id"] == room_id
    assert "persist me" in reloaded.read_messages(room_id)


def test_resolved_room_allows_only_system_or_close_messages(isolated_bus) -> None:
    room_id = _create_room(isolated_bus)
    isolated_bus.invite_agent(room_id, "Gemini")
    resolution_id = isolated_bus.propose_resolution(room_id, "Codex", "Ship reliability pack")

    assert isolated_bus.resolution_vote(room_id, "Gemini", resolution_id, "ack") == "accepted"
    with pytest.raises(ValueError, match="read-only"):
        isolated_bus.post_message(room_id, "Gemini", "late comment", "comment")

    system_id = isolated_bus.post_message(room_id, "System", "allowed", "system")
    close_id = isolated_bus.post_message(room_id, "Codex", "closing", "close")
    assert close_id == system_id + 1


def test_delete_old_terminal_rooms_respects_age_and_status(isolated_bus) -> None:
    import os

    # old closed room → deleted
    old = _create_room(isolated_bus)
    isolated_bus.close_room(old, "Codex")
    old_dir = isolated_bus._room_dir(old)
    eight_days = time.time() - 8 * 86400
    os.utime(old_dir, (eight_days, eight_days))

    # recently closed room → kept (below age cutoff)
    recent = _create_room(isolated_bus)
    isolated_bus.close_room(recent, "Codex")

    # open room → never touched, regardless of mtime
    live = _create_room(isolated_bus)
    os.utime(isolated_bus._room_dir(live), (eight_days, eight_days))

    result = isolated_bus.delete_old_terminal_rooms(7)

    assert old in result["deleted"]
    assert recent not in result["deleted"]
    assert live not in result["deleted"]
    assert not old_dir.exists()
    assert isolated_bus._room_dir(recent).exists()
    assert isolated_bus._room_dir(live).exists()

    # disabled (0 days) is a no-op even on old terminal rooms
    assert isolated_bus.delete_old_terminal_rooms(0)["deleted"] == []


def test_zombie_check_reaps_idle_room_with_dead_owner(isolated_bus) -> None:
    # idle room whose owner_pid is dead must be auto-closed (was leaking before)
    room_id = isolated_bus.create_room("Idle", "Codex", 999_999_999, "/tmp", "s")
    isolated_bus.mark_idle(room_id)
    assert isolated_bus._read_meta(room_id)["status"] == "idle"

    closed = isolated_bus.check_zombie_rooms()

    assert room_id in closed
    assert isolated_bus._read_meta(room_id)["status"] == "closed"


def test_messages_read_until_id_windows(isolated_bus) -> None:
    room_id = _create_room(isolated_bus)
    for index in range(6):
        isolated_bus.post_message(room_id, "Codex", f"message-{index + 1}", "request")

    output = isolated_bus.read_messages(room_id, since_id=1, until_id=3)

    assert "message-2" in output
    assert "message-3" in output
    assert "message-1" not in output  # excluded by since_id
    assert "message-4" not in output  # excluded by until_id


def test_messages_read_truncates_long_body(isolated_bus) -> None:
    room_id = _create_room(isolated_bus)
    isolated_bus.post_message(room_id, "Codex", "X" * 5000, "comment")

    capped = isolated_bus.read_messages(room_id, max_chars=100)
    assert "truncated" in capped
    assert len(capped) < 5000

    full = isolated_bus.read_messages(room_id, max_chars=0)
    assert "X" * 5000 in full


def test_zombie_grace_spares_active_room_with_dead_pid(isolated_bus) -> None:
    # open room, dead owner_pid, but fresh activity → a resumed session, not a
    # zombie: must NOT be reaped.
    room_id = isolated_bus.create_room("Active", "Codex", 999_999_999, "/tmp", "s")

    closed = isolated_bus.check_zombie_rooms()

    assert room_id not in closed
    assert isolated_bus._read_meta(room_id)["status"] == "open"


def test_zombie_reaps_open_room_after_grace(isolated_bus) -> None:
    room_id = isolated_bus.create_room("Stale", "Codex", 999_999_999, "/tmp", "s")
    old = int(time.time()) - isolated_bus.ZOMBIE_GRACE_SECS - 10
    isolated_bus._update_meta_locked(room_id, lambda m: {**m, "last_activity": old})

    closed = isolated_bus.check_zombie_rooms()

    assert room_id in closed


def test_reclaim_room_restamps_owner_pid(isolated_bus) -> None:
    room_id = isolated_bus.create_room("Resumed", "Codex", 111, "/tmp", "old")

    isolated_bus.reclaim_room(room_id, "Codex", 222, "new")

    meta = isolated_bus._read_meta(room_id)
    assert meta["owner_pid"] == 222
    assert meta["session_id"] == "new"


def test_reclaim_room_rejects_non_owner(isolated_bus) -> None:
    room_id = isolated_bus.create_room("Resumed", "Codex", 111, "/tmp", "old")

    with pytest.raises(ValueError):
        isolated_bus.reclaim_room(room_id, "Mallory", 222)
