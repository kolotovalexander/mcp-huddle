"""Tests for Phase 1 (per-agent brief, log capture) and Phase 2 (Codex
thread_id parsing, codex_resume helper, /api/room_agents and SSE endpoint)."""
import json
import os
import subprocess
import sys
import tempfile
import time
import importlib
from pathlib import Path

import pytest

from mcp_huddle import bus, spawn
from mcp_huddle import server


# ── Phase 1: spawn.py ─────────────────────────────────────────────────────────

def test_spawn_agent_writes_log_file(tmp_path: Path, monkeypatch) -> None:
    """spawn_agent must redirect stdout/stderr to <log_dir>/<name>.events.jsonl
    instead of DEVNULL. We use `echo` as a stand-in for codex/gemini."""
    spec: spawn.SpawnSpec = {
        "name": "Echo",
        "cmd": ["echo", "hello {brief}"],
        "enabled": True,
    }
    log_dir = tmp_path / "agents"
    pid, log_path, last_msg = spawn.spawn_agent(spec, "world", str(tmp_path), log_dir)
    assert pid > 0
    # Wait for the spawned echo to flush + exit.
    import time
    for _ in range(20):
        if Path(log_path).exists() and Path(log_path).stat().st_size > 0:
            break
        time.sleep(0.05)
    content = Path(log_path).read_text()
    assert "hello world" in content
    assert last_msg is None  # Echo spec doesn't reference {last_message}


def test_spawn_agent_substitutes_last_message_path(tmp_path: Path) -> None:
    """{last_message} placeholder in cmd must be replaced with a real path."""
    spec: spawn.SpawnSpec = {
        "name": "Probe",
        "cmd": ["echo", "{brief}", "out={last_message}"],
        "enabled": True,
    }
    log_dir = tmp_path / "agents"
    _, log_path, last_msg = spawn.spawn_agent(spec, "x", str(tmp_path), log_dir)
    assert last_msg is not None
    assert last_msg.endswith("probe.last_message.txt")
    import time
    for _ in range(20):
        if Path(log_path).stat().st_size > 0:
            break
        time.sleep(0.05)
    # The echo'd substituted argv should appear in the log.
    assert "probe.last_message.txt" in Path(log_path).read_text()


def test_spawn_all_per_agent_briefs(tmp_path: Path, monkeypatch) -> None:
    """spawn_all must use per-agent brief from the briefs dict."""
    fake_registry: list[spawn.SpawnSpec] = [
        {"name": "Alpha", "cmd": ["echo", "alpha={brief}"], "enabled": True},
        {"name": "Beta",  "cmd": ["echo", "beta={brief}"],  "enabled": True},
    ]
    monkeypatch.setattr(spawn, "load_registry", lambda: fake_registry)
    # Alpha/Beta both resolve to the same effective binary ("echo") — disable
    # the same-binary stagger so this brief-substitution test isn't slowed
    # down by (or coupled to) unrelated staggering behavior.
    monkeypatch.setenv("MCP_HUDDLE_SAME_BIN_STAGGER_SEC", "0")
    log_dir = tmp_path / "agents"
    names, pids, agent_meta = spawn.spawn_all(
        brief="DEFAULT", cwd=str(tmp_path), log_dir=log_dir,
        briefs={"Alpha": "BRIEF-A"},
    )
    assert sorted(names) == ["Alpha", "Beta"]
    import time
    time.sleep(0.3)
    alpha_log = Path(agent_meta["Alpha"]["log_path"]).read_text()
    beta_log = Path(agent_meta["Beta"]["log_path"]).read_text()
    assert "alpha=BRIEF-A" in alpha_log
    assert "beta=DEFAULT" in beta_log  # falls back to default brief


def test_spawn_all_skip_names_excludes_owner(tmp_path: Path, monkeypatch) -> None:
    """skip_names removes matching specs so the room owner isn't duplicated."""
    fake_registry: list[spawn.SpawnSpec] = [
        {"name": "Codex",       "cmd": ["echo", "codex={brief}"],       "enabled": True},
        {"name": "Antigravity", "cmd": ["echo", "antigravity={brief}"], "enabled": True},
        {"name": "Claude",      "cmd": ["echo", "claude={brief}"],      "enabled": True},
    ]
    monkeypatch.setattr(spawn, "load_registry", lambda: fake_registry)
    monkeypatch.setenv("MCP_HUDDLE_SAME_BIN_STAGGER_SEC", "0")  # all 3 use "echo"
    names, _, _ = spawn.spawn_all(
        brief="B", cwd=str(tmp_path), log_dir=tmp_path / "agents",
        skip_names={"Codex"},
    )
    assert sorted(names) == ["Antigravity", "Claude"]
    assert "Codex" not in names


def test_default_registry_roster() -> None:
    """The current canonical agent roster must be in DEFAULT_REGISTRY (enabled
    depends on which binaries the test host has installed). Qwen and DeepSeek
    were retired; the active roster is Claude / Codex / Antigravity / MiMo."""
    spec_names = {s["name"] for s in spawn.DEFAULT_REGISTRY}
    assert spec_names == {
        "Codex", "Antigravity", "MiMo", "OpenCode", "Claude",
        "Claude Opus 5 (direct review)",
    }


def test_opencode_slot_is_opt_in_timeout_wrapped_and_uses_local_model_config() -> None:
    spec = next(s for s in spawn.DEFAULT_REGISTRY if s["name"] == "OpenCode")
    assert spec["enabled"] == (
        spawn._OPENCODE_BIN is not None
        and spawn._OPENCODE_TIMEOUT_BIN is not None
        and os.environ.get("MCP_HUDDLE_OPENCODE_ENABLED", "0") == "1"
    )
    assert spec["cmd"][0] == (spawn._OPENCODE_TIMEOUT_BIN or "timeout")
    assert spec["cmd"][1] == str(spawn._OPENCODE_TIMEOUT_SEC)
    assert "-m" not in spec["cmd"]
    assert not any("9router" in arg for arg in spec["cmd"])


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("not-a-number", "1200"), ("0", "1200"), ("-1", "1200"), ("45", "45")],
)
def test_opencode_timeout_env_cannot_break_server_import(
    raw: str, expected: str,
) -> None:
    env = os.environ.copy()
    env["MCP_HUDDLE_OPENCODE_TIMEOUT_SEC"] = raw
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")

    result = subprocess.run(
        [sys.executable, "-c", "from mcp_huddle import spawn; print(spawn._OPENCODE_TIMEOUT_SEC)"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


def test_mimo_spec_uses_runner() -> None:
    """MiMo huddle slot goes through mimo_runner (MCP hangs upstream in `mimo
    run` 0.1.x, so MiMo cannot call huddle MCP tools itself) and is opt-in:
    default OFF after unreliable headless output, gated by env flag."""
    spec = next(s for s in spawn.DEFAULT_REGISTRY if s["name"] == "MiMo")
    assert "mcp_huddle.mimo_runner" in spec["cmd"]
    assert "{brief}" in spec["cmd"][-1]
    if spawn._MIMO_BIN is None:
        assert spec["enabled"] is False
    else:
        expected = os.environ.get("MCP_HUDDLE_MIMO_ENABLED", "0") == "1"
        assert spec["enabled"] == expected


def test_claude_slot_is_opt_in_off_by_default() -> None:
    """Claude headless slot must default to disabled: since 2026-06-15 `claude -p`
    is metered against a separate Agent SDK credit pool, not the subscription.
    enabled requires BOTH the binary present AND MCP_HUDDLE_CLAUDE_ENABLED=1.
    Tests run without that env var, so the slot must be off regardless of host."""
    spec = next(s for s in spawn.DEFAULT_REGISTRY if s["name"] == "Claude")
    expected = (
        spawn._CLAUDE_BIN is not None
        and os.environ.get("MCP_HUDDLE_CLAUDE_ENABLED", "0") != "0"
    )
    assert spec["enabled"] == expected
    if "MCP_HUDDLE_CLAUDE_ENABLED" not in os.environ:
        assert spec["enabled"] is False


# ── On-disk registry file (~/.mcp-huddle/registry.json) merge precedence ──────

def _isolate_registry(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    """Point the huddle home at a tmp dir and clear the env override so the
    on-disk registry file is the only override in play. Disable read-only mode
    here so these merge-precedence tests see raw specs (read-only has its own
    tests)."""
    monkeypatch.setattr(bus, "HUDDLE_HOME", home)
    monkeypatch.delenv("MCP_HUDDLE_SPAWN_REGISTRY", raising=False)
    monkeypatch.setenv("MCP_HUDDLE_READONLY", "0")


def test_registry_file_absent_uses_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No file → _raw_registry is exactly DEFAULT_REGISTRY (unchanged)."""
    _isolate_registry(monkeypatch, tmp_path)
    assert spawn._raw_registry() == list(spawn.DEFAULT_REGISTRY)


def test_registry_file_appends_new_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file entry with a brand-new name is appended after the defaults."""
    _isolate_registry(monkeypatch, tmp_path)
    new_spec = {"name": "MyModel", "cmd": ["mymodel", "-p", "{brief}"], "enabled": True}
    (tmp_path / "registry.json").write_text(json.dumps([new_spec]))

    merged = spawn._raw_registry()
    # defaults preserved + new one appended at the end
    assert [s["name"] for s in merged] == [
        s["name"] for s in spawn.DEFAULT_REGISTRY
    ] + ["MyModel"]
    assert merged[-1] == new_spec


def test_registry_file_overrides_existing_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file entry reusing a default name replaces it in place (same position)."""
    _isolate_registry(monkeypatch, tmp_path)
    override = {"name": "Codex", "cmd": ["codex", "custom", "{brief}"], "enabled": False}
    (tmp_path / "registry.json").write_text(json.dumps([override]))

    merged = spawn._raw_registry()
    # No duplicate Codex, and order/length match the defaults (pure replacement)
    assert [s["name"] for s in merged] == [s["name"] for s in spawn.DEFAULT_REGISTRY]
    codex = next(s for s in merged if s["name"] == "Codex")
    assert codex == override


def test_registry_file_malformed_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Malformed file → stderr warning + fall back to defaults, never crash."""
    _isolate_registry(monkeypatch, tmp_path)
    (tmp_path / "registry.json").write_text("{ this is not valid json ]")

    merged = spawn._raw_registry()
    assert merged == list(spawn.DEFAULT_REGISTRY)
    assert "ignoring registry file" in capsys.readouterr().err


def test_registry_file_non_array_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Valid JSON but not an array of named specs → warn + ignore."""
    _isolate_registry(monkeypatch, tmp_path)
    (tmp_path / "registry.json").write_text(json.dumps({"name": "Oops"}))

    assert spawn._raw_registry() == list(spawn.DEFAULT_REGISTRY)
    assert "ignoring registry file" in capsys.readouterr().err


def test_env_override_wins_over_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP_HUDDLE_SPAWN_REGISTRY (full replacement) beats the on-disk file."""
    monkeypatch.setattr(bus, "HUDDLE_HOME", tmp_path)
    file_spec = {"name": "FromFile", "cmd": ["x"], "enabled": True}
    (tmp_path / "registry.json").write_text(json.dumps([file_spec]))
    env_spec = {"name": "FromEnv", "cmd": ["y"], "enabled": True}
    env_path = tmp_path / "env_registry.json"
    env_path.write_text(json.dumps([env_spec]))
    monkeypatch.setenv("MCP_HUDDLE_SPAWN_REGISTRY", str(env_path))

    merged = spawn._raw_registry()
    assert merged == [env_spec]


def test_load_registry_merges_file_and_filters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: a file-added enabled spec with no probe is available, and a
    disabled one is filtered out by load_registry."""
    _isolate_registry(monkeypatch, tmp_path)
    specs = [
        {"name": "OnModel", "cmd": ["true"], "enabled": True},
        {"name": "OffModel", "cmd": ["true"], "enabled": False},
    ]
    (tmp_path / "registry.json").write_text(json.dumps(specs))

    names = {s["name"] for s in spawn.load_registry()}
    assert "OnModel" in names
    assert "OffModel" not in names


def test_discovery_summary_reports_reasons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """discovery_summary yields one line per agent with enabled/disabled+reason."""
    _isolate_registry(monkeypatch, tmp_path)
    specs = [
        {"name": "Ready", "cmd": ["true"], "enabled": True},
        {"name": "NoBin", "cmd": ["definitely-not-a-real-binary-xyz"], "enabled": False},
        {"name": "OffFlag", "cmd": ["true"], "enabled": False},
    ]
    (tmp_path / "registry.json").write_text(json.dumps(specs))

    summary = {
        line.split(" -> ", 1)[0]: line.split(" -> ", 1)[1]
        for line in spawn.discovery_summary()
    }
    assert summary["Ready"] == "enabled"
    assert summary["NoBin"] == "disabled (binary not found)"
    assert summary["OffFlag"] == "disabled (off by env flag)"


def test_qwen_probe_requires_exact_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Qwen must auto-spawn only when the local bridge exposes the max model."""

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"data":[{"id":"qwen3.7-plus"},{"id":"qwen3.7-max"}]}'

    monkeypatch.setattr(spawn.urlrequest, "urlopen", lambda _url, timeout: Response())

    assert spawn._spawn_spec_available({
        "name": "Qwen",
        "cmd": ["qwen"],
        "enabled": True,
        "probe_url": "http://127.0.0.1:3264/api/models",
        "requires_model": "qwen3.7-max",
    })
    assert not spawn._spawn_spec_available({
        "name": "Qwen",
        "cmd": ["qwen"],
        "enabled": True,
        "probe_url": "http://127.0.0.1:3264/api/models",
        "requires_model": "qwen-missing",
    })


def test_qwen_probe_requires_working_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    """A live /models endpoint is not enough; chat must answer too."""

    class Response:
        def __init__(self, body: bytes):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return self.body

    calls: list[str] = []

    def fake_urlopen(req, timeout):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        calls.append(url)
        if url.endswith("/models"):
            return Response(b'{"data":[{"id":"qwen3.7-max"}]}')
        return Response(b'{"choices":[{"message":{"content":"OK"}}]}')

    spawn._PROBE_CACHE.clear()
    monkeypatch.setattr(spawn.urlrequest, "urlopen", fake_urlopen)

    assert spawn._spawn_spec_available({
        "name": "Qwen",
        "cmd": ["qwen"],
        "enabled": True,
        "probe_url": "http://127.0.0.1:3264/api/models",
        "requires_model": "qwen3.7-max",
        "probe_chat_url": "http://127.0.0.1:3264/api/chat/completions",
        "probe_chat_model": "qwen3.7-max",
    })
    assert calls == [
        "http://127.0.0.1:3264/api/models",
        "http://127.0.0.1:3264/api/chat/completions",
    ]


def test_spawn_agents_skips_owner_via_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """server._spawn_agents must pass owner into skip_names — verifies the
    end-to-end contract from room_create to spawn_all."""
    fake_registry: list[spawn.SpawnSpec] = [
        {"name": "Codex",       "cmd": ["echo", "codex"],       "enabled": True},
        {"name": "Antigravity", "cmd": ["echo", "antigravity"], "enabled": True},
        {"name": "Claude",      "cmd": ["echo", "claude"],      "enabled": True},
    ]
    monkeypatch.setattr(spawn, "load_registry", lambda: fake_registry)
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path / "huddle"))
    monkeypatch.setattr(bus, "HUDDLE_HOME", tmp_path / "huddle")
    monkeypatch.setenv("MCP_HUDDLE_SAME_BIN_STAGGER_SEC", "0")  # all 3 use "echo"

    captured: dict = {}
    real_spawn_all = spawn.spawn_all

    def capture(*args, **kwargs):
        captured["skip_names"] = kwargs.get("skip_names")
        return real_spawn_all(*args, **kwargs)

    monkeypatch.setattr(spawn, "spawn_all", capture)

    room_id = bus.create_room("test-room", "Claude", os.getpid(), str(tmp_path), "sess-1")
    server._spawn_agents(
        room_id=room_id, name="test-room", goal="g",
        cwd=str(tmp_path), owner="Claude", auto_spawn=True,
    )
    assert captured["skip_names"] == {"Claude"}


