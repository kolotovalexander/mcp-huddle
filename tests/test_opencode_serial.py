from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from mcp_huddle import spawn


HELPER = Path(spawn.__file__).with_name("opencode_serial.py")


def _wrapped(lock_path: Path, *command: str) -> list[str]:
    return [sys.executable, str(HELPER), str(lock_path), "--", *command]


def test_spawn_agent_wraps_timeout_opencode_and_leaves_other_runners_alone(
    tmp_path, monkeypatch,
):
    captured = {}

    class FakePopen:
        pid = 4321

    def popen(argv, **kwargs):
        captured["argv"] = argv
        return FakePopen()

    monkeypatch.setattr(spawn.subprocess, "Popen", popen)
    monkeypatch.setattr(spawn, "_reap_in_background", lambda *a, **k: "handle")
    spec = {
        "name": "OpenCode",
        "cmd": ["/usr/bin/timeout", "1200", "/opt/homebrew/bin/opencode", "run", "{brief}"],
        "enabled": True,
    }
    spawn.spawn_agent(spec, "prompt", str(tmp_path), tmp_path / "logs")
    argv = captured["argv"]
    assert argv[:2] == [sys.executable, str(HELPER)]
    assert argv[2] == str(spawn.bus.HUDDLE_HOME / "internal" / "opencode-run.lock")
    assert argv[3:5] == ["--", "/usr/bin/timeout"]
    assert argv[-2:] == ["run", "prompt"]
    assert spawn._serialize_opencode_argv(["codex", "exec"]) == ["codex", "exec"]


def test_wrapped_opencode_lifetimes_do_not_overlap(tmp_path):
    lock_path = tmp_path / "internal" / "opencode.lock"
    events_path = tmp_path / "events.txt"
    body = (
        "import os,sys,time; "
        "fd=os.open(sys.argv[1],os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600); "
        "os.write(fd,(sys.argv[2]+' start\\n').encode()); time.sleep(.2); "
        "os.write(fd,(sys.argv[2]+' end\\n').encode()); os.close(fd)"
    )
    command = lambda label: _wrapped(
        lock_path, sys.executable, "-c", body, str(events_path), label,
    )
    first = subprocess.Popen(command("one"), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    second = subprocess.Popen(command("two"), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    assert first.wait(timeout=5) == 0
    assert second.wait(timeout=5) == 0

    active = 0
    maximum = 0
    events = events_path.read_text().splitlines()
    assert len(events) == 4
    for event in events:
        if event.endswith(" start"):
            active += 1
            maximum = max(maximum, active)
        else:
            active -= 1
    assert active == 0
    assert maximum == 1


def test_terminating_current_opencode_releases_lock_for_waiting_process(tmp_path):
    lock_path = tmp_path / "internal" / "opencode.lock"
    ready = tmp_path / "ready"
    continued = tmp_path / "continued"
    first_code = "import pathlib,time,sys; pathlib.Path(sys.argv[1]).touch(); time.sleep(30)"
    second_code = "import pathlib,sys; pathlib.Path(sys.argv[1]).touch()"
    first = subprocess.Popen(
        _wrapped(lock_path, sys.executable, "-c", first_code, str(ready)),
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 3
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(.01)
    assert ready.exists()

    second = subprocess.Popen(
        _wrapped(lock_path, sys.executable, "-c", second_code, str(continued)),
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    time.sleep(.1)
    assert not continued.exists()
    first.terminate()
    assert first.wait(timeout=3) != 0
    assert second.wait(timeout=3) == 0
    assert continued.exists()
