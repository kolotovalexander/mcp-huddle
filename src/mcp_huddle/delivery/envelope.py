"""The ``<agent-message>`` envelope: build, sniff hops, refuse loops.

The wrapped ``text`` is never modified -- only the surrounding tag and its
attributes are generated/escaped. See docs/delivery.md for the exact format.
"""

from __future__ import annotations

import html
import re

DEFAULT_HOPS_LIMIT = 4

_OPEN_TAG_RE = re.compile(r"<agent-message\b([^>]*)>", re.IGNORECASE)
_ATTR_RE = re.compile(r'([\w-]+)\s*=\s*"([^"]*)"')


class HopsExceeded(Exception):
    """Raised when the incoming text already carries a hops count at/over
    the configured limit -- callers must refuse to send in that case."""

    def __init__(self, hops: int, limit: int):
        super().__init__(f"hops {hops} >= limit {limit}")
        self.hops = hops
        self.limit = limit


def _escape(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def existing_hops(text: str):
    """Return the ``hops`` attribute of the first ``<agent-message>`` opening
    tag found in ``text``, or ``None`` if there is no such envelope / no hops
    attribute / it isn't an integer."""
    if not text:
        return None
    match = _OPEN_TAG_RE.search(text)
    if not match:
        return None
    attrs = dict(_ATTR_RE.findall(match.group(1)))
    raw = attrs.get("hops")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def next_hops(text: str, limit: int = DEFAULT_HOPS_LIMIT) -> int:
    """Compute the hops value a *new* envelope wrapping ``text`` should carry.

    Raises :class:`HopsExceeded` when ``text`` already contains an
    agent-message envelope whose hops count is at or beyond ``limit`` --
    the caller must refuse to send anything in that case.
    """
    prev = existing_hops(text)
    if prev is None:
        return 1
    if prev >= limit:
        raise HopsExceeded(prev, limit)
    return prev + 1


def build_envelope(*, from_name: str, method: str, msg_id: str, hops: int,
                    reply_to: str, text: str) -> str:
    """Wrap ``text`` unchanged inside the agent-message envelope."""
    return (
        f'<agent-message from="{_escape(from_name)}" from_verified="false" '
        f'via="huddle:{_escape(method)}" id="{_escape(msg_id)}" hops="{hops}" '
        f'reply_to="{_escape(reply_to)}">\n{text}\n</agent-message>'
    )
