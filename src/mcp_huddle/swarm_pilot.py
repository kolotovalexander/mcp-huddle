"""Small, durable four-mode swarm pilot built on Huddle room metadata.

This module owns only room state. The server remains responsible for posting
requests and waking CLI workers, so no message lock is held with the meta lock.
"""

from __future__ import annotations

import hashlib
import re
import time
from typing import Any

from . import bus


MODES = frozenset({"council", "team", "relay", "swarm"})
WORKSPACES = frozenset({"shared_only", "allow_subworktrees"})
_ROOM_ID_RE = re.compile(r"room_[0-9a-f]{8}\Z")
_FINGERPRINT_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MEMBER_ID_RE = re.compile(r"mem_[0-9a-f]{12}\Z")


def create(
    name: str,
    organizer: str,
    goal: str,
    mode: str,
    members: list[str],
    cwd: str = "",
    workspace_strategy: str = "shared_only",
    *,
    room_id: str | None = None,
    client_request_fingerprint: str = "",
    plan_hash: str = "",
    start_requested: bool | None = None,
    registry_availability_checked: bool | None = None,
    expected_specs: dict[str, str] | None = None,
    member_profiles: dict[str, str] | None = None,
) -> str:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {sorted(MODES)}")
    if workspace_strategy not in WORKSPACES:
        raise ValueError(f"workspace_strategy must be one of {sorted(WORKSPACES)}")
    if not name.strip() or not organizer.strip() or not goal.strip():
        raise ValueError("name, organizer and goal must be non-empty")
    if len(name) > 200 or len(organizer) > 120 or len(goal) > 5000:
        raise ValueError("pilot room name, organizer or goal is too long")
    if room_id is not None and (
        not isinstance(room_id, str) or not _ROOM_ID_RE.fullmatch(room_id)
    ):
        raise ValueError("room_id must be room_ followed by 8 lowercase hex digits")
    for label, value in (
        ("client_request_fingerprint", client_request_fingerprint),
        ("plan_hash", plan_hash),
    ):
        if not isinstance(value, str) or (value and not _FINGERPRINT_RE.fullmatch(value)):
            raise ValueError(f"{label} must be empty or sha256 followed by 64 lowercase hex digits")
    if start_requested is not None and not isinstance(start_requested, bool):
        raise ValueError("start_requested must be a boolean")
    if registry_availability_checked is not None and not isinstance(registry_availability_checked, bool):
        raise ValueError("registry_availability_checked must be a boolean")
    if expected_specs is not None:
        if not isinstance(expected_specs, dict) or set(expected_specs) != set(members):
            raise ValueError("expected_specs keys must exactly match members")
        if any(
            not isinstance(fingerprint, str) or not _FINGERPRINT_RE.fullmatch(fingerprint)
            for fingerprint in expected_specs.values()
        ):
            raise ValueError("expected_specs values must be sha256 fingerprints")
    if start_requested is not None and registry_availability_checked is None:
        raise ValueError("registry_availability_checked is required with start_requested")
    if (not members or len(members) > 8 or len(set(members)) != len(members)
            or organizer in members or {"Human", "System"}.intersection(members)):
        raise ValueError("members must be non-empty, unique and exclude organizer")
    for member in members:
        bus._safe_path_component(member, "member")
    if len({name.lower() for name in [organizer, *members]}) != len(members) + 1:
        # Agent log and last-message paths are lower-cased names. The council
        # organizer may also get a wake slot, so include that name too.
        raise ValueError("members must stay unique when compared case-insensitively")
    member_profiles = validate_member_profiles(members, member_profiles)
    # A swarm survives organizer session exit. Ordinary rooms retain their
    # existing owner_pid/SessionEnd lifecycle.
    room_id = bus.create_room(name, organizer, 0, cwd, "", room_id=room_id)
    now = int(time.time())

    def init(meta: dict) -> dict:
        meta["swarm_pilot"] = {
            "schema": 2,
            "mode": mode,
            "goal": goal,
            "organizer": organizer,
            "members": members,
            "member_ids": _member_ids(room_id, members),
            "workspace_strategy": workspace_strategy,
            "round": 1,
            "dispatched": {},
            "done": {},
            "responsibilities": {},
            "tasks": {},
            "decisions": {},
            "facts": {},
            "final": None,
            "phase": "working",
            "created_at": now,
        }
        if member_profiles:
            # Absent for legacy rooms: each member launches its own name.
            meta["swarm_pilot"]["member_profiles"] = dict(member_profiles)
        if client_request_fingerprint:
            meta["swarm_pilot"]["client_request_fingerprint"] = client_request_fingerprint
        if plan_hash:
            meta["swarm_pilot"]["plan_hash"] = plan_hash
        # Keep deterministic-create retries recoverable if the server stops
        # immediately after this room record is written. Legacy direct callers
        # omit start_requested and retain the old metadata shape.
        if client_request_fingerprint and start_requested is not None:
            meta["swarm_pilot"].update({
                "server_create_state": "preparing",
                "start_requested": start_requested,
                "registry_availability_checked": registry_availability_checked,
            })
            if expected_specs is not None:
                meta["swarm_pilot"]["expected_specs"] = dict(expected_specs)
        return meta

    bus._update_meta_locked(room_id, init)
    for member in members:
        bus.invite_agent(room_id, member)
    return room_id


