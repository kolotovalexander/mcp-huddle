from __future__ import annotations

import importlib
import multiprocessing
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from mcp_huddle import bus, child_processes, server, spawn


def _fork_registry_probe(room_id: str, handle: str, results) -> None:
    """Inspect inherited registry state from a real fork child."""
    from mcp_huddle import child_processes as fork_registry

    results.put({
        "state": fork_registry.state(room_id, handle),
        "terminate": fork_registry.terminate(room_id, handle),
        "owned": sorted(fork_registry.owned_room_ids()),
        "close": fork_registry.close_room(room_id),
        "pid": os.getpid(),
    })


class FakeProc:
    def __init__(self, pid: int, returncode=None):
        self.pid = pid
        self.returncode = returncode
        self.terminate_calls = 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminate_calls += 1


@pytest.fixture(autouse=True)
def reset_registry():
    child_processes._reset_for_tests()
    yield
    spawn._drain_background_for_tests()
    child_processes._reset_for_tests()


def test_exact_room_and_handle_are_required_even_when_pid_matches() -> None:
    current = FakeProc(4242)
    child_processes.register(current, "room_new", "wake_new")

    assert child_processes.terminate("room_old", "wake_new") == "unknown"
    assert child_processes.terminate("room_new", "wake_old") == "unknown"
    assert current.terminate_calls == 0

    assert child_processes.terminate("room_new", "wake_new") == "sent"
    assert child_processes.terminate("room_new", "wake_new") == "sent"
    assert current.terminate_calls == 1


def test_fork_child_drops_parent_popen_authority_without_signalling_it() -> None:
    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("requires multiprocessing fork")
    room_id = "room-parent-owned"
    handle = "parent-handle"
    proc = subprocess.Popen([
        sys.executable, "-c", "import time; time.sleep(10)",
    ])
    child_processes.register(proc, room_id, handle)
    context = multiprocessing.get_context("fork")
    results = context.Queue()
    contender = context.Process(
        target=_fork_registry_probe, args=(room_id, handle, results),
    )
    try:
        # Fork while the parent registry lock is held too: the child hook must
        # replace both the authority records and potentially inherited lock.
        with child_processes._LOCK:
            contender.start()
        contender.join(timeout=5)
        assert contender.exitcode == 0
        observed = results.get(timeout=2)
        assert observed["pid"] != os.getpid()
        assert observed["state"] == "unknown"
        assert observed["terminate"] == "unknown"
        assert observed["owned"] == []
        assert observed["close"] == {"sent": 0, "exited": 0, "denied": 0}
        assert proc.poll() is None
    finally:
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=3)
        child_processes.wait(room_id, handle)


def test_close_tombstone_terminates_late_registration() -> None:
    assert child_processes.close_room("room_closed")["sent"] == 0
    late = FakeProc(5252)

    child_processes.register(late, "room_closed", "late")

    assert late.terminate_calls == 1


def test_closed_room_tombstone_history_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(child_processes, "_CLOSED_ROOMS_MAX", 3)
    for index in range(8):
        child_processes.close_room(f"room-{index}")

    assert list(child_processes._CLOSED_ROOMS) == ["room-5", "room-6", "room-7"]
    late = FakeProc(5253)
    child_processes.register(late, "room-7", "late-recent")
    assert late.terminate_calls == 1


def test_owned_room_ids_only_exposes_current_popen_ownership() -> None:
    child_processes.register(FakeProc(5353), "room_a", "a")
    child_processes.register(FakeProc(5454), "room_b", "b")
    child_processes.register(FakeProc(5555), "", "unscoped")

    assert child_processes.owned_room_ids() == {"room_a", "room_b"}


def test_room_close_terminates_owned_popen_not_persisted_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    isolated_bus = importlib.reload(bus)
    room_id = isolated_bus.create_room("owned", "Codex", 0, str(tmp_path), "session")
    owned = FakeProc(6262)
    child_processes.register(owned, room_id, "owned-child")
    meta = isolated_bus.get_room_info(room_id)
    meta["spawned_pids"] = [6262, 7373]
    isolated_bus._write_json(isolated_bus._room_dir(room_id) / "meta.json", meta)

    real_kill = isolated_bus.os.kill

    def no_sigterm(pid, sig):
        if sig != 0:
            pytest.fail("room close must never signal a persisted PID")
        return real_kill(pid, sig)

    monkeypatch.setattr(isolated_bus.os, "kill", no_sigterm)
    isolated_bus.close_room(room_id, "Codex")

    assert owned.terminate_calls == 1


