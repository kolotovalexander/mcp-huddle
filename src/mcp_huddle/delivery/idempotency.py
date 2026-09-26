"""24h idempotency cache for ``message_send``, keyed by caller-supplied key.

A repeated ``idempotency_key`` within the window must never cause a second
send. The naive "check a cache, then send, then write the cache" sequence
has a race: two concurrent callers with the same key can both miss the
cache check before either has written its result, and both go on to send
(different `msg_id`s, a real double turn against the recipient).

The fix is a per-key, cross-process **lock** (``fcntl.flock`` on a stable
``<key>.json.lock`` file that is created once and never deleted) held for
the *entire* read-modify-write of the reservation state, combined with an
*atomic* state publication (write a tmp file in the same directory, then
``os.replace``). Unlike the earlier ``O_CREAT|O_EXCL``-only design, no
window exists between "the state file exists" and "the state file has its
JSON payload" that a second process could observe and misinterpret as
absent/corrupt -- every read and every write of ``<key>.json`` happens
while holding that key's lock.

States on disk: ``{"status": "reserved", "msg_id", "pid", "ts"}`` while the
owner is still sending; ``{"status": "done", "msg_id", "ts", "result"}``
once it's finished; ``{"status": "unknown", "msg_id", "ts"}`` if the owner
died before finishing. A ``"reserved"`` entry whose owning pid is
confirmably dead is moved straight to ``"unknown"`` -- **never** taken over
and retried automatically. Whether the dead owner delivered the message
before it died or crashed before ever attempting to send is
indistinguishable from the outside; resending on a guess could double-send
a message that already landed, and refusing forever could silently drop
one that never went out. Neither is acceptable to choose automatically, so
the outcome is surfaced as durably undetermined -- a caller that needs a
guaranteed resend must supply a new ``idempotency_key``. ``finish()`` only
ever writes ``"done"`` if the reservation still names its own ``msg_id``
(an owner check): if this key was reassigned out from under a caller
between its own ``reserve()`` and ``finish()``, its now-stale result must
never stomp whatever the new owner is doing. A ``"done"`` entry still
expires after ``WINDOW_SECONDS`` (24h) as before.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import config as delivery_config

WINDOW_SECONDS = 24 * 60 * 60


@dataclass
class Reservation:
    status: str  # "reserved" | "in_progress" | "done" | "unknown"
    msg_id: str
    result: Optional[str] = None


def _reservations_dir() -> Path:
    d = delivery_config.state_dir() / "reservations"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _key_path(key: str) -> Path:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return _reservations_dir() / f"{digest}.json"


def _lock_path(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


@contextlib.contextmanager
def _key_lock(path: Path):
    """Hold an exclusive, cross-process lock scoped to ``path``'s key for the
    duration of the ``with`` block. The lock file is created once (`O_CREAT`,
    never `O_EXCL`) and is never unlinked -- deleting a lock file while
    another process might still hold or be about to reopen it would let two
    processes end up locking different inodes for the "same" key, which
    defeats the whole point. `fcntl.flock` associates the lock with the open
    file description, so a fresh `os.open` per call is sufficient; it never
    depends on this being the same in-process object across calls."""
    lock_path = _lock_path(path)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


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


def _write_state_locked(path: Path, data: dict) -> None:
    """Atomically publish ``data`` as ``path``'s content. Must only be called
    while holding ``path``'s key lock (see :func:`_key_lock`) -- the lock is
    what makes this a true read-modify-write, not just an atomic write of a
    value some other process might already be racing to overwrite."""
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(data))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass  # already replaced onto `path`; nothing left to remove


def reserve(key: str, msg_id: str) -> Reservation:
    """Attempt to atomically claim ``key`` for ``msg_id``. Must be called
    *before* any send is attempted.

    - Claimed successfully (no existing entry, or an expired ``"done"``
      entry): ``Reservation("reserved", msg_id)`` -- the caller owns it and
      must call :func:`finish` once done.
    - A prior call already finished within the 24h window:
      ``Reservation("done", <that call's msg_id>, <that call's result>)`` --
      return the stored result verbatim, send nothing.
    - A prior call is still in flight (reserved, owner confirmably alive):
      ``Reservation("in_progress", <that call's msg_id>)`` -- send nothing.
    - A prior call's owner died before finishing: ``Reservation("unknown",
      <that call's msg_id>)`` -- send nothing, and this is never
      automatically retried; the caller needs a new ``idempotency_key`` to
      force a resend (see the module docstring for why).
    """
    if not key:
        return Reservation("reserved", msg_id)

    path = _key_path(key)
    with _key_lock(path):
        if not path.exists():
            _write_state_locked(path, {
                "status": "reserved", "msg_id": msg_id, "pid": os.getpid(), "ts": time.time(),
            })
            return Reservation("reserved", msg_id)

        entry = _read_entry(path)
        if entry is None:
            # The file exists but is empty/unparseable while we hold the
            # exclusive lock. Every writer in this module uses this same
            # locked, atomic-replace path, so this should never happen from
            # normal concurrency -- but an unreadable byte sequence is not
            # proof of "no owner" (external tampering, disk corruption,
            # truncation by something outside this module). Never delete or
            # re-reserve it: treat it as held by an unknown owner.
            return Reservation("in_progress", msg_id)

        status = entry.get("status")
        ts = entry.get("ts", 0)
        owner_msg_id = entry.get("msg_id", msg_id)

        if status == "done":
            if time.time() - ts > WINDOW_SECONDS:
                _write_state_locked(path, {
                    "status": "reserved", "msg_id": msg_id, "pid": os.getpid(), "ts": time.time(),
                })
                return Reservation("reserved", msg_id)
            return Reservation("done", owner_msg_id, entry.get("result"))

        if status == "unknown":
            # A previous owner died before we could learn whether it
            # delivered. No automatic retry ever happens for this outcome --
            # see the module docstring. The key stays "unknown" until a
            # caller supplies a different idempotency_key.
            return Reservation("unknown", owner_msg_id)

        # status == "reserved": in flight, unless the owner is confirmably
        # dead -- in which case we do NOT know whether it sent before dying,
        # so we move the key to "unknown" rather than granting a new
        # reservation (Codex review finding D: the old stale-takeover here
        # could repeat a message that was already delivered before the
        # owning process crashed).
        owner_pid = entry.get("pid")
        if not _pid_alive(owner_pid):
            _write_state_locked(path, {"status": "unknown", "msg_id": owner_msg_id, "ts": time.time()})
            return Reservation("unknown", owner_msg_id)
        return Reservation("in_progress", owner_msg_id)


def finish(key: str, msg_id: str, result_json: str) -> None:
    """Mark ``key``'s reservation done, storing ``result_json`` so a
    same-key retry within the window gets it back verbatim. No-op if
    ``key`` is empty (nothing was reserved).

    Owner-checked: only writes ``"done"`` if the on-disk entry still names
    this ``msg_id`` as the owner. If another caller has since taken this key
    to ``"unknown"`` (this owner was pronounced dead while it was actually
    still running) or reserved it anew, this call must not stomp that state
    with a result for a reservation nobody else recognizes as current.
    """
    if not key:
        return
    path = _key_path(key)
    with _key_lock(path):
        entry = _read_entry(path)
        if entry is not None and entry.get("msg_id") != msg_id:
            return
        _write_state_locked(path, {"status": "done", "msg_id": msg_id, "ts": time.time(), "result": result_json})
