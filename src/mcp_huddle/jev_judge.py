"""Optional Jev-based judging of a worker's final chat result.

Opt-in only: a `kind="result"`/`kind="final"` message_post carries a
`meta={"judge": {...}}` block with a minimal task/acceptance_criteria/
result/evidence summary and an explicit `version`. Reuses the existing
TypeSafe Jev client (provider, secrets, budget, fallback, journal all live
there) rather than adding a new client. Every failure mode here — Jev not
installed, timeout, invalid response, budget exhausted — degrades to "skip":
the caller's message has already been persisted before this runs, so normal
Huddle work is never blocked or broken by Jev being unavailable.

One attempt per unchanged (worker, task, evidence-version): the claim in room
meta is taken BEFORE calling Jev (see _claim_version), so two concurrent
posts of the identical version cannot both fire a live Jev call. That also
means a runtime failure/fallback consumes the claim — this is intentional,
not a bug: this module never auto-retries or re-chases a better score for a
version that already got a (possibly fallback) attempt. The fallback is
still recorded as a visible terminal state in room meta (status="fallback",
with a reason), distinguishable from a real verdict (status="scored"). A
genuinely new attempt requires the caller to submit a new `version`.

Decision / action / outcome are three DIFFERENT things and are recorded
separately, on purpose:
  - decision  = Jev's own score (ready/revise/insufficient). Recorded
    locally in room meta always (status="scored").
  - action    = a REAL consumer choice. The only actions this module
    infers on its own are the concrete, unconditional ones it is itself
    about to take: routing "revise" back to the worker counts (accepted=
    False), because that IS a directed action actually happening, not a
    guess. "ready"/"insufficient" take no such action, so nothing is
    recorded for them here — record_feedback() below is how a real
    consumer (human or agent) reports accepted/rejected afterwards.
  - outcome   = a REAL, OBSERVED result (tests now pass, deploy failed,
    etc.). Never inferred from a later Jev score — a score is a decision,
    not an observation. Only record_feedback() can set it, via ordinary
    message metadata (discoverable, not a private Python-only hook). Until
    someone reports one, the outcome for a decision simply stays absent
    (pending/unknown) rather than fabricated.
"""

from __future__ import annotations

import importlib.util
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from . import bus

RUBRIC_VERSION = "1"
# "semantic_session_claims" is an EXISTING category jev_client.py already
# supports for bounded semantic classification of a claim against evidence
# (see skills/session-context/scripts/classify_claims.py) — structurally the
# same shape as "is this worker's claimed result backed by its evidence".
# Reusing it (rather than a bespoke category) gets the already-supported
# 1000-char fixture budget and the "evidence" fixture kind with zero edits
# to jev_client.py. Our own question/criteria stay caller-defined as always;
# only the category name and its associated limits are borrowed.
_CATEGORY = "semantic_session_claims"
_MAX_COST_USD = "0.02"
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.:-]+")
_REVIEWER_NAMES = ("Claude", "Antigravity")
_FEEDBACK_ACTIONS = ("accepted", "rejected")
_FEEDBACK_OUTCOMES = ("success", "failure", "partial")

# Per-field character budgets for the fixture sent to Jev. _CATEGORY's
# 1000-char fixture budget (vs. the generic 240) leaves real room to keep
# task/criteria/result/evidence intact instead of mangling them to ~35
# chars each. Evidence still gets the largest share: it is what carries
# certainty/uncertainty, and a blind truncation there would make weak
# evidence read as stronger than it is.
_FIELD_LIMITS = {"Task": 150, "Criteria": 150, "Result": 150, "Evidence": 350}
_MAX_FIXTURE_CHARS = 950
# If a field is this many times over its budget even after sanitizing, it is
# not "short" input a clip can safely compress — truncating it further would
# risk presenting weak/ambiguous evidence as if it were complete. Treat that
# as an explicit local "insufficient" rather than silently mangling it.
_OVERSIZE_MULTIPLIER = 4

# Mirrors jev_client.py's own _PRIVATE_PATH_RE/_SECRET_RE/_EMAIL_RE closely
# enough to neutralize them *before* they reach the shared client — without
# this, a task/evidence string containing an ordinary absolute path (very
# common in dev evidence, e.g. "/Users/me/proj/tests/test_x.py") makes the
# shared client's own safety check silently raise InvalidRequest and this
# module would report that as an unavailable-Jev skip for no real reason.
_PRIVATE_PATH_RE = re.compile(r"(?i)(?:/(?:Users|private|home|tmp)/\S*|[A-Z]:\\Users\\\S*)")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_SECRETLIKE_RE = re.compile(
    r"(?i)(?:authorization\s*:|bearer\s+\S+|api[_ -]?key\s*[:=]\s*\S+|"
    r"(?:access|refresh)[_ -]?token\s*[:=]\s*\S+|(?:token|secret|password|passwd)\s*[:=]\s*\S+|"
    r"sk-[A-Za-z0-9_-]{12,}|vck_[A-Za-z0-9_-]{12,}|apikey_[A-Za-z0-9_-]{12,})"
)