def test_spawn_reaper_removes_ownership_then_calls_callback_once() -> None:
    proc = FakeProc(8383, returncode=0)
    calls = []
    done = threading.Event()

    def callback(returncode):
        # A different thread must be able to enter the registry while the
        # callback runs. RLock re-entry from this same callback thread would
        # not prove that the callback is outside the registry critical section.
        registry_available = threading.Event()

        def probe_registry_lock():
            with child_processes._LOCK:
                registry_available.set()

        probe = threading.Thread(target=probe_registry_lock)
        probe.start()
        lock_was_free = registry_available.wait(1)
        probe.join(timeout=1)
        calls.append((
            returncode,
            child_processes.state("room", "handle"),
            lock_was_free,
        ))
        done.set()

    spawn._reap_in_background(
        proc, "Reviewer", on_exit=callback,
        owner_room_id="room", process_handle="handle",
    )

    assert done.wait(2)
    assert calls == [(0, "exited", True)]


def test_background_test_drain_reaches_callback_created_quiescence() -> None:
    completed: list[str] = []

    def second_reaper(index: int) -> None:
        time.sleep(0.001)
        completed.append(f"second-{index}")
        with spawn._BACKGROUND_LOCK:
            spawn._REAPER_THREADS.discard(threading.current_thread())

    def first_reaper(index: int) -> None:
        second = threading.Thread(target=second_reaper, args=(index,), daemon=True)
        with spawn._BACKGROUND_LOCK:
            spawn._REAPER_THREADS.add(second)
            second.start()
        completed.append(f"first-{index}")
        with spawn._BACKGROUND_LOCK:
            spawn._REAPER_THREADS.discard(threading.current_thread())

    for index in range(100):
        first = threading.Thread(target=first_reaper, args=(index,), daemon=True)
        with spawn._BACKGROUND_LOCK:
            spawn._REAPER_THREADS.add(first)
            first.start()

    spawn._drain_background_for_tests(timeout=2)

    assert len(completed) == 200
    assert {item.split("-", 1)[0] for item in completed} == {"first", "second"}
    with spawn._BACKGROUND_LOCK:
        assert spawn._REAPER_THREADS == set()
        assert spawn._SPAWN_TIMERS == set()


def test_exited_child_is_not_terminated() -> None:
    proc = FakeProc(9494, returncode=7)
    child_processes.register(proc, "room", "done")

    assert child_processes.terminate("room", "done") == "exited"
    assert proc.terminate_calls == 0


def test_recent_exited_handle_history_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(child_processes, "_EXITED_HANDLES_MAX", 3)
    for index in range(8):
        proc = FakeProc(20_000 + index, returncode=0)
        handle = f"done-{index}"
        child_processes.register(proc, "room", handle)
        assert child_processes.wait("room", handle) == 0

    assert len(child_processes._EXITED_HANDLES) == 3
    assert child_processes.state("room", "done-0") == "unknown"
    assert child_processes.state("room", "done-7") == "exited"


def test_registration_immediately_stops_child_if_room_already_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    isolated_bus = importlib.reload(bus)
    room_id = isolated_bus.create_room(
        "closing", "Owner", 0, str(tmp_path), "session",
    )
    isolated_bus._update_meta_locked(
        room_id, lambda meta: {**meta, "status": "closing"},
    )
    proc = FakeProc(30_001, returncode=None)

    spawn._reap_in_background(
        proc, "Reviewer", owner_room_id=room_id,
        process_handle="close-race",
    )

    assert proc.terminate_calls == 1
    proc.returncode = -15


def test_foreign_close_and_delete_are_reconciled_by_exact_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Instance B only updates shared state; instance A's watchdog stops its
    own exact children without ever signalling B's persisted PID."""
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    isolated_bus = importlib.reload(bus)
    monkeypatch.setattr(server, "bus", isolated_bus)

    closed_room = isolated_bus.create_room(
        "closed elsewhere", "Owner", 0, str(tmp_path), "session",
    )
    deleted_room = isolated_bus.create_room(
        "deleted elsewhere", "Owner", 0, str(tmp_path), "session",
    )
    closed_child = FakeProc(10_001)
    deleted_child = FakeProc(10_002)
    child_processes.register(closed_child, closed_room, "closed-child")
    child_processes.register(deleted_child, deleted_room, "deleted-child")

    # Simulate another process's close transition without using this process's
    # in-memory registry, followed by a rapid close+delete for another room.
    isolated_bus._update_meta_locked(
        closed_room, lambda meta: {**meta, "status": "closed"},
    )
    shutil.rmtree(isolated_bus._room_dir(deleted_room))

    reconciled = server._reconcile_owned_children()

    assert set(reconciled) == {closed_room, deleted_room}
    assert closed_child.terminate_calls == 1
    assert deleted_child.terminate_calls == 1


def test_owned_spawn_log_does_not_recreate_deleted_room(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    isolated_bus = importlib.reload(bus)
    room_id = "room_deleted"
    spec: spawn.SpawnSpec = {
        "name": "Echo",
        "cmd": ["echo", "{brief}"],
        "enabled": True,
    }

    with pytest.raises((FileNotFoundError, ValueError)):
        spawn.spawn_agent(
            spec, "hello", str(tmp_path), tmp_path / "ignored",
            owner_room_id=room_id,
        )

    assert not isolated_bus._room_dir(room_id).exists()
