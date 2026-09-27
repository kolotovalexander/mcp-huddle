"""Readonly-caller guard for ``message_send`` (see docs/delivery.md).

A read-only Huddle discussant (a swarm member/child spawned by Huddle with
its read-only transform applied, or one whose read-only status Huddle
cannot establish) must not be able to use ``message_send`` to hand work off
to a privileged live session -- that would launder its own read-only
restriction. This module only decides that policy question; the server.py
tool wrapper is responsible for building the :class:`Caller` it is handed
(see the hunk in docs/delivery.md / the assignment's server-hunk file) --
nothing here talks to ``ctx`` or HTTP headers directly, so it stays testable
without any MCP transport.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from . import config as delivery_config
from . import targets as targets_mod


@dataclass(frozen=True)
class Caller:
    """Policy input describing who is calling ``message_send``.

    ``verified_member``: True iff Huddle cryptographically verified the
    caller as a specific pilot member of ``room_id`` for its *current* wake
    (mirrors ``server._verified_member``'s contract).

    ``readonly``: tri-state, not a bool default:
      - ``True``  -- Huddle positively knows this caller's effective CLI has
        its read-only transform applied (registry profile + the process-wide
        ``MCP_HUDDLE_READONLY`` gate; see ``spawn.readonly_enforced``).
      - ``False`` -- Huddle positively knows it does NOT (readonly disabled
        for this profile/process).
      - ``None``  -- unknown. This covers a caller that carries Huddle's own
        member-token header (so Huddle knows it spawned it) but whose
        room/wake/profile could not be resolved -- e.g. no ``room_id`` was
        given, the claim already rotated, or the profile vanished from the
        registry. ``None`` is a "don't know", never "confirmed not
        read-only", and the policy below treats it the same as ``True``.

    A caller with no Huddle member-token header at all (a human, or an
    external client message_send never spawned) is represented as
    ``caller=None`` at the call site, not as a ``Caller`` instance -- see
    :func:`check_readonly_caller`.
    """

    verified_member: bool = False
    readonly: Optional[bool] = None
    room_id: str = ""
    agent: str = ""
    wake_id: str = ""


def _target_allowed(cfg: delivery_config.DeliveryConfig, to: str,
                     target: Optional[targets_mod.Target]) -> bool:
    allowed = cfg.readonly_allowed_targets()
    if not allowed:
        return False
    candidates = {to}
    if target is not None:
        candidates.add(f"{target.harness}:{target.id}")
    return bool(candidates & allowed)


def check_readonly_caller(caller: Optional[Caller], to: str,
                           cfg: delivery_config.DeliveryConfig,
                           target: Optional[targets_mod.Target]) -> Optional[str]:
    """Return a refusal reason string, or ``None`` if the send may proceed.

    Fail-closed: ``caller.readonly is True`` refuses, and so does
    ``caller.readonly is None`` for any caller Huddle knows it spawned
    (``caller is not None``) -- only a caller Huddle positively knows is NOT
    read-only (``readonly is False``), or one Huddle never spawned at all
    (``caller is None``), is allowed through unconditionally. An explicit
    ``delivery.json`` ``readonly_allowed_targets`` entry overrides the
    refusal for both the ``True`` and unknown cases.
    """
    if caller is None:
        return None
    if caller.readonly is False:
        return None
    if _target_allowed(cfg, to, target):
        return None
    return "readonly_caller"