def test_spawn_all_logs_unexpected_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Unexpected OSError subclasses must be logged, not silently swallowed."""
    fake_registry: list[spawn.SpawnSpec] = [
        {"name": "Gemini", "cmd": ["gemini", "-p", "{brief}"], "enabled": True},
    ]
    monkeypatch.setattr(spawn, "load_registry", lambda: fake_registry)

    def broken_spawn(*args, **kwargs):
        raise BlockingIOError("daemon spawn pipe blocked")

    monkeypatch.setattr(spawn, "spawn_agent", broken_spawn)

    with pytest.raises(BlockingIOError):
        spawn.spawn_all("brief", str(tmp_path), tmp_path / "agents")

    err = capsys.readouterr().err
    assert "failed to spawn Gemini" in err
    assert "BlockingIOError" in err
    assert "daemon spawn pipe blocked" in err


# ── Same-binary spawn stagger (spawn.compute_stagger_delays) ────────────────

def test_effective_binary_unwraps_timeout_wrapper() -> None:
    """A `timeout N <bin> ...` prefix must resolve to the same effective
    binary as a bare invocation of <bin>, so a timeout-wrapped and unwrapped
    spec of the same underlying CLI are recognized as colliding."""
    assert spawn._effective_binary(["timeout", "240", "opencode", "run"]) == "opencode"
    assert spawn._effective_binary(
        ["/opt/homebrew/bin/timeout", "590s", "agy", "-p", "x"]
    ) == "agy"
    assert spawn._effective_binary(["opencode", "run"]) == "opencode"
    assert spawn._effective_binary([]) == ""
    # A flag between `timeout` and the duration (e.g. --foreground) is skipped too.
    assert spawn._effective_binary(
        ["timeout", "--foreground", "120", "codex", "exec"]
    ) == "codex"


def test_compute_stagger_delays_same_vs_different_binary() -> None:
    same_binary = [
        {"name": "A", "cmd": ["opencode", "run"], "enabled": True},
        {"name": "B", "cmd": ["opencode", "run"], "enabled": True},
        {"name": "C", "cmd": ["timeout", "240", "opencode", "run"], "enabled": True},
    ]
    delays = spawn.compute_stagger_delays(same_binary, stagger_sec=20.0)
    assert delays == {"A": 0.0, "B": 20.0, "C": 40.0}

    different_binaries = [
        {"name": "A", "cmd": ["codex", "exec"], "enabled": True},
        {"name": "B", "cmd": ["agy", "-p"], "enabled": True},
    ]
    assert spawn.compute_stagger_delays(different_binaries, stagger_sec=20.0) == {
        "A": 0.0, "B": 0.0,
    }

    # stagger_sec=0 (MCP_HUDDLE_SAME_BIN_STAGGER_SEC=0) disables it entirely.
    assert spawn.compute_stagger_delays(same_binary, stagger_sec=0.0) == {
        "A": 0.0, "B": 0.0, "C": 0.0,
    }


def test_spawn_all_staggers_second_same_binary_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """spawn_all must not start two same-binary specs at once (fixes
    OpenCode's local-SQLite 'database is locked' when two `opencode run`
    processes race). The second spec is scheduled via threading.Timer
    instead of spawned synchronously; its identity (log paths) is still
    registered in the return value immediately."""
    fake_registry: list[spawn.SpawnSpec] = [
        {"name": "First",  "cmd": ["echo", "first={brief}"],  "enabled": True},
        {"name": "Second", "cmd": ["echo", "second={brief}"], "enabled": True},
    ]
    monkeypatch.setattr(spawn, "load_registry", lambda: fake_registry)
    monkeypatch.setenv("MCP_HUDDLE_SAME_BIN_STAGGER_SEC", "20")

    scheduled: list[tuple] = []

    class FakeTimer:
        def __init__(self, delay, func, *a, **kw):
            scheduled.append((delay, func))

        def start(self):
            pass  # never actually fires — this test only asserts scheduling

        daemon = False

    monkeypatch.setattr(spawn.threading, "Timer", FakeTimer)

    names, pids, agent_meta = spawn.spawn_all(
        brief="B", cwd=str(tmp_path), log_dir=tmp_path / "agents",
    )

    assert sorted(names) == ["First", "Second"]
    # Only the immediate (non-delayed) spec actually spawned a real process.
    assert len(pids) == 1
    # The delayed spec's identity is registered right away regardless.
    assert agent_meta["Second"]["log_path"].endswith("second.events.jsonl")
    assert len(scheduled) == 1
    assert scheduled[0][0] == 20.0  # occurrence index 1 * stagger_sec


def test_spawn_all_no_stagger_for_different_binaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two specs resolving to DIFFERENT effective binaries must both spawn
    synchronously — the stagger only guards same-binary collisions."""
    fake_registry: list[spawn.SpawnSpec] = [
        {"name": "Alpha", "cmd": ["echo", "a={brief}"], "enabled": True},
        {"name": "Beta",  "cmd": ["true"], "enabled": True},
    ]
    monkeypatch.setattr(spawn, "load_registry", lambda: fake_registry)
    monkeypatch.setenv("MCP_HUDDLE_SAME_BIN_STAGGER_SEC", "20")

    scheduled: list[tuple] = []
    monkeypatch.setattr(
        spawn.threading, "Timer",
        lambda *a, **kw: scheduled.append(a) or type("T", (), {"start": lambda self: None})(),
    )

    names, pids, agent_meta = spawn.spawn_all(
        brief="B", cwd=str(tmp_path), log_dir=tmp_path / "agents",
    )

    assert sorted(names) == ["Alpha", "Beta"]
    assert len(pids) == 2  # both spawned synchronously — no stagger triggered
    assert scheduled == []


# ── Curated auto_spawn=True roster (SpawnSpec "auto" flag) ──────────────────

def test_auto_spawn_true_skips_spec_marked_auto_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """spawn_all (the auto_spawn=True path) must skip a spec with
    "auto": false even though it is enabled+available, while load_registry()
    itself keeps listing it (the flag only curates the auto-spawn roster, it
    doesn't disable the agent outright)."""
    fake_registry: list[spawn.SpawnSpec] = [
        {"name": "Included", "cmd": ["echo", "inc={brief}"], "enabled": True},
        {"name": "Excluded", "cmd": ["echo", "exc={brief}"], "enabled": True,
         "auto": False},
    ]
    monkeypatch.setattr(spawn, "load_registry", lambda: fake_registry)
    monkeypatch.setenv("MCP_HUDDLE_SAME_BIN_STAGGER_SEC", "0")  # both use "echo"

    names, _, _ = spawn.spawn_all(
        brief="B", cwd=str(tmp_path), log_dir=tmp_path / "agents",
    )
    assert names == ["Included"]
    assert "Excluded" not in names

    # load_registry() (the raw enabled+available registry) still lists it —
    # only the auto_spawn=True roster excludes it.
    assert {s["name"] for s in spawn.load_registry()} == {"Included", "Excluded"}


def test_dict_auto_spawn_ignores_auto_false_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit dict auto_spawn={name: brief} always spawns the named
    agent, even if its spec is marked "auto": false — a deliberately named
    agent always works (only auto_spawn=True consults the flag)."""
    fake_registry: list[spawn.SpawnSpec] = [
        {"name": "Excluded", "cmd": ["echo", "exc={brief}"], "enabled": True,
         "auto": False},
    ]
    monkeypatch.setattr(spawn, "load_registry", lambda: fake_registry)
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path / "huddle"))
    monkeypatch.setattr(bus, "HUDDLE_HOME", tmp_path / "huddle")

    room_id = bus.create_room("test-room", "Human", os.getpid(), str(tmp_path), "sess-1")
    server._spawn_agents(
        room_id=room_id, name="test-room", goal="g", cwd=str(tmp_path),
        owner="Human", auto_spawn={"Excluded": "custom brief"},
    )
    info = bus.get_room_info(room_id)
    assert "Excluded" in info.get("participants", [])
    assert "Excluded" in (info.get("agent_meta") or {})


class _CapturedTimer:
    """threading.Timer stand-in: records the fire fn instead of scheduling it,
    so a test can drive 'the stagger window elapsed' deterministically."""
    fired: list = []  # (delay, func) — reset per test

    def __init__(self, delay, func, *a, **kw):
        _CapturedTimer.fired.append((delay, func))
        self.daemon = False

    def start(self):
        pass


def test_delayed_spawn_skipped_when_room_closed_mid_stagger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: a room closed inside the stagger window must NOT get a
    fresh agent spawned into it when the delayed timer fires — the _fire
    gate re-checks room status at fire time, not at scheduling time."""
    fake_registry: list[spawn.SpawnSpec] = [
        {"name": "First",  "cmd": ["echo", "first={brief}"],  "enabled": True},
        {"name": "Second", "cmd": ["echo", "second={brief}"], "enabled": True},
    ]
    monkeypatch.setattr(spawn, "load_registry", lambda: fake_registry)
    monkeypatch.setenv("MCP_HUDDLE_SAME_BIN_STAGGER_SEC", "20")
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path / "huddle"))
    monkeypatch.setattr(bus, "HUDDLE_HOME", tmp_path / "huddle")
    _CapturedTimer.fired = []
    monkeypatch.setattr(spawn.threading, "Timer", _CapturedTimer)

    spawn_calls: list[str] = []
    real_spawn_agent = spawn.spawn_agent

    def counting_spawn_agent(spec, *a, **kw):
        spawn_calls.append(spec["name"])
        return real_spawn_agent(spec, *a, **kw)

    monkeypatch.setattr(spawn, "spawn_agent", counting_spawn_agent)

    room_id = bus.create_room("test-room", "Human", os.getpid(), str(tmp_path), "sess-1")
    server._spawn_agents(
        room_id=room_id, name="test-room", goal="g", cwd=str(tmp_path),
        owner="Human", auto_spawn=True,
    )
    assert spawn_calls == ["First"]  # Second is deferred to the timer
    assert len(_CapturedTimer.fired) == 1

    # The room closes INSIDE the stagger window, before the timer fires.
    bus.close_room(room_id, "Human")

    _CapturedTimer.fired[0][1]()  # the stagger window elapses

    assert spawn_calls == ["First"]  # no spawn into the closed room
    meta = bus._read_meta(room_id)
    assert meta["status"] == "closed"


def test_delayed_spawn_pid_recorded_in_spawned_pids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: a staggered spawn's pid (unknown at _spawn_agents return
    time) must land in meta['spawned_pids'] once the timer fires — otherwise
    close_room's kill sweep (_kill_spawned iterates spawned_pids) would
    orphan a process started during the stagger window."""
    fake_registry: list[spawn.SpawnSpec] = [
        {"name": "First",  "cmd": ["echo", "first={brief}"],  "enabled": True},
        {"name": "Second", "cmd": ["echo", "second={brief}"], "enabled": True},
    ]
    monkeypatch.setattr(spawn, "load_registry", lambda: fake_registry)
    monkeypatch.setenv("MCP_HUDDLE_SAME_BIN_STAGGER_SEC", "20")
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path / "huddle"))
    monkeypatch.setattr(bus, "HUDDLE_HOME", tmp_path / "huddle")
    _CapturedTimer.fired = []
    monkeypatch.setattr(spawn.threading, "Timer", _CapturedTimer)

    room_id = bus.create_room("test-room", "Human", os.getpid(), str(tmp_path), "sess-1")
    server._spawn_agents(
        room_id=room_id, name="test-room", goal="g", cwd=str(tmp_path),
        owner="Human", auto_spawn=True,
    )
    pids_before = list(bus._read_meta(room_id).get("spawned_pids") or [])
    assert len(pids_before) == 1  # only the synchronous First so far
    assert len(_CapturedTimer.fired) == 1

    _CapturedTimer.fired[0][1]()  # room still open — the delayed spawn fires

    pids_after = list(bus._read_meta(room_id).get("spawned_pids") or [])
    assert len(pids_after) == 2  # Second's pid merged in → killable by close_room
    assert set(pids_before) < set(pids_after)


def test_delayed_spawn_dict_branch_gates_and_records_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The explicit-dict auto_spawn branch mirrors the spawn_all path: its
    staggered spawn is gated on room status at fire time and records its pid."""
    fake_registry: list[spawn.SpawnSpec] = [
        {"name": "First",  "cmd": ["echo", "first={brief}"],  "enabled": True},
        {"name": "Second", "cmd": ["echo", "second={brief}"], "enabled": True},
    ]
    monkeypatch.setattr(spawn, "load_registry", lambda: fake_registry)
    monkeypatch.setenv("MCP_HUDDLE_SAME_BIN_STAGGER_SEC", "20")
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path / "huddle"))
    monkeypatch.setattr(bus, "HUDDLE_HOME", tmp_path / "huddle")
    _CapturedTimer.fired = []
    monkeypatch.setattr(spawn.threading, "Timer", _CapturedTimer)

    # Room 1: fire with the room open → pid recorded.
    room_open = bus.create_room("r-open", "Human", os.getpid(), str(tmp_path), "s1")
    server._spawn_agents(
        room_id=room_open, name="r-open", goal="g", cwd=str(tmp_path),
        owner="Human", auto_spawn={"First": "a", "Second": "b"},
    )
    assert len(_CapturedTimer.fired) == 1
    _CapturedTimer.fired[0][1]()
    assert len(bus._read_meta(room_open).get("spawned_pids") or []) == 2

    # Room 2: close before the timer fires → delayed spawn skipped.
    _CapturedTimer.fired = []
    room_closed = bus.create_room("r-closed", "Human", os.getpid(), str(tmp_path), "s2")
    server._spawn_agents(
        room_id=room_closed, name="r-closed", goal="g", cwd=str(tmp_path),
        owner="Human", auto_spawn={"First": "a", "Second": "b"},
    )
    assert len(_CapturedTimer.fired) == 1
    pids_at_close = list(bus._read_meta(room_closed).get("spawned_pids") or [])
    bus.close_room(room_closed, "Human")
    _CapturedTimer.fired[0][1]()
    assert (bus._read_meta(room_closed).get("spawned_pids") or []) == pids_at_close


def test_binary_resolution_uses_absolute_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default agent registry must not depend only on the daemon PATH."""
    fake_codex = tmp_path / "codex"
    fake_codex.write_text("#!/bin/sh\n")
    monkeypatch.setattr(spawn.shutil, "which", lambda _name: None)

    resolved = spawn._first_existing_binary(["codex", str(fake_codex)])

    assert resolved == str(fake_codex)


def test_spawn_agent_verify_alive_rejects_fast_exit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Optional health check logs and rejects processes that die immediately."""
    spec: spawn.SpawnSpec = {
        "name": "FastExit",
        "cmd": ["/bin/sh", "-c", "exit 42"],
        "enabled": True,
    }

    with pytest.raises(spawn.AgentSpawnError):
        spawn.spawn_agent(
            spec,
            "brief",
            str(tmp_path),
            tmp_path / "agents",
            verify_alive_sec=0.05,
        )

    err = capsys.readouterr().err
    assert "failed to spawn FastExit" in err
    assert "exited within" in err
    assert "status 42" in err