def status(room_id: str) -> dict:
    state = bus.get_room_info(room_id).get("swarm_pilot")
    if not isinstance(state, dict):
        raise ValueError("room is not a swarm pilot")
    return _expose_member_ids(room_id, state)


def resolve_member(room_id: str, reference: str, *, allow_organizer: bool = False) -> str:
    """Resolve a pilot member's profile name or stable slot ID to its name."""
    state = status(room_id)
    matches = [name for name in state["members"]
               if reference == name or reference == state["member_ids"][name]]
    if allow_organizer and reference == state["organizer"]:
        matches.append(state["organizer"])
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError("member reference is ambiguous")
    raise ValueError("unknown swarm member")


def validate_member_profiles(
    members: list[str], member_profiles: dict[str, str] | None,
) -> dict[str, str]:
    """Validate an optional member -> registry profile mapping.

    Unmapped members launch the profile named like themselves, as before.
    Several members may share one profile; each keeps its own name,
    member_id, log path and wake claim. Identity entries are dropped.
    """
    if member_profiles is None:
        return {}
    if not isinstance(member_profiles, dict):
        raise ValueError("member_profiles must map member names to profile names")
    if set(member_profiles) - set(members):
        raise ValueError("member_profiles keys must be pilot members")
    for member, profile in member_profiles.items():
        if not isinstance(profile, str) or not profile.strip() or len(profile) > 120:
            raise ValueError(f"member_profiles value for {member} must be a profile name")
    return {member: profile for member, profile in member_profiles.items()
            if profile != member}


# Harnesses that learn the member name from the Huddle brief. Runner profiles
# (MiMo, OpenAI-compatible) bake their room identity into ``--agent`` and would
# ignore requests addressed to a differently named member.
MAPPED_PROFILE_BINARIES = frozenset({"codex", "claude"})


def member_profile(state: dict, member: str) -> str:
    """Registry profile that launches ``member`` (its own name by default)."""
    mapping = state.get("member_profiles") if isinstance(state, dict) else None
    profile = mapping.get(member) if isinstance(mapping, dict) else None
    return profile if isinstance(profile, str) and profile else member


def due_members(room_id: str) -> list[str]:
    state = status(room_id)
    if state["phase"] != "working":
        return []
    members = state["members"]
    done = state["done"]
    dispatched = state["dispatched"]
    if state["mode"] in ("council", "relay"):
        for member in members:
            if member not in done:
                return [] if member in dispatched else [member]
        return []
    return [member for member in members if member not in done and member not in dispatched]


def mark_dispatched(room_id: str, member: str, message_id: int) -> dict:
    if not isinstance(message_id, int) or message_id < 1:
        raise ValueError("message_id must be positive")

    def update(meta: dict) -> dict:
        state = _state(meta)
        if member not in state["members"]:
            raise ValueError("member not in swarm")
        existing = state["dispatched"].get(member)
        if existing is not None and existing != message_id:
            raise ValueError("member already dispatched with a different request")
        state["dispatched"][member] = message_id
        return meta

    state = bus._update_meta_locked(room_id, update)["swarm_pilot"]
    return _expose_member_ids(room_id, state)


def record(room_id: str, member: str, kind: str, key: str, value: str) -> dict:
    if kind not in ("responsibility", "task", "decision", "fact"):
        raise ValueError("kind must be responsibility, task, decision or fact")
    if not key.strip() or not value.strip():
        raise ValueError("key and value must be non-empty")
    if len(key) > 160 or len(value) > 4000:
        raise ValueError("pilot object is too long")

    def update(meta: dict) -> dict:
        state = _state(meta)
        if member not in state["members"]:
            raise PermissionError("only a swarm member can record an object")
        if state["phase"] != "working":
            raise ValueError("swarm is no longer working")
        bucket = state[{"responsibility": "responsibilities", "task": "tasks",
                        "decision": "decisions", "fact": "facts"}[kind]]
        prior = bucket.get(key)
        if kind in ("responsibility", "task") and prior and prior["member"] != member:
            raise ValueError("responsibility has another owner; agree on transfer first")
        if (prior and prior["member"] == member and prior["value"] == value
                and prior.get("round", 1) == state.get("round", 1)):
            return meta
        bucket[key] = {
            "member": member, "value": value,
            "version": (prior or {}).get("version", 0) + 1,
            "updated_at": int(time.time()),
            "round": state.get("round", 1),
        }
        return meta

    state = bus._update_meta_locked(room_id, update)["swarm_pilot"]
    return _expose_member_ids(room_id, state)


