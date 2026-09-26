"""Target resolution across the Claude session registry and Codex session
index -- never touches the real ~/.claude or ~/.codex."""

import json
import subprocess
import sys

import pytest

from mcp_huddle.delivery import targets


def _write_claude_session(directory, filename, **fields):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_text(json.dumps(fields))


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


@pytest.fixture
def claude_dir(tmp_path, monkeypatch):
    d = tmp_path / "claude-sessions"
    monkeypatch.setenv("MCP_HUDDLE_DELIVERY_CLAUDE_SESSIONS_DIR", str(d))
    return d


@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    d = tmp_path / "codex-home"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MCP_HUDDLE_DELIVERY_CODEX_HOME", str(d))
    return d


# ── Claude ───────────────────────────────────────────────────────────────────

def test_resolve_claude_by_session_id(claude_dir, tmp_path):
    sock = tmp_path / "a.sock"
    sock.write_text("")
    _write_claude_session(
        claude_dir, "1.json", pid=1, sessionId="sess-1", name="alice",
        status="running", messagingSocketPath=str(sock), cwd="/proj", entrypoint="cli", kind="claude",
    )
    t = targets.resolve("claude:sess-1")
    assert t.harness == "claude"
    assert t.id == "sess-1"
    assert t.name == "alice"
    assert t.cwd == "/proj"


def test_resolve_claude_by_name_when_live(claude_dir, tmp_path):
    import os
    sock = tmp_path / "b.sock"
    sock.write_text("")
    _write_claude_session(
        claude_dir, "2.json", pid=os.getpid(), sessionId="sess-2", name="bob",
        messagingSocketPath=str(sock), cwd="/proj",
    )
    t = targets.resolve("claude:bob")
    assert t.id == "sess-2"
    assert t.live is True


def test_resolve_claude_not_live_when_pid_dead(claude_dir, tmp_path):
    sock = tmp_path / "c.sock"
    sock.write_text("")
    _write_claude_session(
        claude_dir, "3.json", pid=_dead_pid(), sessionId="sess-3", name="carol",
        messagingSocketPath=str(sock), cwd="/proj",
    )
    t = targets.resolve("claude:sess-3")
    assert t.live is False


def test_resolve_claude_unknown_when_alive_but_socket_missing(claude_dir):
    """Regression for Codex review defect #2: a live pid whose socket can't
    be confirmed must be "unknown", never "dead" -- treating it as dead would
    let `auto` mode fall through to `claude.resume` against a session that's
    actually still running."""
    import os
    _write_claude_session(
        claude_dir, "4.json", pid=os.getpid(), sessionId="sess-4", name="dana",
        messagingSocketPath="/does/not/exist.sock", cwd="/proj",
    )
    t = targets.resolve("claude:sess-4")
    assert t.live is None
    assert t.extra["live_state"] == "unknown"


def test_resolve_claude_ambiguous_name(claude_dir, tmp_path):
    sock = tmp_path / "d.sock"
    sock.write_text("")
    _write_claude_session(claude_dir, "5.json", pid=_dead_pid(), sessionId="sess-5",
                           name="dup", messagingSocketPath=str(sock))
    _write_claude_session(claude_dir, "6.json", pid=_dead_pid(), sessionId="sess-6",
                           name="dup", messagingSocketPath=str(sock))
    with pytest.raises(targets.AmbiguousTarget) as exc_info:
        targets.resolve("claude:dup")
    assert set(exc_info.value.candidates) == {"claude:sess-5", "claude:sess-6"}


def test_resolve_claude_not_found(claude_dir):
    with pytest.raises(targets.TargetNotFound):
        targets.resolve("claude:nope")


# ── Codex ────────────────────────────────────────────────────────────────────

def _write_codex_index(codex_home, lines):
    path = codex_home / "session_index.jsonl"
    path.write_text("\n".join(json.dumps(l) for l in lines) + "\n")


