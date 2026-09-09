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

    assert all(not thread.is_alive() for thread in threads)
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

    assert all(not thread.is_alive() for thread in threads)
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

    assert not t1.is_alive() and not t2.is_alive()
    assert errors == []
    statuses = isolated_bus.get_status(room_id)
    assert statuses.get("A") == "online"  # expired lease reset
    assert statuses.get("B") == "busy"     # concurrent write not clobbered


def test_status_details_persist_phase_and_task_metadata(isolated_bus) -> None:
    room_id = _create_room(isolated_bus)

    isolated_bus.set_status(
        room_id,
        "Codex",
        "busy",
        300,
        "sess",
        phase="thinking",
        task_id=17,
        detail="researching sources",
        source="agent",
    )

    details = isolated_bus.get_status_details(room_id)
    codex = details["Codex"]
    assert codex["status"] == "busy"
    assert codex["phase"] == "thinking"
    assert codex["task_id"] == 17
    assert codex["detail"] == "researching sources"
    assert codex["source"] == "agent"
    assert codex["updated_at"] > 0


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

    assert not t1.is_alive() and not t2.is_alive()
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
            isolated_bus.register_notify(room_id, f"agent-{index}", f"notify-{index}.json")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    registry = json.loads((isolated_bus._room_dir(room_id) / "notify_registry.json").read_text())
    missing = [f"agent-{i}" for i in range(count) if f"agent-{i}" not in registry]
    assert missing == [], f"register_notify lost {missing}"
    root = (isolated_bus.HUDDLE_HOME / "notifications").resolve()
    assert all(Path(path).resolve().parent == root for path in registry.values())


@pytest.mark.parametrize("bad_room_id", [
    "", ".", "..", "../escape", "a/b", "a\\b", "/tmp/escape", "room_abc\n",
])
def test_room_id_rejects_traversal_and_separators(isolated_bus, bad_room_id: str) -> None:
    with pytest.raises(ValueError):
        isolated_bus.get_room_info(bad_room_id)


def test_room_id_accepts_generated_and_safe_historical_ids(isolated_bus) -> None:
    room_id = _create_room(isolated_bus)
    assert isolated_bus._room_dir(room_id).parent == isolated_bus.BUS_DIR.resolve()
    assert isolated_bus._room_dir("historic-room.v1").name == "historic-room.v1"


def test_room_symlink_cannot_escape_bus_root(isolated_bus, tmp_path: Path) -> None:
    isolated_bus.BUS_DIR.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside-room"
    outside.mkdir()
    (isolated_bus.BUS_DIR / "room_deadbeef").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="escapes"):
        isolated_bus.get_room_info("room_deadbeef")


@pytest.mark.parametrize("bad_agent", [
    "", ".", "..", "../escape", "a/b", "a\\b", "agent\x00name", "agent\nname",
])
def test_external_agent_filename_rejects_unsafe_names(isolated_bus, bad_agent: str) -> None:
    room_id = _create_room(isolated_bus)
    with pytest.raises(ValueError):
        isolated_bus.register_external_agent(room_id, bad_agent)
    with pytest.raises(ValueError):
        isolated_bus.append_agent_event(room_id, bad_agent, {"event": "test"})


def test_external_agent_paths_are_contained_and_historical_names_work(isolated_bus) -> None:
    room_id = _create_room(isolated_bus)
    paths = isolated_bus.register_external_agent(
        room_id, "Claude Opus 5 (subscription review)")
    agents_root = (isolated_bus._room_dir(room_id) / "agents").resolve()

    assert Path(paths["log_path"]).resolve().parent == agents_root
    assert Path(paths["last_message_path"]).resolve().parent == agents_root


