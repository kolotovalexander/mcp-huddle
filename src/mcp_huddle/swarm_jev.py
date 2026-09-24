"""Bounded advisory decisions through the shared local Jev service.

This module accepts only closed task facts and verified candidate records. It
does not accept a task prompt, paths, code, commands, or caller-authored text.
Every unavailable or invalid answer becomes an explicit fallback result.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import (
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)


ENDPOINT = "http://127.0.0.1:45987/v1/systemone"
TIMEOUT_SECONDS = 3.0
CONFIDENCE_THRESHOLD = 0.60
_MAX_RESPONSE_BYTES = 64 * 1024
_TASK_KINDS = {
    "analysis": "Analyze information and produce findings.",
    "code_change": "Change or create software in a project.",
    "planning": "Produce an implementation plan without making the changes.",
    "research": "Collect and compare evidence from available sources.",
    "incident": "Diagnose and restore a failing system or workflow.",
}
_MODE_CRITERIA = {
    "sonnet": "One organizer-led room turn; participants respond in sequence and the organizer makes the final decision.",
    "relay": "Agents hand the work to one another in sequence, each continuing from the prior result.",
    "team": "Several agents work as a coordinated team with assigned responsibilities.",
    "swarm": "Several agents work in parallel, coordinate within a shared room, and produce a member-authored result.",
    "none": "No listed mode is a safe fit; defer the choice to the organizer.",
}
_HARNESS_LABELS = {
    "antigravity": "Antigravity",
    "claude": "Claude",
    "codex": "Codex",
    "gemini": "Gemini",
    "opencode": "OpenCode",
}
_TIER_LABELS = {
    "balanced": "balanced reasoning",
    "fast": "fast lightweight reasoning",
    "strong": "strong reasoning",
}
_CANDIDATE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")


@dataclass(frozen=True)
class ModeFacts:
    task_kind: str
    parallel_work: bool
    shared_files: bool
    independent_opinions: bool
    sequential_handoff: bool


@dataclass(frozen=True)
class VerifiedCandidate:
    """Closed facts for a candidate already checked by the caller."""

    candidate_id: str
    harness: str
    reasoning_tier: str
    can_edit_room_files: bool


@dataclass(frozen=True)
class ChoiceResult:
    status: str
    choice: str | None
    confidence: float | None
    reason: str


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def choose_mode(facts: ModeFacts) -> ChoiceResult:
    """Ask Jev to advise among the four Huddle modes and ``none``."""

    state = _serialize_facts(facts)
    if state is None:
        return _fallback("invalid_input")
    return _choose(
        question_id="mode",
        state=state,
        instructions=(
            "Choose the Huddle room mode that best fits these closed task facts. "
            "Choose none when the facts do not support a mode confidently."
        ),
        criteria=_MODE_CRITERIA,
    )


def choose_candidate(
    facts: ModeFacts, candidates: Sequence[VerifiedCandidate]
) -> ChoiceResult:
    """Ask Jev to advise among up to ten locally verified team candidates."""

    state = _serialize_facts(facts)
    criteria = _candidate_criteria(candidates)
    if state is None or criteria is None:
        return _fallback("invalid_input")
    return _choose(
        question_id="candidate",
        state=state,
        instructions=(
            "Choose the best verified agent for this room role using only the "
            "closed task facts and listed candidate capabilities. Choose none "
            "when no listed candidate fits."
        ),
        criteria=criteria,
    )


def _serialize_facts(facts: ModeFacts) -> dict[str, object] | None:
    if not isinstance(facts, ModeFacts):
        return None
    task_description = _TASK_KINDS.get(facts.task_kind) if isinstance(facts.task_kind, str) else None
    flags = (
        facts.parallel_work,
        facts.shared_files,
        facts.independent_opinions,
        facts.sequential_handoff,
    )
    if task_description is None or any(type(flag) is not bool for flag in flags):
        return None
    return {
        "task_kind": task_description,
        "parallel_work": facts.parallel_work,
        "shared_files": facts.shared_files,
        "independent_opinions": facts.independent_opinions,
        "sequential_handoff": facts.sequential_handoff,
    }


def _candidate_criteria(
    candidates: Sequence[VerifiedCandidate],
) -> dict[str, str] | None:
    if isinstance(candidates, (str, bytes)) or not isinstance(candidates, Sequence):
        return None
    if not 2 <= len(candidates) <= 10:
        return None
    criteria: dict[str, str] = {}
    for candidate in candidates:
        if not isinstance(candidate, VerifiedCandidate):
            return None
        if (
            not isinstance(candidate.candidate_id, str)
            or not _CANDIDATE_ID.fullmatch(candidate.candidate_id)
            or candidate.candidate_id == "none"
            or not isinstance(candidate.harness, str)
            or candidate.harness not in _HARNESS_LABELS
            or not isinstance(candidate.reasoning_tier, str)
            or candidate.reasoning_tier not in _TIER_LABELS
            or type(candidate.can_edit_room_files) is not bool
            or candidate.candidate_id in criteria
        ):
            return None
        edit_capability = "can edit room files" if candidate.can_edit_room_files else "read-only in the room"
        criteria[candidate.candidate_id] = (
            f"Verified {_HARNESS_LABELS[candidate.harness]} agent; "
            f"{_TIER_LABELS[candidate.reasoning_tier]}; {edit_capability}."
        )
    criteria["none"] = _MODE_CRITERIA["none"]
    return criteria


def _choose(
    *, question_id: str, state: Mapping[str, object], instructions: str, criteria: Mapping[str, str]
) -> ChoiceResult:
    key_path = _key_path()
    try:
        key = key_path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return _fallback("missing_key")
    if not key or "\r" in key or "\n" in key:
        return _fallback("missing_key")

    payload = {
        "model": "jev-latest",
        "state": dict(state),
        "questions": {
            question_id: {
                "type": "choice",
                "instructions": instructions,
                "criteria": dict(criteria),
            }
        },
    }
    request = Request(
        ENDPOINT,
        data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "X-AgentSync-Producer": "mcp-huddle-swarm",
            "X-AgentSync-Correlation-ID": f"huddle-swarm-{uuid.uuid4().hex}",
        },
        method="POST",
    )
    # Drop the local secret reference as soon as the authenticated request is built.
    del key
    try:
        response = _send(request, TIMEOUT_SECONDS)
    except HTTPError:
        return _fallback("service_error")
    except (URLError, OSError, TimeoutError, ValueError, json.JSONDecodeError):
        return _fallback("service_error")
    except Exception:
        # Keep unexpected transport failures advisory and never surface exception text.
        return _fallback("service_error")
    return _parse_response(response, question_id, criteria)


def _parse_response(
    response: Any, question_id: str, criteria: Mapping[str, str]
) -> ChoiceResult:
    if not isinstance(response, dict):
        return _fallback("invalid_response")
    answers = response.get("answers")
    answer = answers.get(question_id) if isinstance(answers, dict) else None
    if not isinstance(answer, dict) or answer.get("type") != "choice":
        return _fallback("invalid_response")
    choice = answer.get("choice")
    confidence = answer.get("confidence")
    if (
        not isinstance(choice, str)
        or choice not in criteria
        or isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(float(confidence))
        or not 0 <= confidence <= 1
    ):
        return _fallback("invalid_response")
    if confidence < CONFIDENCE_THRESHOLD:
        return _fallback("low_confidence")
    return ChoiceResult("ok", choice, float(confidence), "advisory")


def _send(request: Request, timeout: float) -> dict[str, Any]:
    opener = build_opener(ProxyHandler({}), _RejectRedirects())
    with opener.open(request, timeout=timeout) as response:
        raw = response.read(_MAX_RESPONSE_BYTES + 1)
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise ValueError("oversized response")
    decoded = json.loads(raw.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("invalid response")
    return decoded


def _key_path() -> Path:
    return Path.home() / "Library/Application Support/AgentSync/jev/service.key"


def _fallback(reason: str) -> ChoiceResult:
    return ChoiceResult("fallback", None, None, reason)
