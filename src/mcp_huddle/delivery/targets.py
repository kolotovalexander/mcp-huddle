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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

HARNESS_PREFIXES = ("claude", "codex", "hermes", "opencode", "agy")


class AmbiguousTarget(Exception):
    def __init__(self, candidates):
        candidates = list(candidates)
        super().__init__("ambiguous target: " + ", ".join(candidates))
        self.candidates = candidates


class TargetNotFound(Exception):
    pass


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


def _claude_live(session: dict) -> bool:
    pid = session.get("pid")
    sock = session.get("messagingSocketPath")
    if not pid or not sock:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # process exists, just owned by someone else
    except (TypeError, ValueError, OSError):
        return False
    return Path(sock).exists()


def _claude_target(session: dict) -> Target:
    return Target(
        harness="claude",
        id=str(session.get("sessionId", "")),
        name=str(session.get("name", "")),
        live=_claude_live(session),
        cwd=str(session.get("cwd", "")),
        socket_path=str(session.get("messagingSocketPath", "")),
        extra={
            "pid": session.get("pid"),
            "status": session.get("status"),
            "entrypoint": session.get("entrypoint"),
            "kind": session.get("kind"),
        },
    )


def _find_claude(query: str) -> list:
    sessions = _load_claude_sessions()
    by_id = [s for s in sessions if str(s.get("sessionId", "")) == query]
    if by_id:
        return [_claude_target(s) for s in by_id]
    by_name = [s for s in sessions if str(s.get("name", "")) == query]
    return [_claude_target(s) for s in by_name]


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
    return [_codex_target(tid, rec) for tid, rec in matches]


# ── Other harnesses (no discoverable registry; the id is taken as-is) ──────

def _hermes_target(spec: str) -> Target:
    peer, _, agent = spec.partition("/")
    return Target(harness="hermes", id=spec, name=spec, extra={"peer": peer, "agent": agent})


# ── Public API ───────────────────────────────────────────────────────────────

def resolve(to: str) -> Target:
    to = (to or "").strip()
    if not to:
        raise TargetNotFound("empty target")

    if to.startswith("codex://threads/"):
        thread_id = to[len("codex://threads/"):].strip()
        if not thread_id:
            raise TargetNotFound("empty codex thread id")
        idx = _load_codex_index()
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
            return _hermes_target(rest)
        if prefix == "opencode":
            return Target(harness="opencode", id=rest, name=rest)
        if prefix == "agy":
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