def test_resolve_codex_by_id(codex_home):
    _write_codex_index(codex_home, [{"id": "th-1", "thread_name": "review", "updated_at": 1}])
    t = targets.resolve("codex:th-1")
    assert t.harness == "codex"
    assert t.id == "th-1"
    assert t.name == "review"


def test_resolve_codex_by_thread_name(codex_home):
    _write_codex_index(codex_home, [{"id": "th-2", "thread_name": "planning", "updated_at": 1}])
    t = targets.resolve("codex:planning")
    assert t.id == "th-2"


def test_resolve_codex_uri_form(codex_home):
    _write_codex_index(codex_home, [{"id": "th-3", "thread_name": "x", "updated_at": 1}])
    t = targets.resolve("codex://threads/th-3")
    assert t.id == "th-3"
    assert t.name == "x"


def test_resolve_codex_uri_form_unknown_id_still_resolves(codex_home):
    _write_codex_index(codex_home, [])
    t = targets.resolve("codex://threads/th-unknown")
    assert t.id == "th-unknown"
    assert t.name == ""


def test_resolve_codex_last_line_wins_for_repeated_id(codex_home):
    _write_codex_index(codex_home, [
        {"id": "th-4", "thread_name": "old-name", "updated_at": 1},
        {"id": "th-4", "thread_name": "new-name", "updated_at": 2},
    ])
    t = targets.resolve("codex:th-4")
    assert t.name == "new-name"


def test_resolve_codex_ambiguous_thread_name(codex_home):
    _write_codex_index(codex_home, [
        {"id": "th-5", "thread_name": "dup", "updated_at": 1},
        {"id": "th-6", "thread_name": "dup", "updated_at": 2},
    ])
    with pytest.raises(targets.AmbiguousTarget):
        targets.resolve("codex:dup")


def test_resolve_codex_not_found(codex_home):
    _write_codex_index(codex_home, [])
    with pytest.raises(targets.TargetNotFound):
        targets.resolve("codex:missing")


# ── Claude tri-state liveness + same-sessionId merge ────────────────────────

def test_resolve_claude_permission_error_is_unknown_not_alive(claude_dir, monkeypatch):
    """Regression for Codex review defect #2: `PermissionError` from the
    signal-0 existence probe must be "unknown", not treated as a confirmed
    live process (which would make `claude.native` look eligible even though
    we can't actually confirm the process is ours to reach)."""
    sock_dir = claude_dir.parent / "sockets"
    sock_dir.mkdir()
    sock = sock_dir / "perm.sock"
    sock.write_text("")
    _write_claude_session(
        claude_dir, "perm.json", pid=4242, sessionId="sess-perm", name="perm",
        messagingSocketPath=str(sock), cwd="/proj",
    )

    def fake_kill(pid, sig):
        raise PermissionError("not our process")

    monkeypatch.setattr(targets.os, "kill", fake_kill)
    t = targets.resolve("claude:sess-perm")
    assert t.live is None
    assert t.extra["live_state"] == "unknown"


def test_same_sessionid_in_two_entries_one_alive_is_alive(claude_dir, tmp_path):
    """Regression for Codex review defect #2's last requirement: the same
    sessionId open in another live registry entry must be treated as alive
    overall, even if a stale duplicate file for that id looks dead."""
    import os
    sock = tmp_path / "merge.sock"
    sock.write_text("")
    _write_claude_session(claude_dir, "stale.json", pid=_dead_pid(), sessionId="sess-merge",
                           name="stale-copy", messagingSocketPath=str(sock))
    _write_claude_session(claude_dir, "fresh.json", pid=os.getpid(), sessionId="sess-merge",
                           name="fresh-copy", messagingSocketPath=str(sock))
    t = targets.resolve("claude:sess-merge")
    assert t.live is True  # not ambiguous, not shadowed by the dead duplicate


# ── Hermes peer vs. session target model ────────────────────────────────────