def test_spawn_agent_registers_background_reaper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The long-running huddle daemon must reap spawned children after exit."""
    popen_calls: list[dict] = []
    reaped: list[tuple[int, str]] = []

    class FakeProc:
        pid = 12345

        def poll(self):
            return None

    def fake_popen(argv, cwd, stdin, stdout, stderr, env):
        popen_calls.append({
            "argv": argv, "cwd": cwd, "stdin": stdin, "stderr": stderr, "env": env,
        })
        return FakeProc()

    monkeypatch.setattr(spawn.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        spawn,
        "_reap_in_background",
        lambda proc, name, on_exit=None: reaped.append((proc.pid, name)),
    )

    spec: spawn.SpawnSpec = {
        "name": "Gemini",
        "cmd": ["gemini", "-p", "{brief}"],
        "enabled": True,
    }

    pid, _, _ = spawn.spawn_agent(spec, "brief", str(tmp_path), tmp_path / "agents")

    assert pid == 12345
    assert popen_calls[0]["argv"] == ["gemini", "-p", "brief"]
    assert reaped == [(12345, "Gemini")]


# ── Phase 2: thread_id parsing ────────────────────────────────────────────────

def test_parse_codex_thread_id_from_log(tmp_path: Path) -> None:
    """parse_codex_thread_id must extract thread_id from the first
    {"type":"thread.started",...} JSONL line in the log file."""
    log = tmp_path / "codex.events.jsonl"
    log.write_text(
        '{"type":"thread.started","thread_id":"abc-123-def"}\n'
        '{"type":"turn.started"}\n'
        '{"type":"item.completed","item":{"type":"agent_message","text":"hi"}}\n'
    )
    tid = spawn.parse_codex_thread_id(str(log), timeout=1.0)
    assert tid == "abc-123-def"


def test_parse_codex_thread_id_timeout(tmp_path: Path) -> None:
    """No thread.started → returns None within timeout."""
    log = tmp_path / "empty.jsonl"
    log.write_text('{"type":"some.other.event"}\n')
    tid = spawn.parse_codex_thread_id(str(log), timeout=0.5)
    assert tid is None


def test_parse_codex_thread_id_skips_garbage(tmp_path: Path) -> None:
    """Non-JSON lines (e.g. stderr mixed in) must be skipped, not crash."""
    log = tmp_path / "noisy.jsonl"
    log.write_text(
        'Reading additional input from stdin...\n'
        '{"type":"thread.started","thread_id":"xyz"}\n'
    )
    tid = spawn.parse_codex_thread_id(str(log), timeout=1.0)
    assert tid == "xyz"


def test_codex_log_has_completed_turn(tmp_path: Path) -> None:
    log = tmp_path / "codex.events.jsonl"
    log.write_text(
        '{"type":"thread.started","thread_id":"abc"}\n'
        '{"type":"turn.started"}\n'
    )
    assert not spawn.codex_log_has_completed_turn(str(log))

    log.write_text(log.read_text() + '{"type":"turn.completed"}\n')
    assert spawn.codex_log_has_completed_turn(str(log))


# ── ACP Phase 2.5 stub ────────────────────────────────────────────────────────

def test_acp_stub_raises_clear_error() -> None:
    """The acp.gemini_acp_prompt stub must raise NotImplementedError with
    a useful migration hint, not silently return."""
    from mcp_huddle import acp
    with pytest.raises(acp.AcpNotImplemented) as exc:
        acp.gemini_acp_prompt(None, "test")
    assert "Phase 2.5" in str(exc.value)
    assert "Codex" in str(exc.value)  # mentions the working alternative


# ── Phase 3: persistent room wake-up loop ────────────────────────────────────

def test_request_message_wakes_existing_codex_thread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    isolated_bus = importlib.reload(bus)
    monkeypatch.setattr(server, "bus", isolated_bus)

    calls: list[dict] = []

    def fake_codex_resume(thread_id: str, prompt: str, cwd: str, log_path: str, last_msg_path: str | None = None, on_exit=None) -> int:
        calls.append({
            "thread_id": thread_id,
            "prompt": prompt,
            "cwd": cwd,
            "log_path": log_path,
            "last_msg_path": last_msg_path,
        })
        return 4242

    monkeypatch.setattr(server.spawn, "codex_resume", fake_codex_resume)

    room_id = isolated_bus.create_room("Wake", "Claude", 0, "/tmp/project", "session-1")
    isolated_bus.invite_agent(room_id, "Codex")
    isolated_bus.post_message(room_id, "Gemini", "Prior room context Codex must re-read.", "comment")
    log_path = isolated_bus._room_dir(room_id) / "agents" / "codex.events.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        '{"type":"thread.started","thread_id":"thread-123"}\n'
        '{"type":"turn.completed"}\n'
    )
    meta = isolated_bus.get_room_info(room_id)
    meta["agent_meta"] = {
        "Codex": {
            "log_path": str(log_path),
            "last_message_path": str(isolated_bus._room_dir(room_id) / "agents" / "codex.last_message.txt"),
            "last_seen_id": 1,
        }
    }
    isolated_bus._write_json(isolated_bus._room_dir(room_id) / "meta.json", meta)

    msg_id = server.message_post(
        room_id,
        "Claude",
        "Please review the plan delta.",
        "request",
        to="all",
        idempotency_key="request-1",
    )

    assert msg_id == 2
    assert len(calls) == 1
    assert calls[0]["thread_id"] == "thread-123"
    assert calls[0]["cwd"] == "/tmp/project"
    assert calls[0]["last_msg_path"].endswith("codex.last_message.txt")
    assert "since_id=0" in calls[0]["prompt"]
    assert 'reply_to=2' in calls[0]["prompt"]
    assert 'idempotency_key="codex-wake:' in calls[0]["prompt"]

    updated = isolated_bus.get_room_info(room_id)["agent_meta"]["Codex"]
    assert updated["thread_id"] == "thread-123"
    assert updated["last_wake_msg_id"] == 2
    assert updated["last_seen_id"] == 2
    assert updated["last_wake_pid"] == 4242

    duplicate_id = server.message_post(
        room_id,
        "Claude",
        "Please review the plan delta.",
        "request",
        to="all",
        idempotency_key="request-1",
    )
    assert duplicate_id == 2
    assert len(calls) == 1


def test_room_status_reports_waiting_agent_and_pending_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    isolated_bus = importlib.reload(bus)
    monkeypatch.setattr(server, "bus", isolated_bus)

    room_id = isolated_bus.create_room("Status", "CodexMain", os.getpid(), "/tmp/project", "session-1")
    isolated_bus.invite_agent(room_id, "Antigravity")
    meta = isolated_bus.get_room_info(room_id)
    meta["agent_meta"] = {
        "Antigravity": {
            "last_wake_pid": os.getpid(),
            "last_wake_msg_id": 0,
            "wake_id": "wake-1",
        }
    }
    isolated_bus._write_json(isolated_bus._room_dir(room_id) / "meta.json", meta)
    isolated_bus.set_status(
        room_id, "Antigravity", "busy", 0, "session-1",
        phase="working", task_id=1,
    )
    request_id = isolated_bus.post_message(
        room_id, "CodexMain", "Research this", "request", to="Antigravity",
    )

    snapshot = server.room_status(room_id)

    assert snapshot["wait_recommended"] is True
    assert request_id in [item["id"] for item in snapshot["pending_requests"]]
    assert snapshot["pending_requests"][0]["waiting_for"] == ["Antigravity"]
    agent = snapshot["agents"]["Antigravity"]
    assert agent["phase"] == "working"
    assert agent["process_alive"] is True
    assert agent["task_id"] == 1


def test_agent_reported_phase_does_not_require_server_wake_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Externally launched agents can report work without agent_meta PID."""
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    isolated_bus = importlib.reload(bus)
    monkeypatch.setattr(server, "bus", isolated_bus)

    room_id = isolated_bus.create_room("External", "Claude", 0, "/tmp/project", "session-1")
    isolated_bus.invite_agent(room_id, "ExternalAgent")

    with pytest.raises(ValueError):
        server.status_set(room_id, "ExternalAgent", "completed")
    assert server.status_set(room_id, "ExternalAgent", "working", task_id="research") == "ok"

    agent = server.room_status(room_id)["agents"]["ExternalAgent"]
    assert agent["phase"] == "working"
    assert agent["process_alive"] is False
    assert agent["health"]["stale_lease"] is True


def test_terminal_agent_failure_closes_pending_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    isolated_bus = importlib.reload(bus)
    monkeypatch.setattr(server, "bus", isolated_bus)

    room_id = isolated_bus.create_room("Unavailable", "Claude", 0, "/tmp/project", "session-1")
    isolated_bus.invite_agent(room_id, "Antigravity")
    isolated_bus.post_message(
        room_id, "Claude", "Please research this", "request", to="Antigravity",
    )
    isolated_bus.set_status(
        room_id, "Antigravity", "online", 0, "session-1",
        phase="unavailable", detail="spawn failed",
    )

    snapshot = server.room_status(room_id)

    assert snapshot["pending_requests"] == []
    assert snapshot["wait_recommended"] is False
    assert snapshot["all_terminal"] is True


def test_auto_spawn_false_agent_is_not_marked_starting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    isolated_bus = importlib.reload(bus)
    monkeypatch.setattr(server, "bus", isolated_bus)
    fake_registry: list[spawn.SpawnSpec] = [{
        "name": "OptIn",
        "cmd": ["echo", "{brief}"],
        "enabled": True,
        "auto": False,
    }]
    monkeypatch.setattr(server.spawn, "load_registry", lambda: fake_registry)
    monkeypatch.setattr(server.spawn, "spawn_all", lambda *args, **kwargs: ([], [], {}))

    room_id = isolated_bus.create_room("AutoFilter", "Claude", 0, str(tmp_path), "session-1")
    server._spawn_agents(
        room_id, "AutoFilter", "test", str(tmp_path), "Claude", auto_spawn=True,
    )

    assert "OptIn" not in isolated_bus.get_status_details(room_id)


def test_request_wakeup_skips_self_and_reply_requests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    isolated_bus = importlib.reload(bus)
    monkeypatch.setattr(server, "bus", isolated_bus)

    calls: list[tuple] = []
    monkeypatch.setattr(server.spawn, "codex_resume", lambda *args, **kwargs: calls.append(args) or 100)

    room_id = isolated_bus.create_room("Wake", "Claude", 0, "/tmp/project", "session-1")
    isolated_bus.invite_agent(room_id, "Codex")
    meta = isolated_bus.get_room_info(room_id)
    meta["agent_meta"] = {
        "Codex": {
            "thread_id": "thread-123",
            "log_path": str(isolated_bus._room_dir(room_id) / "agents" / "codex.events.jsonl"),
            "last_message_path": str(isolated_bus._room_dir(room_id) / "agents" / "codex.last_message.txt"),
        }
    }
    isolated_bus._write_json(isolated_bus._room_dir(room_id) / "meta.json", meta)

    server.message_post(room_id, "Codex", "Self request should not wake me.", "request", to="all")
    server.message_post(room_id, "Claude", "This is a reply-shaped request.", "request", to="all", reply_to=1)

    assert calls == []


def test_pending_wakeup_retries_after_initial_turn_completes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    isolated_bus = importlib.reload(bus)
    monkeypatch.setattr(server, "bus", isolated_bus)

    calls: list[tuple] = []
    monkeypatch.setattr(server.spawn, "codex_resume", lambda *args, **kwargs: calls.append(args) or 200)

    room_id = isolated_bus.create_room("Wake", "Claude", 0, "/tmp/project", "session-1")
    isolated_bus.invite_agent(room_id, "Codex")
    log_path = isolated_bus._room_dir(room_id) / "agents" / "codex.events.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        '{"type":"thread.started","thread_id":"thread-123"}\n'
        '{"type":"turn.started"}\n'
    )
    meta = isolated_bus.get_room_info(room_id)
    meta["agent_meta"] = {
        "Codex": {
            "log_path": str(log_path),
            "last_message_path": str(isolated_bus._room_dir(room_id) / "agents" / "codex.last_message.txt"),
        }
    }
    isolated_bus._write_json(isolated_bus._room_dir(room_id) / "meta.json", meta)

    server.message_post(room_id, "Claude", "Arrived before Codex was ready.", "request", to="Codex")
    assert calls == []

    log_path.write_text(log_path.read_text() + '{"type":"turn.completed"}\n')
    wakes = server._wake_pending_agents()

    assert len(calls) == 1
    assert wakes[0]["thread_id"] == "thread-123"
    updated = isolated_bus.get_room_info(room_id)["agent_meta"]["Codex"]
    assert updated["last_wake_msg_id"] == 1


def test_respond_via_agent_fresh_spawns_non_codex_with_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    isolated_bus = importlib.reload(bus)
    monkeypatch.setattr(server, "bus", isolated_bus)

    fake_spec: spawn.SpawnSpec = {
        "name": "Gemini",
        "cmd": ["gemini", "-p", "{brief}"],
        "enabled": True,
    }
    monkeypatch.setattr(server.spawn, "get_enabled_spec", lambda name: fake_spec if name == "Gemini" else None)

    calls: list[dict] = []

    def fake_spawn_agent(spec, brief, cwd, log_dir, verify_alive_sec=0.0, on_exit=None):
        calls.append({"spec": spec, "brief": brief, "cwd": cwd, "log_dir": log_dir})
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "gemini.events.jsonl"
        log_path.write_text('{"type":"init"}\n')
        return 5151, str(log_path), None

    monkeypatch.setattr(server.spawn, "spawn_agent", fake_spawn_agent)

    room_id = isolated_bus.create_room("Fresh", "CodexMain", 0, "/tmp/project", "session-1")
    isolated_bus.invite_agent(room_id, "Gemini")
    isolated_bus.post_message(room_id, "CodexMain", "Can you see this?", "request", to="Gemini")

    result = server.respond_via_agent(
        room_id,
        "Gemini",
        "Answer the latest CodexMain request and cite its message id.",
        post_as_message=False,
    )

    assert result["pid"] == 5151
    assert result["agent"] == "Gemini"
    assert result["thread_id"] == ""
    assert "spawned a fresh" in result["note"]
    assert len(calls) == 1
    assert calls[0]["cwd"] == "/tmp/project"
    assert "[001] CodexMain" in calls[0]["brief"]
    assert "Answer the latest CodexMain request" in calls[0]["brief"]

    meta = isolated_bus.get_room_info(room_id)["agent_meta"]["Gemini"]
    assert meta["log_path"].endswith("gemini.events.jsonl")
    assert meta["last_wake_pid"] == 5151


def test_request_message_wakes_registry_agent_without_uuid_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    isolated_bus = importlib.reload(bus)
    monkeypatch.setattr(server, "bus", isolated_bus)

    fake_spec: spawn.SpawnSpec = {
        "name": "Gemini",
        "cmd": ["gemini", "-p", "{brief}"],
        "enabled": True,
    }
    monkeypatch.setattr(server.spawn, "get_enabled_spec", lambda name: fake_spec if name == "Gemini" else None)

    calls: list[dict] = []

    def fake_spawn_agent(spec, brief, cwd, log_dir, verify_alive_sec=0.0, on_exit=None):
        calls.append({"spec": spec, "brief": brief, "cwd": cwd})
        log_dir.mkdir(parents=True, exist_ok=True)
        return 6161, str(log_dir / "gemini.events.jsonl"), None

    monkeypatch.setattr(server.spawn, "spawn_agent", fake_spawn_agent)

    room_id = isolated_bus.create_room("WakeGemini", "CodexMain", 0, "/tmp/project", "session-1")
    isolated_bus.invite_agent(room_id, "Gemini")
    isolated_bus.post_message(room_id, "CodexMain", "Earlier context Gemini must still see.", "comment")
    log_path = isolated_bus._room_dir(room_id) / "agents" / "gemini.events.jsonl"
    meta = isolated_bus.get_room_info(room_id)
    meta["agent_meta"] = {
        "Gemini": {
            "log_path": str(log_path),
            "last_message_path": None,
            "last_seen_id": 1,
        }
    }
    isolated_bus._write_json(isolated_bus._room_dir(room_id) / "meta.json", meta)

    msg_id = server.message_post(
        room_id,
        "CodexMain",
        "Gemini, please review delivery semantics.",
        "request",
        to="Gemini",
        idempotency_key="request-gemini-1",
    )

    assert msg_id == 2
    assert len(calls) == 1
    assert calls[0]["cwd"] == "/tmp/project"
    assert "New request id: 2" in calls[0]["brief"]
    assert "Current full transcript:" in calls[0]["brief"]
    assert "[001] CodexMain" in calls[0]["brief"]
    assert "Earlier context Gemini must still see." in calls[0]["brief"]
    assert "since_id=0" in calls[0]["brief"]
    assert 'agent="Gemini"' in calls[0]["brief"]
    assert 'reply_to=2' in calls[0]["brief"]

    updated = isolated_bus.get_room_info(room_id)["agent_meta"]["Gemini"]
    assert updated["last_wake_msg_id"] == 2
    assert updated["last_seen_id"] == 2
    assert updated["last_wake_pid"] == 6161


