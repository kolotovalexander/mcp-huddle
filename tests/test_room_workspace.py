"""Room-owned write policy: one admin root, bounded CLI flags, Guard kept.

No model process is started: spawn.spawn_agent is replaced by a recorder, and
Git runs only against throwaway local repositories. The user's Claude
settings are replaced by a temporary CLAUDE_CONFIG_DIR.
"""

import json
import subprocess

import pytest

from mcp_huddle import bus, room_workspace, server, spawn


MCP_URL = "http://127.0.0.1:8014/mcp"
CLAUDE = {"name": "Claude", "enabled": True, "mcp_url": MCP_URL,
          "cmd": ["claude", "--dangerously-skip-permissions", "--model", "sonnet", "-p", "{brief}"]}
CODEX = {"name": "Codex", "enabled": True,
         "cmd": ["codex", "-a", "never", "exec", "--json",
                 "-s", "danger-full-access", "{brief}"]}
OPENCODE = {"name": "OpenCode", "enabled": True, "cmd": ["opencode", "run", "{brief}"]}
CODEX_PINNED = [
    "-c", "sandbox_workspace_write.exclude_tmpdir_env_var=true",
    "-c", "sandbox_workspace_write.exclude_slash_tmp=true",
    "-c", "sandbox_workspace_write.network_access=false",
]
GUARD = {"matcher": "Edit|Write|MultiEdit|NotebookEdit",
         "hooks": [{"type": "command", "command": "python3 ~/guard/rebuild_safety_hook.py"}]}
BASH_HOOK = {"matcher": "Bash", "hooks": [{"type": "command", "command": "bash-guard"}]}
USER_SETTINGS = {
    "hooks": {"PreToolUse": [GUARD, BASH_HOOK]},
    "permissions": {"allow": ["Bash(*)"], "deny": ["Read(~/.ssh/**)"], "ask": ["Write(**/.env)"],
                    "additionalDirectories": ["/"], "defaultMode": "bypassPermissions"},
}


def _after(cmd, flag):
    return cmd[cmd.index(flag) + 1]


def _git_repo(path):
    path.mkdir(parents=True)
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(path)]
    subprocess.run([*git, "init", "-q"], check=True)
    (path / "a.txt").write_text("a\n")
    subprocess.run([*git, "add", "a.txt"], check=True)
    subprocess.run([*git, "commit", "-qm", "init"], check=True)
    return str(path.resolve())


@pytest.fixture
def claude_settings(tmp_path, monkeypatch):
    config = tmp_path / "claude-config"
    config.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setenv("MCP_HUDDLE_CLAUDE_GUARD_COMMAND", "python3 ~/guard/rebuild_safety_hook.py")

    def write(settings):
        (config / "settings.json").write_text(json.dumps(settings))
    write(USER_SETTINGS)
    return write


@pytest.fixture
def repo(tmp_path, monkeypatch, claude_settings):
    root = _git_repo((tmp_path / "work" / "repo").resolve())
    monkeypatch.setenv(room_workspace.WRITE_ROOTS_ENV, root)
    return root


def _specs(claude=CLAUDE):
    return [spawn._apply_readonly(spec) for spec in (claude, CODEX, OPENCODE)]


@pytest.fixture
def registry(monkeypatch):
    # Mirror production: the effective registry is read-only transformed.
    specs = _specs()
    monkeypatch.setattr(spawn, "_raw_registry", lambda: specs)
    monkeypatch.setattr(spawn, "get_enabled_spec",
                        lambda name: next((s for s in specs if s["name"] == name), None))
    return specs


@pytest.fixture
def launches(monkeypatch):
    calls = []

    def fake_spawn(spec, prompt, cwd, log_dir, **kwargs):
        calls.append({"cmd": list(spec["cmd"]), "cwd": cwd})
        return 4242, str(log_dir / "x.log"), None

    monkeypatch.setattr(spawn, "spawn_agent", fake_spawn)
    return calls


def _create(cwd, members, **kwargs):
    return server.swarm_pilot_create(
        "pilot", "Organizer", "Edit code", "team", members, cwd=cwd, start=False, **kwargs,
    )["room_id"]


