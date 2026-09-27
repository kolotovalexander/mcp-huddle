from mcp_huddle.swarm_replacement import plan_replacement

MEMBER = {"member_id": "m1", "responsibility": "api", "harness": "claude",
          "write_rights": {"paths": ["src/api"]}}
A1 = {"route": "claude/opus", "harness": "claude", "model": "opus", "provider": "anthropic"}


def cand(route, harness, provider, **kw):
    base = {"route": route, "harness": harness, "model": route.split("/")[1],
            "provider": provider, "available": True, "quality": 0.8,
            "reliability": 0.8, "cost": 0.5, "can_limit_writes": True}
    base.update(kw)
    return base


def test_quota_falls_back_to_other_harness_keeping_rights():
    cands = [cand("claude/sonnet", "claude", "anthropic"), cand("codex/gpt", "codex", "openai")]
    r = plan_replacement({"text": "429 rate_limit"}, [A1], MEMBER, cands, child_stopped=True)
    assert r["action"] == "replace" and r["route"] == "codex/gpt"
    assert r["member"]["write_rights"] == MEMBER["write_rights"]
    assert r["member"]["member_id"] == "m1"


def test_permission_wait_needs_user_and_unproven_stop_blocks():
    r = plan_replacement({"waiting_for": "permission"}, [A1], MEMBER,
                         [cand("codex/gpt", "codex", "openai")], child_stopped=True)
    assert r["action"] == "needs_user"
    r = plan_replacement({"text": "timeout"}, [A1], MEMBER,
                         [cand("claude/sonnet", "claude", "anthropic")])
    assert r["action"] == "wait_child_stop"


def test_exhausted_routes_terminal():
    r = plan_replacement({"text": "model not found"}, [A1], MEMBER,
                         [cand("claude/opus", "claude", "anthropic")], child_stopped=True)
    assert r["action"] == "terminal"
    full = [A1, dict(A1, route="x/y"), dict(A1, route="z/w")]
    r = plan_replacement({"text": "timeout"}, full, MEMBER,
                         [cand("a/b", "a", "p")], child_stopped=True)
    assert r["action"] == "terminal" and r["attempts_left"] == 0


def test_unknown_failure_and_quiet_child_do_not_trigger_model_replacement():
    candidates = [cand("claude/sonnet", "claude", "anthropic")]
    assert plan_replacement({"text": "unexpected failure"}, [A1], MEMBER,
                            candidates, child_stopped=True)["action"] == "terminal"
    assert plan_replacement({"progress": False}, [A1], MEMBER,
                            candidates, child_stopped=True)["action"] == "check_progress"
    readonly = dict(MEMBER, write_rights=None)
    assert plan_replacement({"text": "timeout"}, [A1], readonly,
                            candidates)["action"] == "wait_child_stop"