def transfer_responsibility(
    room_id: str, member: str, key: str, to_member: str, reason: str,
) -> dict:
    """Move a claimed responsibility to another member, keeping its history.

    The current owner may hand it off at any time. Another member may take it
    over only after every member finished the round and the owner holds no
    active wake claim, i.e. the owner is not running a turn that could still
    finish. Council's final word stays with the organizer, so its reporter
    responsibility is not transferable.
    """
    if not key.strip() or not reason.strip():
        raise ValueError("key and reason must be non-empty")
    if len(key) > 160 or len(reason) > 1000:
        raise ValueError("transfer key or reason is too long")

    def update(meta: dict) -> dict:
        state = _state(meta)
        members = state["members"]
        if member not in members or to_member not in members:
            raise PermissionError("only swarm members can transfer a responsibility")
        if state["phase"] != "working" or state.get("final") is not None:
            raise ValueError("swarm is no longer working")
        if key == "reporter" and state["mode"] == "council":
            raise ValueError("council final word belongs to the organizer")
        prior = state["responsibilities"].get(key)
        if not isinstance(prior, dict) or not prior.get("member"):
            raise ValueError("responsibility is not claimed; claim it with swarm_pilot_record")
        owner = prior["member"]
        if owner == to_member:
            raise ValueError("member already owns this responsibility")
        if member != owner:
            if member != to_member:
                raise PermissionError("only the owner or the taking-over member can transfer")
            if len(state["done"]) != len(members):
                raise ValueError("takeover is allowed only after every member finished the round")
            owner_info = (meta.get("agent_meta") or {}).get(owner) or {}
            if owner_info.get("wake_claim_id"):
                raise ValueError("current owner is still running a turn; wait for it to end")
        now = int(time.time())
        version = prior.get("version", 0) + 1
        state["responsibilities"][key] = {
            **prior, "member": to_member, "version": version, "updated_at": now,
        }
        state.setdefault("transfers", []).append({
            "key": key, "from": owner, "to": to_member, "by": member,
            "reason": reason, "round": state.get("round", 1),
            "version": version, "at": now,
        })
        return meta

    state = bus._update_meta_locked(room_id, update)["swarm_pilot"]
    return _expose_member_ids(room_id, state)


def reporter_transfer_count(state: dict) -> int:
    """Reporter transfers in the current round; versions the final request."""
    round_no = state.get("round", 1)
    return sum(
        1 for item in state.get("transfers") or []
        if isinstance(item, dict) and item.get("key") == "reporter"
        and item.get("round", 1) == round_no
    )


def round_done(room_id: str, member: str, summary: str) -> dict:
    if not summary.strip():
        raise ValueError("summary must be non-empty")
    if len(summary) > 4000:
        raise ValueError("summary is too long")

    def update(meta: dict) -> dict:
        state = _state(meta)
        if member not in state["members"]:
            raise PermissionError("only a swarm member can finish their part")
        if member not in state["dispatched"]:
            raise ValueError("member was not dispatched")
        if state["phase"] != "working":
            raise ValueError("swarm is no longer working")
        previous = state["done"].get(member)
        if previous is not None:
            if previous["summary"] != summary:
                raise ValueError("member already completed this round")
            return meta
        state["done"][member] = {
            "summary": summary, "updated_at": int(time.time()),
        }
        return meta

    state = bus._update_meta_locked(room_id, update)["swarm_pilot"]
    return _expose_member_ids(room_id, state)


def finish(room_id: str, member: str, result: str) -> dict:
    if not result.strip():
        raise ValueError("result must be non-empty")
    if len(result) > 16000:
        raise ValueError("result is too long")

    def update(meta: dict) -> dict:
        state = _state(meta)
        if len(state["done"]) != len(state["members"]):
            raise ValueError("every member must consciously finish their part")
        if state["mode"] == "council":
            if member != state["organizer"]:
                raise PermissionError("organizer has the last word in council")
        else:
            reporter = state["responsibilities"].get("reporter", {}).get("member")
            if member != reporter:
                raise PermissionError("a member must claim reporter responsibility")
        if state["final"] is not None:
            if state["final"]["result"] != result:
                raise ValueError("final result already recorded")
            return meta
        state["final"] = {
            "member": member, "result": result, "updated_at": int(time.time()),
        }
        state["phase"] = "completed"
        return meta

    state = bus._update_meta_locked(room_id, update)["swarm_pilot"]
    return _expose_member_ids(room_id, state)