def test_pending_wakeup_skips_request_agent_already_replied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    isolated_bus = importlib.reload(bus)
    monkeypatch.setattr(server, "bus", isolated_bus)

    calls: list[dict] = []
    monkeypatch.setattr(server.spawn, "spawn_agent", lambda *args, **kwargs: calls.append({"args": args}) or (1, "", None))

    room_id = isolated_bus.create_room("AlreadyReplied", "CodexMain", 0, "/tmp/project", "session-1")
    isolated_bus.invite_agent(room_id, "Gemini")
    isolated_bus.post_message(
        room_id,
        "CodexMain",
        "Gemini, answer once.",
        "request",
        to="Gemini",
    )
    isolated_bus.post_message(
        room_id,
        "Gemini",
        "Answered already.",
        "result",
        to="CodexMain",
        reply_to=1,
    )
    meta = isolated_bus.get_room_info(room_id)
    meta["agent_meta"] = {
        "Gemini": {
            "log_path": str(isolated_bus._room_dir(room_id) / "agents" / "gemini.events.jsonl"),
            "last_message_path": None,
        }
    }
    isolated_bus._write_json(isolated_bus._room_dir(room_id) / "meta.json", meta)

    wakes = server._wake_pending_agents()

    assert wakes == []
    assert calls == []
    updated = isolated_bus.get_room_info(room_id)["agent_meta"]["Gemini"]
    assert updated["last_wake_msg_id"] == 1
    assert updated["last_seen_id"] == 1


def test_wake_pending_agents_wakes_idle_room(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A request addressed to an agent while the room was already idle (e.g.
    posted before the agent was invited, or while the watchdog wasn't
    running) must still be drained by the fallback sweep — idle just means
    quiet, not done."""
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    isolated_bus = importlib.reload(bus)
    monkeypatch.setattr(server, "bus", isolated_bus)

    calls: list[dict] = []

    def fake_spawn_agent(spec, brief, cwd, log_dir, verify_alive_sec=0.0, on_exit=None):
        calls.append({"spec": spec, "brief": brief, "cwd": cwd})
        log_dir.mkdir(parents=True, exist_ok=True)
        return 7171, str(log_dir / "gemini.events.jsonl"), None

    fake_spec: spawn.SpawnSpec = {
        "name": "Gemini",
        "cmd": ["gemini", "-p", "{brief}"],
        "enabled": True,
    }
    monkeypatch.setattr(server.spawn, "get_enabled_spec", lambda name: fake_spec if name == "Gemini" else None)
    monkeypatch.setattr(server.spawn, "spawn_agent", fake_spawn_agent)

    room_id = isolated_bus.create_room("IdleWake", "CodexMain", 0, "/tmp/project", "session-1")
    isolated_bus.invite_agent(room_id, "Gemini")
    isolated_bus.post_message(
        room_id, "CodexMain", "Gemini, please weigh in.", "request", to="Gemini",
    )
    meta = isolated_bus.get_room_info(room_id)
    meta["agent_meta"] = {
        "Gemini": {
            "log_path": str(isolated_bus._room_dir(room_id) / "agents" / "gemini.events.jsonl"),
            "last_message_path": None,
        }
    }
    meta["status"] = "idle"
    isolated_bus._write_json(isolated_bus._room_dir(room_id) / "meta.json", meta)

    wakes = server._wake_pending_agents()

    assert len(calls) == 1
    assert wakes[0]["agent"] == "Gemini"
    assert isolated_bus.get_room_info(room_id)["status"] == "idle"


def test_wake_pending_agents_skips_closed_room(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    isolated_bus = importlib.reload(bus)
    monkeypatch.setattr(server, "bus", isolated_bus)

    calls: list[dict] = []
    monkeypatch.setattr(server.spawn, "spawn_agent", lambda *args, **kwargs: calls.append({"args": args}) or (1, "", None))

    room_id = isolated_bus.create_room("ClosedWake", "CodexMain", 0, "/tmp/project", "session-1")
    isolated_bus.invite_agent(room_id, "Gemini")
    isolated_bus.post_message(
        room_id, "CodexMain", "Gemini, please weigh in.", "request", to="Gemini",
    )
    meta = isolated_bus.get_room_info(room_id)
    meta["agent_meta"] = {
        "Gemini": {
            "log_path": str(isolated_bus._room_dir(room_id) / "agents" / "gemini.events.jsonl"),
            "last_message_path": None,
        }
    }
    meta["status"] = "closed"
    isolated_bus._write_json(isolated_bus._room_dir(room_id) / "meta.json", meta)

    wakes = server._wake_pending_agents()

    assert wakes == []
    assert calls == []


def test_wake_pending_agents_skips_resolved_room(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    isolated_bus = importlib.reload(bus)
    monkeypatch.setattr(server, "bus", isolated_bus)

    calls: list[dict] = []
    monkeypatch.setattr(server.spawn, "spawn_agent", lambda *args, **kwargs: calls.append({"args": args}) or (1, "", None))

    room_id = isolated_bus.create_room("ResolvedWake", "CodexMain", 0, "/tmp/project", "session-1")
    isolated_bus.invite_agent(room_id, "Gemini")
    isolated_bus.post_message(
        room_id, "CodexMain", "Gemini, please weigh in.", "request", to="Gemini",
    )
    meta = isolated_bus.get_room_info(room_id)
    meta["agent_meta"] = {
        "Gemini": {
            "log_path": str(isolated_bus._room_dir(room_id) / "agents" / "gemini.events.jsonl"),
            "last_message_path": None,
        }
    }
    meta["status"] = "resolved"
    isolated_bus._write_json(isolated_bus._room_dir(room_id) / "meta.json", meta)

    wakes = server._wake_pending_agents()

    assert wakes == []
    assert calls == []


# ── Tools surface cleanup (room_c216d6e8 consensus) ──────────────────────────

def test_dropped_tools_no_longer_exposed_as_mcp_tools() -> None:
    """8 tools dropped from agent MCP per design discussion in room_c216d6e8:
    lifecycle close-variants are human-only, telemetry tools without consumers
    are removed, respond_via_agent is superseded by Phase 3 wake-up.
    Functions remain importable for internal use; only @mcp.tool() is gone."""
    import re
    source = (Path(server.__file__)).read_text()
    pat = re.compile(r"@mcp\.tool\(\)\s*\ndef\s+(\w+)\s*\(", re.M)
    exposed = set(pat.findall(source))
    dropped = {
        "room_request_close", "room_close", "room_delete", "room_close_session",
        "respond_via_agent", "status_get",
    }
    assert dropped.isdisjoint(exposed), (
        f"Tools that must NOT be MCP-exposed are still exposed: {dropped & exposed}"
    )
    # The 10 we keep — notify_register re-exposed so externally-launched agents
    # (not auto_spawn'd by huddle) can subscribe to request notifications.
    expected_kept = {
        "room_create", "room_invite", "room_info", "room_list",
        "message_post", "messages_read", "room_summarize",
        "propose_resolution", "resolution_vote", "notify_register",
        "status_set", "room_status",
    }
    assert expected_kept <= exposed, f"Missing expected tools: {expected_kept - exposed}"


def test_room_invite_requires_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """room_invite is owner-only escape hatch. Non-owner caller → PermissionError."""
    home = tmp_path / "huddle-home"
    monkeypatch.setattr(bus, "HUDDLE_HOME", home)
    monkeypatch.setattr(bus, "BUS_DIR", home / "rooms")
    room_id = bus.create_room("rs", owner="Alice", owner_pid=1, cwd="", session_id="")
    # Non-owner attempt → PermissionError
    with pytest.raises(PermissionError):
        server.room_invite(room_id, "Bob", by="Eve")
    # Missing `by` → also rejected
    with pytest.raises(PermissionError):
        server.room_invite(room_id, "Bob")
    # Owner → ok
    assert server.room_invite(room_id, "Bob", by="Alice") == "ok"
    info = bus.get_room_info(room_id)
    assert "Bob" in info["participants"]


def test_room_invite_seeds_agent_meta_for_registry_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug: room_invite only appended to participants; _wake_agents_for_request
    iterates agent_meta (populated only by auto_spawn at room_create). A
    registry agent invited into an existing room was never woken by a later
    addressed request. Fix: room_invite seeds agent_meta for enabled registry
    agents, so the wake path picks it up without an immediate spawn."""
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    isolated_bus = importlib.reload(bus)
    monkeypatch.setattr(server, "bus", isolated_bus)

    fake_spec: spawn.SpawnSpec = {"name": "Antigravity", "cmd": ["echo"], "enabled": True}
    monkeypatch.setattr(
        server.spawn, "get_enabled_spec",
        lambda name: fake_spec if name == "Antigravity" else None,
    )

    room_id = isolated_bus.create_room(
        "no-auto-spawn", "Claude", 1, "/tmp/project", "session-1")
    # No auto_spawn → agent_meta starts empty (reproduces the bug precondition).
    assert isolated_bus.get_room_info(room_id).get("agent_meta", {}) == {}

    assert server.room_invite(room_id, "Antigravity", by="Claude") == "ok"

    info = isolated_bus.get_room_info(room_id)
    assert "Antigravity" in info["participants"]
    assert "Antigravity" in info.get("agent_meta", {}), (
        "room_invite must seed agent_meta for registry-backed agents so "
        "_wake_agents_for_request's iteration (`for agent_name in agent_meta`) "
        "sees them"
    )

    # Not spawned yet — invite is lazy, spawn happens on the first addressed
    # request.
    spawn_calls: list[dict] = []
    monkeypatch.setattr(
        server.spawn, "spawn_agent",
        lambda *args, **kwargs: spawn_calls.append({"args": args, "kwargs": kwargs})
        or (4242, str(tmp_path / "antigravity.events.jsonl"), None),
    )
    assert spawn_calls == []

    msg_id = server.message_post(
        room_id, "Claude", "Antigravity, please weigh in.", "request",
        to="Antigravity",
    )

    assert len(spawn_calls) == 1, "invited registry agent must be fresh-spawned on addressed request"
    updated = isolated_bus.get_room_info(room_id)["agent_meta"]["Antigravity"]
    assert updated["last_wake_msg_id"] == msg_id
    assert updated["last_wake_pid"] == 4242


def test_room_invite_non_registry_agent_seeds_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inviting a non-registry (external/human) agent must keep prior
    behavior: participants only, no agent_meta entry, no error."""
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    isolated_bus = importlib.reload(bus)
    monkeypatch.setattr(server, "bus", isolated_bus)
    monkeypatch.setattr(server.spawn, "get_enabled_spec", lambda name: None)

    room_id = isolated_bus.create_room(
        "no-auto-spawn-2", "Claude", 1, "/tmp/project", "session-1")

    assert server.room_invite(room_id, "SomeHuman", by="Claude") == "ok"

    info = isolated_bus.get_room_info(room_id)
    assert "SomeHuman" in info["participants"]
    assert "SomeHuman" not in info.get("agent_meta", {})


def test_no_dropped_tool_references_in_agent_facing_prompts() -> None:
    """Generated agent prompts and MCP server instructions must not advertise
    tools that were dropped from the agent surface (room_c216d6e8 consensus).
    This catches a class of regression that the surface-only test misses:
    documentation/prompts pointing agents at non-existent MCP tools.

    Per Codex review of feat/agent-tools-cleanup."""
    dropped = [
        "room_close", "room_close_session", "room_delete", "room_request_close",
        "status_get", "respond_via_agent",
    ]
    samples = {
        "default_brief": server._build_default_brief(
            "room_test", "test-room", "test goal", "/tmp"),
        "codex_wakeup": server._build_codex_wakeup_prompt(
            "room_test", "Claude", "test body", "Codex", 42, 0),
        "agent_instructions": server._AGENT_INSTRUCTIONS,
    }
    if hasattr(server, "_build_agent_wakeup_prompt"):
        samples["agent_wakeup"] = server._build_agent_wakeup_prompt(
            "room_test", "Gemini", "Claude", "test body", "Gemini", 42, 0, "")
    failures = []
    for source_name, text in samples.items():
        for tool in dropped:
            if tool in text:
                failures.append(f"{source_name} mentions dropped tool {tool!r}")
    assert not failures, "\n".join(failures)
def test_room_marks_idle_after_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    isolated_bus = importlib.reload(bus)
    monkeypatch.setattr(server, "bus", isolated_bus)
    monkeypatch.setattr(server, "IDLE_TIMEOUT_SECS", 10)
    monkeypatch.setattr(server.time, "time", lambda: 100)

    room_id = isolated_bus.create_room("Idle", "Claude", 0, "/tmp/project", "session-1")
    meta = isolated_bus.get_room_info(room_id)
    meta["last_activity_at"] = 89
    isolated_bus._write_json(isolated_bus._room_dir(room_id) / "meta.json", meta)

    assert server._mark_idle_rooms() == [room_id]
    assert isolated_bus.get_room_info(room_id)["status"] == "idle"


def test_request_revives_idle_room(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    isolated_bus = importlib.reload(bus)
    monkeypatch.setattr(server, "bus", isolated_bus)

    room_id = isolated_bus.create_room("Revive", "Claude", 0, "/tmp/project", "session-1")
    isolated_bus.mark_idle(room_id)

    msg_id = server.message_post(room_id, "Claude", "Anyone back?", "request", to="all")

    assert msg_id == 1
    meta = isolated_bus.get_room_info(room_id)
    assert meta["status"] == "open"
    assert meta["last_activity_at"] >= meta["created_at"]


def test_reply_to_same_agent_cannot_answer_twice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Anti-loop: the same agent may not answer one request twice."""
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    isolated_bus = importlib.reload(bus)
    monkeypatch.setattr(server, "bus", isolated_bus)

    room_id = isolated_bus.create_room("Answered", "Claude", 0, "/tmp/project", "session-1")
    server.message_post(room_id, "Claude", "Codex, answer once.", "request", to="Codex")
    server.message_post(room_id, "Codex", "First answer.", "result", to="Claude", reply_to=1)

    with pytest.raises(ValueError, match="already answered"):
        server.message_post(room_id, "Codex", "Second answer.", "result", to="Claude", reply_to=1)


def test_reply_to_wrong_addressee_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An agent that was not an addressee of a single-recipient request cannot
    answer it."""
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    isolated_bus = importlib.reload(bus)
    monkeypatch.setattr(server, "bus", isolated_bus)

    room_id = isolated_bus.create_room("Addressee", "Claude", 0, "/tmp/project", "session-1")
    server.message_post(room_id, "Claude", "Codex only.", "request", to="Codex")

    with pytest.raises(ValueError, match="not to 'Gemini'"):
        server.message_post(room_id, "Gemini", "Butting in.", "result", to="Claude", reply_to=1)


def test_reply_to_broadcast_allows_one_reply_per_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A to=all request expects one reply per addressee — Codex AND Gemini must
    both be able to answer it (the BUG-2 / multi-addressee fix)."""
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    isolated_bus = importlib.reload(bus)
    monkeypatch.setattr(server, "bus", isolated_bus)

    room_id = isolated_bus.create_room("Broadcast", "Claude", 0, "/tmp/project", "session-1")
    server.message_post(room_id, "Claude", "Everyone, review this.", "request", to="all")
    server.message_post(room_id, "Gemini", "Gemini's review.", "result", to="Claude", reply_to=1)
    # Second addressee must NOT be locked out by the first reply.
    third = server.message_post(room_id, "Codex", "Codex's review.", "result", to="Claude", reply_to=1)
    assert third == 3
    # But the same agent still cannot answer the broadcast twice.
    with pytest.raises(ValueError, match="already answered"):
        server.message_post(room_id, "Gemini", "Gemini again.", "result", to="Claude", reply_to=1)


# ── Rate-limit detection + cooldown ───────────────────────────────────────────

def test_detect_rate_limit_from_codex_turn_failed(tmp_path: Path) -> None:
    log = tmp_path / "codex.events.jsonl"
    log.write_text(
        '{"type":"thread.started","thread_id":"t1"}\n'
        '{"type":"turn.started"}\n'
        '{"type":"turn.failed","error":{"message":"You\'ve hit your usage limit. '
        'Upgrade to Plus, or try again at Jul 16th, 2026 5:17 PM."}}\n'
    )
    reason = spawn.detect_rate_limit(str(log))
    assert reason is not None
    assert "usage limit" in reason.lower()


def test_detect_rate_limit_from_codex_error_event(tmp_path: Path) -> None:
    log = tmp_path / "codex.events.jsonl"
    log.write_text('{"type":"error","message":"Error code: 429 too many requests"}\n')
    assert spawn.detect_rate_limit(str(log)) is not None


def test_detect_rate_limit_from_runner_error_key(tmp_path: Path) -> None:
    """MiMo/openai-compatible runners write {"type": "error", "error": "..."}
    (verified against a real room_8c89ae96 log) — the "error" key must be
    inspected too, and a non-string "error" payload must not crash."""
    log = tmp_path / "mimo.events.jsonl"
    log.write_text(
        '{"type": "error", "error": "provider quota exceeded, try again later"}\n'
        '{"type": "error", "error": {"unexpected": "shape"}}\n'
    )
    reason = spawn.detect_rate_limit(str(log))
    assert reason is not None
    assert "quota" in reason.lower()


def test_detect_rate_limit_ignores_message_body_false_positive(tmp_path: Path) -> None:
    """An agent that POSTS about rate limits must not be flagged as limited."""
    log = tmp_path / "codex.events.jsonl"
    log.write_text(
        '{"type":"item.completed","item":{"type":"mcp_tool_call","tool":"message_post",'
        '"arguments":{"body":"We should add backoff when we hit a usage limit, try again later."},'
        '"error":null,"status":"completed"}}\n'
        '{"type":"turn.completed"}\n'
    )
    assert spawn.detect_rate_limit(str(log)) is None


def test_detect_rate_limit_plaintext_requires_hint(tmp_path: Path) -> None:
    hit = tmp_path / "agy.events.jsonl"
    hit.write_text("Error: you've hit your usage limit. Please try again later.\n")
    assert spawn.detect_rate_limit(str(hit)) is not None

    prose = tmp_path / "agy2.events.jsonl"
    prose.write_text("In my opinion the rate limit design is the core tradeoff here.\n")
    assert spawn.detect_rate_limit(str(prose)) is None


def test_detect_rate_limit_missing_file(tmp_path: Path) -> None:
    assert spawn.detect_rate_limit(str(tmp_path / "nope.jsonl")) is None


def test_wake_skips_agent_in_rate_limit_cooldown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    isolated_bus = importlib.reload(bus)
    monkeypatch.setattr(server, "bus", isolated_bus)

    calls: list[tuple] = []
    monkeypatch.setattr(server.spawn, "codex_resume", lambda *a, **k: calls.append(a) or 1)

    room_id = isolated_bus.create_room("Wake", "Claude", 0, "/tmp/project", "session-1")
    isolated_bus.invite_agent(room_id, "Codex")
    log_path = isolated_bus._room_dir(room_id) / "agents" / "codex.events.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text('{"type":"thread.started","thread_id":"t1"}\n{"type":"turn.completed"}\n')
    meta = isolated_bus.get_room_info(room_id)
    meta["agent_meta"] = {
        "Codex": {
            "thread_id": "t1",
            "log_path": str(log_path),
            "last_message_path": str(log_path.parent / "codex.last_message.txt"),
            "rate_limited_until": int(time.time()) + 10_000,
        }
    }
    isolated_bus._write_json(isolated_bus._room_dir(room_id) / "meta.json", meta)

    server.message_post(room_id, "Claude", "Please review.", "request", to="all")
    assert calls == []  # cooldown gate prevented the doomed re-spawn


def test_handle_rate_limit_sets_cooldown_and_posts_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    monkeypatch.setattr(server, "RATE_LIMIT_COOLDOWN_SECS", 900)
    isolated_bus = importlib.reload(bus)
    monkeypatch.setattr(server, "bus", isolated_bus)

    room_id = isolated_bus.create_room("Wake", "Claude", 0, "/tmp/project", "session-1")
    isolated_bus.invite_agent(room_id, "Codex")
    log_path = isolated_bus._room_dir(room_id) / "agents" / "codex.events.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        '{"type":"turn.failed","error":{"message":"You\'ve hit your usage limit. Try again later."}}\n'
    )
    meta = isolated_bus.get_room_info(room_id)
    meta["agent_meta"] = {"Codex": {"log_path": str(log_path)}}
    isolated_bus._write_json(isolated_bus._room_dir(room_id) / "meta.json", meta)

    assert server._handle_rate_limit_on_exit(room_id, "Codex") is True

    info = isolated_bus.get_room_info(room_id)["agent_meta"]["Codex"]
    assert info["rate_limited_until"] > time.time()
    assert "usage limit" in info["rate_limit_reason"].lower()

    msgs = [m for m in isolated_bus._load_messages(room_id) if m["agent"] == "Codex"]
    assert len(msgs) == 1
    assert msgs[0]["kind"] == "comment"
    assert "лимит" in msgs[0]["body"]

    # Second call within the active window must not re-post or re-stamp.
    assert server._handle_rate_limit_on_exit(room_id, "Codex") is True
    msgs2 = [m for m in isolated_bus._load_messages(room_id) if m["agent"] == "Codex"]
    assert len(msgs2) == 1