def test_external_agent_rejects_symlinked_agents_directory(
    isolated_bus, tmp_path: Path,
) -> None:
    room_id = _create_room(isolated_bus)
    outside = tmp_path / "outside-agents"
    outside.mkdir()
    (isolated_bus._room_dir(room_id) / "agents").symlink_to(
        outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        isolated_bus.append_agent_event(room_id, "Codex", {"secret": "no"})
    assert not (outside / "codex.events.jsonl").exists()


def test_room_owned_file_symlinks_fail_closed(isolated_bus, tmp_path: Path) -> None:
    outside = tmp_path / "outside-owned-file"
    outside.write_text("outside-sentinel")

    meta_room = _create_room(isolated_bus)
    meta_path = isolated_bus._room_dir(meta_room) / "meta.json"
    meta_path.unlink()
    meta_path.symlink_to(outside)
    with pytest.raises((ValueError, OSError)):
        isolated_bus.get_room_info(meta_room)

    messages_room = _create_room(isolated_bus)
    messages_path = isolated_bus._room_dir(messages_room) / "messages.jsonl"
    messages_path.symlink_to(outside)
    with pytest.raises((ValueError, OSError)):
        isolated_bus.post_message(messages_room, "Codex", "must-not-escape", "request")

    status_room = _create_room(isolated_bus)
    status_path = isolated_bus._room_dir(status_room) / "status.json"
    status_path.unlink()
    status_path.symlink_to(outside)
    with pytest.raises((ValueError, OSError)):
        isolated_bus.set_status(status_room, "Codex", "busy")

    notify_room = _create_room(isolated_bus)
    registry_path = isolated_bus._room_dir(notify_room) / "notify_registry.json"
    registry_path.symlink_to(outside)
    with pytest.raises((ValueError, OSError)):
        isolated_bus.register_notify(notify_room, "Codex", "codex.json")

    event_room = _create_room(isolated_bus)
    paths = isolated_bus.register_external_agent(event_room, "Codex")
    event_path = Path(paths["log_path"])
    event_path.unlink()
    event_path.symlink_to(outside)
    with pytest.raises((ValueError, OSError)):
        isolated_bus.append_agent_event(event_room, "Codex", {"must": "not escape"})

    assert outside.read_text() == "outside-sentinel"


def test_room_directory_swap_after_validation_cannot_escape(
    isolated_bus, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    room_id = _create_room(isolated_bus)
    rdir = isolated_bus._room_dir(room_id)
    parked = tmp_path / "parked-room"
    outside = tmp_path / "outside-room-race"
    outside.mkdir()
    (outside / "meta.json").write_text('{"secret": "outside"}')
    real_room_dir = isolated_bus._room_dir
    swapped = False

    def swap_after_validation(requested: str) -> Path:
        nonlocal swapped
        result = real_room_dir(requested)
        if requested == room_id and not swapped:
            swapped = True
            rdir.rename(parked)
            rdir.symlink_to(outside, target_is_directory=True)
        return result

    monkeypatch.setattr(isolated_bus, "_room_dir", swap_after_validation)
    with pytest.raises((ValueError, OSError)):
        isolated_bus.get_room_info(room_id)
    assert (outside / "meta.json").read_text() == '{"secret": "outside"}'


def test_register_notify_rejects_outside_and_symlink_targets(
    isolated_bus, tmp_path: Path,
) -> None:
    room_id = _create_room(isolated_bus)
    outside = tmp_path / "outside-notify.json"
    with pytest.raises(ValueError, match="inside"):
        isolated_bus.register_notify(room_id, "Codex", str(outside))
    with pytest.raises(ValueError):
        isolated_bus.register_notify(room_id, "Codex", "../outside-notify.json")

    notifications = isolated_bus.HUDDLE_HOME / "notifications"
    notifications.mkdir(parents=True, exist_ok=True)
    (notifications / "linked.json").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        isolated_bus.register_notify(room_id, "Codex", "linked.json")
    assert not outside.exists()


@pytest.mark.parametrize("bad_filename", ["bad\\name", "bad\nname", "x" * 256])
def test_register_notify_rejects_unsafe_filename_components(
    isolated_bus, bad_filename: str,
) -> None:
    room_id = _create_room(isolated_bus)
    with pytest.raises(ValueError):
        isolated_bus.register_notify(room_id, "Codex", bad_filename)


@pytest.mark.parametrize("bad_agent", ["../agent", "agent\\name", "agent\x00name", "x" * 256])
def test_register_notify_rejects_unsafe_agent_components(
    isolated_bus, bad_agent: str,
) -> None:
    room_id = _create_room(isolated_bus)
    with pytest.raises(ValueError):
        isolated_bus.register_notify(room_id, bad_agent, "notify.json")


def test_notify_accepts_configured_symlink_root(
    isolated_bus, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    room_id = _create_room(isolated_bus)
    actual_root = tmp_path / "actual-notifications"
    actual_root.mkdir()
    configured_root = tmp_path / "configured-notifications"
    configured_root.symlink_to(actual_root, target_is_directory=True)
    monkeypatch.setattr(isolated_bus, "NOTIFICATIONS_DIR", configured_root)
    lexical_target = configured_root / "watcher.json"

    isolated_bus.register_notify(room_id, "Watcher", str(lexical_target))
    msg_id = isolated_bus.post_message(room_id, "Codex", "wake", "request")

    payload = json.loads((actual_root / "watcher.json").read_text())
    assert payload["msg_id"] == msg_id
    registry = json.loads(
        (isolated_bus._room_dir(room_id) / "notify_registry.json").read_text())
    assert registry["Watcher"] == str(actual_root / "watcher.json")


def test_concurrent_notification_delivery_is_atomic_and_keeps_newest(isolated_bus) -> None:
    room_id = _create_room(isolated_bus)
    target = isolated_bus.HUDDLE_HOME / "notifications" / "watcher.json"
    isolated_bus.register_notify(room_id, "Watcher", str(target))
    count = 16
    barrier = threading.Barrier(count)
    ids: list[int] = []
    errors: list[BaseException] = []
    guard = threading.Lock()

    def worker(index: int) -> None:
        try:
            barrier.wait(timeout=5)
            msg_id = isolated_bus.post_message(
                room_id, f"sender-{index}", f"request-{index}", "request")
            with guard:
                ids.append(msg_id)
        except BaseException as exc:  # noqa: BLE001
            with guard:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert json.loads(target.read_text())["msg_id"] == max(ids)
    assert {item.name for item in target.parent.iterdir()} == {target.name}
    assert any(isolated_bus.NOTIFICATION_LOCKS_DIR.iterdir())


def test_post_message_rejected_after_close(isolated_bus) -> None:
    """A close transition must block subsequent posts — validated under the
    messages lock so a post can't slip into a just-closed room."""
    room_id = _create_room(isolated_bus)
    isolated_bus.close_room(room_id, "Codex")
    with pytest.raises(ValueError, match="closed"):
        isolated_bus.post_message(room_id, "Codex", "late", "comment")


def test_close_and_delete_reject_non_owner_before_side_effects(
    isolated_bus, monkeypatch: pytest.MonkeyPatch,
) -> None:
    room_id = _create_room(isolated_bus)
    calls = {"kill": 0, "message": 0}
    monkeypatch.setattr(
        isolated_bus.child_processes, "close_room",
        lambda _room: calls.__setitem__("kill", calls["kill"] + 1),
    )
    monkeypatch.setattr(
        isolated_bus, "_append_terminal_system",
        lambda _room, _text: calls.__setitem__("message", calls["message"] + 1),
    )

    with pytest.raises(ValueError, match="not the owner"):
        isolated_bus.close_room(room_id, "Mallory")
    assert calls == {"kill": 0, "message": 0}
    assert isolated_bus.get_room_info(room_id)["status"] == "open"

    isolated_bus.close_room(room_id, "Codex")
    with pytest.raises(ValueError, match="not the owner"):
        isolated_bus.delete_room(room_id, "Mallory")
    assert isolated_bus._room_dir(room_id).exists()


def test_concurrent_close_has_one_message_and_one_kill_sweep(
    isolated_bus, monkeypatch: pytest.MonkeyPatch,
) -> None:
    room_id = _create_room(isolated_bus)
    count = 12
    barrier = threading.Barrier(count)
    errors: list[BaseException] = []
    kills = 0
    kills_guard = threading.Lock()
    real_kill = isolated_bus.child_processes.close_room

    def counted_kill(target_room_id: str) -> dict:
        nonlocal kills
        with kills_guard:
            kills += 1
        return real_kill(target_room_id)

    monkeypatch.setattr(isolated_bus.child_processes, "close_room", counted_kill)

    def worker() -> None:
        try:
            barrier.wait(timeout=5)
            isolated_bus.close_room(room_id, "Codex")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert kills == 1
    close_messages = [
        m for m in isolated_bus._load_messages(room_id)
        if m.get("agent") == "System" and m.get("body") == "Чат закрыт."
    ]
    assert len(close_messages) == 1


def test_delete_cannot_race_close_side_effects(
    isolated_bus, monkeypatch: pytest.MonkeyPatch,
) -> None:
    room_id = _create_room(isolated_bus)
    termination_started = threading.Event()
    allow_termination = threading.Event()
    errors: list[BaseException] = []

    def slow_close(target_room_id: str) -> dict:
        assert target_room_id == room_id
        termination_started.set()
        assert allow_termination.wait(timeout=5)
        return {"sent": 0, "exited": 0, "denied": 0}

    monkeypatch.setattr(isolated_bus.child_processes, "close_room", slow_close)

    def closer() -> None:
        try:
            isolated_bus.close_room(room_id, "Codex")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=closer)
    thread.start()
    assert termination_started.wait(timeout=5)
    assert isolated_bus.get_room_info(room_id)["status"] == "closing"

    with pytest.raises(ValueError, match="Close it first"):
        isolated_bus.delete_room(room_id, "Codex")
    assert isolated_bus._room_dir(room_id).exists()

    allow_termination.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert errors == []
    assert isolated_bus.get_room_info(room_id)["status"] == "closed"
    close_messages = [
        item for item in isolated_bus._load_messages(room_id)
        if item.get("body") == "Чат закрыт."
    ]
    assert len(close_messages) == 1

    isolated_bus.delete_room(room_id, "Codex")
    assert not isolated_bus._room_dir(room_id).exists()


def test_corrupt_meta_fails_closed_without_overwrite(isolated_bus) -> None:
    room_id = _create_room(isolated_bus)
    meta_path = isolated_bus._room_dir(room_id) / "meta.json"
    corrupt = "{not-json\n"
    meta_path.write_text(corrupt)

    with pytest.raises(ValueError, match="Corrupt"):
        isolated_bus.get_room_info(room_id)
    with pytest.raises(ValueError, match="refusing to overwrite"):
        isolated_bus.invite_agent(room_id, "Gemini")
    assert meta_path.read_text() == corrupt


@pytest.mark.parametrize(("field", "bad_value"), [
    ("id", "room_wrong"),
    ("status", "teleported"),
    ("participants", "Codex"),
    ("participants", []),
    ("participants", ["Codex", 7]),
    ("owner", "Mallory"),
    ("created_at", "yesterday"),
    ("created_at", float("nan")),
])
def test_semantically_invalid_meta_is_skipped_and_not_overwritten(
    isolated_bus, field: str, bad_value,
) -> None:
    room_id = _create_room(isolated_bus)
    meta_path = isolated_bus._room_dir(room_id) / "meta.json"
    meta = isolated_bus.get_room_info(room_id)
    meta[field] = bad_value
    invalid_raw = json.dumps(meta, ensure_ascii=False)
    meta_path.write_text(invalid_raw)

    with pytest.raises(ValueError, match="Corrupt"):
        isolated_bus.get_room_info(room_id)
    with pytest.raises(ValueError, match="refusing to overwrite"):
        isolated_bus.invite_agent(room_id, "Gemini")
    assert room_id not in {item["id"] for item in isolated_bus.list_rooms()}
    assert meta_path.read_text() == invalid_raw


def test_circuit_breaker_check_is_atomic_with_append(isolated_bus) -> None:
    room_id = _create_room(isolated_bus)
    for index in range(isolated_bus.CIRCUIT_BREAKER_LIMIT - 1):
        isolated_bus.post_message(room_id, "Gemini", f"comment-{index}", "comment")

    count = 10
    barrier = threading.Barrier(count)
    successes: list[int] = []
    errors: list[BaseException] = []
    guard = threading.Lock()

    def worker(index: int) -> None:
        try:
            barrier.wait(timeout=5)
            msg_id = isolated_bus.post_message(
                room_id, "Gemini", f"parallel-{index}", "comment")
            with guard:
                successes.append(msg_id)
        except BaseException as exc:  # noqa: BLE001
            with guard:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert len(successes) == 1
    assert len(errors) == count - 1
    assert all("Circuit breaker" in str(exc) for exc in errors)
    assert len(isolated_bus._load_messages(room_id)) == isolated_bus.CIRCUIT_BREAKER_LIMIT


def test_stored_message_body_has_hard_byte_cap(isolated_bus) -> None:
    room_id = _create_room(isolated_bus)
    accepted = "x" * isolated_bus.MAX_STORED_BODY_BYTES
    isolated_bus.post_message(room_id, "Codex", accepted, "request")

    with pytest.raises(ValueError, match="too large"):
        isolated_bus.post_message(
            room_id, "Codex", "y" * (isolated_bus.MAX_STORED_BODY_BYTES + 1), "request")
    assert len(isolated_bus._load_messages(room_id)) == 1


@pytest.mark.parametrize("oversized_field", ["agent", "to", "idempotency_key", "meta"])
def test_complete_serialized_message_has_unicode_byte_cap(
    isolated_bus, oversized_field: str,
) -> None:
    room_id = _create_room(isolated_bus)
    huge = "🔥" * isolated_bus.MAX_STORED_MESSAGE_BYTES
    kwargs = {
        "agent": "Codex",
        "to": None,
        "idempotency_key": None,
        "msg_meta": None,
    }
    if oversized_field == "meta":
        kwargs["msg_meta"] = {"model": huge}
    else:
        kwargs[oversized_field] = huge

    with pytest.raises(ValueError, match="Serialized message is too large"):
        isolated_bus.post_message(
            room_id,
            kwargs["agent"],
            "small body",
            "request",
            to=kwargs["to"],
            idempotency_key=kwargs["idempotency_key"],
            msg_meta=kwargs["msg_meta"],
        )
    assert isolated_bus._load_messages(room_id) == []


def test_message_entry_rejects_non_json_numeric_metadata(isolated_bus) -> None:
    room_id = _create_room(isolated_bus)
    with pytest.raises(ValueError, match="not JSON-serializable"):
        isolated_bus.post_message(
            room_id, "Codex", "small", "request",
            msg_meta={"duration_ms": float("nan")},
        )
    assert isolated_bus._load_messages(room_id) == []


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


def test_room_rate_limit_has_deterministic_open_window_boundary(
    isolated_bus, monkeypatch: pytest.MonkeyPatch,
) -> None:
    room_id = _create_room(isolated_bus)
    clock = [1_000]
    monkeypatch.setattr(isolated_bus, "ROOM_MESSAGE_RATE_LIMIT", 3)
    monkeypatch.setattr(isolated_bus, "ROOM_MESSAGE_RATE_WINDOW_SECS", 60)
    monkeypatch.setattr(isolated_bus.time, "time", lambda: clock[0])

    for index in range(3):
        isolated_bus.post_message(room_id, f"agent-{index}", "ok", "request")
    with pytest.raises(ValueError, match="Room rate limit"):
        isolated_bus.post_message(room_id, "agent-3", "blocked", "request")

    # Exactly one full window old is outside the open lower boundary.
    clock[0] = 1_060
    assert isolated_bus.post_message(room_id, "agent-3", "admitted", "request") == 4


def test_room_rate_limit_cannot_be_bypassed_by_human_or_system_and_close_marks(
    isolated_bus, monkeypatch: pytest.MonkeyPatch,
) -> None:
    room_id = _create_room(isolated_bus)
    monkeypatch.setattr(isolated_bus, "ROOM_MESSAGE_RATE_LIMIT", 2)

    isolated_bus.post_message(room_id, "Human", "one", "comment")
    isolated_bus.post_message(room_id, "System", "two", "system")
    with pytest.raises(ValueError, match="Room rate limit"):
        isolated_bus.post_message(room_id, "Human", "spoofed bypass", "request")

    isolated_bus.close_room(room_id, "Codex")
    messages = isolated_bus._load_messages(room_id)
    assert len(messages) == 3
    assert messages[-1]["body"] == "Чат закрыт."


def test_idempotent_retry_precedes_room_rate_admission(
    isolated_bus, monkeypatch: pytest.MonkeyPatch,
) -> None:
    room_id = _create_room(isolated_bus)
    monkeypatch.setattr(isolated_bus, "ROOM_MESSAGE_RATE_LIMIT", 1)
    first = isolated_bus.post_message(
        room_id, "Codex", "once", "request", idempotency_key="same")

    assert isolated_bus.post_message(
        room_id, "Codex", "once", "request", idempotency_key="same") == first
    with pytest.raises(ValueError, match="Room rate limit"):
        isolated_bus.post_message(room_id, "Codex", "different", "request")
    assert len(isolated_bus._load_messages(room_id)) == 1


def test_room_rate_admission_is_atomic_across_concurrent_posters(
    isolated_bus, monkeypatch: pytest.MonkeyPatch,
) -> None:
    room_id = _create_room(isolated_bus)
    limit = 12
    count = 30
    monkeypatch.setattr(isolated_bus, "ROOM_MESSAGE_RATE_LIMIT", limit)
    barrier = threading.Barrier(count)
    successes: list[int] = []
    errors: list[BaseException] = []
    guard = threading.Lock()

    def worker(index: int) -> None:
        try:
            barrier.wait(timeout=5)
            msg_id = isolated_bus.post_message(
                room_id, f"agent-{index}", "parallel", "request")
            with guard:
                successes.append(msg_id)
        except BaseException as exc:  # noqa: BLE001
            with guard:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert sorted(successes) == list(range(1, limit + 1))
    assert len(errors) == count - limit
    assert all("Room rate limit" in str(exc) for exc in errors)
    assert len(isolated_bus._load_messages(room_id)) == limit


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


def test_bulk_close_blocks_delete_until_teardown_finishes(
    isolated_bus, monkeypatch: pytest.MonkeyPatch,
) -> None:
    room_id = _create_room(isolated_bus)
    teardown_started = threading.Event()
    allow_teardown = threading.Event()
    errors: list[BaseException] = []

    def slow_close(target_room_id: str) -> dict:
        assert target_room_id == room_id
        teardown_started.set()
        assert allow_teardown.wait(timeout=5)
        return {"sent": 0, "exited": 0, "denied": 0}

    monkeypatch.setattr(isolated_bus.child_processes, "close_room", slow_close)

    def bulk_closer() -> None:
        try:
            result = isolated_bus.close_all_rooms()
            assert result["closed"] == [room_id]
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=bulk_closer)
    thread.start()
    assert teardown_started.wait(timeout=5)
    assert isolated_bus.get_room_info(room_id)["status"] == "closing"
    with pytest.raises(ValueError, match="Close it first"):
        isolated_bus.delete_room(room_id, "Codex")

    allow_teardown.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert errors == []
    assert isolated_bus.get_room_info(room_id)["status"] == "closed"


def test_retention_tombstones_resolved_room_children_before_delete(isolated_bus) -> None:
    import os

    class FakeProc:
        pid = 424242

        def __init__(self) -> None:
            self.terminated = False

        def poll(self):
            return -15 if self.terminated else None

        def terminate(self) -> None:
            self.terminated = True

    isolated_bus.child_processes._reset_for_tests()
    try:
        room_id = _create_room(isolated_bus)
        isolated_bus._update_meta_locked(
            room_id, lambda meta: {**meta, "status": "resolved"})
        old = time.time() - 8 * 86400
        os.utime(isolated_bus._room_dir(room_id), (old, old))
        existing = FakeProc()
        isolated_bus.child_processes.register(existing, room_id, "existing")

        result = isolated_bus.delete_old_terminal_rooms(7)

        assert result["deleted"] == [room_id]
        assert existing.terminated
        assert not isolated_bus._room_dir(room_id).exists()

        late = FakeProc()
        isolated_bus.child_processes.register(late, room_id, "late")
        assert late.terminated
    finally:
        isolated_bus.child_processes._reset_for_tests()


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
    assert "cut" in capped  # elision marker
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


def test_advance_round_stamps_and_filters(isolated_bus) -> None:
    room_id = isolated_bus.create_room("Debate", "Codex", 0, "/tmp", "s")
    isolated_bus.post_message(room_id, "A", "pre-round", "comment")  # round 0

    assert isolated_bus.advance_round(room_id, "Codex", "opening") == 1
    isolated_bus.post_message(room_id, "A", "r1-msg", "comment")
    assert isolated_bus.advance_round(room_id, "Codex") == 2
    isolated_bus.post_message(room_id, "B", "r2-msg", "comment")

    r1 = isolated_bus.read_messages(room_id, round=1)
    assert "r1-msg" in r1
    assert "Round 1: opening" in r1  # visible divider
    assert "r2-msg" not in r1
    assert "pre-round" not in r1

    # current round (-1) == round 2
    assert "r2-msg" in isolated_bus.read_messages(room_id, round=-1)


def test_advance_round_owner_only(isolated_bus) -> None:
    room_id = isolated_bus.create_room("Debate", "Codex", 0, "/tmp", "s")
    with pytest.raises(ValueError):
        isolated_bus.advance_round(room_id, "Mallory")


def test_read_messages_kind_filter(isolated_bus) -> None:
    room_id = isolated_bus.create_room("Mix", "Codex", 0, "/tmp", "s")
    isolated_bus.post_message(room_id, "A", "chatter", "comment")
    isolated_bus.post_message(room_id, "A", "the-deliverable", "result")

    out = isolated_bus.read_messages(room_id, kind="result")
    assert "the-deliverable" in out
    assert "chatter" not in out


def test_head_tail_truncation_preserves_conclusion(isolated_bus) -> None:
    room_id = isolated_bus.create_room("Long", "Codex", 0, "/tmp", "s")
    body = "OPENING_MARK" + ("." * 5000) + "CONCLUSION_MARK"
    isolated_bus.post_message(room_id, "A", body, "comment")

    out = isolated_bus.read_messages(room_id, max_chars=300)
    assert "OPENING_MARK" in out       # head kept
    assert "CONCLUSION_MARK" in out    # tail kept (head-only would drop this)
    assert "cut" in out                # elision marker present
    assert len(out) < 5000
