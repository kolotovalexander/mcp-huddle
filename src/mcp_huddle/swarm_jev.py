"""Bounded advisory decisions through the shared local Jev service.

This module accepts only closed task facts and verified candidate records. It
does not accept a task prompt, paths, code, commands, or caller-authored text.
Every unavailable or invalid answer becomes an explicit fallback result.
"""

from __future__ import annotations

import json
import math
import unicodedata
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
_TASK_TYPES = {"question", "review", "design", "research", "code_change"}
_FILE_NEEDS = {"none", "read", "write"}
_WORK_PARTS = {"one", "two_three", "four_plus"}
_BUDGETS = {"free", "cheap", "any"}
_MODE_CRITERIA = {
    "council": "The organizer chairs a discussion; participants respond in sequence, and the organizer makes the final decision.",
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
_COST_LABELS = {
    "free": "free cost class",
    "cheap": "low cost class",
    "paid": "paid cost class",
    "unknown": "unknown cost class",
}
_MAX_LOCAL_CANDIDATE_ID_LENGTH = 256


@dataclass(frozen=True)
class ModeFacts:
    task_type: str
    needs_files: str
    parts: str
    sequential_dependency: bool
    diverse_opinions: bool
    max_members: int
    budget: str


@dataclass(frozen=True)
class VerifiedCandidate:
    """Closed facts for a candidate already checked by the caller."""

    candidate_id: str
    harness: str
    model_class: str
    cost_class: str
    readonly_enforced: bool


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
    """Ask Jev among verified candidates, reserving a Jev option for ``none``."""

    state = _serialize_facts(facts)
    criteria = _candidate_criteria(candidates)
    if state is None or criteria is None:
        return _fallback("invalid_input")
    result = _choose(
        question_id="candidate",
        state=state,
        instructions=(
            "Choose the best verified agent for this room role using only the "
            "closed task facts and listed candidate capabilities. Choose none "
            "when no listed candidate fits."
        ),
        criteria=criteria,
    )
    if result.status != "ok" or result.choice == "none":
        return result
    try:
        candidate_index = int(result.choice[1:]) - 1
        selected = candidates[candidate_index]
    except (ValueError, IndexError, TypeError):
        return _fallback("invalid_response")
    return ChoiceResult("ok", selected.candidate_id, result.confidence, "advisory")


def _serialize_facts(facts: ModeFacts) -> dict[str, object] | None:
    if not isinstance(facts, ModeFacts):
        return None
    if (
        not isinstance(facts.task_type, str)
        or facts.task_type not in _TASK_TYPES
        or not isinstance(facts.needs_files, str)
        or facts.needs_files not in _FILE_NEEDS
        or not isinstance(facts.parts, str)
        or facts.parts not in _WORK_PARTS
        or not isinstance(facts.budget, str)
        or facts.budget not in _BUDGETS
        or type(facts.sequential_dependency) is not bool
        or type(facts.diverse_opinions) is not bool
        or type(facts.max_members) is not int
        or not 1 <= facts.max_members <= 8
    ):
        return None
    return {
        "task_type": facts.task_type,
        "needs_files": facts.needs_files,
        "parts": facts.parts,
        "sequential_dependency": facts.sequential_dependency,
        "diverse_opinions": facts.diverse_opinions,
        "max_members": facts.max_members,
        "budget": facts.budget,
    }


def _candidate_criteria(
    candidates: Sequence[VerifiedCandidate],
) -> dict[str, str] | None:
    if isinstance(candidates, (str, bytes)) or not isinstance(candidates, Sequence):
        return None
    # Jev supports at most ten choice options; reserve one for the required none.
    if not 2 <= len(candidates) <= 9:
        return None
    criteria: dict[str, str] = {}
    seen_ids: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, VerifiedCandidate):
            return None
        if (
            not isinstance(candidate.candidate_id, str)
            or not candidate.candidate_id.strip()
            or len(candidate.candidate_id) > _MAX_LOCAL_CANDIDATE_ID_LENGTH
            or any(unicodedata.category(char) == "Cc" for char in candidate.candidate_id)
            or not isinstance(candidate.harness, str)
            or candidate.harness not in _HARNESS_LABELS
            or not isinstance(candidate.model_class, str)
            or candidate.model_class not in _TIER_LABELS
            or not isinstance(candidate.cost_class, str)
            or candidate.cost_class not in _COST_LABELS
            or type(candidate.readonly_enforced) is not bool
            or candidate.candidate_id in seen_ids
        ):
            return None
        if len(criteria) == 10:
            return None
        readonly = (
            "the harness enforces read-only access"
            if candidate.readonly_enforced
            else "read-only access is not enforced by the harness"
        )
        option_id = f"c{len(criteria) + 1}"
        seen_ids.add(candidate.candidate_id)
        criteria[option_id] = (
            f"Verified {_HARNESS_LABELS[candidate.harness]} agent; "
            f"{_TIER_LABELS[candidate.model_class]}; "
            f"{_COST_LABELS[candidate.cost_class]}; {readonly}."
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
