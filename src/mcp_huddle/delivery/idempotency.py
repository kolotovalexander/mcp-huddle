"""24h idempotency cache for ``message_send``, keyed by caller-supplied key.

A repeated ``idempotency_key`` within the window must never cause a second
send. The naive "check a cache, then send, then write the cache" sequence
has a race: two concurrent callers with the same key can both miss the
cache check before either has written its result, and both go on to send
(different `msg_id`s, a real double turn against the recipient).

The fix is an atomic *reservation*, taken before either caller sends
anything: the first caller to create `<state_dir>/reservations/<sha256(key)>
.json` via `os.open(..., O_CREAT|O_EXCL)` (atomic even across processes, on
a local filesystem) owns the key and proceeds to send; every other caller
sees the reservation already exists and never sends. Once the owner
finishes, it overwrites the reservation with the final result (`"done"`),
which is what a same-key retry after completion returns verbatim.

States on disk: `{"status": "reserved", "msg_id", "pid", "ts"}` while the
owner is still sending, `{"status": "done", "msg_id", "ts", "result"}` once
it's finished. A `"reserved"` entry whose owning pid is confirmably dead
*and* older than `RESERVATION_STALE_SECONDS` is stale and may be taken over
(the owner crashed before finishing). A `"done"` entry expires after
`WINDOW_SECONDS` (24h), same as before.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import config as delivery_config

WINDOW_SECONDS = 24 * 60 * 60
# How long a "reserved" (not yet "done") entry must sit untouched, with its
# owning pid confirmably dead, before another caller may take it over. Short
# relative to WINDOW_SECONDS: a genuinely crashed sender should be retriable
# well within the same request's normal retry horizon, not a whole day.
RESERVATION_STALE_SECONDS = 5 * 60

_LOCK = threading.Lock()  # serializes threads within this process only;
# cross-process safety comes from O_CREAT|O_EXCL, not this lock.


@dataclass
class Reservation:
    status: str  # "reserved" | "in_progress" | "done"
    msg_id: str
    result: Optional[str] = None


def _reservations_dir() -> Path:
    d = delivery_config.state_dir() / "reservations"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _key_path(key: str) -> Path:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return _reservations_dir() / f"{digest}.json"


def _read_entry(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours -- do not treat as dead
    except (TypeError, ValueError, OSError):
        return True  # unknown -- err toward "still owns it", not takeover
    return True


def _write_reserved(path: Path, msg_id: str) -> None:
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    tmp.write_text(json.dumps({"status": "reserved", "msg_id": msg_id, "pid": os.getpid(), "ts": time.time()}))
    os.replace(tmp, path)


def _create_exclusive(path: Path, msg_id: str) -> bool:
    """Atomically create `path` iff it doesn't exist. Returns True iff *this*
    call created it (i.e. this caller now owns the reservation)."""
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps({"status": "reserved", "msg_id": msg_id, "pid": os.getpid(), "ts": time.time()}))
    return True


def reserve(key: str, msg_id: str) -> Reservation:
    """Attempt to atomically claim ``key`` for ``msg_id``. Must be called
    *before* any send is attempted.

    - Claimed successfully (no existing entry, or a stale/expired one taken
      over): ``Reservation("reserved", msg_id)`` -- the caller owns it and
      must call :func:`finish` once done.
    - A prior call already finished within the 24h window:
      ``Reservation("done", <that call's msg_id>, <that call's result>)`` --
      return the stored result verbatim, send nothing.
    - A prior call is still in flight (reserved, not stale):
      ``Reservation("in_progress", <that call's msg_id>)`` -- send nothing.
    """
    if not key:
        return Reservation("reserved", msg_id)

    path = _key_path(key)
    with _LOCK:
        if _create_exclusive(path, msg_id):
            return Reservation("reserved", msg_id)

        entry = _read_entry(path)
        if entry is None:
            # Corrupt/unreadable/vanished between the failed create and this
            # read -- treat like a stale reservation and try to reclaim.
            return _reclaim(path, msg_id)

        status = entry.get("status")
        ts = entry.get("ts", 0)
        if status == "done":
            if time.time() - ts > WINDOW_SECONDS:
                return _reclaim(path, msg_id)
            return Reservation("done", entry.get("msg_id", msg_id), entry.get("result"))

        # status == "reserved": still in flight, unless the owner is
        # confirmably dead and the reservation has sat long enough that we
        # trust it crashed rather than being merely slow.
        owner_pid = entry.get("pid")
        stale = (not _pid_alive(owner_pid)) and (time.time() - ts > RESERVATION_STALE_SECONDS)
        if stale:
            return _reclaim(path, msg_id)
        return Reservation("in_progress", entry.get("msg_id", msg_id))


def _reclaim(path: Path, msg_id: str) -> Reservation:
    """Best-effort takeover of a stale/expired/corrupt reservation file. If
    another process wins the recreate race in between, defer to whatever it
    left behind rather than clobbering it."""
    try:
        path.unlink()
    except OSError:
        pass
    if _create_exclusive(path, msg_id):
        return Reservation("reserved", msg_id)
    entry = _read_entry(path) or {}
    if entry.get("status") == "done":
        return Reservation("done", entry.get("msg_id", msg_id), entry.get("result"))
    return Reservation("in_progress", entry.get("msg_id", msg_id))


def finish(key: str, msg_id: str, result_json: str) -> None:
    """Mark ``key``'s reservation done, storing ``result_json`` so a
    same-key retry within the window gets it back verbatim. No-op if
    ``key`` is empty (nothing was reserved)."""
    if not key:
        return
    path = _key_path(key)
    with _LOCK:
        tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
        tmp.write_text(json.dumps({"status": "done", "msg_id": msg_id, "ts": time.time(), "result": result_json}))
        os.replace(tmp, path)
