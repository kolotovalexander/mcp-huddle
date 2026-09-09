"""Process-local ownership registry for spawned Huddle children.

Persisted PIDs are diagnostics, never authority to signal a process. Only an
exact ``Popen`` registered by this server instance may be inspected or
terminated. Polling/reaping and termination are serialized so a child cannot
be reaped and have its PID reused between the liveness check and SIGTERM.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Literal

ProcessState = Literal["alive", "exited", "unknown"]
TerminateResult = Literal["sent", "exited", "unknown", "denied"]


@dataclass
class _Record:
    proc: subprocess.Popen
    room_id: str
    handle: str
    terminate_requested: bool = False


_LOCK = threading.RLock()
_RECORDS: dict[tuple[str, str], _Record] = {}
_EXITED_HANDLES_MAX = 2048
_EXITED_HANDLES: OrderedDict[tuple[str, str], None] = OrderedDict()
_CLOSED_ROOMS_MAX = 4096
_CLOSED_ROOMS: OrderedDict[str, None] = OrderedDict()
_OWNER_PID = os.getpid()


def _reset_after_fork() -> None:
    """Drop inherited Popen authority and recreate locks in a fork child."""
    global _LOCK, _RECORDS, _EXITED_HANDLES, _CLOSED_ROOMS, _OWNER_PID
    _LOCK = threading.RLock()
    _RECORDS = {}
    _EXITED_HANDLES = OrderedDict()
    _CLOSED_ROOMS = OrderedDict()
    _OWNER_PID = os.getpid()


def _ensure_owner_process() -> None:
    """Defensive PID guard for runtimes that bypass register_at_fork hooks."""
    if os.getpid() != _OWNER_PID:
        _reset_after_fork()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork)


def new_handle() -> str:
    _ensure_owner_process()
    return uuid.uuid4().hex


def _remember_exited(key: tuple[str, str]) -> None:
    _EXITED_HANDLES[key] = None
    _EXITED_HANDLES.move_to_end(key)
    while len(_EXITED_HANDLES) > _EXITED_HANDLES_MAX:
        _EXITED_HANDLES.popitem(last=False)


def _remember_closed(room_id: str) -> None:
    _CLOSED_ROOMS[room_id] = None
    _CLOSED_ROOMS.move_to_end(room_id)
    while len(_CLOSED_ROOMS) > _CLOSED_ROOMS_MAX:
        _CLOSED_ROOMS.popitem(last=False)


def _key(room_id: str, handle: str) -> tuple[str, str]:
    return room_id, handle


def _terminate_locked(record: _Record) -> TerminateResult:
    if record.proc.poll() is not None:
        return "exited"
    if record.terminate_requested:
        return "sent"
    try:
        # Popen owns this exact child. The registry lock prevents its reaper
        # from waitpid/poll reaping it before this signal is sent.
        record.proc.terminate()
        record.terminate_requested = True
        return "sent"
    except ProcessLookupError:
        return "exited"
    except PermissionError:
        return "denied"
    except OSError:
        return "denied"


def register(
    proc: subprocess.Popen,
    room_id: str,
    handle: str | None = None,
) -> str:
    """Register one exact child and terminate it if its room already closed."""
    _ensure_owner_process()
    handle = handle or new_handle()
    key = _key(room_id, handle)
    with _LOCK:
        if key in _RECORDS:
            raise ValueError("duplicate child-process ownership handle")
        if key in _EXITED_HANDLES:
            raise ValueError("child-process ownership handle was already used")
        record = _Record(proc=proc, room_id=room_id, handle=handle)
        _RECORDS[key] = record
        if room_id and room_id in _CLOSED_ROOMS:
            _terminate_locked(record)
    return handle


def state(room_id: str, handle: str | None) -> ProcessState:
    """Return exact current-instance state; absent ownership is ``unknown``."""
    _ensure_owner_process()
    if not handle:
        return "unknown"
    with _LOCK:
        record = _RECORDS.get(_key(room_id, handle))
        if record is None:
            return "exited" if _key(room_id, handle) in _EXITED_HANDLES else "unknown"
        return "exited" if record.proc.poll() is not None else "alive"


def state_for_pid(room_id: str, pid: int | None) -> ProcessState:
    """Diagnostic lookup for initial spawns that predate persisted handles."""
    _ensure_owner_process()
    if not pid:
        return "unknown"
    with _LOCK:
        matches = [record for record in _RECORDS.values()
                   if record.room_id == room_id and record.proc.pid == pid]
        if len(matches) != 1:
            return "unknown"
        return "exited" if matches[0].proc.poll() is not None else "alive"


def terminate(room_id: str, handle: str | None) -> TerminateResult:
    """Terminate only a child owned by this instance and exact room/handle."""
    _ensure_owner_process()
    if not handle:
        return "unknown"
    with _LOCK:
        record = _RECORDS.get(_key(room_id, handle))
        if record is None:
            return "exited" if _key(room_id, handle) in _EXITED_HANDLES else "unknown"
        return _terminate_locked(record)


def close_room(room_id: str) -> dict[str, int]:
    """Tombstone a room and terminate all of its current-instance children.

    Recent tombstones cover close-vs-register races in this registry. The
    spawn layer also synchronously checks persisted room state immediately
    after every exact registration, so bounded tombstone retention cannot let
    an old terminal/missing room keep a production child alive.
    """
    _ensure_owner_process()
    counts = {"sent": 0, "exited": 0, "denied": 0}
    with _LOCK:
        _remember_closed(room_id)
        records = [record for record in _RECORDS.values()
                   if record.room_id == room_id]
        for record in records:
            result = _terminate_locked(record)
            counts[result] += 1
    return counts


def owned_room_ids() -> set[str]:
    """Return rooms with exact children owned by this server instance.

    The snapshot contains no PIDs and grants no signalling authority by
    itself. Callers must still use :func:`close_room`, which operates on the
    registered ``Popen`` objects while holding the registry lock.
    """
    _ensure_owner_process()
    with _LOCK:
        return {record.room_id for record in _RECORDS.values()
                if record.room_id}


def wait(room_id: str, handle: str, poll_interval: float = 0.05) -> int | None:
    """Reap an owned child. Exactly one spawn-managed waiter calls this."""
    _ensure_owner_process()
    key = _key(room_id, handle)
    while True:
        with _LOCK:
            record = _RECORDS.get(key)
            if record is None:
                return None
            returncode = record.proc.poll()
            if returncode is not None:
                del _RECORDS[key]
                _remember_exited(key)
                return returncode
        time.sleep(poll_interval)


def _reset_for_tests() -> None:
    """Forget ownership without signalling. Tests must own their fake children."""
    _ensure_owner_process()
    with _LOCK:
        _RECORDS.clear()
        _EXITED_HANDLES.clear()
        _CLOSED_ROOMS.clear()
