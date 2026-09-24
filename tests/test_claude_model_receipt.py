"""Claude stream-json model receipts are bounded to one process log segment."""

import json

import pytest

from mcp_huddle.claude_model_receipt import parse_claude_model_receipt


def _event(**fields: object) -> bytes:
    return json.dumps(fields, ensure_ascii=False).encode() + b"\n"


def _receipt(tmp_path, data: bytes, *, start: int = 0, end: int | None = None):
    path = tmp_path / "claude.jsonl"
    path.write_bytes(data)
    return parse_claude_model_receipt(path, start_offset=start, end_offset=len(data) if end is None else end)


def test_assistant_model_takes_precedence_over_init(tmp_path):
    data = _event(type="system", subtype="init", model="claude-sonnet-5")
    data += _event(type="assistant", parent_tool_use_id=None, message={"model": "claude-opus-5-5"})
    assert _receipt(tmp_path, data) == {"reported_model": "claude-opus-5-5", "source": "assistant"}


def test_matching_init_and_assistant_and_init_only(tmp_path):
    init = _event(type="system", subtype="init", model="claude-sonnet-5")
    assistant = _event(type="assistant", parent_tool_use_id=None, message={"model": "claude-sonnet-5"})
    assert _receipt(tmp_path, init + assistant) == {"reported_model": "claude-sonnet-5", "source": "assistant"}
    assert _receipt(tmp_path, init) == {"reported_model": "claude-sonnet-5", "source": "init"}


def test_different_main_assistant_models_are_mixed(tmp_path):
    data = _event(type="assistant", parent_tool_use_id=None, message={"model": "claude-sonnet-5"})
    data += _event(type="assistant", parent_tool_use_id=None, message={"model": "claude-opus-5-5"})
    assert _receipt(tmp_path, data) == {"reported_model": None, "source": "mixed"}


def test_ignores_stderr_junk_and_incomplete_last_line(tmp_path):
    data = b"permission warning: prompt text\n"
    data += _event(type="system", subtype="init", model="claude-sonnet-5")
    data += b'{"type":"assistant","parent_tool_use_id":null,"message":{"model":"claude-opus-5-5"}'
    assert _receipt(tmp_path, data) == {"reported_model": "claude-sonnet-5", "source": "init"}


def test_skips_oversize_line_and_continues(tmp_path):
    too_long = _event(type="assistant", parent_tool_use_id=None, message={"model": "claude-opus-5-5", "content": "x" * 70_000})
    valid = _event(type="assistant", parent_tool_use_id=None, message={"model": "claude-sonnet-5"})
    assert _receipt(tmp_path, too_long + valid) == {"reported_model": "claude-sonnet-5", "source": "assistant"}


def test_ignores_subagent_and_missing_parent_marker(tmp_path):
    data = _event(type="assistant", parent_tool_use_id="toolu_123", message={"model": "claude-opus-5-5"})
    data += _event(type="assistant", message={"model": "claude-opus-5-5"})
    data += _event(type="system", subtype="init", model="claude-sonnet-5")
    assert _receipt(tmp_path, data) == {"reported_model": "claude-sonnet-5", "source": "init"}


def test_reads_only_requested_segment(tmp_path):
    old = _event(type="assistant", parent_tool_use_id=None, message={"model": "claude-opus-5-5"})
    current = _event(type="assistant", parent_tool_use_id=None, message={"model": "claude-sonnet-5"})
    assert _receipt(tmp_path, old + current, start=len(old)) == {"reported_model": "claude-sonnet-5", "source": "assistant"}


def test_malformed_model_and_content_never_leak(tmp_path):
    secret = "PRIVATE PROMPT CONTENT"
    data = _event(type="system", subtype="init", model=f"claude-opus-5-5 {secret}")
    data += _event(type="assistant", parent_tool_use_id=None, message={"model": "https://example.test/secret", "content": secret})
    receipt = _receipt(tmp_path, data)
    assert receipt == {"reported_model": None, "source": "none"}
    assert secret not in repr(receipt)


def test_rejects_unbounded_or_invalid_offsets_without_reading(tmp_path):
    path = tmp_path / "claude.jsonl"
    path.write_bytes(b"")
    for start, end in ((-1, 0), (1, 0), (0, 1_048_577), (True, 1)):
        with pytest.raises(ValueError):
            parse_claude_model_receipt(path, start_offset=start, end_offset=end)


def test_skips_partial_previous_line_at_segment_start(tmp_path):
    previous = b'{"type":"assistant","message":{"model":"claude-opus-5-5"}'
    current = _event(type="system", subtype="init", model="claude-sonnet-5")
    assert _receipt(tmp_path, previous + b" tail\n" + current, start=len(previous)) == {
        "reported_model": "claude-sonnet-5", "source": "init",
    }


def test_mixed_cli_sessions_do_not_claim_a_model(tmp_path):
    data = _event(type="system", subtype="init", session_id="one", model="claude-sonnet-5")
    data += _event(type="assistant", session_id="two", parent_tool_use_id=None,
                   message={"model": "claude-opus-5-5"})
    assert _receipt(tmp_path, data) == {"reported_model": None, "source": "mixed"}


def test_open_file_identity_must_match_expected_inode(tmp_path):
    path = tmp_path / "claude.jsonl"
    data = _event(type="system", subtype="init", model="claude-sonnet-5")
    path.write_bytes(data)
    with path.open("rb") as stream:
        with pytest.raises(ValueError, match="identity"):
            parse_claude_model_receipt(
                stream, start_offset=0, end_offset=len(data), expected_inode=-1,
            )