def test_clean_turn_clears_rate_limit_cooldown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    isolated_bus = importlib.reload(bus)
    monkeypatch.setattr(server, "bus", isolated_bus)

    room_id = isolated_bus.create_room("Wake", "Claude", 0, "/tmp/project", "session-1")
    isolated_bus.invite_agent(room_id, "Codex")
    wake_id = "wake-abc"
    meta = isolated_bus.get_room_info(room_id)
    meta["agent_meta"] = {"Codex": {
        "wake_id": wake_id,
        "rate_limited_until": int(time.time()) + 10_000,
    }}
    isolated_bus._write_json(isolated_bus._room_dir(room_id) / "meta.json", meta)
    isolated_bus.set_status(room_id, "Codex", "busy", 300, "session-1")

    server._on_wake_exit(room_id, "Codex", wake_id, 0)

    info = isolated_bus.get_room_info(room_id)["agent_meta"]["Codex"]
    assert int(info.get("rate_limited_until", 0)) == 0


# ── Read-only discussant mode (MCP_HUDDLE_READONLY) ───────────────────────────

def test_readonly_default_on_codex_and_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    """By default (no env), agents spawn read-only: Codex uses -s read-only +
    auto-approved huddle MCP; Claude drops --dangerously-skip-permissions for an
    allow/deny tool list. They still talk via MCP, just can't edit files."""
    monkeypatch.delenv("MCP_HUDDLE_READONLY", raising=False)
    monkeypatch.delenv("MCP_HUDDLE_SPAWN_REGISTRY", raising=False)
    reg = {s["name"]: s for s in spawn._raw_registry()}

    codex = " ".join(reg["Codex"]["cmd"])
    assert "-s read-only" in codex
    assert "danger-full-access" not in codex
    assert 'mcp_servers.huddle.default_tools_approval_mode="approve"' in codex

    claude = reg["Claude"]["cmd"]
    assert "--dangerously-skip-permissions" not in claude
    assert "--allowedTools" in claude and "mcp__huddle__*" in " ".join(claude)
    assert "--disallowedTools" in claude
    di = claude.index("--disallowedTools")
    assert "Edit" in claude[di + 1] and "Write" in claude[di + 1] and "Bash" in claude[di + 1]


def test_readonly_opt_out_restores_full_access(monkeypatch: pytest.MonkeyPatch) -> None:
    """MCP_HUDDLE_READONLY=0 restores the full-access spawn (worker mode)."""
    monkeypatch.setenv("MCP_HUDDLE_READONLY", "0")
    monkeypatch.delenv("MCP_HUDDLE_SPAWN_REGISTRY", raising=False)
    reg = {s["name"]: s for s in spawn._raw_registry()}
    assert "danger-full-access" in " ".join(reg["Codex"]["cmd"])
    assert "--dangerously-skip-permissions" in reg["Claude"]["cmd"]


def test_direct_anthropic_opus_profile_is_manual_and_readonly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The separately named direct profile keeps Claude's read-only envelope."""
    monkeypatch.delenv("MCP_HUDDLE_READONLY", raising=False)
    profile = spawn._claude_opus_review_spec()

    assert profile["name"] == "Claude Opus 5 (direct review)"
    assert profile["enabled"] is False
    assert profile["auto"] is False
    assert profile["profile"] == "claude-opus-direct-review"
    assert profile["cmd"] == [spawn._CLAUDE_BIN or "claude"]
    assert "9router" not in " ".join(profile["cmd"])
    assert "--dangerously-skip-permissions" not in spawn._apply_readonly(profile)["cmd"]
    assert "--allowedTools" in spawn._apply_readonly(profile)["cmd"]


def test_direct_anthropic_opus_stays_readonly_when_global_toggle_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A direct review profile never turns into a writer through the global toggle."""
    monkeypatch.setenv("MCP_HUDDLE_READONLY", "0")
    monkeypatch.setitem(spawn._claude_opus_review_spec(), "enabled", True)
    reg = {spec["name"]: spec for spec in spawn._raw_registry()}

    cmd = reg["Claude Opus 5 (direct review)"]["cmd"]
    assert "--allowedTools" in cmd
    assert "--dangerously-skip-permissions" not in cmd


def test_direct_anthropic_opus_spawn_uses_only_direct_api_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Direct API spawn passes its API key, workspace header, and no OAuth aliases."""
    profile = spawn._claude_opus_review_spec()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("MCP_HUDDLE_CLAUDE_OPUS_WORKSPACE_HEADER", "workspace-header")
    monkeypatch.setenv("MCP_HUDDLE_DIRECT_REVIEW_MCP_URL", "http://127.0.0.1:45111/mcp")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "oauth-alias")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "claude-oauth")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "1")
    monkeypatch.setenv("CLAUDE_CODE_USE_FOUNDRY", "1")
    monkeypatch.setenv("ANTHROPIC_MODEL", "other-model")
    captured: dict[str, object] = {}

    class FakeProc:
        pid = 12345

        def poll(self):
            return None

    def fake_popen(argv, cwd, stdin, stdout, stderr, env):
        captured["argv"] = argv
        captured["env"] = env
        captured["cwd"] = cwd
        return FakeProc()

    monkeypatch.setattr(spawn.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(spawn, "_reap_in_background", lambda *args, **kwargs: None)

    project = tmp_path / "approved-project"
    project.mkdir()
    spawn.spawn_agent(profile, "review", str(project), tmp_path / "agents")

    env = captured["env"]
    assert isinstance(env, dict)
    assert env["ANTHROPIC_BASE_URL"] == "https://api.anthropic.com"
    assert env["ANTHROPIC_CUSTOM_HEADERS"] == "workspace-header"
    assert env["ANTHROPIC_API_KEY"] == "test-key"
    assert "MCP_HUDDLE_CLAUDE_OPUS_WORKSPACE_HEADER" not in env
    assert "MCP_HUDDLE_DIRECT_REVIEW_MCP_URL" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert "CLAUDE_CODE_USE_BEDROCK" not in env
    assert "CLAUDE_CODE_USE_VERTEX" not in env
    assert "CLAUDE_CODE_USE_FOUNDRY" not in env
    assert "ANTHROPIC_MODEL" not in env
    assert "9router" not in " ".join(captured["argv"])
    assert captured["cwd"] == spawn._DIRECT_OPUS_REVIEW_CWD
    assert "--allowedTools" in captured["argv"]
    argv = captured["argv"]
    assert "--bare" in argv and "--restricted" in argv and "--strict-mcp-config" in argv
    assert argv[argv.index("--model") + 1] == "claude-opus-5"
    assert argv[argv.index("--add-dir") + 1] == str(project.resolve())
    mcp_config = json.loads(argv[argv.index("--mcp-config") + 1])
    assert mcp_config["mcpServers"]["huddle"]["url"] == "http://127.0.0.1:45111/mcp"


def test_direct_anthropic_opus_missing_key_fails_before_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Missing direct API credentials are explicit and never reach Popen or logs."""
    profile = spawn._claude_opus_review_spec()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("MCP_HUDDLE_CLAUDE_OPUS_WORKSPACE_HEADER", "workspace-header")
    monkeypatch.setattr(
        spawn.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("must not spawn")
    )

    with pytest.raises(spawn.AgentSpawnError, match="ANTHROPIC_API_KEY"):
        spawn.spawn_agent(profile, "review", str(tmp_path), tmp_path / "agents")

    spawn.log_spawn_failure(
        profile, "review", str(tmp_path), tmp_path / "agents",
        spawn.AgentSpawnError("missing key"),
    )
    assert "workspace-header" not in capsys.readouterr().err


def test_direct_anthropic_opus_rejects_unsafe_runtime_route_before_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The direct profile accepts only a local Huddle endpoint, never a remote route."""
    profile = spawn._claude_opus_review_spec()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("MCP_HUDDLE_CLAUDE_OPUS_WORKSPACE_HEADER", "workspace-header")
    monkeypatch.setenv("MCP_HUDDLE_DIRECT_REVIEW_MCP_URL", "https://example.com/mcp")
    monkeypatch.setattr(
        spawn.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("must not spawn")
    )

    with pytest.raises(spawn.AgentSpawnError, match="MCP_HUDDLE_DIRECT_REVIEW_MCP_URL"):
        spawn.spawn_agent(profile, "review", str(tmp_path), tmp_path / "agents")


def test_direct_anthropic_opus_rejects_symlink_read_root_before_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The restricted runner grants reads only to a real, explicitly supplied room cwd."""
    profile = spawn._claude_opus_review_spec()
    target = tmp_path / "project"
    target.mkdir()
    link = tmp_path / "project-link"
    link.symlink_to(target, target_is_directory=True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("MCP_HUDDLE_CLAUDE_OPUS_WORKSPACE_HEADER", "workspace-header")
    monkeypatch.setenv("MCP_HUDDLE_DIRECT_REVIEW_MCP_URL", "http://127.0.0.1:45111/mcp")
    monkeypatch.setattr(
        spawn.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("must not spawn")
    )

    with pytest.raises(spawn.AgentSpawnError, match="approved read root"):
        spawn.spawn_agent(profile, "review", str(link), tmp_path / "agents")


# ── Rate-limit detection: ANSI stripping + OpenRouter/OpenCode phrasing ───────

def test_detect_rate_limit_strips_ansi_and_matches_openrouter_free_tier(
    tmp_path: Path,
) -> None:
    """OpenCode's `opencode run` writes styled plain text; the OpenRouter
    free-tier 429 must still be recognized once ANSI/SGR codes are stripped."""
    log = tmp_path / "opencode.events.jsonl"
    log.write_bytes(
        "\x1b[0m⚙ \x1b[0mhuddle_room_list\n"
        "\x1b[31mError: Rate limit exceeded: free-models-per-day. "
        "Add 10 credits to unlock 1000 free model requests per day.\x1b[0m\n"
        .encode("utf-8")
    )
    reason = spawn.detect_rate_limit(str(log))
    assert reason is not None
    assert "free-models-per-day" in reason.lower()
    # The stripped reason must not retain raw escape bytes.
    assert "\x1b" not in reason


def test_detect_rate_limit_openrouter_prose_without_hint_not_flagged(
    tmp_path: Path,
) -> None:
    """Bare mention of 'rate limit' with no recovery/quota hint (e.g. an
    agent's own commentary) must stay unflagged — same conservative contract
    as the pre-existing plaintext-requires-hint behavior."""
    log = tmp_path / "opencode-prose.events.jsonl"
    log.write_text(
        "\x1b[0mI think the OpenRouter rate limit policy is reasonable.\x1b[0m\n"
    )
    assert spawn.detect_rate_limit(str(log)) is None


def test_strip_ansi_removes_sgr_sequences() -> None:
    assert spawn._strip_ansi("\x1b[0m⚙ \x1b[0mfoo\x1b[31mbar\x1b[0m") == "⚙ foobar"


# ── Spawn-failure visibility in the room ──────────────────────────────────────

def test_spawn_agents_dict_branch_announces_failure_to_room(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A per-agent spawn exception in the room_create auto_spawn={...} path
    must post one room comment instead of only printing to stdout."""
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path / "huddle"))
    isolated_bus = importlib.reload(bus)
    monkeypatch.setattr(server, "bus", isolated_bus)

    fake_registry: list[spawn.SpawnSpec] = [
        {"name": "Codex", "cmd": ["codex", "exec", "{brief}"], "enabled": True},
    ]
    monkeypatch.setattr(server.spawn, "load_registry", lambda: fake_registry)

    def broken_spawn_agent(spec, brief, cwd, log_dir, verify_alive_sec=0.0, on_exit=None):
        raise FileNotFoundError("codex binary not found")

    monkeypatch.setattr(server.spawn, "spawn_agent", broken_spawn_agent)
    monkeypatch.setattr(server.spawn, "log_spawn_failure", lambda *a, **k: None)

    room_id = isolated_bus.create_room("Fail", "Claude", os.getpid(), str(tmp_path), "sess-1")
    server._spawn_agents(
        room_id=room_id, name="Fail", goal="g",
        cwd=str(tmp_path), owner="Claude",
        auto_spawn={"Codex": "review this"},
    )

    messages = isolated_bus._load_messages(room_id)
    comments = [m for m in messages if m.get("kind") == "comment"]
    assert len(comments) == 1
    assert "Codex" in comments[0]["body"]
    assert "не заспавнился" in comments[0]["body"]
    assert "FileNotFoundError" in comments[0]["body"]


