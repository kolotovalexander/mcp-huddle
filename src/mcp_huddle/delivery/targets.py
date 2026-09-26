"""Resolve a ``to`` string to a concrete cross-harness :class:`Target`.

Accepted forms: ``claude:<name|sessionId>``, ``codex:<threadId|thread_name>``,
``codex://threads/<id>``, ``hermes:<peer[/agent]|session>``,
``opencode:<sessionId>``, ``agy:<conversationId>``, or a bare name searched
across Claude + Codex registries.

Registries read here never return tokens or file contents -- only the
metadata fields listed in docs/delivery.md.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

HARNESS_PREFIXES = ("claude", "codex", "hermes", "opencode", "agy")

# A caller-controlled id/peer/session that will end up as its own argv token
# (or a merged --flag=value) for a subprocess-based method must never be
# free-form: something like "--dangerously-skip-permissions" must never reach
# that far. This is defense in depth on top of the merged-flag argv templates
# in config.py -- never a leading '-' (which a CLI could parse as an option),
# and only characters that legitimate session ids / peer names / thread ids
# plausibly use.
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,199}$")
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class AmbiguousTarget(Exception):
    def __init__(self, candidates):
        candidates = list(candidates)
        super().__init__("ambiguous target: " + ", ".join(candidates))
        self.candidates = candidates


class TargetNotFound(Exception):
    pass


def _require_safe_id(harness: str, value: str) -> str:
    if not _SAFE_ID_RE.match(value or ""):
        raise TargetNotFound(f"invalid id for harness {harness!r}: {value!r}")
    return value


@dataclass
class Target:
    harness: str
    id: str
    name: str = ""
    live: Optional[bool] = None
    cwd: str = ""
    socket_path: str = ""
    extra: dict = field(default_factory=dict)


# ── Claude session registry ────────────────────────────────────────────────

def _claude_sessions_dir() -> Path:
    override = os.environ.get("MCP_HUDDLE_DELIVERY_CLAUDE_SESSIONS_DIR")
    if override:
        return Path(override)
    return Path.home() / ".claude" / "sessions"


def _load_claude_sessions() -> list:
    directory = _claude_sessions_dir()
    out = []
    if not directory.is_dir():
        return out
    for f in sorted(directory.glob("*.json")):
        try:
            data = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def _claude_pid_state(pid) -> str:
    """Tri-state read of whether ``pid`` is a live process: ``"alive"``,
    ``"dead"``, or ``"unknown"``. Only ``"dead"`` (``ProcessLookupError``, a
    definitive "no such process") ever licenses ``claude.resume`` -- anything
    else (including ``PermissionError``, which some sandboxes/hardened setups
    raise even for a signal-0 existence probe) must not be treated as proof
    of death."""
    if not pid:
        return "unknown"
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return "dead"
    except PermissionError:
        return "unknown"
    except (TypeError, ValueError, OSError):
        return "unknown"
    return "alive"


def _claude_session_state(session: dict) -> str:
    """Tri-state liveness for one registry entry: ``"alive"`` (native is
    reachable), ``"dead"`` (resume is safe), or ``"unknown"`` (neither --
    e.g. the process is alive but its messaging socket can't be confirmed,
    which used to be misreported as "not live" and could send `claude.resume`
    at a live session). Process-alive-ness and socket-reachability are
    checked separately on purpose: only "alive + socket reachable" is
    `claude.native`-eligible, and only a *confirmed* dead pid is
    `claude.resume`-eligible."""
    pid = session.get("pid")
    sock = session.get("messagingSocketPath")
    proc_state = _claude_pid_state(pid)
    if proc_state != "alive":
        return proc_state  # "dead" or "unknown"
    if not sock or not Path(sock).exists():
        # Process is confirmably alive, but we can't confirm the socket is
        # reachable -- unknown, not dead. Resuming here could fork a live
        # session's history.
        return "unknown"
    return "alive"


def _merge_claude_states(sessions: list) -> str:
    """Merge the tri-states of several registry entries that share the same
    sessionId (e.g. a stale file left behind by a prior process alongside a
    fresh one for the current process). If *any* entry independently reads
    as alive, the sessionId as a whole is alive -- a live session must never
    be treated as resumable just because one on-disk record about it looks
    stale."""
    states = [_claude_session_state(s) for s in sessions]
    if "alive" in states:
        return "alive"
    if "unknown" in states:
        return "unknown"
    return "dead"


def _state_to_live(state: str) -> Optional[bool]:
    return {"alive": True, "dead": False}.get(state)  # "unknown" -> None


def _claude_target(session: dict, state: Optional[str] = None) -> Target:
    if state is None:
        state = _claude_session_state(session)
    return Target(
        harness="claude",
        id=str(session.get("sessionId", "")),
        name=str(session.get("name", "")),
        live=_state_to_live(state),
        cwd=str(session.get("cwd", "")),
        socket_path=str(session.get("messagingSocketPath", "")),
        extra={
            "pid": session.get("pid"),
            "status": session.get("status"),
            "entrypoint": session.get("entrypoint"),
            "kind": session.get("kind"),
            "live_state": state,
        },
    )


def _merge_claude_group(group: list) -> Target:
    """Build one :class:`Target` for a set of registry entries that all share
    the same sessionId. Same rule for the id path and the name path: a stale
    duplicate must never shadow a live entry for the same sessionId."""
    if len(group) == 1:
        return _claude_target(group[0])
    merged_state = _merge_claude_states(group)
    pick = next((s for s in group if _claude_session_state(s) == merged_state), group[0])
    return _claude_target(pick, state=merged_state)


def _find_claude(query: str) -> list:
    sessions = _load_claude_sessions()
    by_id = [s for s in sessions if str(s.get("sessionId", "")) == query]
    if by_id:
        # Same sessionId in more than one registry file: not genuinely
        # ambiguous (it's one logical session), so merge rather than raising
        # AmbiguousTarget -- and never let a live entry be shadowed by a
        # stale/dead one for the same id.
        return [_merge_claude_group(by_id)]
    by_name = [s for s in sessions if str(s.get("name", "")) == query]
    if not by_name:
        return []
    # A name match only tells us which sessionId(s) to resolve -- the actual
    # liveness/socket must come from the SAME merge across every registry
    # entry for that sessionId (not just the ones that happen to still carry
    # this name), otherwise a stale record under an old name can shadow a
    # live entry for the same session (Codex review finding A).
    seen_ids: list = []
    for s in by_name:
        sid = str(s.get("sessionId", ""))
        if sid not in seen_ids:
            seen_ids.append(sid)
    out = []
    for sid in seen_ids:
        group = [s for s in sessions if str(s.get("sessionId", "")) == sid]
        out.append(_merge_claude_group(group))
    return out


# ── Codex session index ─────────────────────────────────────────────────────

def _codex_home() -> Path:
    override = os.environ.get("MCP_HUDDLE_DELIVERY_CODEX_HOME")
    if override:
        return Path(override)
    return Path.home() / ".codex"


def _load_codex_index() -> dict:
    path = _codex_home() / "session_index.jsonl"
    idx: dict = {}
    if not path.is_file():
        return idx
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        rid = rec.get("id")
        if rid:
            idx[rid] = rec  # later lines overwrite earlier -> "last line wins"
    return idx


def _codex_target(thread_id: str, rec: Optional[dict]) -> Target:
    rec = rec or {}
    return Target(
        harness="codex",
        id=str(thread_id),
        name=str(rec.get("thread_name", "")),
        live=None,
        extra={"updated_at": rec.get("updated_at")},
    )


def _find_codex(query: str) -> list:
    idx = _load_codex_index()
    if query in idx:
        return [_codex_target(query, idx[query])]
    matches = [(tid, rec) for tid, rec in idx.items() if rec.get("thread_name") == query]
    if not matches and _UUID_RE.match(query):
        # Unnamed threads have no session_index entry; a well-formed thread
        # UUID is still a valid `codex queue --thread` target.
        return [_codex_target(query, None)]
    return [_codex_target(tid, rec) for tid, rec in matches]


# ── Other harnesses (no discoverable registry; the id is taken as-is) ──────

def _hermes_target(spec: str, kind: str) -> Target:
    """``kind`` is ``"peer"`` (a DM-able peer[/agent], usable only with
    ``hermes.native``) or ``"session"`` (a resumable session id, usable only
    with ``hermes.resume``). A hermes peer name is never a session id and
    must never be substituted for one -- see docs/delivery.md."""
    if kind == "session":
        return Target(harness="hermes", id=spec, name=spec, extra={"hermes_kind": "session"})
    peer, _, agent = spec.partition("/")
    return Target(harness="hermes", id=spec, name=spec,
                   extra={"peer": peer, "agent": agent, "hermes_kind": "peer"})


# ── Public API ───────────────────────────────────────────────────────────────

def resolve(to: str) -> Target:
    to = (to or "").strip()
    if not to:
        raise TargetNotFound("empty target")

    if to.startswith("codex://threads/"):
        thread_id = to[len("codex://threads/"):].strip()
        idx = _load_codex_index()
        if thread_id not in idx and not _UUID_RE.match(thread_id):
            raise TargetNotFound(f"codex thread id must be a UUID or indexed: {thread_id!r}")
        return _codex_target(thread_id, idx.get(thread_id))

    prefix, sep, rest = to.partition(":")
    if sep and prefix in HARNESS_PREFIXES:
        rest = rest.strip()
        if not rest:
            raise TargetNotFound(f"empty id for harness {prefix!r}")
        if prefix == "claude":
            matches = _find_claude(rest)
            if not matches:
                raise TargetNotFound(f"no claude session matches {rest!r}")
            if len(matches) > 1:
                raise AmbiguousTarget(f"claude:{m.id}" for m in matches)
            return matches[0]
        if prefix == "codex":
            matches = _find_codex(rest)
            if not matches:
                raise TargetNotFound(f"no codex thread matches {rest!r}")
            if len(matches) > 1:
                raise AmbiguousTarget(f"codex:{m.id}" for m in matches)
            return matches[0]
        if prefix == "hermes":
            if rest.startswith("peer:"):
                sub = rest[len("peer:"):].strip()
                if not sub:
                    raise TargetNotFound("empty hermes peer")
                _require_safe_id("hermes", sub)
                return _hermes_target(sub, kind="peer")
            if rest.startswith("session:"):
                sub = rest[len("session:"):].strip()
                if not sub:
                    raise TargetNotFound("empty hermes session id")
                _require_safe_id("hermes", sub)
                return _hermes_target(sub, kind="session")
            # Backward-compatible bare form: `hermes:<peer[/agent]>` is a peer
            # target (the original, still-supported syntax).
            _require_safe_id("hermes", rest)
            return _hermes_target(rest, kind="peer")
        if prefix == "opencode":
            _require_safe_id("opencode", rest)
            return Target(harness="opencode", id=rest, name=rest)
        if prefix == "agy":
            _require_safe_id("agy", rest)
            return Target(harness="agy", id=rest, name=rest)

    # Bare name: search across Claude + Codex.
    claude_matches = _find_claude(to)
    codex_matches = _find_codex(to)
    total = claude_matches + codex_matches
    if not total:
        raise TargetNotFound(f"no session matches {to!r}")
    if len(total) > 1:
        candidates = [f"claude:{m.id}" for m in claude_matches] + [f"codex:{m.id}" for m in codex_matches]
        raise AmbiguousTarget(candidates)
    return total[0]


def list_targets(harness: str = "") -> list:
    out = []
    if not harness or harness == "claude":
        out.extend(_claude_target(s) for s in _load_claude_sessions())
    if not harness or harness == "codex":
        idx = _load_codex_index()
        out.extend(_codex_target(tid, rec) for tid, rec in idx.items())
    return out
