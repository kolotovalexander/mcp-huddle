"""One-shot MiMo Code huddle agent runner.

MiMo Code 0.1.x has an upstream bug: headless `mimo run` hangs forever before
the session starts when ANY MCP server is configured (stdio or HTTP, including
its own auto-import from ~/.claude.json). So MiMo cannot call huddle MCP tools
itself. Like openai_compatible_runner, this runner enforces the huddle
anti-loop contract in process: read room messages from disk, answer one pending
request by shelling out to `mimo run` with all MCP hard-disabled via env
kill-switches, and append the result through the same bus layer.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import tempfile
import time

from . import bus
from .openai_compatible_runner import (
    _event,
    extract_request_id,
    extract_room_id,
    select_request,
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1b\].*?(?:\x07|\x1b\\)|\[0m")

# Without these, a single configured MCP server makes `mimo run` hang forever
# (racy deadlock, MiMo Code 0.1.0 / 0.1.1-preview.1, verified 2026-06-13).
# MIMOCODE_CONFIG_DIR points at an empty dir so the user's
# ~/.config/mimocode/mimocode.json (which may enable MCP) is never loaded.
_ISOLATED_CONFIG_DIR = os.path.join(tempfile.gettempdir(), "mimo-runner-home")
_MCP_KILL_SWITCHES = {
    "MIMOCODE_CONFIG_DIR": _ISOLATED_CONFIG_DIR,
    "MIMOCODE_DISABLE_CLAUDE_CODE_MCP": "1",
    "MIMOCODE_DISABLE_CLAUDE_IMPORT": "1",
    "MIMOCODE_DISABLE_AUTOUPDATE": "1",
    "MIMOCODE_DISABLE_PROJECT_CONFIG": "1",
    "MIMOCODE_DISABLE_LSP_DOWNLOAD": "1",
}


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def build_prompt(agent: str, transcript: str, request_msg: dict) -> str:
    return (
        f"You are {agent}, an independent reviewer in an mcp-huddle room. "
        "Reply with only the room-visible technical answer: cite concrete "
        "message ids, point out risks, and avoid thanks/ack-only chatter. "
        "Consensus is not correctness: evaluate the stated goal and constraints, "
        "evidence quality, risks/unknowns, and reversibility. Support every "
        "verifiable factual claim with a source URL, file:line, test/command result, "
        "or specific message id; otherwise label it inference or unknown. Opinions "
        "need reasoning, not fake citations. "
        "Do not claim tool access; the runner will post your answer. "
        "The server owns the lifecycle and marks this turn completed only "
        "after the runner posts the result. A live process or quiet log is "
        "not completion; process means work is still in progress. "
        "Do not read or write any files.\n\n"
        f"Current transcript:\n{transcript}\n\n"
        f"Answer request #{request_msg['id']} from {request_msg['agent']}:\n"
        f"{request_msg.get('body', '')}\n\n"
        "Return the final huddle response only."
    )


# Signatures of a failed `mimo run` whose error text landed in stdout despite a
# 0 exit code. Kept specific to the provider/CLI error shape so a normal
# discussion reply that merely mentions "error" or "403" is not flagged.
_MIMO_ERROR_SIGNS = (
    "mimo-free bootstrap failed",
    "bootstrap failed:",
    "illegal_access",
    '"illegal access"',
    "\"type\": \"illegal_access\"",
)


def _is_error_output(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    if any(sign in low for sign in _MIMO_ERROR_SIGNS):
        return True
    # A bare CLI error line with no real content (e.g. "Error: ...") and short.
    stripped = text.strip()
    return stripped.startswith("Error:") and len(stripped) < 400


def call_mimo(mimo_bin: str, prompt: str, timeout: float, model: str | None = None) -> tuple[str, dict]:
    argv = [mimo_bin, "run", "--dangerously-skip-permissions"]
    if model:
        argv += ["--model", model]
    argv.append(prompt)
    os.makedirs(_ISOLATED_CONFIG_DIR, exist_ok=True)
    env = {**os.environ, **_MCP_KILL_SWITCHES}
    started = time.time()
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        # Neutral cwd: running inside a real repo triggers project scanning
        # (git/LSP/instructions discovery), which slows or wedges `mimo run`.
        cwd=_ISOLATED_CONFIG_DIR,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"mimo run exited {proc.returncode}: {proc.stderr.strip()[:500]}")
    answer = _strip_ansi(proc.stdout).strip()
    # `mimo run` prefixes streamed text with a "> title · model" banner line.
    lines = [ln for ln in answer.splitlines() if not ln.startswith("> ")]
    answer = "\n".join(lines).strip()
    if not answer:
        raise RuntimeError("mimo run produced empty output")
    # Output validator ("checker"): `mimo run` exits 0 even when the free
    # provider rejects the request (e.g. "mimo-free bootstrap failed: 403
    # Illegal access"), printing the error to stdout. Without this, that error
    # text would be posted as MiMo's reply. Treat error-shaped output as a
    # failure so the runner posts nothing instead of garbage.
    if _is_error_output(answer) or _is_error_output(_strip_ansi(proc.stderr)):
        raise RuntimeError(f"mimo provider error: {answer[:200] or proc.stderr.strip()[:200]}")
    return answer, {
        "model": model or "mimo-auto",
        "duration_ms": int((time.time() - started) * 1000),
    }


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", default="MiMo")
    parser.add_argument("--mimo-bin", default="mimo")
    parser.add_argument("--model", default=None)
    parser.add_argument("--brief", required=True)
    parser.add_argument("--timeout-sec", type=float, default=300.0)
    args = parser.parse_args(argv)

    room_id = extract_room_id(args.brief)
    if not room_id:
        _event("error", error="room_id_not_found")
        return 2

    requested_id = extract_request_id(args.brief)
    _event("started", agent=args.agent, room_id=room_id, model=args.model or "mimo-auto")
    request_msg = select_request(room_id, args.agent, requested_id)
    if request_msg is None:
        _event("noop", reason="no_pending_request", room_id=room_id)
        return 0

    transcript = bus.read_messages(room_id, since_id=0, limit=50)
    prompt = build_prompt(args.agent, transcript, request_msg)
    try:
        answer, meta = call_mimo(args.mimo_bin, prompt, args.timeout_sec, args.model)
        msg_id = bus.post_message(
            room_id,
            args.agent,
            answer,
            kind="result",
            to=request_msg.get("agent"),
            reply_to=int(request_msg["id"]),
            idempotency_key=f"{args.agent.lower()}-wake:{room_id}:{request_msg['id']}",
            msg_meta=meta,
        )
    except ValueError as exc:
        if "already answered" in str(exc):
            _event("noop", reason="already_answered", request_id=request_msg["id"])
            return 0
        _event("error", error=str(exc), request_id=request_msg["id"])
        return 1
    except Exception as exc:
        _event("error", error=str(exc), request_id=request_msg["id"])
        return 1

    _event("posted", message_id=msg_id, reply_to=request_msg["id"], meta=meta)
    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
