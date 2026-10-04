"""Behavior tests for the opt-in Jev result-judging hook.

Scoped to jev_judge.py / server.py's message_post hook and the concrete
risks named across two independent reviews: version dedup, unavailable-Jev
continuation, no broadcast on an uncertain verdict, dedup collisions across
workers, accepted/outcome not fabricated from a bare score, field-length
truncation not destroying meaning, and the confirmed event-loop block from
running Jev synchronously inside a sync @mcp.tool() handler.
"""

import threading
import time
import types

from mcp_huddle import bus, jev_judge, server


_BASE_META = {
    "task": "Do X",
    "acceptance_criteria": "X works",
    "result": "Done X",
    "evidence": "tests pass",
}


def _stub_client(choice: str, calls: list, decision_calls: list = None, outcome_calls: list = None):
    class StubClient:
        def __init__(self):
            pass

        def decide(self, request):
            calls.append(request)
            if choice == "ready":
                probs = {"ready": 0.9, "revise": 0.05, "insufficient": 0.05}
            else:
                probs = {"ready": 0.1, "revise": 0.1, "insufficient": 0.1}
                probs[choice] = 0.8
            return {
                "status": "ok",
                "answers": {
                    "verdict": {
                        "type": "choice", "choice": choice,
                        "probabilities": probs, "confidence": 0.8,
                    }
                },
                "model": "jev-test", "latency_ms": 5, "cost_usd": 0.001,
            }

        def record_consumer_decision(self, **kwargs):
            if decision_calls is not None:
                decision_calls.append(kwargs)

        def reconcile_outcome(self, **kwargs):
            if outcome_calls is not None:
                outcome_calls.append(kwargs)

    return types.SimpleNamespace(JevClient=StubClient)


def _fallback_client(calls: list):
    class FallbackClient:
        def __init__(self):
            pass

        def decide(self, request):
            calls.append(request)
            return {"status": "fallback", "reason": "budget_exhausted", "answers": {}}

        def record_consumer_decision(self, **kwargs):
            pass

        def reconcile_outcome(self, **kwargs):
            pass

    return types.SimpleNamespace(JevClient=FallbackClient)


def test_evaluate_result_dedups_by_version_and_skips_when_jev_unavailable(monkeypatch):
    room_id = bus.create_room("JevJudge", "Claude", 0, "/tmp/project", "session-1")
    judge_meta = {**_BASE_META, "version": "v1"}

    monkeypatch.setattr(jev_judge, "_load_jev_module", lambda: None)
    assert jev_judge.evaluate_result(room_id, "Worker", None, judge_meta) is None

    calls: list = []
    monkeypatch.setattr(jev_judge, "_load_jev_module", lambda: _stub_client("ready", calls))
    verdict = jev_judge.evaluate_result(room_id, "Worker", None, judge_meta)
    assert verdict is not None and verdict.choice == "ready"
    assert len(calls) == 1

    again = jev_judge.evaluate_result(room_id, "Worker", None, judge_meta)
    assert again is None
    assert len(calls) == 1

    verdict2 = jev_judge.evaluate_result(room_id, "Worker", None, {**judge_meta, "version": "v2"})
    assert verdict2 is not None
    assert len(calls) == 2


def test_fallback_consumes_the_claim_and_is_not_auto_retried(monkeypatch):
    room_id = bus.create_room("JevFallback", "Claude", 0, "/tmp/project", "session-1")
    judge_meta = {**_BASE_META, "version": "v1"}

    calls: list = []
    monkeypatch.setattr(jev_judge, "_load_jev_module", lambda: _fallback_client(calls))
    out = jev_judge.evaluate_result(room_id, "Worker", None, judge_meta)
    assert out is None
    assert len(calls) == 1

    out_again = jev_judge.evaluate_result(room_id, "Worker", None, judge_meta)
    assert out_again is None
    assert len(calls) == 1

    entry = jev_judge._read_claim(room_id, jev_judge._claim_key("Worker", "root"))
    assert entry is not None and entry["status"] == "fallback"

    monkeypatch.setattr(jev_judge, "_load_jev_module", lambda: _stub_client("ready", calls))
    out_v2 = jev_judge.evaluate_result(room_id, "Worker", None, {**judge_meta, "version": "v2"})
    assert out_v2 is not None
    assert len(calls) == 2


def test_dedup_is_scoped_per_worker_not_shared_across_workers(monkeypatch):
    room_id = bus.create_room("JevCrossWorker", "Claude", 0, "/tmp/project", "session-1")
    judge_meta = {**_BASE_META, "version": "v1"}

    calls: list = []
    monkeypatch.setattr(jev_judge, "_load_jev_module", lambda: _stub_client("ready", calls))
    verdict_a = jev_judge.evaluate_result(room_id, "WorkerA", None, judge_meta)
    verdict_b = jev_judge.evaluate_result(room_id, "WorkerB", None, judge_meta)
    assert verdict_a is not None
    assert verdict_b is not None, "second worker's result was wrongly dropped as a duplicate"
    assert len(calls) == 2


