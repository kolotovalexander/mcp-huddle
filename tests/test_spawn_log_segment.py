"""The append-log boundary for one freshly spawned agent generation."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from mcp_huddle import bus, spawn


@pytest.fixture(autouse=True)
def drain_spawn_reapers():
    yield
    spawn._drain_background_for_tests()


def _spec():
    return {
        "name": "SegmentProbe",
        "cmd": [sys.executable, "-c", "print('new generation')"],
        "enabled": True,
    }


@pytest.mark.parametrize("previous", [b"", b"previous generation\n"])
def test_callback_receives_open_log_size_before_popen(tmp_path: Path, monkeypatch, previous: bytes) -> None:
    log_dir = tmp_path / "agents"
    log_dir.mkdir()
    log_path = log_dir / "segmentprobe.events.jsonl"
    if previous:
        log_path.write_bytes(previous)

    events = []
    original_popen = subprocess.Popen

    def record_popen(*args, **kwargs):
        events.append("popen")
        return original_popen(*args, **kwargs)

    monkeypatch.setattr(spawn.subprocess, "Popen", record_popen)

    def on_log_open(start_offset: int, opened_path: str) -> None:
        events.append("callback")
        assert opened_path == str(log_path)
        assert start_offset == len(previous)
        assert log_path.read_bytes() == previous

    pid, actual_path, last_message_path = spawn.spawn_agent(
        _spec(), "brief", str(tmp_path), log_dir, on_log_open=on_log_open,
    )
    assert pid > 0
    assert actual_path == str(log_path)
    assert last_message_path is None
    assert events == ["callback", "popen"]


def test_callback_error_prevents_child_and_closes_log(tmp_path: Path, monkeypatch) -> None:
    opened = []
    original_open = spawn._open_standalone_log

    def record_open(*args, **kwargs):
        log_file = original_open(*args, **kwargs)
        opened.append(log_file)
        return log_file

    def forbidden_popen(*args, **kwargs):
        pytest.fail("Popen was called after the log callback failed")

    monkeypatch.setattr(spawn, "_open_standalone_log", record_open)
    monkeypatch.setattr(spawn.subprocess, "Popen", forbidden_popen)

    def fail_callback(start_offset: int, log_path: str) -> None:
        raise RuntimeError("receipt store unavailable")

    with pytest.raises(RuntimeError, match="receipt store unavailable"):
        spawn.spawn_agent(
            _spec(), "brief", str(tmp_path), tmp_path / "agents",
            on_log_open=fail_callback,
        )
    assert len(opened) == 1
    assert opened[0].closed


def test_existing_caller_needs_no_callback(tmp_path: Path) -> None:
    pid, log_path, last_message_path = spawn.spawn_agent(
        _spec(), "brief", str(tmp_path), tmp_path / "agents",
    )
    assert pid > 0
    assert Path(log_path).exists()
    assert last_message_path is None


def test_room_log_callback_uses_confined_append_file(tmp_path: Path, monkeypatch) -> None:
    huddle_home = tmp_path / "huddle"
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(huddle_home))
    monkeypatch.setattr(bus, "HUDDLE_HOME", huddle_home)
    room_id = bus.create_room("Segment", "Human", os.getpid(), str(tmp_path), "session")
    expected_log, _ = bus._agent_paths(room_id, "SegmentProbe", create=True)
    previous = b"previous room generation\n"
    expected_log.write_bytes(previous)
    observed = []

    spawn.spawn_agent(
        _spec(), "brief", str(tmp_path), tmp_path / "ignored",
        owner_room_id=room_id,
        on_log_open=lambda offset, path: observed.append((offset, path)),
    )
    assert observed == [(len(previous), str(expected_log))]
