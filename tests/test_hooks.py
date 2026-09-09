import json
import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "script",
    [
        ROOT / "src/mcp_huddle/hooks/claude-check.sh",
        ROOT / "src/mcp_huddle/hooks/gemini-check.sh",
        ROOT / "examples/hooks/claude-check.sh",
        ROOT / "examples/hooks/gemini-check.sh",
    ],
)
def test_hook_claims_managed_notification_without_filename_injection(
    script: Path, tmp_path: Path
) -> None:
    notification_dir = tmp_path / "notifications"
    notification_dir.mkdir()
    target = notification_dir / (
        'agent-bus-x\');__import__("pathlib").Path("PWNED").touch();#-notify.json'
    )
    target.write_text(json.dumps({
        "room_id": "room_safe",
        "from_agent": "Reviewer\nforged-line",
        "msg_id": 7,
    }))
    env = dict(os.environ, MCP_HUDDLE_HOME=str(tmp_path))

    completed = subprocess.run(
        ["bash", str(script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    assert "Huddle [room_safe]" in completed.stdout
    assert "Reviewer forged-line" in completed.stdout
    assert not target.exists()
    assert not (tmp_path / "PWNED").exists()
    assert list(notification_dir.glob("*.claim.*")) == []


@pytest.mark.parametrize(
    "script",
    [
        ROOT / "src/mcp_huddle/hooks/claude-check.sh",
        ROOT / "src/mcp_huddle/hooks/gemini-check.sh",
        ROOT / "examples/hooks/claude-check.sh",
        ROOT / "examples/hooks/gemini-check.sh",
    ],
)
def test_hook_refuses_symlink_notification_without_reading_target(
    script: Path, tmp_path: Path
) -> None:
    notification_dir = tmp_path / "notifications"
    notification_dir.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({
        "room_id": "SECRET_OUTSIDE_NOTIFICATION_ROOT",
        "from_agent": "attacker",
        "msg_id": 99,
    }))
    target = notification_dir / "agent-bus-test-notify.json"
    target.symlink_to(outside)

    completed = subprocess.run(
        ["bash", str(script)],
        cwd=tmp_path,
        env=dict(os.environ, MCP_HUDDLE_HOME=str(tmp_path)),
        capture_output=True,
        text=True,
        check=True,
    )

    assert completed.stdout == ""
    assert outside.exists()
    assert "SECRET_OUTSIDE_NOTIFICATION_ROOT" in outside.read_text()
    assert not target.exists()
    assert list(notification_dir.glob("*.claim.*")) == []
