"""Room-owned write workspace for swarm pilot rooms.

A room is read-only unless its metadata carries ``room_workspace`` with
``write_policy == "shared_write"``. That record names one validated local Git
worktree (the shared workspace) and, with ``allow_subworktrees``, one detached
Huddle-created subworktree per member. Every launch re-validates the paths
and derives the member's CLI permissions from this record via
``spawn.apply_room_write_policy``; nothing here reads MCP_HUDDLE_READONLY.

Authorization: a room caller (any MCP participant) cannot choose a write area.
MCP callers are not authenticated per room, so a participant of one write
room could otherwise create another room for a different root. The pilot
boundary is therefore ONE exact Git worktree root per server: the server admin
sets ``MCP_HUDDLE_WRITE_ROOTS`` to a single canonical absolute path in the
server's own environment. Unset, empty, several entries, or a broad path
(filesystem root, home or its ancestors) all disable write rooms. The root is
checked at creation and again at every launch, so changing it and restarting
the server revokes older rooms. The organizer name is never authority.
With ``allow_subworktrees`` Huddle also creates detached copies of that same
repository under its own home; they are not additional user roots.

Huddle never deletes a worktree it created; removal stays with the owner
(``git worktree remove``).
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from . import bus, spawn


READ_ONLY = "read_only"
SHARED_WRITE = "shared_write"
WRITE_POLICIES = frozenset({READ_ONLY, SHARED_WRITE})
_MEMBER_ID_RE = re.compile(r"mem_[0-9a-f]{12}\Z")
_GIT_TIMEOUT_SEC = 20
WRITE_ROOTS_ENV = "MCP_HUDDLE_WRITE_ROOTS"


def admin_write_root() -> str:
    """Return the single admin-approved write root or raise (write disabled)."""
    entries = [raw for raw in os.environ.get(WRITE_ROOTS_ENV, "").split(os.pathsep) if raw]
    if not entries:
        raise ValueError(
            f"write rooms are disabled: the server admin has not set {WRITE_ROOTS_ENV}"
        )
    if len(entries) != 1:
        raise ValueError(
            f"write rooms are disabled: {WRITE_ROOTS_ENV} must name exactly one "
            "worktree root until callers are authenticated per room"
        )
    entry = entries[0]
    if not os.path.isabs(entry) or str(Path(entry).resolve()) != entry:
        raise ValueError(f"write rooms are disabled: {WRITE_ROOTS_ENV} must be canonical and absolute")
    resolved = Path(entry)
    if resolved == Path(resolved.anchor) or Path.home().resolve().is_relative_to(resolved):
        raise ValueError(f"write rooms are disabled: {WRITE_ROOTS_ENV} is too broad")
    return entry


def _require_admin_allowed(path: str) -> None:
    if path != admin_write_root():
        raise ValueError(f"write room cwd is not the admin-approved root ({WRITE_ROOTS_ENV})")


def _git(cwd: str, *args: str) -> str:
    """Run a local git command with repository hooks and fsmonitor disabled."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
             "-C", cwd, *args],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=_GIT_TIMEOUT_SEC, env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"git is unavailable for the room workspace: {exc}") from None
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()[-1:] or ["failed"]
        raise ValueError(f"git {args[0]} failed: {detail[0][:200]}")
    return result.stdout


def _worktree_identity(path: str) -> tuple[str, str]:
    """Return (toplevel, common git dir) for a non-bare work tree."""
    out = _git(path, "rev-parse", "--path-format=absolute",
               "--is-bare-repository", "--show-toplevel", "--git-common-dir").splitlines()
    if len(out) != 3 or out[0].strip() != "false":
        raise ValueError("room workspace must be a non-bare Git worktree")
    return str(Path(out[1].strip()).resolve()), str(Path(out[2].strip()).resolve())


def validate_worktree(cwd: str) -> dict[str, str]:
    """Validate an admin-allowed ``cwd`` that is the top of a local worktree."""
    if not isinstance(cwd, str) or not cwd or not os.path.isabs(cwd):
        raise ValueError("write room cwd must be an absolute path")
    admin_write_root()  # fail before touching the filesystem when disabled
    if not spawn._WRITE_ROOT_RE.fullmatch(cwd):
        raise ValueError("write room cwd must be a plain ASCII path")
    path = Path(cwd)
    if not path.is_dir():
        raise ValueError("write room cwd must be an existing directory")
    resolved = str(path.resolve())
    if resolved != cwd:
        raise ValueError(f"write room cwd must be canonical: {resolved}")
    _require_admin_allowed(resolved)
    toplevel, common = _worktree_identity(resolved)
    if toplevel != resolved:
        raise ValueError(f"write room cwd must be the worktree top level: {toplevel}")
    return {"root": resolved, "common_dir": common}


def check_request(cwd: str, strategy: str, members: list[str],
                  specs: dict[str, dict | None]) -> dict[str, str]:
    """Reject an unsupported write request before any room is created."""
    checked = validate_worktree(cwd)
    extra = [checked["root"]] if strategy == "allow_subworktrees" else []
    for member in members:
        spec = specs.get(member)
        if spec is None:
            raise ValueError(f"write room member has no registry profile: {member}")
        try:
            spawn.apply_room_write_policy(spec, extra)
        except spawn.WritePolicyUnsupported as exc:
            raise ValueError(f"unsupported write profile {member}: {exc}") from None
    return checked


