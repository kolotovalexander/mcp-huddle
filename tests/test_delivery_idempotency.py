"""Direct unit tests for the idempotency reservation contract (Codex review
defect #5): atomic cross-process reservation before any send, `done` reuse,
`in_progress` for a concurrent in-flight caller, and stale-reservation
takeover. Hermetic: everything lives under MCP_HUDDLE_HOME (a tmp dir per
test)."""

import json
import os
import subprocess
import sys
import time

import pytest

from mcp_huddle.delivery import idempotency


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path / "huddle"))
    return tmp_path


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_reserve_with_no_existing_entry_succeeds():
    res = idempotency.reserve("k1", "msg-1")
    assert res.status == "reserved"
    assert res.msg_id == "msg-1"


def test_reserve_again_while_in_flight_returns_in_progress_with_original_msg_id():
    first = idempotency.reserve("k2", "msg-first")
    assert first.status == "reserved"
    second = idempotency.reserve("k2", "msg-second")
    assert second.status == "in_progress"
    assert second.msg_id == "msg-first"  # never the second caller's own id


def test_finish_then_reserve_returns_done_with_stored_result():
    idempotency.reserve("k3", "msg-3")
    idempotency.finish("k3", "msg-3", json.dumps({"delivered": True, "msg_id": "msg-3"}))
    res = idempotency.reserve("k3", "msg-3-retry")
    assert res.status == "done"
    assert res.msg_id == "msg-3"
    assert json.loads(res.result)["msg_id"] == "msg-3"


def test_empty_key_always_reserves_without_touching_disk():
    res1 = idempotency.reserve("", "a")
    res2 = idempotency.reserve("", "b")
    assert res1.status == "reserved"
    assert res2.status == "reserved"
    assert res1.msg_id == "a" and res2.msg_id == "b"  # independent, no cross-talk
    # finish() with an empty key must also be a no-op, not raise.
    idempotency.finish("", "a", "{}")


def test_stale_reservation_from_dead_owner_is_taken_over():
    path = idempotency._key_path("k4")
    path.parent.mkdir(parents=True, exist_ok=True)
    dead_pid = _dead_pid()
    path.write_text(json.dumps({
        "status": "reserved", "msg_id": "old-owner-msg", "pid": dead_pid,
        "ts": time.time() - idempotency.RESERVATION_STALE_SECONDS - 30,
    }))
    res = idempotency.reserve("k4", "new-msg")
    assert res.status == "reserved"
    assert res.msg_id == "new-msg"


def test_reservation_from_dead_owner_but_not_yet_stale_stays_in_progress():
    """A dead owner alone isn't enough -- the reservation must also be older
    than the stale window before anyone takes it over (otherwise a merely
    slow-but-fresh reservation with a dead-looking pid field could be
    hijacked too eagerly)."""
    path = idempotency._key_path("k5")
    path.parent.mkdir(parents=True, exist_ok=True)
    dead_pid = _dead_pid()
    path.write_text(json.dumps({
        "status": "reserved", "msg_id": "recent-msg", "pid": dead_pid, "ts": time.time(),
    }))
    res = idempotency.reserve("k5", "new-msg")
    assert res.status == "in_progress"
    assert res.msg_id == "recent-msg"


def test_reservation_from_live_owner_never_taken_over_even_if_old():
    path = idempotency._key_path("k6")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "status": "reserved", "msg_id": "live-owner-msg", "pid": os.getpid(),
        "ts": time.time() - idempotency.RESERVATION_STALE_SECONDS - 30,
    }))
    res = idempotency.reserve("k6", "new-msg")
    assert res.status == "in_progress"
    assert res.msg_id == "live-owner-msg"


def test_done_entry_expires_after_window(monkeypatch):
    idempotency.reserve("k7", "msg-7")
    idempotency.finish("k7", "msg-7", json.dumps({"ok": True}))
    path = idempotency._key_path("k7")
    entry = json.loads(path.read_text())
    entry["ts"] = time.time() - idempotency.WINDOW_SECONDS - 10
    path.write_text(json.dumps(entry))
    res = idempotency.reserve("k7", "msg-7-new")
    assert res.status == "reserved"
    assert res.msg_id == "msg-7-new"


def test_corrupt_reservation_file_is_reclaimed():
    path = idempotency._key_path("k8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json{{{")
    res = idempotency.reserve("k8", "msg-8")
    assert res.status == "reserved"
    assert res.msg_id == "msg-8"
