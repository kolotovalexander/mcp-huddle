"""Interactive runner daemon for agents that require a live TTY (e.g. Antigravity/agy).

Usage:
    python -m mcp_huddle.interactive_runner --agent Antigravity
    python -m mcp_huddle.interactive_runner --agent Antigravity --runner-dir /tmp/my-runner

The daemon:
- Writes its PID to <runner_dir>/pid on start so spawn.py can detect liveness.
- Polls <runner_dir>/tasks/ every 0.5 s for JSON task files written by spawn_via_runner().
- For each task, substitutes {brief} into the agent cmd, then runs it with stdin
  inherited from the terminal so OAuth/interactive prompts work.
- Streams stdout+stderr to both the terminal (for human watching) and the task log file.
- Moves finished task files to <runner_dir>/done/.
- Removes the PID file on clean exit (SIGTERM / SIGINT / normal return).

Per-room lifecycle: each agent child is spawned in its own process group
(start_new_session=True). The daemon registers the child's pid into the task's
room (bus.runner_register_child) so close_room can SIGTERM the whole subtree and
free its RAM the moment the room closes — the long-lived daemon itself is never
killed. A task whose room closed while it sat queued is skipped.

Task JSON schema (written by spawn_via_runner):
    {"id": "<hex12>", "brief": "<prompt>", "cwd": "<path>", "log": "<log path>",
     "room_id": "<room>", "agent": "<name>", "wake_id": "<id>",
     "session_id": "<id>"}
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from mcp_huddle import bus, spawn


def _write_pid(pid_file: Path, pid: int) -> None:
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(pid))


def _remove_pid(pid_file: Path) -> None:
    try:
        pid_file.unlink(missing_ok=True)
    except OSError:
        pass


def _tee(src, *dsts) -> None:
    """Forward bytes from src to all dsts until EOF."""
    while True:
        chunk = src.read(4096)
        if not chunk:
            break
        for dst in dsts:
            try:
                dst.write(chunk)
                dst.flush()
            except OSError:
                pass


def run_task(task: dict, spec: spawn.SpawnSpec) -> None:
    """Run one task synchronously — blocks until the agent exits.

    Per-room lifecycle: the child is spawned in its OWN process group
    (start_new_session=True) so close_room can SIGTERM the whole agy subtree
    (it spawns helpers) without touching this long-lived daemon. The child pid
    is registered into the room's meta on start and removed on exit, so the
    server's close/kill path frees the RAM the moment the room closes. A task
    whose room already closed while it sat queued is skipped.
    """
    brief = task.get("brief", "")
    cwd = task.get("cwd") or None
    room_id = task.get("room_id") or ""
    agent = task.get("agent") or spec.get("name") or ""
    session_id = task.get("session_id") or ""
    log_path = Path(task["log"])
    log_path.parent.mkdir(parents=True, exist_ok=True)

    if room_id and not bus.room_is_active(room_id):
        print(f"[interactive_runner] skip task {task['id']}: room {room_id} "
              "closed/gone before start", flush=True)
        return

    cmd = [part.replace("{brief}", brief) for part in spec["cmd"]]

    print(f"\n[interactive_runner] starting task {task['id']}: {cmd[0]} …", flush=True)

    log_file = open(log_path, "ab", buffering=0)
    proc = None
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdin=None,            # inherit terminal — allows OAuth prompts
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # own process group → killpg kills the subtree
        )
        if room_id:
            bus.runner_register_child(room_id, agent, proc.pid, session_id)
        assert proc.stdout is not None
        _tee(proc.stdout, sys.stdout.buffer, log_file)
        proc.wait()
    finally:
        log_file.close()
        if room_id and proc is not None:
            bus.runner_unregister_child(room_id, agent, proc.pid, session_id)

    print(f"[interactive_runner] task {task['id']} exited (rc={proc.returncode})", flush=True)


def run_daemon(agent_name: str, runner_dir: Path) -> None:
    pid_file = runner_dir / "pid"
    tasks_dir = runner_dir / "tasks"
    done_dir = runner_dir / "done"

    runner_dir.mkdir(parents=True, exist_ok=True)
    tasks_dir.mkdir(parents=True, exist_ok=True)
    done_dir.mkdir(parents=True, exist_ok=True)

    _write_pid(pid_file, os.getpid())
    print(f"[interactive_runner] started for '{agent_name}' (pid={os.getpid()})", flush=True)
    print(f"[interactive_runner] watching {tasks_dir}", flush=True)

    # Resolve the spec once so we don't hit disk on every poll.
    spec: spawn.SpawnSpec | None = None
    for s in spawn._raw_registry():
        if s.get("name") == agent_name:
            spec = s
            break
    if spec is None:
        print(f"[interactive_runner] ERROR: no registry entry for '{agent_name}'", file=sys.stderr)
        _remove_pid(pid_file)
        sys.exit(1)

    def _cleanup(signum=None, frame=None):
        print("\n[interactive_runner] shutting down", flush=True)
        _remove_pid(pid_file)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _cleanup)
    signal.signal(signal.SIGINT, _cleanup)

    try:
        while True:
            task_files = sorted(tasks_dir.glob("*.json"))
            for tf in task_files:
                try:
                    task = json.loads(tf.read_text())
                except (OSError, json.JSONDecodeError):
                    continue  # file still being written; try next poll
                try:
                    run_task(task, spec)
                except Exception as exc:
                    print(f"[interactive_runner] task {tf.name} failed: {exc}", file=sys.stderr)
                finally:
                    try:
                        tf.rename(done_dir / tf.name)
                    except OSError:
                        pass
            time.sleep(0.5)
    except Exception:
        _remove_pid(pid_file)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Interactive runner daemon for mcp-huddle agents")
    parser.add_argument("--agent", required=True, help="Agent name (must exist in registry, e.g. Antigravity)")
    parser.add_argument("--runner-dir", help="Override default runner directory (~/.mcp-huddle/runners/<agent>)")
    args = parser.parse_args()

    runner_dir = Path(args.runner_dir) if args.runner_dir else spawn._default_runner_dir(args.agent)
    run_daemon(args.agent, runner_dir)


if __name__ == "__main__":
    main()
