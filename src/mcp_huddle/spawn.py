"""Configurable agent-spawn registry for auto_spawn=True rooms.

Default registry uses Codex + Gemini CLIs. Override via the
MCP_HUDDLE_SPAWN_REGISTRY env var pointing to a JSON file.

Each registry entry is a SpawnSpec:
  {
    "name": "Codex",                              # display name in the room
    "cmd":  ["codex", "...", "{brief}"],          # argv; "{brief}" is replaced
    "enabled": true                               # set False to skip
  }

A missing binary is auto-disabled at module-load time so room_create with
auto_spawn=True never crashes — it just spawns whatever is available.
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import TypedDict


class SpawnSpec(TypedDict):
    name: str
    cmd: list[str]
    enabled: bool


# Default registry: enabled=False if the binary is missing.
DEFAULT_REGISTRY: list[SpawnSpec] = [
    {
        "name": "Codex",
        "cmd": [
            "codex", "-a", "never", "exec",
            "-m", "gpt-5.4",
            "-c", 'model_reasoning_effort="medium"',
            "-s", "workspace-write",
            "{brief}",
        ],
        "enabled": shutil.which("codex") is not None,
    },
    {
        "name": "Gemini",
        "cmd": [
            "gemini", "-m", "gemini-3.1-pro-preview", "-y",
            "-p", "{brief}",
        ],
        "enabled": shutil.which("gemini") is not None,
    },
]


def load_registry() -> list[SpawnSpec]:
    """Return registry from MCP_HUDDLE_SPAWN_REGISTRY (JSON file) or DEFAULT_REGISTRY."""
    path = os.environ.get("MCP_HUDDLE_SPAWN_REGISTRY")
    if path and Path(path).is_file():
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError(f"{path}: expected JSON array of SpawnSpec")
        return data
    return DEFAULT_REGISTRY


def spawn_agent(spec: SpawnSpec, brief: str, cwd: str) -> int:
    """Spawn one agent. Returns its PID."""
    argv = [arg.replace("{brief}", brief) for arg in spec["cmd"]]
    proc = subprocess.Popen(
        argv,
        cwd=cwd or None,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc.pid


def spawn_all(brief: str, cwd: str) -> tuple[list[str], list[int]]:
    """Spawn every enabled agent in the registry. Returns (names, pids)."""
    names: list[str] = []
    pids: list[int] = []
    for spec in load_registry():
        if not spec.get("enabled"):
            continue
        try:
            pid = spawn_agent(spec, brief, cwd)
            pids.append(pid)
            names.append(spec["name"])
        except (FileNotFoundError, OSError):
            # Tolerate races (binary disappears between check and spawn).
            pass
    return names, pids
