"""Extract Claude CLI's reported model from one bounded stream-json log segment.

This is an observation of CLI output, not proof of the model that served a
request. The caller owns the process generation and passes offsets for that
generation only. No prompt, answer, or other event fields leave this parser.
"""

from __future__ import annotations

import json
import os
import re
from contextlib import nullcontext
from pathlib import Path
from typing import BinaryIO, Literal, TypedDict


MAX_SEGMENT_BYTES = 1_048_576
MAX_LINE_BYTES = 65_536
_MODEL_ID = re.compile(r"claude-[A-Za-z0-9][A-Za-z0-9._-]{0,120}\Z", re.ASCII)


class ClaudeModelReceipt(TypedDict):
    reported_model: str | None
    source: Literal["assistant", "init", "mixed", "none"]


def _valid_model(value: object) -> str | None:
    if isinstance(value, str) and len(value) <= 128 and _MODEL_ID.fullmatch(value):
        return value
    return None


def parse_claude_model_receipt(
    log_path: str | os.PathLike[str] | BinaryIO, *, start_offset: int, end_offset: int,
    expected_device: int | None = None, expected_inode: int | None = None,
) -> ClaudeModelReceipt:
    """Read only ``[start_offset, end_offset)`` and return a model receipt.

    Offsets are byte offsets in one Claude ``stream-json`` log. The caller
    should start at a line boundary and must associate them with one exact
    child process generation. Incomplete final lines and lines above 64 KiB
    are ignored. A segment above 1 MiB is rejected before the file is opened.
    """
    if (
        type(start_offset) is not int
        or type(end_offset) is not int
        or start_offset < 0
        or end_offset < start_offset
        or end_offset - start_offset > MAX_SEGMENT_BYTES
    ):
        raise ValueError("invalid or oversized Claude log segment")

    assistant_models: set[str] = set()
    init_models: set[str] = set()
    init_sessions: set[str] = set()
    assistant_sessions: set[str] = set()
    remaining = end_offset - start_offset
    opened = (Path(log_path).open("rb")
              if isinstance(log_path, (str, os.PathLike))
              else nullcontext(log_path))
    with opened as stream:
        file_stat = os.fstat(stream.fileno())
        if ((expected_device is not None and file_stat.st_dev != expected_device)
                or (expected_inode is not None and file_stat.st_ino != expected_inode)
                or file_stat.st_size < end_offset):
            raise ValueError("Claude log file identity or size changed")
        if start_offset:
            stream.seek(start_offset - 1)
            if stream.read(1) != b"\n":
                # The earlier process left a partial line. Skip its tail.
                stream.seek(start_offset)
                skipped = stream.readline(min(remaining, MAX_LINE_BYTES + 1))
                remaining -= len(skipped)
                if not skipped.endswith(b"\n"):
                    while remaining:
                        skipped = stream.readline(min(remaining, MAX_LINE_BYTES + 1))
                        if not skipped:
                            raise ValueError("Claude log segment ended during line skip")
                        remaining -= len(skipped)
                        if skipped.endswith(b"\n"):
                            break
        if start_offset == 0:
            stream.seek(0)
        while remaining:
            line = stream.readline(min(remaining, MAX_LINE_BYTES + 1))
            if not line:
                break
            remaining -= len(line)
            if not line.endswith(b"\n"):
                # Skip the remainder of a long line within this exact segment.
                # A short line here is incomplete at EOF or at end_offset.
                while remaining:
                    skipped = stream.readline(min(remaining, MAX_LINE_BYTES + 1))
                    if not skipped:
                        break
                    remaining -= len(skipped)
                    if skipped.endswith(b"\n"):
                        break
                continue
            if len(line) > MAX_LINE_BYTES:
                continue
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError, RecursionError):
                continue
            if not isinstance(event, dict):
                continue

            if event.get("type") == "system" and event.get("subtype") == "init":
                session = event.get("session_id")
                if isinstance(session, str) and 0 < len(session) <= 128:
                    init_sessions.add(session)
                model = _valid_model(event.get("model"))
                if model:
                    init_models.add(model)
            elif (
                event.get("type") == "assistant"
                and "parent_tool_use_id" in event
                and event["parent_tool_use_id"] is None
                and isinstance(event.get("message"), dict)
            ):
                session = event.get("session_id")
                if isinstance(session, str) and 0 < len(session) <= 128:
                    assistant_sessions.add(session)
                model = _valid_model(event["message"].get("model"))
                if model and (not init_sessions or session in init_sessions):
                    assistant_models.add(model)

    if (len(init_sessions) > 1 or len(assistant_sessions) > 1
            or (init_sessions and assistant_sessions - init_sessions)):
        return {"reported_model": None, "source": "mixed"}
    if len(assistant_models) > 1:
        return {"reported_model": None, "source": "mixed"}
    if assistant_models:
        return {"reported_model": next(iter(assistant_models)), "source": "assistant"}
    if len(init_models) > 1:
        return {"reported_model": None, "source": "mixed"}
    if init_models:
        return {"reported_model": next(iter(init_models)), "source": "init"}
    return {"reported_model": None, "source": "none"}
