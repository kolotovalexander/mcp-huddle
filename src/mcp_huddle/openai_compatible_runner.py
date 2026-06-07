"""One-shot OpenAI-compatible huddle agent runner.

Used by local bridges such as FreeQwenApi. The model itself cannot call huddle
MCP tools, so this runner enforces the huddle anti-loop contract in process:
read room messages from disk, answer one pending request, and append the result
through the same bus layer used by MCP tools.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

from . import bus


def _event(event: str, **fields: Any) -> None:
    print(json.dumps({"type": event, **fields}, ensure_ascii=False), flush=True)


def extract_room_id(text: str) -> str | None:
    patterns = [
        r"\*\*Room ID:\*\*\s*(room_[A-Za-z0-9_-]+)",
        r"Room:\s*(room_[A-Za-z0-9_-]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return None


def extract_request_id(text: str) -> int | None:
    match = re.search(r"New request id:\s*(\d+)", text)
    return int(match.group(1)) if match else None


def _already_replied(messages: list[dict], agent: str, msg_id: int) -> bool:
    return any(msg.get("agent") == agent and msg.get("reply_to") == msg_id for msg in messages)


def select_request(room_id: str, agent: str, requested_id: int | None) -> dict | None:
    messages = bus._load_messages(room_id)
    candidates = messages
    if requested_id is not None:
        candidates = [msg for msg in messages if msg.get("id") == requested_id]
    else:
        candidates = list(reversed(messages))

    for msg in candidates:
        if msg.get("kind") != "request" or msg.get("reply_to") is not None:
            continue
        if msg.get("agent") == agent:
            continue
        to = msg.get("to")
        if to and to not in (agent, "all"):
            continue
        if _already_replied(messages, agent, int(msg["id"])):
            continue
        return msg
    return None


def build_messages(agent: str, transcript: str, request_msg: dict, reasoning: str) -> list[dict]:
    return [
        {
            "role": "system",
            "content": (
                f"You are {agent}, an independent reviewer in an mcp-huddle room. "
                "Use maximum careful reasoning internally. Reply with only the "
                "room-visible technical answer: cite concrete message ids, point "
                "out risks, and avoid thanks/ack-only chatter. Do not claim tool "
                "access; the runner will post your answer."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Reasoning mode requested: {reasoning}\n\n"
                f"Current transcript:\n{transcript}\n\n"
                f"Answer request #{request_msg['id']} from {request_msg['agent']}:\n"
                f"{request_msg.get('body', '')}\n\n"
                "Return the final huddle response only."
            ),
        },
    ]


def completion_payload(model: str, messages: list[dict], reasoning: str, include_reasoning: bool = True) -> dict:
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 2400,
    }
    if include_reasoning:
        payload.update({
            "reasoning_effort": "high" if reasoning == "max" else reasoning,
            "enable_thinking": True,
            "thinking": {"type": "enabled", "budget": "max"},
        })
    return payload


def call_openai_compatible(
    base_url: str,
    model: str,
    messages: list[dict],
    reasoning: str,
    timeout: float,
) -> tuple[str, dict]:
    url = f"{base_url.rstrip('/')}/chat/completions"
    last_error: Exception | None = None
    for include_reasoning in (True, False):
        payload = completion_payload(model, messages, reasoning, include_reasoning=include_reasoning)
        req = urlrequest.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "authorization": "Bearer dummy-key",
            },
            method="POST",
        )
        started = time.time()
        try:
            with urlrequest.urlopen(req, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
            usage = data.get("usage") or {}
            return content.strip(), {
                "model": data.get("model") or model,
                "reasoning": reasoning if include_reasoning else f"{reasoning}:fallback-no-reasoning-fields",
                "tokens_in": usage.get("prompt_tokens") or usage.get("input_tokens"),
                "tokens_out": usage.get("completion_tokens") or usage.get("output_tokens"),
                "tokens_total": usage.get("total_tokens"),
                "duration_ms": int((time.time() - started) * 1000),
            }
        except urlerror.HTTPError as exc:
            last_error = exc
            if exc.code not in (400, 422):
                break
            _event("reasoning_fields_rejected", status=exc.code, retry_without_reasoning=True)
        except (OSError, KeyError, ValueError) as exc:
            last_error = exc
            break
    raise RuntimeError(f"OpenAI-compatible completion failed: {last_error}")


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--reasoning", default="max")
    parser.add_argument("--brief", required=True)
    parser.add_argument("--timeout-sec", type=float, default=120.0)
    args = parser.parse_args(argv)

    room_id = extract_room_id(args.brief)
    if not room_id:
        _event("error", error="room_id_not_found")
        return 2

    requested_id = extract_request_id(args.brief)
    _event("started", agent=args.agent, room_id=room_id, model=args.model, reasoning=args.reasoning)
    request_msg = select_request(room_id, args.agent, requested_id)
    if request_msg is None:
        _event("noop", reason="no_pending_request", room_id=room_id)
        return 0

    transcript = bus.read_messages(room_id, since_id=0, limit=50)
    messages = build_messages(args.agent, transcript, request_msg, args.reasoning)
    try:
        answer, meta = call_openai_compatible(
            args.base_url, args.model, messages, args.reasoning, args.timeout_sec)
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