_JEV_MODULE: Any = None


@dataclass(frozen=True)
class JudgeVerdict:
    choice: str
    comment_body: str
    comment_to: Optional[str] = None
    route_to: Optional[str] = None
    route_body: Optional[str] = None


def _candidate_client_paths() -> list[Path]:
    """Well-known locations for the neighboring Jev skill's client module.

    Huddle ships as its own package (not a sibling of the Jev skill), so —
    unlike an in-repo AgentSync skill — it cannot assume a fixed relative
    layout. Absence of all candidates is a normal, expected "Jev not
    installed here" outcome, not an error.
    """
    paths = []
    override = os.environ.get("MCP_HUDDLE_JEV_CLIENT_PATH")
    if override:
        paths.append(Path(override))
    home = Path.home()
    paths.append(home / ".claude" / "skills" / "jev" / "scripts" / "jev_client.py")
    paths.append(home / ".codex" / "skills" / "jev" / "scripts" / "jev_client.py")
    try:
        here = Path(__file__).resolve()
        agentsync_root = here.parents[4]  # .../AgentSync/mcp/huddle/src/mcp_huddle/jev_judge.py
        paths.append(agentsync_root / "skills" / "global" / "jev" / "scripts" / "jev_client.py")
    except IndexError:
        pass
    return paths


def _load_jev_module() -> Any:
    """Find and import the Jev client. Only a *successful* load is cached.

    A failed/absent lookup is retried on every call rather than latched
    permanently — the Jev skill can be installed or repaired after this
    process started, and a new judge request later should be able to see
    that instead of being stuck with a stale "not found" verdict forever.
    """
    global _JEV_MODULE
    if _JEV_MODULE is not None:
        return _JEV_MODULE
    for candidate in _candidate_client_paths():
        try:
            if not candidate.is_file():
                continue
            spec = importlib.util.spec_from_file_location("mcp_huddle._jev_client", candidate)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception:
            continue
        _JEV_MODULE = module
        return module
    return None


def _sanitize(value: str) -> str:
    text = _PRIVATE_PATH_RE.sub("[path]", value)
    text = _EMAIL_RE.sub("[email]", text)
    text = _SECRETLIKE_RE.sub("[redacted]", text)
    return text


def _clip(value: str, limit: int) -> str:
    text = " ".join(value.split())
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def _safe_id(value: str, fallback: str) -> str:
    cleaned = _SAFE_ID_RE.sub("-", value).strip("-")
    if not cleaned or not cleaned[0].isalnum():
        cleaned = f"{fallback}-{cleaned}" if cleaned else fallback
    return cleaned[:80]


def _room_owner(room_id: str) -> Optional[str]:
    try:
        return bus.get_room_info(room_id).get("owner")
    except Exception:
        return None


def _pick_reviewer(room_id: str) -> Optional[str]:
    """An explicitly designated Claude/Antigravity reviewer already in the
    room, if any. Never falls back to an unrelated participant or "all"."""
    try:
        participants = bus.get_room_info(room_id).get("participants") or []
    except Exception:
        return None
    for name in _REVIEWER_NAMES:
        if name in participants:
            return name
    return None


def _claim_key(agent: str, task_key: str) -> str:
    # Scoped by worker AND task so two different workers answering the same
    # (or no) request can never collide on one another's dedup entry.
    return f"{_safe_id(agent, 'agent')}:{task_key}"


def _claim_version(room_id: str, claim_key: str, version: str) -> bool:
    """Atomically claim (claim_key, version) under the room's meta lock.

    Returns True only for the first caller to see this exact
    (claim_key, version, RUBRIC_VERSION) combination — a genuinely new
    task/result/rubric version. Taken before calling Jev: two concurrent
    calls for the identical version must not both fire a live request.
    """
    claimed = {"ok": False}

    def _update(meta: dict) -> dict:
        state = meta.setdefault("jev_judge", {})
        if not isinstance(state, dict):
            state = {}
            meta["jev_judge"] = state
        entry = state.get(claim_key)
        if (isinstance(entry, dict) and entry.get("version") == version
                and entry.get("rubric") == RUBRIC_VERSION):
            claimed["ok"] = False
        else:
            state[claim_key] = {
                "version": version, "rubric": RUBRIC_VERSION, "status": "claimed",
                "reason": None, "choice": None, "correlation_id": None,
            }
            claimed["ok"] = True
        return meta

    try:
        bus._update_meta_locked(room_id, _update)
    except Exception:
        return False
    return claimed["ok"]


