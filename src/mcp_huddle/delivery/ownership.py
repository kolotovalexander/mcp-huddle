"""Huddle-owned-session guard for ``message_send`` (see docs/delivery.md).

``message_send`` must never deliver in parallel into a session/thread that
Huddle itself currently owns via an active wake claim -- a native/resume
method firing at the same time as Huddle's own wake would run two turns in
one history.

Reads room metadata through ``bus``'s existing public, read-only accessor
(``bus.list_rooms``, which itself calls the same confined ``_read_meta``
used everywhere else) -- never a new lock, never two locks held at once,
never a write. Best-effort: a room whose meta can't be read is skipped, not
guessed at (mirrors ``bus.list_rooms``'s own per-room ``except: pass``).
"""

from __future__ import annotations

from typing import Optional

from . import targets as targets_mod


def _codex_owned_thread_ids() -> frozenset:
    """Codex thread ids Huddle currently owns via an active wake claim.

    An ``agent_meta`` entry counts iff it carries both a non-empty
    ``wake_claim_id`` (Huddle's cross-process wake-exclusion marker -- see
    CLAUDE.md "Cross-process wake exclusion is the persisted claim under the
    meta lock") and a non-empty ``thread_id`` (the Codex thread id Huddle
    captured for that member and resumes -- see server.py's phase-2
    thread_id capture / ``codex_resume``). Both must be present: a
    ``thread_id`` alone just means Huddle has resumed that thread before,
    not that a turn is in flight right now.
    """
    try:
        from mcp_huddle import bus as _bus  # local import: avoid any import cycle
    except Exception:
        return frozenset()
    owned = set()
    try:
        rooms = _bus.list_rooms()
    except Exception:
        return frozenset()
    for meta in rooms:
        if not isinstance(meta, dict):
            continue
        agent_meta = meta.get("agent_meta")
        if not isinstance(agent_meta, dict):
            continue
        for info in agent_meta.values():
            if not isinstance(info, dict):
                continue
            thread_id = info.get("thread_id")
            if info.get("wake_claim_id") and isinstance(thread_id, str) and thread_id:
                owned.add(thread_id)
    return frozenset(owned)


def check_not_huddle_owned(target: targets_mod.Target) -> Optional[str]:
    """Return a refusal reason if ``target`` is currently Huddle-owned.

    Only Codex is checked: ``agent_meta[member]["thread_id"]`` is the one
    place in the data model that positively ties an active wake claim to the
    exact id a ``codex:<id>`` target resolves to. There is no equivalent
    persisted mapping from a Huddle wake claim to a *Claude* session id (a
    spawned/woken Claude member has no analogous "captured session id"
    field in ``agent_meta``) -- guessing one would be worse than not
    checking, so a Claude target is deliberately left to delivery's existing
    liveness tri-state only. See docs/delivery.md "Limitations".
    """
    if target.harness != "codex":
        return None
    if target.id in _codex_owned_thread_ids():
        return "huddle_owned_session"
    return None
