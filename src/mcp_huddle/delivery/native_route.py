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

from .targets import Target

_TOOL_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
_REASON_MAX = 500


def refusal_for_declared_route(
    target: Target,
    declared_routes: Any,
    original_target: str = "",
) -> dict[str, str] | None:
    """Return a native-route refusal for an exact, caller-declared target.

    Each declaration has ``target`` (the exact input target or normalized
    ``harness:id``), ``tool`` (a concrete caller-available tool name), and an
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
        if not isinstance(tool, str) or not _TOOL_NAME.fullmatch(tool):
            continue
        reason = route.get("reason", "The caller declared this native route available.")
        if not isinstance(reason, str):
            reason = "The caller declared this native route available."
        reason = reason.strip()[:_REASON_MAX]
        if not reason:
            reason = "The caller declared this native route available."
        return {
            "reason": "native_route_required",
            "suggested_tool": tool,
            "note": (
                f"Use the declared native tool {tool!r} for {canonical_target}; "
                f"Huddle delivery was refused. {reason}"
            ),
        }
    return None