def _finalize_claim(room_id: str, claim_key: str, version: str, **fields: Any) -> None:
    def _update(meta: dict) -> dict:
        state = meta.get("jev_judge")
        if isinstance(state, dict):
            entry = state.get(claim_key)
            if isinstance(entry, dict) and entry.get("version") == version:
                entry.update(fields)
        return meta

    try:
        bus._update_meta_locked(room_id, _update)
    except Exception:
        pass


def _read_claim(room_id: str, claim_key: str) -> Optional[dict]:
    try:
        state = bus.get_room_info(room_id).get("jev_judge")
    except Exception:
        return None
    entry = state.get(claim_key) if isinstance(state, dict) else None
    return entry if isinstance(entry, dict) else None


def _oversized_field(sanitized: dict[str, str]) -> Optional[str]:
    for label, value in sanitized.items():
        if len(value) > _FIELD_LIMITS[label] * _OVERSIZE_MULTIPLIER:
            return label
    return None


def record_feedback(
    room_id: str,
    worker: Any,
    task_id: Any,
    action: Any,
    outcome: Any,
) -> bool:
    """Discoverable, opt-in hook for a REAL consumer to report what actually
    happened after a judged result. Reached via ordinary message metadata —
    message_post(meta={"judge_feedback": {"worker": ..., "task_id": ...,
    "action": "accepted"|"rejected", "outcome": "success"|"failure"|
    "partial"}}) — not a private Python-only call.

    `action` records that a consumer (human or agent) made an actual
    accept/reject choice about the result. `outcome` records a real,
    OBSERVED execution result (e.g. tests now pass, deploy failed) — never
    inferred from a later Jev score. Both are optional but at least one is
    required. Returns False (never raises) if the payload is invalid or
    there is nothing pending scored for (worker, task_id) to attach it to;
    the underlying claim's decision/outcome otherwise stays exactly as it
    was — never fabricated.
    """
    if not isinstance(worker, str) or not worker.strip():
        return False
    if action is None and outcome is None:
        return False
    if action is not None and action not in _FEEDBACK_ACTIONS:
        return False
    if outcome is not None and outcome not in _FEEDBACK_OUTCOMES:
        return False
    module = _load_jev_module()
    if module is None:
        return False
    claim_key = _claim_key(worker, _safe_id(str(task_id or "root"), "task"))
    entry = _read_claim(room_id, claim_key)
    if not entry or entry.get("status") != "scored" or not entry.get("correlation_id"):
        return False

    client = module.JevClient()
    recorded = False
    if action is not None:
        try:
            client.record_consumer_decision(
                correlation_id=entry["correlation_id"], category=_CATEGORY,
                candidate_ids=["result"],
                selected_id="result" if action == "accepted" else None,
                accepted=action == "accepted",
            )
            recorded = True
        except Exception:
            pass
    if outcome is not None:
        try:
            client.reconcile_outcome(
                correlation_id=entry["correlation_id"], category=_CATEGORY,
                candidate_ids=["result"],
                selected_id="result" if action == "accepted" else None,
                outcome=outcome,
            )
            recorded = True
        except Exception:
            pass
    return recorded


