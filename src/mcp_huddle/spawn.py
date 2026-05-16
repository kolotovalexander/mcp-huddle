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
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import TypedDict


class SpawnSpec(TypedDict):
    name: str
    cmd: list[str]
    enabled: bool


class AgentSpawnError(RuntimeError):
    """Raised when a process starts but fails the optional health check."""


def _first_existing_binary(candidates: list[str]) -> str | None:
    """Return the first executable found by PATH lookup or absolute fallback.

    Launchd/daemon environments often have a reduced PATH. Using absolute
    fallbacks keeps auto_spawn stable even when an interactive shell can see a
    binary that the MCP daemon cannot.
    """
    for candidate in candidates:
        if "/" in candidate:
            if Path(candidate).exists():
                return candidate
            continue
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return None


_CODEX_BIN = _first_existing_binary([
    "codex",
    "/opt/homebrew/bin/codex",
    "/Applications/Codex.app/Contents/Resources/codex",
])
_GEMINI_BIN = _first_existing_binary([
    "gemini",
    "/opt/homebrew/bin/gemini",
])


# Default registry: enabled=False if the binary is missing.
# `--json` (Codex) / `-o stream-json` (Gemini) emit structured events to stdout
# which the dashboard SSE endpoint streams to the browser. `{last_message}` is
# replaced with a per-agent file path so the agent's last reply is captured for
# downstream tools.
DEFAULT_REGISTRY: list[SpawnSpec] = [
    {
        "name": "Codex",
        "cmd": [
            _CODEX_BIN or "codex", "-a", "never", "exec",
            "--disable", "guardian_approval",
            "--json",
            "--output-last-message", "{last_message}",
            "-m", "gpt-5.5",
            "-c", 'model_reasoning_effort="medium"',
            "-s", "read-only",
            "{brief}",
        ],
        "enabled": _CODEX_BIN is not None,
    },
    {
        "name": "Gemini",
        "cmd": [
            _GEMINI_BIN or "gemini",
            "-m", "gemini-3.1-pro-preview", "-y",
            "-o", "stream-json",
            "-p", "{brief}",
        ],
        "enabled": _GEMINI_BIN is not None,
    },
]


def _resolve_spawn_args(
    spec: SpawnSpec,
    brief: str,
    log_dir: Path,
) -> tuple[list[str], str | None]:
    name = spec["name"]
    last_msg_path: str | None = None
    argv = []
    for arg in spec["cmd"]:
        if "{brief}" in arg:
            arg = arg.replace("{brief}", brief)
        if "{last_message}" in arg:
            last_msg_path = str(log_dir / f"{name.lower()}.last_message.txt")
            arg = arg.replace("{last_message}", last_msg_path)
        argv.append(arg)
    return argv, last_msg_path


def log_spawn_failure(
    spec: SpawnSpec,
    brief: str,
    cwd: str,
    log_dir: Path,
    exc: BaseException,
) -> None:
    """Write spawn failures to stderr so daemon logs keep the real cause."""
    try:
        argv, _ = _resolve_spawn_args(spec, brief, log_dir)
    except Exception:  # pragma: no cover - defensive logging only
        argv = list(spec.get("cmd", []))
    log_path = log_dir / f"{spec['name'].lower()}.events.jsonl"
    print(
        "[mcp-huddle] failed to spawn "
        f"{spec['name']}: {type(exc).__name__}: {exc}; "
        f"cwd={cwd!r}; argv={argv!r}; log_path={log_path}",
        file=sys.stderr,
        flush=True,
    )
    traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)


def _reap_in_background(proc: subprocess.Popen, name: str) -> None:
    """Ensure short-lived spawned agents do not remain as defunct children."""
    def wait_for_exit() -> None:
        try:
            proc.wait()
        except Exception:
            pass

    thread = threading.Thread(
        target=wait_for_exit,
        name=f"mcp-huddle-reap-{name}-{proc.pid}",
        daemon=True,
    )
    thread.start()


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


def get_enabled_spec(agent_name: str) -> SpawnSpec | None:
    """Return an enabled registry spec by display name."""
    for spec in load_registry():
        if spec.get("name") == agent_name and spec.get("enabled"):
            return spec
    return None


def spawn_agent(
    spec: SpawnSpec,
    brief: str,
    cwd: str,
    log_dir: Path,
    verify_alive_sec: float = 0.0,
) -> tuple[int, str, str | None]:
    """Spawn one agent.

    Returns (pid, log_path, last_message_path).
    last_message_path is None for agents whose argv doesn't reference {last_message}.

    Side effects: creates log_dir, opens log file, redirects stdout+stderr to it.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    name = spec["name"]
    log_path = log_dir / f"{name.lower()}.events.jsonl"
    argv, last_msg_path = _resolve_spawn_args(spec, brief, log_dir)

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
    _reap_in_background(proc, name)
    if verify_alive_sec > 0:
        time.sleep(verify_alive_sec)
        returncode = proc.poll()
        if returncode is not None:
            exc = AgentSpawnError(
                f"{name} exited within {verify_alive_sec:.3g}s with status {returncode}"
            )
            log_spawn_failure(spec, brief, cwd, log_dir, exc)
            raise exc
    return proc.pid, str(log_path), last_msg_path


def spawn_all(
    brief: str,
    cwd: str,
    log_dir: Path,
    briefs: dict[str, str] | None = None,
    verify_alive_sec: float = 0.0,
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
            pid, log_path, last_msg = spawn_agent(
                spec, agent_brief, cwd, log_dir, verify_alive_sec=verify_alive_sec
            )
            pids.append(pid)
            names.append(spec["name"])
            agent_meta[spec["name"]] = {
                "log_path": log_path,
                "last_message_path": last_msg,
            }
        except (FileNotFoundError, PermissionError) as exc:
            # Tolerate races (binary disappears between check and spawn).
            log_spawn_failure(spec, agent_brief, cwd, log_dir, exc)
        except AgentSpawnError:
            # spawn_agent already logged the concrete early-exit status.
            pass
        except OSError as exc:
            log_spawn_failure(spec, agent_brief, cwd, log_dir, exc)
            raise
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


def codex_log_has_completed_turn(log_path: str) -> bool:
    """Return True once Codex has persisted at least one completed turn."""
    p = Path(log_path)
    if not p.exists():
        return False
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
                if obj.get("type") == "turn.completed":
                    return True
    except OSError:
        return False
    return False


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
        _CODEX_BIN or "codex", "-a", "never",            # top-level: never auto-approve tool calls
        "exec", "resume", thread_id,                     # subcommand
        "--json",                                        # JSONL events to stdout
        "-c", 'model="gpt-5.5"',                         # config override (resume supports -c)
        "-c", 'model_reasoning_effort="medium"',
        "-c", "features.guardian_approval=false",
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
    _reap_in_background(proc, "Codex")
    return proc.pid
