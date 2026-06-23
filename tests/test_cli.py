"""CLI subcommands (post/read/rooms) — the headless-agent path into a room.

These drive main(argv) directly against an isolated bus, proving a non-MCP
process can post and read through bus.py's file locks.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

import mcp_huddle.bus as bus
from mcp_huddle.__main__ import main


@pytest.fixture()
def isolated_bus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    reloaded = importlib.reload(bus)
    yield reloaded
    monkeypatch.delenv("MCP_HUDDLE_HOME", raising=False)
    importlib.reload(bus)


def test_post_then_read_roundtrip(isolated_bus, capsys: pytest.CaptureFixture[str]) -> None:
    room_id = isolated_bus.create_room("CLI", "Owner", 0)

    main(["post", "--room", room_id, "--agent", "Tester", "--body", "hello from headless"])
    posted = capsys.readouterr().out.strip()
    assert posted.isdigit()  # prints the assigned message id

    main(["read", "--room", room_id])
    log = capsys.readouterr().out
    assert "Tester" in log
    assert "hello from headless" in log


def test_rooms_lists_the_room(isolated_bus, capsys: pytest.CaptureFixture[str]) -> None:
    room_id = isolated_bus.create_room("Findme", "Owner", 0)
    main(["rooms"])
    out = capsys.readouterr().out
    assert room_id in out
    assert "Findme" in out


def test_post_to_closed_room_exits_nonzero(
    isolated_bus, capsys: pytest.CaptureFixture[str]
) -> None:
    room_id = isolated_bus.create_room("CLI", "Owner", 0)
    isolated_bus.close_room(room_id, "Owner")

    with pytest.raises(SystemExit) as exc:
        main(["post", "--room", room_id, "--agent", "Tester", "--body", "too late"])
    assert exc.value.code == 1
    assert "closed" in capsys.readouterr().err.lower()
