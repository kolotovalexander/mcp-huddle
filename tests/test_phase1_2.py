"""Tests for Phase 1 (per-agent brief, log capture) and Phase 2 (Codex
thread_id parsing, codex_resume helper, /api/room_agents and SSE endpoint)."""
import json
import os
import tempfile
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
    names, _, _ = spawn.spawn_all(
        brief="B", cwd=str(tmp_path), log_dir=tmp_path / "agents",
        skip_names={"Codex"},
    )
    assert sorted(names) == ["Antigravity", "Claude"]
    assert "Codex" not in names


def test_default_registry_includes_claude_codex_antigravity_qwen_deepseek() -> None:
    """All six canonical agents must be in DEFAULT_REGISTRY (enabled depends
    on which binaries the test host has installed)."""
    spec_names = {s["name"] for s in spawn.DEFAULT_REGISTRY}
    assert spec_names == {"Codex", "Antigravity", "MiMo", "Qwen", "DeepSeek", "Claude"}


def test_mimo_spec_uses_runner() -> None:
    """MiMo huddle slot goes through mimo_runner (MCP hangs upstream in `mimo
    run` 0.1.x, so MiMo cannot call huddle MCP tools itself) and is gated by
    env flag."""
    spec = next(s for s in spawn.DEFAULT_REGISTRY if s["name"] == "MiMo")
    assert "mcp_huddle.mimo_runner" in spec["cmd"]
    assert "{brief}" in spec["cmd"][-1]
    if spawn._MIMO_BIN is None:
        assert spec["enabled"] is False
    else:
        expected = os.environ.get("MCP_HUDDLE_MIMO_ENABLED", "1") != "0"
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


def test_deepseek_spec_uses_max_local_model() -> None:
    """DeepSeek huddle slot should use the strongest real FreeDeepseekAPI alias.
    `deepseek-reasoner` (V4-Flash thinking mode) is the strongest supported id;
    the old `deepseek-v4-pro` alias does not exist on the bridge → probe failed."""
    spec = next(s for s in spawn.DEFAULT_REGISTRY if s["name"] == "DeepSeek")
    assert spec["requires_model"] == "deepseek-reasoner"
    assert spec["probe_url"] == "http://127.0.0.1:9655/v1/models"
    assert spec["probe_chat_url"] == "http://127.0.0.1:9655/v1/chat/completions"
    assert "--model" in spec["cmd"]
    assert "deepseek-reasoner" in spec["cmd"]


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

    def fake_popen(argv, cwd, stdin, stdout, stderr):
        popen_calls.append({"argv": argv, "cwd": cwd, "stdin": stdin, "stderr": stderr})
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
        "respond_via_agent", "status_set", "status_get",
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


def test_no_dropped_tool_references_in_agent_facing_prompts() -> None:
    """Generated agent prompts and MCP server instructions must not advertise
    tools that were dropped from the agent surface (room_c216d6e8 consensus).
    This catches a class of regression that the surface-only test misses:
    documentation/prompts pointing agents at non-existent MCP tools.

    Per Codex review of feat/agent-tools-cleanup."""
    dropped = [
        "room_close", "room_close_session", "room_delete", "room_request_close",
        "status_set", "status_get", "respond_via_agent",
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