def test_insufficient_routes_to_designated_reviewer_never_broadcasts(monkeypatch):
    room_with_reviewer = bus.create_room("JevInsufficientA", "Coordinator", 0, "/tmp/project", "s1")
    bus.invite_agent(room_with_reviewer, "Antigravity")
    calls: list = []
    monkeypatch.setattr(jev_judge, "_load_jev_module", lambda: _stub_client("insufficient", calls))
    verdict = jev_judge.evaluate_result(room_with_reviewer, "Worker", None, {**_BASE_META, "version": "v1"})
    assert verdict.route_to == "Antigravity"
    assert verdict.route_body is not None

    room_without_reviewer = bus.create_room("JevInsufficientB", "Coordinator", 0, "/tmp/project", "s2")
    calls2: list = []
    monkeypatch.setattr(jev_judge, "_load_jev_module", lambda: _stub_client("insufficient", calls2))
    verdict2 = jev_judge.evaluate_result(room_without_reviewer, "Worker", None, {**_BASE_META, "version": "v1"})
    assert verdict2.route_to is None
    assert verdict2.route_body is None
    assert verdict2.comment_to == "Coordinator"

    bus.invite_agent(room_without_reviewer, "Worker")
    req_id = server.message_post(room_without_reviewer, "Coordinator", "Do Y", "request", to="Worker")
    monkeypatch.setattr(jev_judge, "_load_jev_module", lambda: _stub_client("insufficient", calls2))
    server.message_post(
        room_without_reviewer, "Worker", "Completed Y", "result", to="Coordinator", reply_to=req_id,
        meta={"judge": {**_BASE_META, "task": "Do Y", "version": "v1"}},
    )
    server._drain_judge_threads_for_tests()
    posted = bus._load_messages(room_without_reviewer)
    judge_messages = [m for m in posted if m.get("agent") == "jev-judge"]
    assert all(m["kind"] != "request" for m in judge_messages), judge_messages
    assert any(m["kind"] == "comment" and m.get("to") == "Coordinator" for m in judge_messages), judge_messages


def test_message_post_exposes_verdict_and_routes_revise_to_same_worker(monkeypatch):
    room_id = bus.create_room("JevRevise", "Claude", 0, "/tmp/project", "session-2")
    bus.invite_agent(room_id, "Worker")
    req_id = server.message_post(room_id, "Claude", "Do Y", "request", to="Worker")

    calls: list = []
    monkeypatch.setattr(jev_judge, "_load_jev_module", lambda: _stub_client("revise", calls))

    server.message_post(
        room_id, "Worker", "Completed Y", "result", to="Claude", reply_to=req_id,
        meta={"judge": {**_BASE_META, "task": "Do Y", "version": "v1"}},
    )
    server._drain_judge_threads_for_tests()

    posted = bus._load_messages(room_id)
    judge_messages = [m for m in posted if m.get("agent") == "jev-judge"]
    assert any(m["kind"] == "comment" for m in judge_messages), judge_messages
    assert any(
        m["kind"] == "request" and m.get("to") == "Worker" for m in judge_messages
    ), judge_messages


def test_accepted_only_recorded_for_revise_or_real_feedback_never_from_bare_score(monkeypatch):
    """A Jev score alone is neither a consumer action nor an observed
    outcome. ready/insufficient must not fabricate acceptance; revise is the
    one case that records an action because Huddle really does take it
    (routes back to the worker); a real outcome only comes from
    record_feedback (message-metadata reported), never a later score."""
    room_id = bus.create_room("JevAccept", "Claude", 0, "/tmp/project", "s1")
    decision_calls: list = []
    outcome_calls: list = []
    calls: list = []

    monkeypatch.setattr(
        jev_judge, "_load_jev_module",
        lambda: _stub_client("ready", calls, decision_calls, outcome_calls),
    )
    verdict = jev_judge.evaluate_result(room_id, "Worker", None, {**_BASE_META, "version": "v1"})
    assert verdict.choice == "ready"
    assert decision_calls == [], "a bare 'ready' score must not record consumer acceptance"
    assert outcome_calls == [], "a score is not an observed outcome"

    monkeypatch.setattr(
        jev_judge, "_load_jev_module",
        lambda: _stub_client("revise", calls, decision_calls, outcome_calls),
    )
    verdict2 = jev_judge.evaluate_result(room_id, "Worker", None, {**_BASE_META, "version": "v2"})
    assert verdict2.choice == "revise"
    assert len(decision_calls) == 1 and decision_calls[0]["accepted"] is False, (
        "the directed revise request IS a real action Huddle takes, and should record it")
    assert outcome_calls == [], "revise is an action, not an observed outcome either"

    # A real consumer (human/agent) reports what actually happened, via
    # ordinary message metadata (not a private Python-only call).
    ok = jev_judge.record_feedback(room_id, "Worker", None, "accepted", "success")
    assert ok is True
    assert len(decision_calls) == 2 and decision_calls[-1]["accepted"] is True
    assert len(outcome_calls) == 1 and outcome_calls[-1]["outcome"] == "success"


