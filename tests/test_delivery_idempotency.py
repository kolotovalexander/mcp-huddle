"""Direct unit tests for the idempotency reservation contract: cross-process
locked reservation before any send, `done` reuse, `in_progress` for a
concurrent in-flight caller, and dead-owner handling (Codex review defects
#3 and #4 from the second independent review). Hermetic: everything lives
under MCP_HUDDLE_HOME (a tmp dir per test)."""

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


def test_reservation_from_live_owner_never_taken_over_even_if_old():
    path = idempotency._key_path("k6")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "status": "reserved", "msg_id": "live-owner-msg", "pid": os.getpid(),
        "ts": time.time() - 999999,
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


def test_corrupt_reservation_file_is_never_reclaimed():
    """Codex review finding C (generalized): an unparseable/empty state file
    must never be inferred as "no owner" and silently deleted/re-reserved --
    it must be treated as held by an unknown owner instead."""
    path = idempotency._key_path("k8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json{{{")
    res = idempotency.reserve("k8", "msg-8")
    assert res.status == "in_progress"
    assert path.read_text() == "not json{{{"  # left untouched, never deleted


# ── Codex review finding D: dead owner -> "unknown", never auto-resent ─────

def test_dead_owner_reservation_becomes_unknown_not_retried():
    path = idempotency._key_path("k9")
    path.parent.mkdir(parents=True, exist_ok=True)
    dead_pid = _dead_pid()
    path.write_text(json.dumps({
        "status": "reserved", "msg_id": "abandoned-msg", "pid": dead_pid, "ts": time.time(),
    }))
    res = idempotency.reserve("k9", "new-msg")
    assert res.status == "unknown"
    assert res.msg_id == "abandoned-msg"  # the original id, never replaced
    # A second attempt against the same key must not resend either -- the
    # key stays "unknown" forever (until a caller uses a different key).
    res2 = idempotency.reserve("k9", "another-new-msg")
    assert res2.status == "unknown"
    assert res2.msg_id == "abandoned-msg"


def test_finish_never_clobbers_a_reservation_it_no_longer_owns():
    """Owner check: a belated finish() for an old msg_id must never stomp a
    DIFFERENT, currently-active reservation for the same key -- e.g. after
    the old entry expired (24h `done` window) and a new caller has since
    reserved the key under a new msg_id."""
    path = idempotency._key_path("k10")
    idempotency.reserve("k10", "owner-msg")
    idempotency.finish("k10", "owner-msg", json.dumps({"delivered": True}))
    entry = json.loads(path.read_text())
    entry["ts"] = time.time() - idempotency.WINDOW_SECONDS - 10  # force expiry
    path.write_text(json.dumps(entry))

    # A new caller reserves the now-expired key under a brand new msg_id.
    res = idempotency.reserve("k10", "new-owner-msg")
    assert res.status == "reserved"
    assert res.msg_id == "new-owner-msg"

    # The original (long-finished, now-irrelevant) owner belatedly calls
    # finish() again with its OLD msg_id -- must not touch the new owner's
    # active reservation.
    idempotency.finish("k10", "owner-msg", json.dumps({"delivered": True, "stale": True}))
    entry = json.loads(path.read_text())
    assert entry["status"] == "reserved"
    assert entry["msg_id"] == "new-owner-msg"


def test_finish_owner_check_allows_the_real_current_owner():
    idempotency.reserve("k11", "msg-11")
    idempotency.finish("k11", "msg-11", json.dumps({"ok": True}))
    entry = json.loads(idempotency._key_path("k11").read_text())
    assert entry["status"] == "done"
    assert entry["msg_id"] == "msg-11"


# ── Codex review finding C: real cross-process reservation race ────────────

def test_two_real_processes_racing_initial_reservation_only_one_wins(tmp_path):
    """Regression for Codex review finding C: two independent OS processes
    racing to reserve the same brand-new key must never both end up
    "reserved" -- exactly one must win and the other must see its
    reservation (never a second, independent one)."""
    root = tmp_path / "huddle"
    root.mkdir()
    go = tmp_path / "go"
    exitf = tmp_path / "exit"
    out_a = tmp_path / "out-a.json"
    out_b = tmp_path / "out-b.json"

    child_code = """
import json, os, sys, time
sys.path.insert(0, {repo!r})
os.environ["MCP_HUDDLE_HOME"] = {home!r}
from mcp_huddle.delivery import idempotency
go = {go!r}
exitf = {exitf!r}
out = {out!r}
while not os.path.exists(go):
    time.sleep(0.005)
r = idempotency.reserve("interprocess-race", {msg_id!r})
with open(out, "w") as f:
    json.dump({{"status": r.status, "msg_id": r.msg_id}}, f)
while not os.path.exists(exitf):
    time.sleep(0.01)
"""
    repo_src = str((__import__("pathlib").Path(__file__).resolve().parent.parent / "src"))

    procs = []
    for out_path, msg_id in ((out_a, "owner-A"), (out_b, "owner-B")):
        code = child_code.format(repo=repo_src, home=str(root), go=str(go), exitf=str(exitf),
                                  out=str(out_path), msg_id=msg_id)
        procs.append(subprocess.Popen([sys.executable, "-c", code],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))

    try:
        # Give both children a moment to reach their busy-wait on `go`.
        time.sleep(0.2)
        go.write_text("yes")

        deadline = time.monotonic() + 10
        while (not out_a.exists() or not out_b.exists()) and time.monotonic() < deadline:
            for p in procs:
                if p.poll() is not None and p.returncode != 0:
                    _, stderr = p.communicate(timeout=1)
                    raise AssertionError(f"child failed: {stderr}")
            time.sleep(0.02)

        assert out_a.exists() and out_b.exists(), "both children must have reserved/observed"
        exitf.write_text("yes")
        for p in procs:
            p.wait(timeout=10)

        result_a = json.loads(out_a.read_text())
        result_b = json.loads(out_b.read_text())
    finally:
        exitf.write_text("yes")
        for p in procs:
            if p.poll() is None:
                p.kill()
                p.wait()

    statuses = {result_a["status"], result_b["status"]}
    assert statuses == {"reserved", "in_progress"}, (result_a, result_b)
    winner = result_a if result_a["status"] == "reserved" else result_b
    loser = result_b if winner is result_a else result_a
    assert loser["msg_id"] == winner["msg_id"]  # the loser reports the winner's id, never its own
