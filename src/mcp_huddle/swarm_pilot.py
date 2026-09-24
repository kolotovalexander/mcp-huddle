"""Small, durable four-mode swarm pilot built on Huddle room metadata.

This module owns only room state. The server remains responsible for posting
requests and waking CLI workers, so no message lock is held with the meta lock.
"""

from __future__ import annotations

import re
import time
from typing import Any

from . import bus


MODES = frozenset({"council", "team", "relay", "swarm"})
WORKSPACES = frozenset({"shared_only", "allow_subworktrees"})
_ROOM_ID_RE = re.compile(r"room_[0-9a-f]{8}\Z")
_FINGERPRINT_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


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
    if (not members or len(members) > 8 or len(set(members)) != len(members)
            or organizer in members or {"Human", "System"}.intersection(members)):
        raise ValueError("members must be non-empty, unique and exclude organizer")
    for member in members:
        bus._safe_path_component(member, "member")
    # A swarm survives organizer session exit. Ordinary rooms retain their
    # existing owner_pid/SessionEnd lifecycle.
    room_id = bus.create_room(name, organizer, 0, cwd, "", room_id=room_id)
    now = int(time.time())

    def init(meta: dict) -> dict:
        meta["swarm_pilot"] = {
            "schema": 1,
            "mode": mode,
            "goal": goal,
            "organizer": organizer,
            "members": members,
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
        if client_request_fingerprint:
            meta["swarm_pilot"]["client_request_fingerprint"] = client_request_fingerprint
        if plan_hash:
            meta["swarm_pilot"]["plan_hash"] = plan_hash
        return meta

    bus._update_meta_locked(room_id, init)
    for member in members:
        bus.invite_agent(room_id, member)
    return room_id


def status(room_id: str) -> dict:
    state = bus.get_room_info(room_id).get("swarm_pilot")
    if not isinstance(state, dict):
        raise ValueError("room is not a swarm pilot")
    return state


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

    return bus._update_meta_locked(room_id, update)["swarm_pilot"]


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
        if prior and prior["member"] == member and prior["value"] == value:
            return meta
        bucket[key] = {
            "member": member, "value": value,
            "version": (prior or {}).get("version", 0) + 1,
            "updated_at": int(time.time()),
        }
        return meta

    return bus._update_meta_locked(room_id, update)["swarm_pilot"]


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

    return bus._update_meta_locked(room_id, update)["swarm_pilot"]


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

    return bus._update_meta_locked(room_id, update)["swarm_pilot"]


def _state(meta: dict[str, Any]) -> dict:
    state = meta.get("swarm_pilot")
    if not isinstance(state, dict):
        raise ValueError("room is not a swarm pilot")
    return state
