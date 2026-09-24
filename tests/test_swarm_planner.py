"""Pure preflight planning for the Huddle swarm pilot."""

from copy import deepcopy

import pytest

from mcp_huddle.swarm_planner import build_plan


def profile(**overrides):
    value = {
        "task_type": "code_change",
        "needs_files": "read",
        "parts": "two_three",
        "sequential_dependency": False,
        "diverse_opinions": False,
        "max_members": 4,
        "budget": "cheap",
    }
    value.update(overrides)
    return value


def candidate(member_id, *, readonly=True, fingerprint=None, **overrides):
    value = {
        "id": member_id,
        "name": member_id.title(),
        "cli_kind": "opencode",
        "model": "provider/model",
        "effort": "medium",
        "variant": None,
        "readonly_enforced": readonly,
        "enabled": True,
        "static_ok": True,
        "cost_class": "cheap",
        "spec_fingerprint": fingerprint or f"fingerprint-{member_id}",
        "reasons": [],
    }
    value.update(overrides)
    return value


def test_explicit_mode_and_members_take_precedence_over_jev():
    result = build_plan(
        profile(),
        [candidate("a"), candidate("b"), candidate("c")],
        jev_advice={"confidence": 0.95, "mode": "council", "member_ids": ["a", "b"]},
        explicit_mode="relay",
        explicit_members=["c", "b"],
    )

    assert result["mode"] == "relay"
    assert [item["id"] for item in result["members"]] == ["c", "b"]
    assert result["status"] == "planned"


def test_write_request_is_explicitly_unsupported_without_permission_downgrade():
    result = build_plan(profile(needs_files="write"), [candidate("a"), candidate("b")])

    assert result["status"] == "unsupported"
    assert result["members"] == []
    assert any("write" in item.lower() for item in result["decisions"])


def test_unenforced_read_candidate_is_excluded_by_default():
    result = build_plan(
        profile(needs_files="read", parts="one"),
        [candidate("a", readonly=False)],
    )

    assert result["members"] == []
    assert result["status"] == "blocked"
    assert result["excluded"]["a"] == ["readonly_not_enforced"]


def test_unenforced_read_requires_explicit_allow_and_is_marked():
    result = build_plan(
        profile(needs_files="read", parts="one"),
        [candidate("a", readonly=False)],
        explicit_allow_unenforced_read=True,
    )

    assert [item["id"] for item in result["members"]] == ["a"]
    assert result["members"][0]["readonly_enforced"] is False
    assert any("unenforced" in item for item in result["decisions"])


@pytest.mark.parametrize(
    "advice",
    [
        {"confidence": 0.95, "mode": "team", "member_ids": ["a", "missing"]},
        {"confidence": 0.2, "mode": "council", "member_ids": ["a"]},
    ],
)
def test_invalid_jev_advice_falls_back_to_deterministic_plan(advice):
    candidates = [candidate("a"), candidate("b"), candidate("c")]
    expected = build_plan(profile(), candidates)

    result = build_plan(profile(), candidates, jev_advice=advice)

    assert result["mode"] == expected["mode"] == "team"
    assert [item["id"] for item in result["members"]] == ["a", "b", "c"]
    assert any("fallback" in item for item in result["decisions"])


def test_jev_roster_over_profile_limit_falls_back_without_changing_explicit_mode():
    result = build_plan(
        profile(max_members=2),
        [candidate("a"), candidate("b"), candidate("c")],
        jev_advice={"confidence": 0.95, "mode": "swarm", "member_ids": ["a", "b", "c"]},
        explicit_mode="relay",
    )

    assert result["mode"] == "relay"
    assert [member["id"] for member in result["members"]] == ["a", "b"]
    assert any("fallback" in item for item in result["decisions"])


def test_plan_hash_is_stable_and_changes_when_member_fingerprint_changes():
    candidates = [candidate("a"), candidate("b")]
    first = build_plan(profile(), candidates)
    second = build_plan(profile(), deepcopy(candidates))
    candidates[0]["spec_fingerprint"] = "new-generation"
    changed = build_plan(profile(), candidates)

    assert first["plan_hash"] == second["plan_hash"]
    assert first["plan_hash"] != changed["plan_hash"]


def test_profile_and_candidate_schemas_reject_raw_runtime_arguments():
    with pytest.raises(ValueError, match="unsupported candidate field"):
        build_plan(profile(), [candidate("a", argv=["opencode", "run"])])

    with pytest.raises(ValueError, match="unsupported profile field"):
        build_plan({**profile(), "cwd": "/tmp/project"}, [candidate("a")])


def test_candidate_reasons_are_metadata_unless_a_preflight_flag_excludes_it():
    result = build_plan(
        profile(task_type="question", parts="one"),
        [candidate("a", reasons=["provider response not probed"])],
    )

    assert result["status"] == "planned"
    assert result["members"][0]["reasons"] == ["provider response not probed"]
    assert any("not proof" in item for item in result["decisions"])