def test_write_room_launches_bounded_claude_and_codex(repo, registry, launches):
    room = _create(repo, ["Claude", "Codex"], write_policy="shared_write")
    meta = bus.get_room_info(room)
    assert meta["room_workspace"]["root"] == repo
    assert "you MAY edit files in the shared Git worktree" in server._swarm_pilot_request(room, "Claude")

    server._spawn_fresh_room_agent(room, "Claude", "go", meta, wake_id="w1")
    server._spawn_fresh_room_agent(room, "Codex", "go", meta, wake_id="w2")
    claude, codex = launches
    assert claude["cwd"] == repo and codex["cwd"] == repo

    c = claude["cmd"]
    assert c[1:1 + len(spawn._CLAUDE_WRITE_FLAGS)] == spawn._CLAUDE_WRITE_FLAGS
    assert "--restricted" in c and "--strict-mcp-config" in c and _after(c, "--setting-sources") == ""
    assert _after(c, "--tools").split(",") == ["Read", "Glob", "Grep", "Edit", "Write", "ToolSearch"]
    assert json.loads(_after(c, "--mcp-config")) == {
        "mcpServers": {"huddle": {"type": "http", "url": MCP_URL}}}
    assert _after(c, "--permission-mode") == "acceptEdits" and _after(c, "--permission-prompts") == "none"
    assert "--dangerously-skip-permissions" not in c and "--add-dir" not in c
    assert c[-2:] == ["-p", "{brief}"] and _after(c, "--model") == "sonnet"
    # The user's Guard survives --restricted; nothing that widens access does.
    assert json.loads(_after(c, "--settings")) == {
        "hooks": {"PreToolUse": [GUARD]},
        "permissions": {"deny": ["Read(~/.ssh/**)"], "ask": ["Write(**/.env)"]},
    }

    x = codex["cmd"]
    assert _after(x, "-s") == "workspace-write" and "danger-full-access" not in x
    i = x.index("sandbox_workspace_write.writable_roots=[]")
    assert x[i + 1:i + 1 + len(CODEX_PINNED)] == CODEX_PINNED


def test_ordinary_room_stays_read_only(repo, registry, launches):
    room = _create(repo, ["Claude", "Codex"])
    meta = bus.get_room_info(room)
    assert "room_workspace" not in meta
    assert "MAY edit" not in server._swarm_pilot_request(room, "Claude")
    server._spawn_fresh_room_agent(room, "Claude", "go", meta, wake_id="w1")
    server._spawn_fresh_room_agent(room, "Codex", "go", meta, wake_id="w2")
    claude, codex = launches
    assert claude["cmd"][1:9] == spawn._CLAUDE_RO_FLAGS
    assert _after(codex["cmd"], "-s") == "read-only"


def test_write_room_ignores_global_full_access_and_pins_resume(repo, registry, launches, monkeypatch):
    monkeypatch.setenv("MCP_HUDDLE_READONLY", "0")
    room = _create(repo, ["Codex"], write_policy="shared_write")
    server._spawn_fresh_room_agent(room, "Codex", "go", bus.get_room_info(room), wake_id="w")
    assert _after(launches[0]["cmd"], "-s") == "workspace-write"
    resume = spawn._codex_resume_sandbox_args([])
    assert resume[1] == 'sandbox_mode="workspace-write"'
    assert "sandbox_workspace_write.writable_roots=[]" in resume
    assert resume[-len(CODEX_PINNED):] == CODEX_PINNED


@pytest.mark.parametrize("case, match", [
    ("no_root", "write rooms are disabled"),
    ("two_roots", "exactly one"),
    ("second_repo", "not the admin-approved root"),
    ("broad_root", "too broad"),
    ("opencode", "unsupported write profile OpenCode"),
    ("claude_without_mcp_url", "loopback mcp_url"),
    ("claude_without_guard", "Guard hook for Edit, Write"),
    ("claude_advice_only", "Guard hook for Edit, Write"),
    ("claude_guard_unconfigured", "MCP_HUDDLE_CLAUDE_GUARD_COMMAND"),
    ("claude_guard_edit_only", "Guard hook for Write"),
    ("claude_hooks_disabled", "disable or omit hooks"),
    ("plain_dir", "git rev-parse failed|non-bare Git worktree"),
    ("subdir", "worktree top level"),
])
def test_write_room_rejected_before_room_exists(
        repo, registry, tmp_path, monkeypatch, claude_settings, case, match):
    cwd, members = repo, ["Claude"]
    if case == "no_root":
        monkeypatch.delenv(room_workspace.WRITE_ROOTS_ENV)
    elif case == "two_roots":
        other = _git_repo(tmp_path / "work" / "other")
        monkeypatch.setenv(room_workspace.WRITE_ROOTS_ENV, f"{repo}:{other}")
    elif case == "second_repo":
        # A participant of a room on the approved root cannot open another.
        cwd = _git_repo(tmp_path / "work" / "other")
    elif case == "broad_root":
        monkeypatch.setenv(room_workspace.WRITE_ROOTS_ENV, "/")
    elif case == "opencode":
        members = ["OpenCode"]
    elif case == "claude_without_mcp_url":
        no_url = {k: v for k, v in CLAUDE.items() if k != "mcp_url"}
        monkeypatch.setattr(spawn, "_raw_registry", lambda: _specs(no_url))
    elif case == "claude_without_guard":
        claude_settings({"hooks": {"PreToolUse": [BASH_HOOK]}})
    elif case == "claude_advice_only":
        claude_settings({"hooks": {"PreToolUse": [
            {"matcher": "Edit|Write", "hooks": [
                {"type": "command", "command": "python3 ~/guard/jev-risk-advice.py"}]}
        ]}})
    elif case == "claude_guard_unconfigured":
        monkeypatch.delenv("MCP_HUDDLE_CLAUDE_GUARD_COMMAND")
    elif case == "claude_guard_edit_only":
        claude_settings({"hooks": {"PreToolUse": [dict(GUARD, matcher="Edit")]}})
    elif case == "claude_hooks_disabled":
        claude_settings(dict(USER_SETTINGS, disableAllHooks=True))
    elif case == "plain_dir":
        plain = (tmp_path / "work" / "plain").resolve()
        plain.mkdir()
        cwd = str(plain)
        monkeypatch.setenv(room_workspace.WRITE_ROOTS_ENV, cwd)
    elif case == "subdir":
        cwd = str((tmp_path / "work" / "repo" / "subdir").resolve())
        (tmp_path / "work" / "repo" / "subdir").mkdir()
        monkeypatch.setenv(room_workspace.WRITE_ROOTS_ENV, cwd)
    before = len(bus.list_rooms())
    with pytest.raises(ValueError, match=match):
        _create(cwd, members, write_policy="shared_write")
    assert len(bus.list_rooms()) == before