def _subworktree_path(room_id: str, member_id: str) -> Path:
    if not _MEMBER_ID_RE.fullmatch(member_id):
        raise ValueError("invalid member id for subworktree")
    bus._safe_path_component(room_id, "room_id")
    return Path(bus.HUDDLE_HOME).resolve() / "worktrees" / room_id / member_id


def _ensure_subworktree(root: str, common: str, path: Path) -> str:
    """Create (or reuse) one detached subworktree; never delete anything."""
    target = str(path)
    if not spawn._WRITE_ROOT_RE.fullmatch(target):
        raise ValueError("subworktree path must be a plain ASCII path")
    if path.exists():
        toplevel, existing_common = _worktree_identity(target)
        if toplevel != target or existing_common != common:
            raise ValueError("subworktree path is occupied by another checkout")
        return target
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(root, "worktree", "add", "--detach", target, "HEAD")
    toplevel, created_common = _worktree_identity(target)
    if toplevel != target or created_common != common:
        raise ValueError("created subworktree does not belong to the room repository")
    return target


def install(room_id: str, root: str, strategy: str, member_ids: dict[str, str]) -> dict:
    """Create subworktrees if selected and persist the room write policy."""
    common = validate_worktree(root)["common_dir"]
    subworktrees: dict[str, str] = {}
    if strategy == "allow_subworktrees":
        for member, member_id in member_ids.items():
            subworktrees[member] = _ensure_subworktree(
                root, common, _subworktree_path(room_id, member_id),
            )
    record = {
        "schema": 1,
        "write_policy": SHARED_WRITE,
        "root": root,
        "strategy": strategy,
        "subworktrees": subworktrees,
    }

    def update(meta: dict) -> dict:
        meta["room_workspace"] = record
        return meta

    bus._update_meta_locked(room_id, update)
    return record


def write_policy(meta: dict) -> str:
    workspace = meta.get("room_workspace")
    if workspace is None:
        return READ_ONLY
    if not isinstance(workspace, dict) or workspace.get("write_policy") != SHARED_WRITE:
        raise ValueError("room workspace record is invalid")
    return SHARED_WRITE


def _member_paths(meta: dict, agent_name: str) -> tuple[str, list[str]]:
    """Return (launch cwd, extra writable roots) after re-validating paths."""
    workspace = meta["room_workspace"]
    root = workspace.get("root")
    if not isinstance(root, str) or root != meta.get("cwd"):
        raise ValueError("room workspace root does not match the room cwd")
    common = validate_worktree(root)["common_dir"]
    if workspace.get("strategy") == "allow_subworktrees":
        sub = (workspace.get("subworktrees") or {}).get(agent_name)
        if not isinstance(sub, str):
            raise ValueError(f"no subworktree recorded for {agent_name}")
        toplevel, sub_common = _worktree_identity(sub)
        if toplevel != sub or sub_common != common:
            raise ValueError(f"subworktree for {agent_name} is no longer valid")
        return sub, [root]
    if workspace.get("strategy") != "shared_only":
        raise ValueError("room workspace strategy is invalid")
    return root, []


def launch(meta: dict, agent_name: str, spec: dict) -> tuple[dict, str]:
    """Return the (spec, cwd) a fresh room launch must use.

    Read-only rooms get the registry spec and room cwd unchanged. Write rooms
    get the bounded write variant or raise before any process is started.
    """
    if write_policy(meta) == READ_ONLY:
        return spec, meta.get("cwd", "") or ""
    cwd, extra = _member_paths(meta, agent_name)
    try:
        return spawn.apply_room_write_policy(spec, extra), cwd
    except spawn.WritePolicyUnsupported as exc:
        raise ValueError(f"write room refuses {agent_name}: {exc}") from None


def resume(meta: dict, agent_name: str) -> tuple[str, list[str] | None]:
    """Return (cwd, workspace_write_roots) for ``spawn.codex_resume``."""
    if write_policy(meta) == READ_ONLY:
        return meta.get("cwd", "") or "", None
    return _member_paths(meta, agent_name)


def brief_note(meta: dict, agent_name: str) -> str:
    if write_policy(meta) == READ_ONLY:
        return ""
    workspace = meta["room_workspace"]
    root = workspace["root"]
    sub = (workspace.get("subworktrees") or {}).get(agent_name)
    lines = [
        f"\nWorkspace: you MAY edit files in the shared Git worktree {root}.",
        "Writes outside it (including /tmp) are denied; shell commands are not "
        "available to Claude and Codex runs sandboxed without network. "
        "Coordinate file ownership with peers via "
        "swarm_pilot_record before editing; resolve conflicts yourselves.",
    ]
    if sub:
        lines.append(
            f"Your private detached subworktree is {sub} (your cwd). Draft there, "
            f"then transfer finished changes into {root} yourself."
        )
    return "\n".join(lines)