def test_spawn_agents_bool_branch_announces_failure_to_room(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same as above but through the auto_spawn=True path (spawn.spawn_all),
    which needs the on_spawn_fail callback threaded through."""
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path / "huddle"))
    isolated_bus = importlib.reload(bus)
    monkeypatch.setattr(server, "bus", isolated_bus)

    fake_registry: list[spawn.SpawnSpec] = [
        {"name": "Codex", "cmd": ["codex", "exec", "{brief}"], "enabled": True},
    ]
    monkeypatch.setattr(server.spawn, "load_registry", lambda: fake_registry)

    def broken_spawn_agent(spec, brief, cwd, log_dir, verify_alive_sec=0.0, on_exit=None):
        raise FileNotFoundError("codex binary not found")

    monkeypatch.setattr(server.spawn, "spawn_agent", broken_spawn_agent)
    monkeypatch.setattr(server.spawn, "log_spawn_failure", lambda *a, **k: None)

    room_id = isolated_bus.create_room("Fail2", "Claude", os.getpid(), str(tmp_path), "sess-1")
    server._spawn_agents(
        room_id=room_id, name="Fail2", goal="g",
        cwd=str(tmp_path), owner="Claude", auto_spawn=True,
    )

    messages = isolated_bus._load_messages(room_id)
    comments = [m for m in messages if m.get("kind") == "comment"]
    assert len(comments) == 1
    assert "Codex" in comments[0]["body"]
    assert "не заспавнился" in comments[0]["body"]


def test_fresh_spawn_failure_announces_once_and_is_idempotent_on_repeat_wake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raise inside _spawn_fresh_room_agent (registry-agent wake path) must
    post exactly one room comment, and re-waking for the same request must
    not duplicate it (idempotency_key keyed on msg_id)."""
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    isolated_bus = importlib.reload(bus)
    monkeypatch.setattr(server, "bus", isolated_bus)

    fake_spec: spawn.SpawnSpec = {
        "name": "Gemini",
        "cmd": ["gemini", "-p", "{brief}"],
        "enabled": True,
    }
    monkeypatch.setattr(server.spawn, "get_enabled_spec", lambda name: fake_spec if name == "Gemini" else None)

    def broken_spawn_fresh(*args, **kwargs):
        raise RuntimeError("gemini binary crashed on launch")

    monkeypatch.setattr(server, "_spawn_fresh_room_agent", broken_spawn_fresh)

    room_id = isolated_bus.create_room("WakeFail", "CodexMain", 0, "/tmp/project", "session-1")
    isolated_bus.invite_agent(room_id, "Gemini")
    # agent_meta must exist for Gemini or the wake loop skips it entirely
    # (invite_agent alone only adds it to participants).
    log_path = isolated_bus._room_dir(room_id) / "agents" / "gemini.events.jsonl"
    meta = isolated_bus.get_room_info(room_id)
    meta["agent_meta"] = {
        "Gemini": {
            "log_path": str(log_path),
            "last_message_path": None,
            "last_seen_id": 0,
        }
    }
    isolated_bus._write_json(isolated_bus._room_dir(room_id) / "meta.json", meta)

    msg_id = server.message_post(
        room_id, "CodexMain", "Gemini, please review.", "request", to="Gemini",
        idempotency_key="request-gemini-fail-1",
    )

    messages = isolated_bus._load_messages(room_id)
    comments = [m for m in messages if m.get("kind") == "comment"]
    assert len(comments) == 1
    assert "Gemini" in comments[0]["body"]
    assert "не заспавнился" in comments[0]["body"]
    assert "RuntimeError" in comments[0]["body"]

    # Simulate a second wake attempt for the SAME request (e.g. the watchdog
    # fallback retry) — must not post a second comment.
    server._wake_agents_for_request(
        room_id, "CodexMain", "Gemini, please review.", "Gemini", None, msg_id)

    messages_after = isolated_bus._load_messages(room_id)
    comments_after = [m for m in messages_after if m.get("kind") == "comment"]
    assert len(comments_after) == 1


# ── No-reply notices: exit without a posted reply / hung wake ────────────────

def test_on_wake_exit_nonlimit_error_posts_noreply_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-zero exit that is NOT a detected rate-limit, and where the agent
    posted nothing back, must get exactly one noreply comment — and a repeat
    exit callback for the same wake must not duplicate it."""
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    isolated_bus = importlib.reload(bus)
    monkeypatch.setattr(server, "bus", isolated_bus)

    room_id = isolated_bus.create_room("Wake", "Claude", 0, "/tmp/project", "session-1")
    isolated_bus.invite_agent(room_id, "Codex")
    log_path = isolated_bus._room_dir(room_id) / "agents" / "codex.events.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        '{"type":"turn.failed","error":{"message":"boom, unrelated crash"}}\n'
    )

    wake_id = "wake-err"
    meta = isolated_bus.get_room_info(room_id)
    meta["agent_meta"] = {"Codex": {
        "wake_id": wake_id,
        "log_path": str(log_path),
        "last_wake_msg_id": 5,
    }}
    isolated_bus._write_json(isolated_bus._room_dir(room_id) / "meta.json", meta)
    isolated_bus.set_status(room_id, "Codex", "busy", 300, "session-1")

    server._on_wake_exit(room_id, "Codex", wake_id, 1)

    messages = isolated_bus._load_messages(room_id)
    comments = [m for m in messages if m.get("kind") == "comment"]
    assert len(comments) == 1
    assert "не ответил" in comments[0]["body"]
    assert "exit 1" in comments[0]["body"]

    # Repeat exit callback for the SAME wake — idempotent, no duplicate.
    isolated_bus.set_status(room_id, "Codex", "busy", 300, "session-1")
    server._on_wake_exit(room_id, "Codex", wake_id, 1)
    messages_after = isolated_bus._load_messages(room_id)
    comments_after = [m for m in messages_after if m.get("kind") == "comment"]
    assert len(comments_after) == 1


def test_result_reply_marks_agent_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    isolated_bus = importlib.reload(bus)
    monkeypatch.setattr(server, "bus", isolated_bus)

    room_id = isolated_bus.create_room("Complete", "Claude", 0, "/tmp/project", "session-1")
    isolated_bus.invite_agent(room_id, "Codex")
    request_id = isolated_bus.post_message(
        room_id, "Claude", "Review this", "request", to="Codex",
    )
    isolated_bus.set_status(
        room_id, "Codex", "busy", 0, "session-1",
        phase="working", task_id=request_id,
    )

    server.message_post(
        room_id, "Codex", "Review complete", "result", to="Claude",
        reply_to=request_id,
    )

    assert isolated_bus.get_status_details(room_id)["Codex"]["phase"] == "completed"


def test_comment_does_not_mark_agent_completed_on_wake_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    isolated_bus = importlib.reload(bus)
    monkeypatch.setattr(server, "bus", isolated_bus)

    room_id = isolated_bus.create_room("CommentOnly", "Claude", 0, "/tmp/project", "session-1")
    isolated_bus.invite_agent(room_id, "Codex")
    request_id = isolated_bus.post_message(
        room_id, "Claude", "Review this", "request", to="Codex",
    )
    wake_id = "wake-comment-only"
    meta = isolated_bus.get_room_info(room_id)
    meta["agent_meta"] = {"Codex": {
        "wake_id": wake_id, "last_wake_msg_id": request_id,
    }}
    isolated_bus._write_json(isolated_bus._room_dir(room_id) / "meta.json", meta)
    isolated_bus.set_status(room_id, "Codex", "busy", 300, "session-1")
    isolated_bus.post_message(room_id, "Codex", "still researching", "comment")

    server._on_wake_exit(room_id, "Codex", wake_id, 0)

    assert isolated_bus.get_status_details(room_id)["Codex"]["phase"] == "unavailable"


def test_already_announced_dead_wake_is_not_rate_limited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    isolated_bus = importlib.reload(bus)
    monkeypatch.setattr(server, "bus", isolated_bus)

    room_id = isolated_bus.create_room("Dead", "Claude", 0, "/tmp/project", "session-1")
    isolated_bus.invite_agent(room_id, "Codex")
    wake_id = "wake-dead"
    meta = isolated_bus.get_room_info(room_id)
    meta["agent_meta"] = {"Codex": {
        "wake_id": wake_id, "last_wake_msg_id": 1,
    }}
    isolated_bus._write_json(isolated_bus._room_dir(room_id) / "meta.json", meta)
    isolated_bus.set_status(room_id, "Codex", "busy", 300, "session-1")

    server._on_wake_exit(room_id, "Codex", wake_id, 1, already_announced=True)

    status = isolated_bus.get_status_details(room_id)["Codex"]
    assert status["phase"] == "unavailable"
    assert status.get("source") == "server"


def test_on_wake_exit_clean_exit_no_message_posts_noreply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rc==0 but the agent posted nothing back must still get a noreply
    comment (silent success is still silence from the organizer's view)."""
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    isolated_bus = importlib.reload(bus)
    monkeypatch.setattr(server, "bus", isolated_bus)

    room_id = isolated_bus.create_room("Wake", "Claude", 0, "/tmp/project", "session-1")
    isolated_bus.invite_agent(room_id, "Codex")
    wake_id = "wake-silent"
    meta = isolated_bus.get_room_info(room_id)
    meta["agent_meta"] = {"Codex": {"wake_id": wake_id, "last_wake_msg_id": 3}}
    isolated_bus._write_json(isolated_bus._room_dir(room_id) / "meta.json", meta)
    isolated_bus.set_status(room_id, "Codex", "busy", 300, "session-1")

    server._on_wake_exit(room_id, "Codex", wake_id, 0)

    messages = isolated_bus._load_messages(room_id)
    comments = [m for m in messages if m.get("kind") == "comment"]
    assert len(comments) == 1
    assert "без ответа" in comments[0]["body"]
    assert "exit 0" in comments[0]["body"]


def test_on_wake_exit_clean_exit_with_reply_no_noreply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rc==0 and the agent DID post something (any message, not necessarily a
    formal reply_to) after the wake must NOT get a noreply notice."""
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    isolated_bus = importlib.reload(bus)
    monkeypatch.setattr(server, "bus", isolated_bus)

    room_id = isolated_bus.create_room("Wake", "Claude", 0, "/tmp/project", "session-1")
    isolated_bus.invite_agent(room_id, "Codex")
    req_id = isolated_bus.post_message(room_id, "Claude", "please review", "request", to="Codex")
    isolated_bus.post_message(room_id, "Codex", "looks good", "result", reply_to=req_id)

    wake_id = "wake-replied"
    meta = isolated_bus.get_room_info(room_id)
    meta["agent_meta"] = {"Codex": {"wake_id": wake_id, "last_wake_msg_id": req_id}}
    isolated_bus._write_json(isolated_bus._room_dir(room_id) / "meta.json", meta)
    isolated_bus.set_status(room_id, "Codex", "busy", 300, "session-1")

    server._on_wake_exit(room_id, "Codex", wake_id, 0)

    messages = isolated_bus._load_messages(room_id)
    comments = [m for m in messages if m.get("kind") == "comment"]
    assert comments == []


def test_on_wake_exit_rate_limit_suppresses_noreply_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A detected rate-limit exit already explains the silence via its own
    notice — the noreply path must not pile on a second, redundant comment."""
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    monkeypatch.setattr(server, "RATE_LIMIT_COOLDOWN_SECS", 900)
    isolated_bus = importlib.reload(bus)
    monkeypatch.setattr(server, "bus", isolated_bus)

    room_id = isolated_bus.create_room("Wake", "Claude", 0, "/tmp/project", "session-1")
    isolated_bus.invite_agent(room_id, "Codex")
    log_path = isolated_bus._room_dir(room_id) / "agents" / "codex.events.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        '{"type":"turn.failed","error":{"message":"You\'ve hit your usage limit. Try again later."}}\n'
    )
    wake_id = "wake-limited"
    meta = isolated_bus.get_room_info(room_id)
    meta["agent_meta"] = {"Codex": {
        "wake_id": wake_id,
        "log_path": str(log_path),
        "last_wake_msg_id": 7,
    }}
    isolated_bus._write_json(isolated_bus._room_dir(room_id) / "meta.json", meta)
    isolated_bus.set_status(room_id, "Codex", "busy", 300, "session-1")

    server._on_wake_exit(room_id, "Codex", wake_id, 1)

    messages = isolated_bus._load_messages(room_id)
    comments = [m for m in messages if m.get("kind") == "comment"]
    assert len(comments) == 1
    assert "лимит" in comments[0]["body"]
    assert "не ответил" not in comments[0]["body"]
    assert "без ответа" not in comments[0]["body"]


