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


def test_idempotency_key_reuses_message_id(isolated_bus) -> None:
    room_id = _create_room(isolated_bus)

    first = isolated_bus.post_message(room_id, "Codex", "same", "request", idempotency_key="same-key")
    second = isolated_bus.post_message(room_id, "Codex", "same", "request", idempotency_key="same-key")

    assert first == second
    assert len(isolated_bus._load_messages(room_id)) == 1


def test_http_message_post_honors_idempotency_key(isolated_bus) -> None:
    from mcp_huddle.server import api_message_post

    class RequestStub:
        def __init__(self, data: dict):
            self._data = data

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
