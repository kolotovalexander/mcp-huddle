"""Pure, deterministic preflight planner for a Huddle multi-agent room.

``build_plan`` accepts only a closed task profile and already-normalized
candidate records. It performs no filesystem, network, subprocess, or Huddle
room operations. The returned dictionary has this public shape::

    {"status": "planned|blocked|unsupported", "mode": str,
     "members": [normalized candidate, ...], "excluded": {id: [reason, ...]},
     "decisions": [str, ...], "plan_hash": "sha256:<hex>"}

Jev advice is advisory. Explicit organizer choices win; invalid or low-
confidence advice is recorded and the deterministic profile rule is used.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any


MODES = frozenset({"council", "relay", "team", "swarm"})
_PROFILE_FIELDS = frozenset({
    "task_type", "needs_files", "parts", "sequential_dependency",
    "diverse_opinions", "max_members", "budget",
})
_CANDIDATE_FIELDS = frozenset({
    "id", "name", "cli_kind", "model", "effort", "variant",
    "readonly_enforced", "enabled", "static_ok", "cost_class",
    "spec_fingerprint", "reasons",
})
_TASK_TYPES = frozenset({"question", "review", "design", "research", "code_change"})
_FILE_NEEDS = frozenset({"none", "read", "write"})
_PART_COUNTS = frozenset({"one", "two_three", "four_plus"})
_BUDGETS = frozenset({"free", "cheap", "any"})
_COST_CLASSES = frozenset({"free", "cheap", "paid", "unknown"})
_MIN_JEV_CONFIDENCE = 0.6


def _closed_mapping(value: Any, fields: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    unknown = set(value) - fields
    missing = fields - set(value)
    if unknown:
        raise ValueError(f"unsupported {label} field: {sorted(unknown)[0]}")
    if missing:
        raise ValueError(f"missing {label} field: {sorted(missing)[0]}")
    return dict(value)


def _text(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _validate_profile(profile: Mapping[str, Any]) -> dict[str, Any]:
    data = _closed_mapping(profile, _PROFILE_FIELDS, "profile")
    enum_fields = {
        "task_type": _TASK_TYPES,
        "needs_files": _FILE_NEEDS,
        "parts": _PART_COUNTS,
        "budget": _BUDGETS,
    }
    for field, choices in enum_fields.items():
        if not isinstance(data[field], str) or data[field] not in choices:
            raise ValueError(f"invalid profile {field}")
    for field in ("sequential_dependency", "diverse_opinions"):
        if not isinstance(data[field], bool):
            raise ValueError(f"profile {field} must be boolean")
    if isinstance(data["max_members"], bool) or not isinstance(data["max_members"], int):
        raise ValueError("profile max_members must be an integer")
    if not 1 <= data["max_members"] <= 8:
        raise ValueError("profile max_members must be between 1 and 8")
    return data


def _normalize_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    data = _closed_mapping(candidate, _CANDIDATE_FIELDS, "candidate")
    for field in ("id", "name", "cli_kind", "model", "spec_fingerprint"):
        _text(data[field], f"candidate {field}")
    for field in ("effort", "variant"):
        if data[field] is not None:
            _text(data[field], f"candidate {field}")
    for field in ("readonly_enforced", "enabled", "static_ok"):
        if not isinstance(data[field], bool):
            raise ValueError(f"candidate {field} must be boolean")
    if not isinstance(data["cost_class"], str) or data["cost_class"] not in _COST_CLASSES:
        raise ValueError("invalid candidate cost_class")
    if not isinstance(data["reasons"], list):
        raise ValueError("candidate reasons must be a list of strings")
    reasons = [_text(reason, "candidate reason") for reason in data["reasons"]]
    data["reasons"] = reasons
    return data


def _deterministic_mode(profile: Mapping[str, Any]) -> str:
    if profile["diverse_opinions"]:
        return "council"
    if profile["sequential_dependency"]:
        return "relay"
    if profile["parts"] == "four_plus":
        return "swarm"
    if profile["parts"] == "two_three":
        return "team"
    if profile["task_type"] in {"question", "review", "design", "research"}:
        return "council"
    return "team"


def _minimum_members(mode: str) -> int:
    return 1 if mode == "council" else 2


def _budget_allows(budget: str, cost_class: str) -> bool:
    if budget == "any":
        return True
    if budget == "cheap":
        return cost_class in {"free", "cheap"}
    return cost_class == "free"


def _advice(advice: Any, known_ids: set[str], eligible_ids: set[str]) -> tuple[dict[str, Any] | None, str]:
    if advice is None:
        return None, "no Jev recommendation; fallback to deterministic profile rule"
    try:
        data = _closed_mapping(advice, frozenset({"confidence", "mode", "member_ids"}),
                               "Jev advice")
        confidence = data["confidence"]
        if (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
                or not 0 <= confidence <= 1):
            raise ValueError("invalid confidence")
        mode = data["mode"]
        if mode is not None and (not isinstance(mode, str) or mode not in MODES):
            raise ValueError("unknown Jev mode")
        member_ids = data["member_ids"]
        if (not isinstance(member_ids, list)
                or any(not isinstance(member_id, str) or not member_id for member_id in member_ids)
                or len(member_ids) != len(set(member_ids))):
            raise ValueError("invalid Jev member IDs")
        if mode is None and not member_ids:
            raise ValueError("empty Jev advice")
        unknown = set(member_ids) - known_ids
        if unknown:
            raise ValueError("unknown Jev member ID")
        unavailable = set(member_ids) - eligible_ids
        if unavailable:
            raise ValueError("Jev selected an ineligible member")
        if confidence < _MIN_JEV_CONFIDENCE:
            return None, "low-confidence Jev recommendation; fallback to deterministic profile rule"
        return {"mode": mode, "member_ids": member_ids}, "Jev recommendation accepted"
    except (ValueError, TypeError):
        return None, "invalid Jev recommendation; fallback to deterministic profile rule"


def _hash_plan(profile: Mapping[str, Any], mode: str, status: str,
               members: Sequence[Mapping[str, Any]], excluded: Mapping[str, Any],
               decisions: Sequence[str]) -> str:
    canonical = {
        "profile": profile,
        "mode": mode,
        "status": status,
        "members": [
            {"id": member["id"], "spec_fingerprint": member["spec_fingerprint"]}
            for member in members
        ],
        "excluded": excluded,
        "decisions": list(decisions),
    }
    payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def build_plan(
    profile: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    *,
    jev_advice: Mapping[str, Any] | None = None,
    explicit_mode: str | None = None,
    explicit_members: Sequence[str] | None = None,
    explicit_allow_unenforced_read: bool = False,
) -> dict[str, Any]:
    """Choose a mode and roster without launching agents or creating rooms."""
    task = _validate_profile(profile)
    if explicit_mode is not None and (
        not isinstance(explicit_mode, str) or explicit_mode not in MODES
    ):
        raise ValueError("invalid explicit mode")
    if not isinstance(explicit_allow_unenforced_read, bool):
        raise ValueError("explicit_allow_unenforced_read must be boolean")

    if (not isinstance(candidates, Sequence)
            or isinstance(candidates, (str, bytes))):
        raise ValueError("candidates must be a sequence of normalized records")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in candidates:
        candidate = _normalize_candidate(raw)
        if candidate["id"] in seen:
            raise ValueError("candidate IDs must be unique")
        seen.add(candidate["id"])
        normalized.append(candidate)
    by_id = {candidate["id"]: candidate for candidate in normalized}

    excluded: dict[str, list[str]] = {}
    eligible: list[dict[str, Any]] = []
    allow_unenforced = explicit_allow_unenforced_read and task["needs_files"] == "read"
    for candidate in normalized:
        reasons = list(candidate["reasons"])
        blocking: list[str] = []
        if not candidate["enabled"]:
            blocking.append("disabled")
        if not candidate["static_ok"]:
            blocking.append("static_preflight_failed")
        if not candidate["readonly_enforced"] and not allow_unenforced:
            blocking.append("readonly_not_enforced")
        elif not candidate["readonly_enforced"]:
            reasons.append("explicit_allow_unenforced_read")
        if not _budget_allows(task["budget"], candidate["cost_class"]):
            blocking.append("outside_budget")
        if blocking:
            excluded[candidate["id"]] = reasons + blocking
        else:
            candidate["reasons"] = reasons
            eligible.append(candidate)

    eligible_ids = {candidate["id"] for candidate in eligible}
    recommendation, advice_reason = _advice(jev_advice, set(by_id), eligible_ids)
    advised_mode = explicit_mode or (recommendation and recommendation["mode"]) or _deterministic_mode(task)
    # A mode recommendation is only usable when the bounded roster can meet
    # that mode's minimum. If the deterministic mode is also too large, one
    # available agent can still run a council room.
    roster_capacity = min(len(eligible), task["max_members"])
    effective_advised_mode = (recommendation["mode"] if recommendation
                              and recommendation["mode"] else _deterministic_mode(task))
    if (explicit_mode is None and recommendation
            and roster_capacity < _minimum_members(effective_advised_mode)):
        recommendation = None
        advice_reason = (
            f"Jev mode {effective_advised_mode} requires "
            f"{_minimum_members(effective_advised_mode)} eligible member(s); "
            "fallback to a feasible deterministic mode"
        )
    if (recommendation and explicit_members is None and recommendation["member_ids"]
            and (len(recommendation["member_ids"]) > task["max_members"]
                 or len(recommendation["member_ids"]) < _minimum_members(advised_mode))):
        recommendation = None
        advice_reason = "Jev roster does not fit the profile; fallback to deterministic profile rule"
    decisions: list[str] = [advice_reason]
    if any(not candidate["readonly_enforced"] for candidate in eligible):
        decisions.append("unenforced read-only status was explicitly allowed for this read-only plan")

    mode = explicit_mode or (recommendation and recommendation["mode"]) or _deterministic_mode(task)
    if explicit_mode is None and roster_capacity < _minimum_members(mode) and roster_capacity >= 1:
        mode = "council"
        recommendation = None
        decisions.append("only one eligible member fits; selected feasible council mode")
    mode_source = "explicit organizer choice" if explicit_mode else (
        "Jev recommendation" if recommendation and recommendation["mode"] else
        "deterministic profile rule"
    )
    decisions.append(f"mode={mode} ({mode_source})")

    requested_members = list(explicit_members) if explicit_members is not None else None
    if requested_members is not None:
        if (any(not isinstance(member_id, str) or not member_id for member_id in requested_members)
                or len(requested_members) != len(set(requested_members))):
            raise ValueError("explicit member IDs must be unique non-empty strings")
        member_source = "explicit organizer choice"
    elif recommendation and recommendation["member_ids"]:
        requested_members = list(recommendation["member_ids"])
        member_source = "Jev recommendation"
    else:
        requested_members = [candidate["id"] for candidate in eligible[:task["max_members"]]]
        member_source = "eligible candidate order"
    decisions.append(f"members={member_source}")
    decisions.append("static preflight is not proof that the provider will reply")

    status = "planned"
    selected: list[dict[str, Any]] = []
    if task["needs_files"] == "write":
        status = "unsupported"
        decisions.append("write access is unsupported in planner stage 1; permissions were not downgraded")
    elif requested_members is None:
        status = "blocked"
        decisions.append("no member roster could be selected")
    elif len(requested_members) > task["max_members"]:
        status = "blocked"
        decisions.append("requested roster exceeds profile max_members")
    else:
        unavailable = [member_id for member_id in requested_members if member_id not in eligible_ids]
        if unavailable:
            status = "blocked"
            decisions.append("requested members are unavailable or excluded: " + ", ".join(unavailable))
        else:
            selected = [by_id[member_id] for member_id in requested_members]
            if len(selected) < _minimum_members(mode):
                status = "blocked"
                decisions.append(f"mode {mode} requires at least {_minimum_members(mode)} eligible member(s)")

    if status == "unsupported":
        selected = []
    members = [dict(candidate) for candidate in selected]
    return {
        "status": status,
        "mode": mode,
        "members": members,
        "excluded": {member_id: sorted(set(reasons)) for member_id, reasons in sorted(excluded.items())},
        "decisions": decisions,
        "plan_hash": _hash_plan(task, mode, status, members,
                                {member_id: sorted(set(reasons))
                                 for member_id, reasons in sorted(excluded.items())}, decisions),
    }