def test_launch_fails_closed_when_guard_root_or_worktree_changes(
        repo, registry, launches, tmp_path, monkeypatch, claude_settings):
    room = _create(repo, ["Claude"], write_policy="shared_write")
    meta = bus.get_room_info(room)
    with pytest.raises(ValueError, match="write room refuses"):
        room_workspace.launch(meta, "Claude", OPENCODE)
    claude_settings({"hooks": {}})
    with pytest.raises(ValueError, match="Guard hook"):
        server._spawn_fresh_room_agent(room, "Claude", "go", meta, wake_id="w")
    claude_settings(USER_SETTINGS)
    monkeypatch.setenv(room_workspace.WRITE_ROOTS_ENV, _git_repo(tmp_path / "work" / "other"))
    with pytest.raises(ValueError, match="not the admin-approved root"):
        server._spawn_fresh_room_agent(room, "Claude", "go", meta, wake_id="w")
    monkeypatch.setenv(room_workspace.WRITE_ROOTS_ENV, repo)
    subprocess.run(["mv", repo, str(tmp_path / "work" / "moved")], check=True)
    with pytest.raises(ValueError):
        server._spawn_fresh_room_agent(room, "Claude", "go", meta, wake_id="w")
    assert launches == []


def test_allow_subworktrees_gives_member_private_cwd_and_shared_root(repo, registry, launches):
    room = _create(repo, ["Claude", "Codex"], write_policy="shared_write",
                   workspace_strategy="allow_subworktrees")
    subs = bus.get_room_info(room)["room_workspace"]["subworktrees"]
    assert set(subs) == {"Claude", "Codex"} and len(set(subs.values())) == 2
    for path in subs.values():
        top = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, check=True).stdout.strip()
        assert top == path

    meta = bus.get_room_info(room)
    server._spawn_fresh_room_agent(room, "Claude", "go", meta, wake_id="w1")
    server._spawn_fresh_room_agent(room, "Codex", "go", meta, wake_id="w2")
    claude, codex = launches
    assert claude["cwd"] == subs["Claude"] and _after(claude["cmd"], "--add-dir") == repo
    assert codex["cwd"] == subs["Codex"]
    assert f'sandbox_workspace_write.writable_roots=["{repo}"]' in codex["cmd"]
    assert room_workspace.resume(meta, "Codex") == (subs["Codex"], [repo])


def test_idempotent_write_request_reuses_room(repo, registry):
    kwargs = dict(cwd=repo, start=False, client_request_id="req-1")
    first = server.swarm_pilot_create(
        "pilot", "Organizer", "Edit", "team", ["Claude"], write_policy="shared_write", **kwargs)
    again = server.swarm_pilot_create(
        "pilot", "Organizer", "Edit", "team", ["Claude"], write_policy="shared_write", **kwargs)
    assert again["reused"] and again["room_id"] == first["room_id"]
    with pytest.raises(ValueError, match="conflict"):
        server.swarm_pilot_create("pilot", "Organizer", "Edit", "team", ["Claude"], **kwargs)