def test_check_stuck_wakes_announces_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wake held 'busy' well past WAKE_STUCK_SECS with a live pid and no
    posted message gets exactly one stuck notice; a repeat sweep for the same
    wake_id does not duplicate it."""
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    monkeypatch.setattr(server, "WAKE_STUCK_SECS", 60)
    # last_wake_pid below is THIS TEST PROCESS's own pid (a real live pid is
    # needed for the "alive" branch) — the stuck-kill feature (see
    # test_check_stuck_wakes_kills_live_pid) must stay OFF here, or it would
    # SIGTERM the test runner itself.
    monkeypatch.setattr(server, "STUCK_KILL_ENABLED", False)
    isolated_bus = importlib.reload(bus)
    monkeypatch.setattr(server, "bus", isolated_bus)

    room_id = isolated_bus.create_room("Wake", "Claude", 0, "/tmp/project", "session-1")
    isolated_bus.invite_agent(room_id, "Codex")

    wake_id = "wake-stuck"
    meta = isolated_bus.get_room_info(room_id)
    meta["agent_meta"] = {"Codex": {
        "wake_id": wake_id,
        "last_wake_pid": os.getpid(),  # alive — this test process itself
        "last_wake_at": int(time.time()) - 3600,
        "last_wake_msg_id": 4,
    }}
    isolated_bus._write_json(isolated_bus._room_dir(room_id) / "meta.json", meta)
    isolated_bus.set_status(room_id, "Codex", "busy", 300, "session-1")

    announced = server._check_stuck_wakes()
    assert announced == [f"Codex@{room_id}"]

    messages = isolated_bus._load_messages(room_id)
    comments = [m for m in messages if m.get("kind") == "comment"]
    assert len(comments) == 1
    assert "не отвечает" in comments[0]["body"]
    assert "жив" in comments[0]["body"]

    # Second sweep for the same wake_id must not duplicate.
    announced_again = server._check_stuck_wakes()
    assert announced_again == []
    messages_after = isolated_bus._load_messages(room_id)
    comments_after = [m for m in messages_after if m.get("kind") == "comment"]
    assert len(comments_after) == 1


def test_check_stuck_wakes_disabled_by_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WAKE_STUCK_SECS=0 disables the check entirely."""
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    monkeypatch.setattr(server, "WAKE_STUCK_SECS", 0)
    isolated_bus = importlib.reload(bus)
    monkeypatch.setattr(server, "bus", isolated_bus)

    room_id = isolated_bus.create_room("Wake", "Claude", 0, "/tmp/project", "session-1")
    isolated_bus.invite_agent(room_id, "Codex")
    meta = isolated_bus.get_room_info(room_id)
    meta["agent_meta"] = {"Codex": {
        "wake_id": "wake-x",
        "last_wake_pid": os.getpid(),
        "last_wake_at": int(time.time()) - 100_000,
        "last_wake_msg_id": 1,
    }}
    isolated_bus._write_json(isolated_bus._room_dir(room_id) / "meta.json", meta)
    isolated_bus.set_status(room_id, "Codex", "busy", 300, "session-1")

    assert server._check_stuck_wakes() == []


def test_check_stuck_wakes_kills_live_pid_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default STUCK_KILL_ENABLED behavior: a stuck wake with a genuinely
    live pid gets SIGTERMed after the announcement, and the kill is recorded
    via stuck_killed_wake_id (so the reaper's own _on_wake_exit doesn't post
    a second, redundant notice — see test_on_wake_exit_after_stuck_kill_*)."""
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    monkeypatch.setattr(server, "WAKE_STUCK_SECS", 60)
    monkeypatch.setattr(server, "STUCK_KILL_ENABLED", True)
    isolated_bus = importlib.reload(bus)
    monkeypatch.setattr(server, "bus", isolated_bus)

    room_id = isolated_bus.create_room("Wake", "Claude", 0, "/tmp/project", "session-1")
    isolated_bus.invite_agent(room_id, "Codex")

    proc = subprocess.Popen(["sleep", "30"])
    try:
        wake_id = "wake-kill"
        meta = isolated_bus.get_room_info(room_id)
        meta["agent_meta"] = {"Codex": {
            "wake_id": wake_id,
            "last_wake_pid": proc.pid,
            "last_wake_at": int(time.time()) - 3600,
            "last_wake_msg_id": 4,
        }}
        isolated_bus._write_json(isolated_bus._room_dir(room_id) / "meta.json", meta)
        isolated_bus.set_status(room_id, "Codex", "busy", 300, "session-1")

        announced = server._check_stuck_wakes()
        assert announced == [f"Codex@{room_id}"]

        rc = proc.wait(timeout=5)  # SIGTERM was delivered — must exit promptly
        assert rc != 0  # terminated, not a clean exit

        messages = isolated_bus._load_messages(room_id)
        comments = [m for m in messages if m.get("kind") == "comment"]
        assert len(comments) == 1
        assert "остановлен" in comments[0]["body"]

        info = isolated_bus.get_room_info(room_id)["agent_meta"]["Codex"]
        assert info["stuck_killed_wake_id"] == wake_id
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_check_stuck_wakes_disabled_flag_announces_only_no_kill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP_HUDDLE_STUCK_KILL=0 (STUCK_KILL_ENABLED False) → legacy
    announce-only behavior, process is left running, no stuck_killed_wake_id."""
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    monkeypatch.setattr(server, "WAKE_STUCK_SECS", 60)
    monkeypatch.setattr(server, "STUCK_KILL_ENABLED", False)
    isolated_bus = importlib.reload(bus)
    monkeypatch.setattr(server, "bus", isolated_bus)

    room_id = isolated_bus.create_room("Wake", "Claude", 0, "/tmp/project", "session-1")
    isolated_bus.invite_agent(room_id, "Codex")

    proc = subprocess.Popen(["sleep", "30"])
    try:
        wake_id = "wake-no-kill"
        meta = isolated_bus.get_room_info(room_id)
        meta["agent_meta"] = {"Codex": {
            "wake_id": wake_id,
            "last_wake_pid": proc.pid,
            "last_wake_at": int(time.time()) - 3600,
            "last_wake_msg_id": 4,
        }}
        isolated_bus._write_json(isolated_bus._room_dir(room_id) / "meta.json", meta)
        isolated_bus.set_status(room_id, "Codex", "busy", 300, "session-1")

        announced = server._check_stuck_wakes()
        assert announced == [f"Codex@{room_id}"]

        assert proc.poll() is None  # still alive — not killed

        messages = isolated_bus._load_messages(room_id)
        comments = [m for m in messages if m.get("kind") == "comment"]
        assert len(comments) == 1
        assert "жив" in comments[0]["body"]
        assert "остановлен" not in comments[0]["body"]

        info = isolated_bus.get_room_info(room_id)["agent_meta"]["Codex"]
        assert "stuck_killed_wake_id" not in info
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_on_wake_exit_after_stuck_kill_suppresses_noreply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once _check_stuck_wakes has recorded stuck_killed_wake_id for a wake,
    the killed process's own reaper on_exit (_on_wake_exit) must not post a
    second, redundant noreply notice on top of the stuck notice."""
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    isolated_bus = importlib.reload(bus)
    monkeypatch.setattr(server, "bus", isolated_bus)

    room_id = isolated_bus.create_room("Wake", "Claude", 0, "/tmp/project", "session-1")
    isolated_bus.invite_agent(room_id, "Codex")

    wake_id = "wake-kill-2"
    meta = isolated_bus.get_room_info(room_id)
    meta["agent_meta"] = {"Codex": {
        "wake_id": wake_id,
        "last_wake_pid": 999999999,  # _on_wake_exit doesn't consult this
        "last_wake_at": int(time.time()) - 3600,
        "last_wake_msg_id": 4,
        "stuck_killed_wake_id": wake_id,  # as if _check_stuck_wakes just killed it
    }}
    isolated_bus._write_json(isolated_bus._room_dir(room_id) / "meta.json", meta)
    isolated_bus.set_status(room_id, "Codex", "busy", 300, "session-1")
    # The stuck notice itself, as _check_stuck_wakes would have posted it.
    isolated_bus.post_message(room_id, "Codex", "⏳ stuck — killed", "comment")

    server._on_wake_exit(room_id, "Codex", wake_id, -15)  # SIGTERM exit

    messages = isolated_bus._load_messages(room_id)
    comments = [m for m in messages if m.get("kind") == "comment"]
    assert len(comments) == 1  # no second (noreply) notice piled on top
    statuses = isolated_bus.get_status(room_id)
    assert statuses.get("Codex") == "online"  # lease still released normally


# ── Dead-wake watchdog check (_check_dead_wakes) ────────────────────────────

_DEAD_PID = 999_999_999  # convention used across the suite: no such process


def test_check_dead_wakes_announces_and_clears_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 'busy' lease whose pid is dead, past the grace window, with no
    reply posted, gets exactly one deadwake notice and its lease is cleared
    (status back to online) — a repeat sweep does not duplicate it."""
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    monkeypatch.setattr(server, "DEAD_WAKE_GRACE_SECS", 60)
    isolated_bus = importlib.reload(bus)
    monkeypatch.setattr(server, "bus", isolated_bus)

    room_id = isolated_bus.create_room("Wake", "Claude", 0, "/tmp/project", "session-1")
    isolated_bus.invite_agent(room_id, "Codex")

    wake_id = "wake-dead"
    meta = isolated_bus.get_room_info(room_id)
    meta["agent_meta"] = {"Codex": {
        "wake_id": wake_id,
        "last_wake_pid": _DEAD_PID,
        "last_wake_at": int(time.time()) - 3600,
        "last_wake_msg_id": 4,
    }}
    isolated_bus._write_json(isolated_bus._room_dir(room_id) / "meta.json", meta)
    isolated_bus.set_status(room_id, "Codex", "busy", 300, "session-1")

    announced = server._check_dead_wakes()
    assert announced == [f"Codex@{room_id}"]

    messages = isolated_bus._load_messages(room_id)
    comments = [m for m in messages if m.get("kind") == "comment"]
    assert len(comments) == 1
    assert "умер" in comments[0]["body"]
    assert str(_DEAD_PID) in comments[0]["body"]

    statuses = isolated_bus.get_status(room_id)
    assert statuses.get("Codex") == "online"

    # Repeat sweep: lease already cleared (status no longer 'busy') — no
    # second comment.
    announced_again = server._check_dead_wakes()
    assert announced_again == []
    messages_after = isolated_bus._load_messages(room_id)
    comments_after = [m for m in messages_after if m.get("kind") == "comment"]
    assert len(comments_after) == 1


def test_check_dead_wakes_respects_grace_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead pid whose wake started less than DEAD_WAKE_GRACE_SECS ago is
    left alone — gives the normal reaper callback a chance to fire first."""
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    monkeypatch.setattr(server, "DEAD_WAKE_GRACE_SECS", 3600)
    isolated_bus = importlib.reload(bus)
    monkeypatch.setattr(server, "bus", isolated_bus)

    room_id = isolated_bus.create_room("Wake", "Claude", 0, "/tmp/project", "session-1")
    isolated_bus.invite_agent(room_id, "Codex")
    meta = isolated_bus.get_room_info(room_id)
    meta["agent_meta"] = {"Codex": {
        "wake_id": "wake-fresh",
        "last_wake_pid": _DEAD_PID,
        "last_wake_at": int(time.time()) - 5,  # just started
        "last_wake_msg_id": 1,
    }}
    isolated_bus._write_json(isolated_bus._room_dir(room_id) / "meta.json", meta)
    isolated_bus.set_status(room_id, "Codex", "busy", 300, "session-1")

    assert server._check_dead_wakes() == []
    statuses = isolated_bus.get_status(room_id)
    assert statuses.get("Codex") == "busy"  # lease untouched


def test_check_dead_wakes_skips_notice_but_still_clears_lease_if_agent_replied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agent posted after the wake started (e.g. its final message raced
    a crash of the wrapper process) — not a dead-man wake, so no extra
    notice, but the lease must still be released: skipping the release would
    leak the busy status forever (a dead pid is never eligible for the
    stuck-wake path, which requires a LIVE pid)."""
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    monkeypatch.setattr(server, "DEAD_WAKE_GRACE_SECS", 60)
    isolated_bus = importlib.reload(bus)
    monkeypatch.setattr(server, "bus", isolated_bus)

    room_id = isolated_bus.create_room("Wake", "Claude", 0, "/tmp/project", "session-1")
    isolated_bus.invite_agent(room_id, "Codex")
    msg_id = isolated_bus.post_message(room_id, "Claude", "go", "request")
    reply_id = isolated_bus.post_message(room_id, "Codex", "done", "ack")

    meta = isolated_bus.get_room_info(room_id)
    meta["agent_meta"] = {"Codex": {
        "wake_id": "wake-replied",
        "last_wake_pid": _DEAD_PID,
        "last_wake_at": int(time.time()) - 3600,
        "last_wake_msg_id": msg_id,
    }}
    isolated_bus._write_json(isolated_bus._room_dir(room_id) / "meta.json", meta)
    isolated_bus.set_status(room_id, "Codex", "busy", 300, "session-1")

    announced = server._check_dead_wakes()
    assert announced == [f"Codex@{room_id}"]  # lease released, no notice needed

    messages = isolated_bus._load_messages(room_id)
    assert not any(m.get("kind") == "comment" for m in messages)
    assert reply_id  # sanity: the reply itself was recorded

    statuses = isolated_bus.get_status(room_id)
    assert statuses.get("Codex") == "online"  # lease actually released


def test_check_dead_wakes_skips_notice_for_already_stuck_killed_wake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If _check_stuck_wakes killed a process whose own reaper thread never
    fires (e.g. it died together with the server), _check_dead_wakes must
    still release the lease but must NOT post its own "процесс умер" notice
    on top of the stuck notice that already explained the silence."""
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    monkeypatch.setattr(server, "DEAD_WAKE_GRACE_SECS", 60)
    isolated_bus = importlib.reload(bus)
    monkeypatch.setattr(server, "bus", isolated_bus)

    room_id = isolated_bus.create_room("Wake", "Claude", 0, "/tmp/project", "session-1")
    isolated_bus.invite_agent(room_id, "Codex")
    isolated_bus.post_message(room_id, "Codex", "⏳ stuck — killed", "comment")

    wake_id = "wake-stuck-then-dead"
    meta = isolated_bus.get_room_info(room_id)
    meta["agent_meta"] = {"Codex": {
        "wake_id": wake_id,
        "last_wake_pid": _DEAD_PID,
        "last_wake_at": int(time.time()) - 3600,
        "last_wake_msg_id": 4,
        "stuck_killed_wake_id": wake_id,
    }}
    isolated_bus._write_json(isolated_bus._room_dir(room_id) / "meta.json", meta)
    isolated_bus.set_status(room_id, "Codex", "busy", 300, "session-1")

    announced = server._check_dead_wakes()
    assert announced == [f"Codex@{room_id}"]  # lease released

    messages = isolated_bus._load_messages(room_id)
    comments = [m for m in messages if m.get("kind") == "comment"]
    assert len(comments) == 1  # only the earlier stuck notice — no dup

    statuses = isolated_bus.get_status(room_id)
    assert statuses.get("Codex") == "online"


def test_check_dead_wakes_ignores_alive_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live pid is left for _check_stuck_wakes (the slow path) — the
    dead-wake sweep never touches it."""
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    monkeypatch.setattr(server, "DEAD_WAKE_GRACE_SECS", 60)
    isolated_bus = importlib.reload(bus)
    monkeypatch.setattr(server, "bus", isolated_bus)

    room_id = isolated_bus.create_room("Wake", "Claude", 0, "/tmp/project", "session-1")
    isolated_bus.invite_agent(room_id, "Codex")
    meta = isolated_bus.get_room_info(room_id)
    meta["agent_meta"] = {"Codex": {
        "wake_id": "wake-alive",
        "last_wake_pid": os.getpid(),  # alive — this test process itself
        "last_wake_at": int(time.time()) - 3600,
        "last_wake_msg_id": 1,
    }}
    isolated_bus._write_json(isolated_bus._room_dir(room_id) / "meta.json", meta)
    isolated_bus.set_status(room_id, "Codex", "busy", 300, "session-1")

    assert server._check_dead_wakes() == []
    statuses = isolated_bus.get_status(room_id)
    assert statuses.get("Codex") == "busy"  # untouched, still eligible for stuck-check


