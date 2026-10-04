"""Caller-declared native routes for avoiding unnecessary Huddle delivery.

MCP does not expose the connected agent's live tool catalog to this server.
Callers may therefore make a narrow, per-call declaration that a concrete
native tool is available for one exact target. This is an attestation, not an
independently verified capability; an absent or malformed declaration never
blocks the existing cross-harness route.
"""

from __future__ import annotations

import re
from typing import Any

from .targets import Target, HARNESS_PREFIXES

_TOOL_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
_REASON_MAX = 500


def refusal_for_declared_route(
    target: Target,
    declared_routes: Any,
    original_target: str = "",
) -> dict[str, str] | None:
    """Return a native-route refusal for an exact, caller-declared target.

    Each declaration has ``target`` (the exact input target or normalized
    ``harness:id``), ``available: true`` or ``tool`` (an optional concrete tool name), and an
    optional ``reason``. No matching declaration means normal Huddle delivery.
    """
    if not isinstance(declared_routes, list):
        return None

    canonical_target = f"{target.harness}:{target.id}"
    for route in declared_routes:
        if not isinstance(route, dict):
            continue
        route_target = route.get("target")
        tool = route.get("tool")
        if route_target not in (canonical_target, original_target):
            continue
        valid_tool = isinstance(tool, str) and bool(_TOOL_NAME.fullmatch(tool))
        if not valid_tool and route.get("available") is not True:
            continue
        tool = tool if valid_tool else None
        reason = route.get("reason", "The caller declared this native route available.")
        if not isinstance(reason, str):
            reason = "The caller declared this native route available."
        reason = reason.strip()[:_REASON_MAX]
        if not reason:
            reason = "The caller declared this native route available."
        result = {
            "reason": "native_route_required",
            "note": (
                f"Use your available native communication tools for {canonical_target}; "
                f"Huddle delivery was refused. {reason}"
            ),
        }
        if tool:
            result["suggested_tool"] = tool
        return result
    return None


def refusal_for_same_harness(target: Target, sender_harness: str,
                             native_unavailable_reason: str = "") -> dict[str, str] | None:
    """Advisory routing only: a caller declaration never grants permissions."""
    sender = sender_harness.strip().lower() if isinstance(sender_harness, str) else ""
    if sender not in HARNESS_PREFIXES or sender != target.harness:
        return None
    if isinstance(native_unavailable_reason, str) and native_unavailable_reason.strip():
        return None
    return {
        "reason": "native_route_required",
        "note": (
            "The recipient uses the same harness you declared. Use your native "
            "session-messaging tools if they can reach this recipient. Nothing was sent. "
            "If no native route is available, retry with native_unavailable_reason "
            "briefly explaining why. Do not bypass a permission denial. "
            "Huddle rooms and collaboration modes remain available."
        ),
    }
