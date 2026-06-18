from pathlib import Path

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
        "done",
        "result",
        to="Claude",
        reply_to=target,
        idempotency_key="qwen-test",
    )

    assert runner.select_request(room_id, "Qwen", requested_id=target) is None


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