def test_check_dead_wakes_disabled_by_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP_HUDDLE_DEAD_WAKE_GRACE_SEC=0 disables the check entirely."""
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    monkeypatch.setattr(server, "DEAD_WAKE_GRACE_SECS", 0)
    isolated_bus = importlib.reload(bus)
    monkeypatch.setattr(server, "bus", isolated_bus)

    room_id = isolated_bus.create_room("Wake", "Claude", 0, "/tmp/project", "session-1")
    isolated_bus.invite_agent(room_id, "Codex")
    meta = isolated_bus.get_room_info(room_id)
    meta["agent_meta"] = {"Codex": {
        "wake_id": "wake-x",
        "last_wake_pid": _DEAD_PID,
        "last_wake_at": int(time.time()) - 100_000,
        "last_wake_msg_id": 1,
    }}
    isolated_bus._write_json(isolated_bus._room_dir(room_id) / "meta.json", meta)
    isolated_bus.set_status(room_id, "Codex", "busy", 300, "session-1")

    assert server._check_dead_wakes() == []


# ── No-reply notices for the room_create INITIAL auto_spawn (not a wake) ────
#
# _spawn_agents (room_create auto_spawn) attached no exit callback beyond a
# best-effort rate-limit check — an agent that started fine but died before
# posting anything (e.g. a real `opencode run` exit with "database is
# locked", rc != 0) left the room silent with no notice. _on_initial_spawn_exit
# closes that gap the same way _on_wake_exit does for wakes, just without a
# busy lease to release (there never was one for the very first spawn).

def test_on_initial_spawn_exit_nonzero_no_post_announces_noreply_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An initial auto_spawn agent whose process exits rc!=0 having posted
    nothing to the room at all gets exactly one noreply comment; a repeat
    exit callback for the same initial spawn must not duplicate it."""
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    isolated_bus = importlib.reload(bus)
    monkeypatch.setattr(server, "bus", isolated_bus)

    room_id = isolated_bus.create_room("Init", "Claude", 0, "/tmp/project", "session-1")
    isolated_bus.invite_agent(room_id, "OpenCode")
    log_path = isolated_bus._room_dir(room_id) / "agents" / "opencode.events.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text('{"type":"error","error":"database is locked"}\n')
    meta = isolated_bus.get_room_info(room_id)
    meta["agent_meta"] = {"OpenCode": {"log_path": str(log_path)}}
    isolated_bus._write_json(isolated_bus._room_dir(room_id) / "meta.json", meta)

    server._on_initial_spawn_exit(room_id, "OpenCode", 1)

    messages = isolated_bus._load_messages(room_id)
    comments = [m for m in messages if m.get("kind") == "comment"]
    assert len(comments) == 1
    assert "не ответил" in comments[0]["body"]
    assert "exit 1" in comments[0]["body"]

    # Repeat exit callback (e.g. reaper fires twice) — idempotent, no duplicate.
    server._on_initial_spawn_exit(room_id, "OpenCode", 1)
    messages_after = isolated_bus._load_messages(room_id)
    comments_after = [m for m in messages_after if m.get("kind") == "comment"]
    assert len(comments_after) == 1


def test_on_initial_spawn_exit_posted_message_no_noreply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An initial auto_spawn agent that posted ANY message before exiting
    (even non-zero rc) must NOT get a noreply notice — it already explained
    itself to the room."""
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    isolated_bus = importlib.reload(bus)
    monkeypatch.setattr(server, "bus", isolated_bus)

    room_id = isolated_bus.create_room("Init", "Claude", 0, "/tmp/project", "session-1")
    isolated_bus.invite_agent(room_id, "OpenCode")
    isolated_bus.post_message(room_id, "OpenCode", "reviewing now", "comment")

    server._on_initial_spawn_exit(room_id, "OpenCode", 1)

    messages = isolated_bus._load_messages(room_id)
    comments = [m for m in messages if m.get("kind") == "comment"]
    # Only the agent's own pre-exit comment — no noreply notice piled on.
    assert len(comments) == 1
    assert comments[0]["agent"] == "OpenCode"


def test_on_initial_spawn_exit_rate_limit_suppresses_noreply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A detected rate-limit on an initial-spawn exit already explains the
    silence via its own notice — the noreply path must not pile on a second,
    redundant comment."""
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    monkeypatch.setattr(server, "RATE_LIMIT_COOLDOWN_SECS", 900)
    isolated_bus = importlib.reload(bus)
    monkeypatch.setattr(server, "bus", isolated_bus)

    room_id = isolated_bus.create_room("Init", "Claude", 0, "/tmp/project", "session-1")
    isolated_bus.invite_agent(room_id, "Codex")
    log_path = isolated_bus._room_dir(room_id) / "agents" / "codex.events.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        '{"type":"turn.failed","error":{"message":"You\'ve hit your usage limit. Try again later."}}\n'
    )
    meta = isolated_bus.get_room_info(room_id)
    meta["agent_meta"] = {"Codex": {"log_path": str(log_path)}}
    isolated_bus._write_json(isolated_bus._room_dir(room_id) / "meta.json", meta)

    server._on_initial_spawn_exit(room_id, "Codex", 1)

    messages = isolated_bus._load_messages(room_id)
    comments = [m for m in messages if m.get("kind") == "comment"]
    assert len(comments) == 1
    assert "лимит" in comments[0]["body"]
    assert "не ответил" not in comments[0]["body"]
    assert "без ответа" not in comments[0]["body"]


def test_spawn_agents_dict_branch_wires_noreply_callback_on_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end wiring check: _spawn_agents (auto_spawn={...} dict branch)
    must pass an on_exit that, when invoked with a non-zero returncode and no
    posted reply, produces a noreply notice — not just a rate-limit check."""
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path / "huddle"))
    isolated_bus = importlib.reload(bus)
    monkeypatch.setattr(server, "bus", isolated_bus)

    fake_registry: list[spawn.SpawnSpec] = [
        {"name": "OpenCode", "cmd": ["opencode", "run", "{brief}"], "enabled": True},
    ]
    monkeypatch.setattr(server.spawn, "load_registry", lambda: fake_registry)

    captured_on_exit = {}

    def fake_spawn_agent(spec, brief, cwd, log_dir, verify_alive_sec=0.0, on_exit=None):
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "opencode.events.jsonl"
        log_path.write_text('{"type":"error","error":"database is locked"}\n')
        captured_on_exit["cb"] = on_exit
        return 4242, str(log_path), None

    monkeypatch.setattr(server.spawn, "spawn_agent", fake_spawn_agent)

    room_id = isolated_bus.create_room("Init2", "Claude", os.getpid(), str(tmp_path), "sess-1")
    server._spawn_agents(
        room_id=room_id, name="Init2", goal="g",
        cwd=str(tmp_path), owner="Claude",
        auto_spawn={"OpenCode": "review this"},
    )

    assert captured_on_exit.get("cb") is not None
    captured_on_exit["cb"](1)  # simulate the reaper thread reporting exit 1

    messages = isolated_bus._load_messages(room_id)
    comments = [m for m in messages if m.get("kind") == "comment"]
    assert len(comments) == 1
    assert "OpenCode" in comments[0]["body"]
    assert "не ответил" in comments[0]["body"]

    assert server._check_dead_wakes() == []


# ── Cold-spawn brief protocol discipline (room_31d32c82) ────────────────────
# Root cause: the default auto_spawn=True brief was one shared generic blob
# with no concrete "you are X" — small models spawned cold answered on
# stdout instead of calling message_post, or posted under another agent's
# name. The wake-up prompt never had this problem (explicit room_id, own
# name, exact tool call). These tests lock the cold-spawn briefs to the same
# discipline.


def test_default_brief_contains_room_id_own_name_and_goal() -> None:
    brief = server._build_default_brief(
        "room_xyz", "room-name", "review the diff", "/tmp/proj",
        agent_name="OpenCode")
    assert "room_xyz" in brief
    assert "OpenCode" in brief
    assert "message_post" in brief
    assert "review the diff" in brief
    # Own-identity instruction, not just a bare mention of the name.
    assert 'You are: OpenCode' in brief
    assert 'agent="OpenCode"' in brief


def test_all_spawn_prompts_include_lifecycle_wait_protocol() -> None:
    """Every spawned turn must know how to report work and when to wait."""
    samples = {
        "fresh": server._build_fresh_agent_prompt(
            "room_xyz", "OpenCode", "review this", "1: prior message"),
        "registry": server._build_registry_agent_wakeup_prompt(
            "room_xyz", "OpenCode", "Claude", "review this", "OpenCode",
            7, 3, "1: prior message"),
        "codex": server._build_codex_wakeup_prompt(
            "room_xyz", "Claude", "review this", "Codex", 7, 3),
        "default": server._build_default_brief(
            "room_xyz", "room-name", "review this", "/tmp/proj",
            agent_name="OpenCode"),
        "custom": server._wrap_user_brief(
            "room_xyz", "OpenCode", "review this"),
    }
    for name, prompt in samples.items():
        assert "status_set" in prompt, name
        assert "room_status" in prompt, name
        assert "queued" in prompt and "completed" in prompt, name
        assert "process_alive" in prompt, name


def test_non_mcp_runner_prompts_explain_server_owned_completion() -> None:
    from mcp_huddle import mimo_runner, openai_compatible_runner

    request = {"id": 7, "agent": "Claude", "body": "review this"}
    samples = {
        "openai": openai_compatible_runner.build_messages(
            "OpenRouter", "1: prior message", request, "high")[0]["content"],
        "mimo": mimo_runner.build_prompt(
            "MiMo", "1: prior message", request),
    }
    for name, prompt in samples.items():
        assert "completed" in prompt, name
        assert "server" in prompt, name
        assert "process" in prompt, name


def test_all_reviewer_prompts_require_evidence_before_consensus() -> None:
    """No agent path may turn agreement into an unsupported factual verdict."""
    from mcp_huddle import mimo_runner, openai_compatible_runner

    request = {"id": 7, "agent": "Claude", "body": "review this"}
    samples = {
        "fresh": server._build_fresh_agent_prompt(
            "room_xyz", "OpenCode", "review this", "1: prior message"),
        "registry": server._build_registry_agent_wakeup_prompt(
            "room_xyz", "OpenCode", "Claude", "review this", "OpenCode",
            7, 3, "1: prior message"),
        "codex": server._build_codex_wakeup_prompt(
            "room_xyz", "Claude", "review this", "Codex", 7, 3),
        "default": server._build_default_brief(
            "room_xyz", "room-name", "review this", "/tmp/proj",
            agent_name="OpenCode"),
        "custom": server._wrap_user_brief(
            "room_xyz", "OpenCode", "review this"),
        "openai": openai_compatible_runner.build_messages(
            "OpenRouter", "1: prior message", request, "high")[0]["content"],
        "mimo": mimo_runner.build_prompt(
            "MiMo", "1: prior message", request),
    }
    for name, prompt in samples.items():
        assert "Consensus is not correctness" in prompt, name
        assert "stated goal" in prompt and "constraints" in prompt, name
        assert "evidence quality" in prompt and "risks/unknowns" in prompt, name
        assert "reversibility" in prompt, name
        assert "source URL" in prompt and "file:line" in prompt, name
        assert "test/command result" in prompt and "message id" in prompt, name
        assert "inference" in prompt and "unknown" in prompt, name


def test_auto_spawn_true_gives_each_agent_its_own_name_in_the_brief(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two agents spawned via auto_spawn=True must NOT receive the exact same
    brief text — each one's brief must carry its OWN name, not a neighbor's
    (this is what let a model post as "Claude" while actually being spawned
    as a different registry agent)."""
    fake_registry: list[spawn.SpawnSpec] = [
        {"name": "First",  "cmd": ["echo", "first={brief}"],  "enabled": True},
        {"name": "Second", "cmd": ["echo", "second={brief}"], "enabled": True},
    ]
    monkeypatch.setattr(spawn, "load_registry", lambda: fake_registry)
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path / "huddle"))
    monkeypatch.setattr(bus, "HUDDLE_HOME", tmp_path / "huddle")
    monkeypatch.setenv("MCP_HUDDLE_SAME_BIN_STAGGER_SEC", "0")  # both use "echo"

    captured: dict[str, str] = {}
    real_spawn_agent = spawn.spawn_agent

    def capture(spec, brief, *a, **kw):
        captured[spec["name"]] = brief
        return real_spawn_agent(spec, brief, *a, **kw)

    monkeypatch.setattr(spawn, "spawn_agent", capture)

    room_id = bus.create_room("test-room", "Human", os.getpid(), str(tmp_path), "sess-1")
    server._spawn_agents(
        room_id=room_id, name="test-room", goal="g", cwd=str(tmp_path),
        owner="Human", auto_spawn=True,
    )

    assert set(captured) == {"First", "Second"}
    assert 'You are: First' in captured["First"]
    assert 'agent="First"' in captured["First"]
    assert 'You are: Second' in captured["Second"]
    assert 'agent="Second"' in captured["Second"]
    # Cross-check: First's own-identity line never claims to be Second.
    assert 'You are: Second' not in captured["First"]
    assert 'You are: First' not in captured["Second"]


def test_dict_auto_spawn_wraps_user_brief_with_protocol_preamble(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """auto_spawn={name: brief} must preserve the user's text verbatim while
    adding the same room_id/own-name/message_post/anti-loop preamble as the
    default brief — the user text is the task, the preamble is the delivery
    protocol (both are needed, per live room_31d32c82 evidence)."""
    fake_registry: list[spawn.SpawnSpec] = [
        {"name": "Reviewer", "cmd": ["echo", "r={brief}"], "enabled": True},
    ]
    monkeypatch.setattr(spawn, "load_registry", lambda: fake_registry)
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path / "huddle"))
    monkeypatch.setattr(bus, "HUDDLE_HOME", tmp_path / "huddle")

    captured: dict[str, str] = {}
    real_spawn_agent = spawn.spawn_agent

    def capture(spec, brief, *a, **kw):
        captured[spec["name"]] = brief
        return real_spawn_agent(spec, brief, *a, **kw)

    monkeypatch.setattr(spawn, "spawn_agent", capture)

    user_text = "Please look for off-by-one errors in the pagination code."
    room_id = bus.create_room("test-room", "Human", os.getpid(), str(tmp_path), "sess-1")
    server._spawn_agents(
        room_id=room_id, name="test-room", goal="g", cwd=str(tmp_path),
        owner="Human", auto_spawn={"Reviewer": user_text},
    )

    brief = captured["Reviewer"]
    assert user_text in brief  # verbatim, not rephrased
    assert room_id in brief
    assert 'You are: Reviewer' in brief
    assert 'agent="Reviewer"' in brief
    assert "message_post" in brief
    assert "Anti-loop" in brief


def test_wakeup_prompt_format_unchanged_after_refactor() -> None:
    """The wake-up prompt's exact wire format (parsed by
    openai_compatible_runner.extract_request_id and asserted on by other
    tests) must survive the shared-helper refactor byte-for-byte for the
    lines other code depends on."""
    prompt = server._build_registry_agent_wakeup_prompt(
        "room_test", "OpenCode", "Claude", "please check this", "OpenCode",
        7, 3, "1: hi\n2: there")
    assert "Room: room_test" in prompt
    assert "You are: OpenCode" in prompt
    assert "New request id: 7" in prompt
    assert "Current full transcript:" in prompt
    assert ('message_post(room_id="room_test", agent="OpenCode", kind="result", '
            'to="Claude", reply_to=7, '
            'idempotency_key="opencode-wake:room_test:7")') in prompt

    codex_prompt = server._build_codex_wakeup_prompt(
        "room_test", "Claude", "please check this", "Codex", 7, 3)
    assert ('message_post(room_id="room_test", agent="Codex", kind="result", '
            'to="Claude", reply_to=7, '
            'idempotency_key="codex-wake:room_test:7")') in codex_prompt
