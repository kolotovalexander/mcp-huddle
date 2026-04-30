"""Configurable agent-spawn registry for auto_spawn rooms.

Default registry uses Codex + Gemini CLIs. Override via the
MCP_HUDDLE_SPAWN_REGISTRY env var pointing to a JSON file.

Each registry entry is a SpawnSpec:
  {
    "name": "Codex",                              # display name in the room
    "cmd":  ["codex", "...", "{brief}"],          # argv; "{brief}" is replaced
    "enabled": true                               # set False to skip
  }

A missing binary is auto-disabled at module-load time so room_create with
auto_spawn never crashes — it just spawns whatever is available.

Phase 1 changes (2026-04-30):
  * Codex spawned with `--json` and `--output-last-message <file>` for
    structured event capture and last-message extraction.
  * Gemini spawned with `-o stream-json` for streaming events.
  * stdout+stderr redirected to per-room per-agent JSONL log file
    (~/.mcp-huddle/rooms/<id>/agents/<name>.events.jsonl) instead of DEVNULL.
    This is what feeds the dashboard SSE pane.
  * spawn_all accepts an optional briefs dict {AgentName: brief} for
    per-agent custom briefs (Phase 1: Claude can task each spawned agent
    differently).
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
# `--json` (Codex) / `-o stream-json` (Gemini) emit structured events to stdout
# which the dashboard SSE endpoint streams to the browser. `{last_message}` is
# replaced with a per-agent file path so the agent's last reply is captured for
# downstream tools.
DEFAULT_REGISTRY: list[SpawnSpec] = [
    {
        "name": "Codex",
        "cmd": [
            "codex", "-a", "never", "exec",
            "--json",
            "--output-last-message", "{last_message}",
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
            "-o", "stream-json",
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


def spawn_agent(spec: SpawnSpec, brief: str, cwd: str, log_dir: Path) -> tuple[int, str, str | None]:
    """Spawn one agent.

    Returns (pid, log_path, last_message_path).
    last_message_path is None for agents whose argv doesn't reference {last_message}.

    Side effects: creates log_dir, opens log file, redirects stdout+stderr to it.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    name = spec["name"]
    log_path = log_dir / f"{name.lower()}.events.jsonl"
    last_msg_path: str | None = None

    argv = []
    for arg in spec["cmd"]:
        if "{brief}" in arg:
            arg = arg.replace("{brief}", brief)
        if "{last_message}" in arg:
            last_msg_path = str(log_dir / f"{name.lower()}.last_message.txt")
            arg = arg.replace("{last_message}", last_msg_path)
        argv.append(arg)

    log_file = open(log_path, "ab", buffering=0)
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd or None,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
    finally:
        # Popen dups the fd into the child; we can close ours so the parent
        # process doesn't keep the log file held open after the child exits.
        log_file.close()
    return proc.pid, str(log_path), last_msg_path


def spawn_all(
    brief: str,
    cwd: str,
    log_dir: Path,
    briefs: dict[str, str] | None = None,
) -> tuple[list[str], list[int], dict[str, dict[str, str | None]]]:
    """Spawn every enabled agent in the registry.

    Args:
      brief: default brief used when `briefs` doesn't have a per-agent entry.
      cwd: working directory for spawned processes.
      log_dir: where each agent's <name>.events.jsonl is written.
      briefs: optional {AgentName: brief} for per-agent customization.

    Returns:
      (names, pids, agent_meta) where agent_meta is
      {name: {"log_path": "...", "last_message_path": "..." or None}}.
    """
    names: list[str] = []
    pids: list[int] = []
    agent_meta: dict[str, dict[str, str | None]] = {}
    briefs = briefs or {}
    for spec in load_registry():
        if not spec.get("enabled"):
            continue
        agent_brief = briefs.get(spec["name"], brief)
        try:
            pid, log_path, last_msg = spawn_agent(spec, agent_brief, cwd, log_dir)
            pids.append(pid)
            names.append(spec["name"])
            agent_meta[spec["name"]] = {
                "log_path": log_path,
                "last_message_path": last_msg,
            }
        except (FileNotFoundError, OSError):
            # Tolerate races (binary disappears between check and spawn).
            pass
    return names, pids, agent_meta


# ── Phase 2: thread_id capture + Codex resume ────────────────────────────────

def parse_codex_thread_id(log_path: str, timeout: float = 10.0) -> str | None:
    """Tail the agent log file until we see a {"type":"thread.started",...} event.
    Returns thread_id (UUID string) or None on timeout / non-Codex agents.

    Codex --json emits this as the very first line of stdout, so this typically
    completes in <1s after spawn.
    """
    import time
    deadline = time.time() + timeout
    p = Path(log_path)
    while time.time() < deadline:
        if p.exists() and p.stat().st_size > 0:
            try:
                with open(p, "rb") as f:
                    for raw in f:
                        line = raw.decode("utf-8", errors="replace").strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if obj.get("type") == "thread.started" and "thread_id" in obj:
                            return obj["thread_id"]
            except OSError:
                pass
        time.sleep(0.1)
    return None


def codex_resume(thread_id: str, prompt: str, cwd: str, log_path: str,
                 last_msg_path: str | None = None) -> int:
    """Resume a Codex thread with a new prompt. Cheaper than fresh spawn —
    Codex remembers prior conversation via its rollout file.

    Appends events to the same log_path so the dashboard SSE keeps streaming.
    Returns PID of the spawned codex exec resume process.

    `codex exec resume` only accepts a small set of options (no -m/-s/-a
    subcommand-local). Top-level `codex` flags must come BEFORE `exec`.
    Model + reasoning effort overrides are passed via -c key=value (which
    `exec resume` does support).
    """
    argv = [
        "codex", "-a", "never",                          # top-level: never auto-approve tool calls
        "exec", "resume", thread_id,                     # subcommand
        "--json",                                        # JSONL events to stdout
        "-c", 'model="gpt-5.4"',                         # config override (resume supports -c)
        "-c", 'model_reasoning_effort="medium"',
    ]
    if last_msg_path:
        argv += ["-o", last_msg_path]                    # short form of --output-last-message
    argv.append(prompt)

    log_file = open(log_path, "ab", buffering=0)
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd or None,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
    finally:
        log_file.close()
    return proc.pid