def test_judge_feedback_meta_reaches_jev_judge_via_message_post(monkeypatch):
    room_id = bus.create_room("JevFeedback", "Claude", 0, "/tmp/project", "s1")
    decision_calls: list = []
    outcome_calls: list = []
    calls: list = []
    monkeypatch.setattr(
        jev_judge, "_load_jev_module",
        lambda: _stub_client("ready", calls, decision_calls, outcome_calls),
    )
    jev_judge.evaluate_result(room_id, "Worker", None, {**_BASE_META, "version": "v1"})
    assert decision_calls == []

    server.message_post(
        room_id, "Coordinator", "merged it", "comment",
        meta={"judge_feedback": {"worker": "Worker", "action": "accepted", "outcome": "success"}},
    )
    assert len(decision_calls) == 1 and decision_calls[0]["accepted"] is True
    assert len(outcome_calls) == 1 and outcome_calls[0]["outcome"] == "success"


def test_oversized_field_is_explicit_local_insufficient_not_truncated(monkeypatch):
    room_id = bus.create_room("JevOversize", "Claude", 0, "/tmp/project", "s1")
    calls: list = []
    monkeypatch.setattr(jev_judge, "_load_jev_module", lambda: _stub_client("ready", calls))
    huge_evidence = "x" * 2000
    verdict = jev_judge.evaluate_result(
        room_id, "Worker", None, {**_BASE_META, "evidence": huge_evidence, "version": "v1"},
    )
    assert verdict is not None
    assert verdict.choice == "insufficient"
    assert "LOCAL-INSUFFICIENT" in verdict.comment_body
    assert len(calls) == 0, "oversized input must not reach Jev at all"
    assert verdict.route_to == "Worker"

    room_id2 = bus.create_room("JevNormalLength", "Claude", 0, "/tmp/project", "s2")
    calls2: list = []
    monkeypatch.setattr(jev_judge, "_load_jev_module", lambda: _stub_client("ready", calls2))
    longish_evidence = "pytest tests/test_x.py -q -> 12 passed, 0 failed; coverage 87%; ran in 4.2s on CI"
    jev_judge.evaluate_result(
        room_id2, "Worker", None, {**_BASE_META, "evidence": longish_evidence, "version": "v1"},
    )
    assert len(calls2) == 1
    sent_description = calls2[0]["fixtures"][0]["description"]
    assert "87%" in sent_description and "12 passed" in sent_description, sent_description


def test_judge_runs_in_background_and_does_not_block_message_post(monkeypatch):
    room_id = bus.create_room("JevAsync", "Claude", 0, "/tmp/project", "s1")
    bus.invite_agent(room_id, "Worker")
    req_id = server.message_post(room_id, "Claude", "Do Z", "request", to="Worker")

    release = threading.Event()
    calls: list = []

    class SlowClient:
        def __init__(self):
            pass

        def decide(self, request):
            release.wait(timeout=5)
            calls.append(request)
            return {
                "status": "ok",
                "answers": {"verdict": {
                    "type": "choice", "choice": "ready",
                    "probabilities": {"ready": 0.9, "revise": 0.05, "insufficient": 0.05},
                    "confidence": 0.9,
                }},
                "model": "jev-test", "latency_ms": 5, "cost_usd": 0.001,
            }

        def record_consumer_decision(self, **kwargs):
            pass

        def reconcile_outcome(self, **kwargs):
            pass

    monkeypatch.setattr(jev_judge, "_load_jev_module", lambda: types.SimpleNamespace(JevClient=SlowClient))

    started = time.monotonic()
    server.message_post(
        room_id, "Worker", "Completed Z", "result", to="Claude", reply_to=req_id,
        meta={"judge": {**_BASE_META, "task": "Do Z", "version": "v1"}},
    )
    elapsed = time.monotonic() - started
    assert elapsed < 1.0, "message_post must return before the Jev call finishes"
    assert len(calls) == 0, "Jev must not have been called synchronously on the caller's thread"

    release.set()
    server._drain_judge_threads_for_tests()
    assert len(calls) == 1
