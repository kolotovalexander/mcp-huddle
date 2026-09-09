import json
from pathlib import Path

import pytest

from mcp_huddle import bus
from mcp_huddle import openai_compatible_runner as runner


def test_extract_room_and_request_from_huddle_prompts() -> None:
    assert runner.extract_room_id("**Room ID:** room_abcd1234\n") == "room_abcd1234"
    assert runner.extract_room_id("Room: room_deadbeef\n") == "room_deadbeef"
    assert runner.extract_request_id("New request id: 42\n") == 42


def test_select_request_respects_address_and_existing_reply(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path / "huddle"))
    monkeypatch.setattr(bus, "HUDDLE_HOME", tmp_path / "huddle")
    monkeypatch.setattr(bus, "BUS_DIR", tmp_path / "huddle" / "rooms")
    room_id = bus.create_room("qwen", "Claude", 0, str(tmp_path), "sess")
    bus.invite_agent(room_id, "Qwen")
    bus.invite_agent(room_id, "Codex")

    ignored = bus.post_message(room_id, "Claude", "Codex only", "request", to="Codex")
    target = bus.post_message(room_id, "Claude", "Qwen review", "request", to="Qwen")

    selected = runner.select_request(room_id, "Qwen", requested_id=None)

    assert selected is not None
    assert selected["id"] == target
    assert selected["id"] != ignored

    bus.post_message(
        room_id,
        "Qwen",
        "working",
        "busy",
        to="Claude",
        reply_to=target,
    )
    assert runner.select_request(room_id, "Qwen", requested_id=target)["id"] == target

    bus.post_message(
        room_id,
        "Qwen",
        "done",
        "result",
        to="Claude",
        reply_to=target,
        idempotency_key="qwen-test",
    )

    assert runner.select_request(room_id, "Qwen", requested_id=target) is None


def test_terminal_final_suppresses_request_but_progress_does_not() -> None:
    request_id = 17
    messages = [
        {"agent": "Qwen", "reply_to": request_id, "kind": "ack"},
        {"agent": "Qwen", "reply_to": request_id, "kind": "busy"},
        {"agent": "Qwen", "reply_to": request_id, "kind": "comment"},
    ]
    assert runner._already_replied(messages, "Qwen", request_id) is False
    messages.append({"agent": "Qwen", "reply_to": request_id, "kind": "final"})
    assert runner._already_replied(messages, "Qwen", request_id) is True


def test_completion_payload_adds_reasoning_fields() -> None:
    payload = runner.completion_payload(
        "qwen3.7-max",
        [{"role": "user", "content": "x"}],
        "max",
        include_reasoning=True,
    )

    assert payload["model"] == "qwen3.7-max"
    assert payload["reasoning_effort"] == "high"
    assert payload["enable_thinking"] is True


class _Response:
    def __init__(self, payload: object):
        self._body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return self._body


@pytest.mark.parametrize("payload", [
    {},
    {"choices": None},
    {"choices": []},
    {"choices": [None]},
    {"choices": [{}]},
    {"choices": [{"message": None}]},
    {"choices": [{"message": {}}]},
    {"choices": [{"message": {"content": None}}]},
    {"choices": [{"message": {"content": ""}}]},
    {"choices": [{"message": {"content": "  \n\t"}}]},
])
def test_completion_rejects_invalid_or_empty_content_before_post(
    payload: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request)
        return _Response(payload)

    monkeypatch.setattr(runner.urlrequest, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="invalid response|empty response") as err:
        runner.call_openai_compatible(
            "http://127.0.0.1:1234", "model", [], "max", 1, api_key="never-log-key"
        )

    assert len(calls) == 1
    assert "never-log-key" not in str(err.value)
    assert json.dumps(payload) not in str(err.value)


def test_completion_accepts_nonempty_string_content(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        runner.urlrequest,
        "urlopen",
        lambda request, timeout: _Response({
            "choices": [{"message": {"content": "  answer  "}}],
            "usage": {"total_tokens": 3},
        }),
    )

    content, meta = runner.call_openai_compatible(
        "http://127.0.0.1:1234", "model", [], "max", 1
    )

    assert content == "answer"
    assert meta["tokens_total"] == 3
