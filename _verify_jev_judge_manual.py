"""Author self-check only (pytest is unavailable in this sandbox — see
receipt for the exact authorized command). Re-derives tests/test_jev_judge.py
by direct call so an obviously broken diff isn't handed off."""
import importlib
import os
import sys
import tempfile
import types

sys.path.insert(0, "src")

tmp_home = tempfile.mkdtemp(prefix="huddle-jev-judge-")
os.environ["MCP_HUDDLE_HOME"] = tmp_home

from mcp_huddle import bus
importlib.reload(bus)
from mcp_huddle import jev_judge, server

BASE_META = dict(task="Do X", acceptance_criteria="X works", result="Done X", evidence="tests pass")


def stub_client(choice, calls):
    class StubClient(object):
        def __init__(self):
            pass

        def decide(self, request):
            calls.append(request)
            if choice == "ready":
                probs = dict(ready=0.9, revise=0.05, insufficient=0.05)
            else:
                probs = dict(ready=0.1, revise=0.1, insufficient=0.1)
                probs[choice] = 0.8
            answer = dict(type="choice", choice=choice, probabilities=probs, confidence=0.8)
            return dict(status="ok", answers=dict(verdict=answer),
                        model="jev-test", latency_ms=5, cost_usd=0.001)

        def record_consumer_decision(self, **kwargs):
            pass

        def reconcile_outcome(self, **kwargs):
            pass

    ns = types.SimpleNamespace()
    ns.JevClient = StubClient
    return ns


def fallback_client(calls):
    class FallbackClient(object):
        def __init__(self):
            pass

        def decide(self, request):
            calls.append(request)
            return dict(status="fallback", reason="budget_exhausted", answers={})

        def record_consumer_decision(self, **kwargs):
            pass

        def reconcile_outcome(self, **kwargs):
            pass

    ns = types.SimpleNamespace()
    ns.JevClient = FallbackClient
    return ns


def test_dedup_and_unavailable():
    room_id = bus.create_room("JevJudge", "Claude", 0, "/tmp/project", "session-1")
    meta = dict(BASE_META, version="v1")

    jev_judge._load_jev_module = lambda: None
    assert jev_judge.evaluate_result(room_id, "Worker", None, meta) is None

    calls = []
    jev_judge._load_jev_module = lambda: stub_client("ready", calls)
    verdict = jev_judge.evaluate_result(room_id, "Worker", None, meta)
    assert verdict is not None and verdict.choice == "ready"
    assert len(calls) == 1

    again = jev_judge.evaluate_result(room_id, "Worker", None, meta)
    assert again is None
    assert len(calls) == 1

    meta_v2 = dict(meta, version="v2")
    verdict2 = jev_judge.evaluate_result(room_id, "Worker", None, meta_v2)
    assert verdict2 is not None
    assert len(calls) == 2
    print("test_dedup_and_unavailable: OK")


def test_fallback_terminal_not_retried():
    room_id = bus.create_room("JevFallback", "Claude", 0, "/tmp/project", "session-1")
    meta = dict(BASE_META, version="v1")
    calls = []
    jev_judge._load_jev_module = lambda: fallback_client(calls)
    out = jev_judge.evaluate_result(room_id, "Worker", None, meta)
    assert out is None
    assert len(calls) == 1
    out_again = jev_judge.evaluate_result(room_id, "Worker", None, meta)
    assert out_again is None
    assert len(calls) == 1, "fallback must not be auto-retried"
    entry = jev_judge._read_claim(room_id, jev_judge._claim_key("Worker", "root"))
    assert entry is not None and entry["status"] == "fallback", entry
    jev_judge._load_jev_module = lambda: stub_client("ready", calls)
    out_v2 = jev_judge.evaluate_result(room_id, "Worker", None, dict(meta, version="v2"))
    assert out_v2 is not None
    assert len(calls) == 2
    print("test_fallback_terminal_not_retried: OK")


def test_dedup_scoped_per_worker():
    room_id = bus.create_room("JevCrossWorker", "Claude", 0, "/tmp/project", "session-1")
    meta = dict(BASE_META, version="v1")
    calls = []
    jev_judge._load_jev_module = lambda: stub_client("ready", calls)
    va = jev_judge.evaluate_result(room_id, "WorkerA", None, meta)
    vb = jev_judge.evaluate_result(room_id, "WorkerB", None, meta)
    assert va is not None and vb is not None, "cross-worker dedup collision"
    assert len(calls) == 2
    print("test_dedup_scoped_per_worker: OK")


def test_insufficient_never_broadcasts():
    room_with = bus.create_room("JevInsufficientA", "Coordinator", 0, "/tmp/project", "s1")
    bus.invite_agent(room_with, "Antigravity")
    calls = []
    jev_judge._load_jev_module = lambda: stub_client("insufficient", calls)
    v = jev_judge.evaluate_result(room_with, "Worker", None, dict(BASE_META, version="v1"))
    assert v.route_to == "Antigravity"
    assert v.route_body is not None

    room_without = bus.create_room("JevInsufficientB", "Coordinator", 0, "/tmp/project", "s2")
    calls2 = []
    jev_judge._load_jev_module = lambda: stub_client("insufficient", calls2)
    v2 = jev_judge.evaluate_result(room_without, "Worker", None, dict(BASE_META, version="v1"))
    assert v2.route_to is None and v2.route_body is None
    assert v2.comment_to == "Coordinator"

    bus.invite_agent(room_without, "Worker")
    req_id = server.message_post(room_without, "Coordinator", "Do Y", "request", to="Worker")
    jev_judge._load_jev_module = lambda: stub_client("insufficient", calls2)
    server.message_post(
        room_without, "Worker", "Completed Y", "result", to="Coordinator", reply_to=req_id,
        meta=dict(judge=dict(BASE_META, task="Do Y", version="v1")),
    )
    posted = bus._load_messages(room_without)
    judge_messages = [m for m in posted if m.get("agent") == "jev-judge"]
    assert all(m["kind"] != "request" for m in judge_messages), judge_messages
    assert any(m["kind"] == "comment" and m.get("to") == "Coordinator" for m in judge_messages), judge_messages
    print("test_insufficient_never_broadcasts: OK")


def test_message_post_revise_routes_to_worker():
    room_id = bus.create_room("JevRevise", "Claude", 0, "/tmp/project", "session-2")
    bus.invite_agent(room_id, "Worker")
    req_id = server.message_post(room_id, "Claude", "Do Y", "request", to="Worker")
    calls = []
    jev_judge._load_jev_module = lambda: stub_client("revise", calls)
    server.message_post(
        room_id, "Worker", "Completed Y", "result", to="Claude", reply_to=req_id,
        meta=dict(judge=dict(BASE_META, task="Do Y", version="v1")),
    )
    posted = bus._load_messages(room_id)
    judge_messages = [m for m in posted if m.get("agent") == "jev-judge"]
    assert any(m["kind"] == "comment" for m in judge_messages), judge_messages
    assert any(m["kind"] == "request" and m.get("to") == "Worker" for m in judge_messages), judge_messages
    print("test_message_post_revise_routes_to_worker: OK")


test_dedup_and_unavailable()
test_fallback_terminal_not_retried()
test_dedup_scoped_per_worker()
test_insufficient_never_broadcasts()
test_message_post_revise_routes_to_worker()
print("ALL OK")