MAX_ROUNDS = 8
NEXT_ROUND_DECISION = "next_round"


def open_round(room_id: str, by: str, reason: str, from_round: int) -> dict:
    """Deliberately open round ``from_round + 1`` after that round's final.

    Only the organizer, or a member after some member recorded the decision
    ``next_round`` during the round being closed, may open it. Per-round
    ``dispatched``/``done``/``final`` move into ``round_history``;
    responsibilities, tasks, decisions, facts and messages stay. Nothing opens
    a round automatically, and ``MAX_ROUNDS`` bounds the total. Repeating the
    same call after success returns the state unchanged.
    """
    if not isinstance(from_round, int) or isinstance(from_round, bool) or from_round < 1:
        raise ValueError("from_round must be a positive integer")
    if not reason.strip():
        raise ValueError("reason must be non-empty")
    if len(reason) > 1000:
        raise ValueError("reason is too long")

    def update(meta: dict) -> dict:
        state = _state(meta)
        current = state.get("round", 1)
        history = state.get("round_history") or []
        if current == from_round + 1:
            last = history[-1] if history else {}
            if (last.get("round") == from_round
                    and last.get("next_round_opened_by") == by
                    and last.get("next_round_reason") == reason):
                return meta
            raise ValueError("round was already advanced")
        if current != from_round:
            raise ValueError("from_round does not match the current round")
        if meta.get("status") not in ("open", "idle"):
            raise ValueError("room is not open")
        if state["phase"] != "completed" or state.get("final") is None:
            raise ValueError("current round has no final yet; finish it first")
        if current >= MAX_ROUNDS:
            raise ValueError(f"round limit {MAX_ROUNDS} reached")
        agent_meta = meta.get("agent_meta") or {}
        busy = sorted(
            name for name in state["members"]
            if ((agent_meta.get(name) or {}).get("wake_claim_id"))
        )
        if busy:
            # Fail closed: a member CLI from the closing round still holds
            # its wake claim, so a new dispatch could run on top of it.
            raise ValueError(
                "members still running a turn: " + ", ".join(busy)
                + "; open the next round after they exit"
            )
        if by != state["organizer"]:
            if by not in state["members"]:
                raise PermissionError("only the organizer or a member can open a round")
            decision = state["decisions"].get(NEXT_ROUND_DECISION)
            if not isinstance(decision, dict) or decision.get("round", 1) != current:
                raise PermissionError(
                    "a member may open a round only after the room recorded "
                    f"decision '{NEXT_ROUND_DECISION}' in this round"
                )
        now = int(time.time())
        state["round_history"] = [*history, {
            "round": current,
            "dispatched": state["dispatched"],
            "done": state["done"],
            "final": state["final"],
            "next_round_opened_by": by,
            "next_round_reason": reason,
            "next_round_opened_at": now,
        }]
        state.update({
            "round": current + 1, "dispatched": {}, "done": {}, "final": None,
            "phase": "working", "round_started_at": now,
        })
        return meta

    state = bus._update_meta_locked(room_id, update)["swarm_pilot"]
    return _expose_member_ids(room_id, state)


def _state(meta: dict[str, Any]) -> dict:
    state = meta.get("swarm_pilot")
    if not isinstance(state, dict):
        raise ValueError("room is not a swarm pilot")
    return state


def _member_ids(room_id: str, members: list[str]) -> dict[str, str]:
    """Return opaque IDs derived from the room and slot order.

    These IDs label slots only. The mapping key and all current operations
    still use the configured profile name; this does not support renaming a
    profile while preserving its operational identity.
    """
    return {
        member: "mem_" + hashlib.sha256(f"{room_id}:{ordinal}".encode("utf-8")).hexdigest()[:12]
        for ordinal, member in enumerate(members, start=1)
    }


def _expose_member_ids(room_id: str, state: dict) -> dict:
    """Validate schema-2 IDs, or derive schema-1 IDs without writing them."""
    exposed = dict(state)
    schema = exposed.get("schema")
    stored = exposed.get("member_ids")
    members = exposed.get("members")
    if not isinstance(members, list) or any(not isinstance(member, str) for member in members):
        raise ValueError("swarm pilot members are invalid")

    if schema == 1 and stored is None:
        exposed["member_ids"] = _member_ids(room_id, exposed["members"])
        return exposed
    if schema not in (1, 2):
        raise ValueError("unsupported swarm pilot schema")
    if (
        not isinstance(stored, dict)
        or set(stored) != set(members)
        or any(not isinstance(member_id, str) or not _MEMBER_ID_RE.fullmatch(member_id)
               for member_id in stored.values())
        or len(set(stored.values())) != len(stored)
    ):
        raise ValueError("swarm pilot member_ids are invalid")
    return exposed
