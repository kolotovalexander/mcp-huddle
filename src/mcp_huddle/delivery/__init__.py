"""Native cross-harness message delivery.

Huddle picks a deterministic "postman" per harness (no LLM) and tries
delivery methods in a configured order, passing the caller's text unchanged
inside a small envelope. See ``docs/delivery.md`` for the full design.

Public surface used by ``server.py``:
    - :func:`message_send`
    - :func:`message_targets`
"""

from .core import message_send, message_targets

__all__ = ["message_send", "message_targets"]