def evaluate_result(
    room_id: str,
    agent: str,
    reply_to: Optional[int],
    judge_meta: Any,
) -> Optional[JudgeVerdict]:
    """Run at most one bounded Jev evaluation for a final worker result.

    Returns None when there is nothing to expose: no judge request, an
    incomplete request, a duplicate version, or an unavailable/failed Jev
    call. Never raises — callers rely on that to keep message_post safe.
    """
    if not isinstance(judge_meta, dict):
        return None
    task = judge_meta.get("task")
    acceptance = judge_meta.get("acceptance_criteria")
    result_text = judge_meta.get("result")
    evidence = judge_meta.get("evidence")
    version = judge_meta.get("version")
    if not all(isinstance(v, str) and v.strip() for v in (task, acceptance, result_text, evidence)):
        return None
    if version is None or not str(version).strip():
        return None
    version = str(version).strip()

    module = _load_jev_module()
    if module is None:
        return None

    task_key = _safe_id(str(judge_meta.get("task_id") or reply_to or "root"), "task")
    claim_key = _claim_key(agent, task_key)

    try:
        if not _claim_version(room_id, claim_key, version):
            return None
    except Exception:
        return None

    sanitized = {
        "Task": _sanitize(task), "Criteria": _sanitize(acceptance),
        "Result": _sanitize(result_text), "Evidence": _sanitize(evidence),
    }
    oversized = _oversized_field(sanitized)
    if oversized is not None:
        _finalize_claim(room_id, claim_key, version, status="oversized",
                         reason=f"field_too_long:{oversized}")
        return JudgeVerdict(
            choice="insufficient",
            comment_body=(
                f"[jev-judge] LOCAL-INSUFFICIENT: '{oversized}' is too long to "
                f"summarize safely without risking false certainty (Jev was not "
                f"called). version={version}"
            ),
            route_to=agent,
            route_body=(
                f"Your judge request's '{oversized}' field is too long to safely "
                f"summarize — please shorten it and resubmit with a new version."
            ),
        )

    description = " | ".join(
        f"{label}: {_clip(value, _FIELD_LIMITS[label])}"
        for label, value in sanitized.items()
    )
    correlation_id = _safe_id(f"huddle-{room_id}-{claim_key}-{version}", "huddle")

    request = {
        "category": _CATEGORY,
        "correlation_id": correlation_id,
        "result_link": None,
        "candidate_ids": ["result"],
        "metadata": {"task_kind": "huddle_result", "harness": "cli", "candidate_count": 1},
        "fixtures": [
            {"id": "result", "kind": "evidence", "description": _clip(description, _MAX_FIXTURE_CHARS), "tags": []},
        ],
        "questions": {
            "verdict": {
                "type": "choice",
                "instructions": (
                    "Judge the worker result against the task and acceptance "
                    "criteria using only the given evidence."
                ),
                "criteria": {
                    "ready": "Meets acceptance criteria; evidence supports it.",
                    "revise": "Concrete defect found; same worker should fix it.",
                    "insufficient": "Not enough evidence to judge either way.",
                },
            },
        },
        "max_cost_usd": _MAX_COST_USD,
    }

    try:
        client = module.JevClient()
        outcome = client.decide(request)
    except Exception:
        _finalize_claim(room_id, claim_key, version, status="fallback", reason="client_exception")
        return None

    if not isinstance(outcome, dict) or outcome.get("status") != "ok":
        reason = outcome.get("reason") if isinstance(outcome, dict) else "invalid_outcome"
        _finalize_claim(room_id, claim_key, version, status="fallback", reason=str(reason))
        return None
    answer = (outcome.get("answers") or {}).get("verdict")
    if not isinstance(answer, dict) or "choice" not in answer:
        _finalize_claim(room_id, claim_key, version, status="fallback", reason="invalid_answer")
        return None

    choice = answer["choice"]
    confidence = answer.get("confidence")
    probabilities = answer.get("probabilities")
    # This is Jev's SCORE, recorded locally as the decision — not yet an
    # accepted consumer action and never an observed outcome. See module
    # docstring for why those stay separate.
    _finalize_claim(
        room_id, claim_key, version, status="scored", reason=None,
        choice=choice, correlation_id=correlation_id,
    )
    comment_body = (
        f"[jev-judge] result #{reply_to if reply_to is not None else '?'} by {agent}: "
        f"verdict={choice} confidence={confidence} probabilities={probabilities} "
        f"model={outcome.get('model')} latency_ms={outcome.get('latency_ms')} "
        f"cost_usd={outcome.get('cost_usd')} version={version} task_id={task_key} "
        f"basis={description}"
    )

    if choice == "revise":
        # The only case where THIS module records a consumer action on its
        # own: routing back to the worker is a real, concrete, unconditional
        # action Huddle is actually taking right now (not a guess about what
        # someone might do with a score) — accepted=False because the result
        # is being sent back, not accepted.
        try:
            client.record_consumer_decision(
                correlation_id=correlation_id, category=_CATEGORY,
                candidate_ids=["result"], selected_id=None, accepted=False,
            )
        except Exception:
            pass
        return JudgeVerdict(
            choice=choice,
            comment_body=comment_body,
            route_to=agent,
            route_body=(
                f"Jev found a concrete gap vs. acceptance criteria on your result "
                f"(#{reply_to if reply_to is not None else '?'}) — please revise and "
                f"resubmit with a new version. {comment_body}"
            ),
        )
    if choice == "insufficient":
        reviewer = _pick_reviewer(room_id)
        if reviewer is not None:
            return JudgeVerdict(
                choice=choice,
                comment_body=comment_body,
                route_to=reviewer,
                route_body=(
                    f"Jev could not confidently judge this result — needs your "
                    f"review. {comment_body}"
                ),
            )
        # No designated Claude/Antigravity reviewer in this room: never
        # broadcast to all participants. Leave a needs-review comment for
        # the coordinator (room owner) instead of posting any request.
        return JudgeVerdict(
            choice=choice,
            comment_body=f"[jev-judge] NEEDS-REVIEW (no reviewer in room): {comment_body}",
            comment_to=_room_owner(room_id),
        )
    # "ready": exposed for a real consumer to see and act on. No
    # accepted/outcome is recorded here — see record_feedback().
    return JudgeVerdict(choice=choice, comment_body=comment_body)