def test_resolve_hermes_explicit_peer_prefix():
    t = targets.resolve("hermes:peer:desktop/reviewer")
    assert t.extra["hermes_kind"] == "peer"
    assert t.extra["peer"] == "desktop"


def test_resolve_hermes_explicit_session_prefix():
    t = targets.resolve("hermes:session:sess-abc")
    assert t.extra["hermes_kind"] == "session"
    assert t.id == "sess-abc"
    assert "peer" not in t.extra


def test_resolve_hermes_bare_form_defaults_to_peer():
    t = targets.resolve("hermes:desktop")
    assert t.extra["hermes_kind"] == "peer"


# ── Strict id allowlist (defense in depth against argv injection) ──────────

def test_resolve_agy_rejects_flag_like_id():
    with pytest.raises(targets.TargetNotFound):
        targets.resolve("agy:--dangerously-skip-permissions")


def test_resolve_hermes_session_rejects_flag_like_id():
    with pytest.raises(targets.TargetNotFound):
        targets.resolve("hermes:session:--resume")


def test_resolve_opencode_rejects_flag_like_id():
    with pytest.raises(targets.TargetNotFound):
        targets.resolve("opencode:--session")


# ── Other harnesses (no registry; id passed through) ────────────────────────

def test_resolve_hermes_peer_and_agent():
    t = targets.resolve("hermes:desktop/reviewer")
    assert t.harness == "hermes"
    assert t.extra["peer"] == "desktop"
    assert t.extra["agent"] == "reviewer"


def test_resolve_hermes_peer_only():
    t = targets.resolve("hermes:desktop")
    assert t.extra["peer"] == "desktop"
    assert t.extra["agent"] == ""


def test_resolve_opencode_and_agy():
    assert targets.resolve("opencode:sess-9").id == "sess-9"
    assert targets.resolve("agy:conv-1").id == "conv-1"


# ── Bare name across harnesses ───────────────────────────────────────────────

def test_resolve_bare_name_matches_single_harness(claude_dir, codex_home, tmp_path):
    sock = tmp_path / "e.sock"
    sock.write_text("")
    _write_claude_session(claude_dir, "7.json", pid=_dead_pid(), sessionId="sess-7",
                           name="unique-name", messagingSocketPath=str(sock))
    _write_codex_index(codex_home, [])
    t = targets.resolve("unique-name")
    assert t.harness == "claude"
    assert t.id == "sess-7"


def test_resolve_bare_name_ambiguous_across_harnesses(claude_dir, codex_home, tmp_path):
    sock = tmp_path / "f.sock"
    sock.write_text("")
    _write_claude_session(claude_dir, "8.json", pid=_dead_pid(), sessionId="sess-8",
                           name="shared", messagingSocketPath=str(sock))
    _write_codex_index(codex_home, [{"id": "th-8", "thread_name": "shared", "updated_at": 1}])
    with pytest.raises(targets.AmbiguousTarget) as exc_info:
        targets.resolve("shared")
    assert set(exc_info.value.candidates) == {"claude:sess-8", "codex:th-8"}


def test_resolve_bare_name_not_found(claude_dir, codex_home):
    with pytest.raises(targets.TargetNotFound):
        targets.resolve("nobody")


def test_resolve_empty_target_not_found():
    with pytest.raises(targets.TargetNotFound):
        targets.resolve("")


def test_list_targets_filters_by_harness(claude_dir, codex_home, tmp_path):
    sock = tmp_path / "g.sock"
    sock.write_text("")
    _write_claude_session(claude_dir, "9.json", pid=_dead_pid(), sessionId="sess-9",
                           name="listed", messagingSocketPath=str(sock))
    _write_codex_index(codex_home, [{"id": "th-9", "thread_name": "listed2", "updated_at": 1}])

    only_claude = targets.list_targets("claude")
    assert [t.harness for t in only_claude] == ["claude"]

    everything = targets.list_targets()
    assert {t.harness for t in everything} == {"claude", "codex"}
