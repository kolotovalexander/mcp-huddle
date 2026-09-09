from pathlib import Path
from types import SimpleNamespace

from mcp_huddle import child_processes, spawn


def _secret_markers(monkeypatch) -> None:
    monkeypatch.setenv("AUDIT_UNRELATED_SECRET", "must-not-pass")
    monkeypatch.setenv("GH_TOKEN", "must-not-pass")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-pass")


def test_initial_spawn_passes_only_baseline_and_explicit_provider_env(
    monkeypatch, tmp_path: Path
) -> None:
    captured = {}
    _secret_markers(monkeypatch)
    monkeypatch.setenv("TEST_PROVIDER_KEY", "provider-value")
    monkeypatch.setenv("EXPLICIT_TOOL_CONFIG", "config-value")
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path / "huddle"))

    def fake_popen(*args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(pid=1234, poll=lambda: None)

    monkeypatch.setattr(spawn.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(spawn, "_reap_in_background", lambda *a, **k: None)
    spec: spawn.SpawnSpec = {
        "name": "Provider",
        "cmd": ["runner", "--api-key-env", "TEST_PROVIDER_KEY", "{brief}"],
        "enabled": True,
        "pass_env": ["EXPLICIT_TOOL_CONFIG"],
    }

    spawn.spawn_agent(spec, "review", str(tmp_path), tmp_path / "logs")

    env = captured["env"]
    assert env["TEST_PROVIDER_KEY"] == "provider-value"
    assert env["EXPLICIT_TOOL_CONFIG"] == "config-value"
    assert env["MCP_HUDDLE_HOME"] == str(tmp_path / "huddle")
    assert "AUDIT_UNRELATED_SECRET" not in env
    assert "GH_TOKEN" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env


def test_codex_resume_uses_sanitized_environment(monkeypatch, tmp_path: Path) -> None:
    captured = {}
    _secret_markers(monkeypatch)
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path / "huddle"))

    def fake_popen(*args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(pid=4321)

    monkeypatch.setattr(spawn.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(spawn, "_reap_in_background", lambda *a, **k: None)
    spawn.codex_resume("thread-id", "prompt", str(tmp_path), str(tmp_path / "codex.jsonl"))

    env = captured["env"]
    assert env["MCP_HUDDLE_HOME"] == str(tmp_path / "huddle")
    assert "AUDIT_UNRELATED_SECRET" not in env
    assert "GH_TOKEN" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env


def test_codex_resume_retained_fallback_does_not_report_spawn_failure(
    monkeypatch, tmp_path: Path,
) -> None:
    callbacks = []

    class LiveProc:
        pid = 4_322
        returncode = None

        def poll(self):
            return self.returncode

    proc = LiveProc()

    class UnstartableThread:
        def __init__(self, *, target, name, daemon):
            self.name = name

        def start(self):
            raise RuntimeError("fallback Thread.start failed")

    monkeypatch.setattr(spawn.subprocess, "Popen", lambda *a, **kw: proc)
    monkeypatch.setattr(spawn.threading, "Thread", UnstartableThread)
    monkeypatch.setattr(
        spawn, "_reap_in_background",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("reaper setup failed")),
    )
    monkeypatch.setattr(spawn, "_terminate_unregistered_child", lambda *a, **kw: False)

    pid = spawn.codex_resume(
        "thread-id", "prompt", str(tmp_path), str(tmp_path / "codex.jsonl"),
        on_exit=callbacks.append, process_handle="codex-fallback",
    )

    assert pid == proc.pid
    assert child_processes.owns_exact(proc, "", "codex-fallback") is True
    proc.returncode = 0
    spawn._drain_background_for_tests(timeout=1)
    assert callbacks == [0]


def test_codex_resume_registry_failure_retains_direct_popen_lifecycle(
    monkeypatch, tmp_path: Path,
) -> None:
    callbacks = []

    class LiveProc:
        pid = 4_323
        returncode = None

        def poll(self):
            return self.returncode

    proc = LiveProc()
    monkeypatch.setattr(spawn.subprocess, "Popen", lambda *a, **kw: proc)
    monkeypatch.setattr(
        child_processes, "register",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("registry unavailable")),
    )
    monkeypatch.setattr(spawn, "_terminate_unregistered_child", lambda *a, **kw: False)

    pid = spawn.codex_resume(
        "thread-id", "prompt", str(tmp_path), str(tmp_path / "codex.jsonl"),
        on_exit=callbacks.append, process_handle="codex-unowned",
    )

    assert pid == proc.pid
    assert callbacks == []
    assert child_processes.owns_exact(proc, "", "codex-unowned") is False
    proc.returncode = 0
    spawn._drain_background_for_tests(timeout=1)
    assert callbacks == [0]
