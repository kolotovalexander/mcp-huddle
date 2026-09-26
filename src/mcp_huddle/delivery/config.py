"""Delivery configuration: state dir resolution and ``delivery.json`` overrides.

Nothing here is cached at import time — every accessor re-derives its value
from the environment / ``bus.HUDDLE_HOME`` / the config file on each call, so
tests that monkeypatch ``MCP_HUDDLE_HOME`` (or the delivery-specific env vars)
between calls see the effect immediately, the same way ``bus.py`` and
``spawn.py`` behave.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional


def home_dir() -> Path:
    """The huddle home directory, reusing ``bus.HUDDLE_HOME`` when importable
    (it tracks ``MCP_HUDDLE_HOME`` and is reloaded by the test suite), else
    recomputing the same default ``bus.py`` uses."""
    try:
        from mcp_huddle import bus as _bus  # local import: avoid any import cycle

        return _bus.HUDDLE_HOME
    except Exception:
        return Path(os.environ.get("MCP_HUDDLE_HOME", str(Path.home() / ".mcp-huddle")))


def state_dir() -> Path:
    return home_dir() / "delivery"


def spool_dir() -> Path:
    return state_dir() / "spool"


def log_path() -> Path:
    return state_dir() / "log.jsonl"


def config_path() -> Path:
    return home_dir() / "delivery.json"


# ── Defaults ─────────────────────────────────────────────────────────────────

DEFAULT_ORDER = {
    "claude": ["native", "resume", "spool"],
    "codex": ["native", "resume", "spool"],
    "hermes": ["native", "resume", "spool"],
    "opencode": ["native", "resume", "spool"],
    "agy": ["resume", "spool"],
}

# argv templates. Placeholders: {id} {peer} {text} {cwd}. Always a list
# (never shell=True); the first element is resolved with shutil.which.
#
# `text` is the fully attacker/model-controlled message body, so any
# template where it lands as its own argv token could have it parsed as a
# CLI flag if it happens to start with "-" (a plain positional arg starting
# with "-" is commonly misread as an option by argparse-style parsers).
# Templates guard against that in one of two ways:
#  - a literal "--" token immediately before {text} where it's the trailing
#    positional argument (the GNU/POSIX "end of options" convention), or
#  - a merged "--flag={text}" single token where {text} is a named option's
#    value (a "--" between the flag and its value would just be consumed as
#    the terminator and never attached to the flag).
DEFAULT_ARGV = {
    "claude.resume": ["claude", "-p", "--resume", "{id}", "--", "{text}"],
    "codex.native": ["codex", "queue", "--thread={id}", "--message={text}"],
    "codex.resume": ["codex", "exec", "resume", "--", "{id}", "{text}"],
    "hermes.native": ["hermes", "peer", "dm", "--", "{peer}", "{text}"],
    # Unverified config default template -- see docs/delivery.md.
    "hermes.resume": ["hermes", "--resume", "{id}", "chat", "-q", "--", "{text}"],
    "opencode.resume": ["opencode", "run", "--session", "{id}", "--", "{text}"],
    "agy.resume": ["agy", "--conversation", "{id}", "-p", "--", "{text}"],
}

DEFAULT_TIMEOUT = {
    "claude.native": 5.0,
    "codex.native": 30.0,
    "hermes.native": 120.0,
    "opencode.native": 10.0,
}

DEFAULT_HOPS_LIMIT = 4


def _load_raw() -> dict:
    path = config_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


class DeliveryConfig:
    """Snapshot of ``delivery.json`` merged over the built-in defaults.

    Call :func:`load` to get a fresh snapshot; don't hold one across calls
    that might race a config-file edit.
    """

    def __init__(self, raw: Optional[dict] = None):
        self._raw = raw if raw is not None else _load_raw()

    def order(self, harness: str) -> list:
        override = self._raw.get("harnesses", {}).get(harness, {}).get("order")
        if isinstance(override, list) and override:
            return list(override)
        return list(DEFAULT_ORDER.get(harness, []))

    def harness_enabled(self, harness: str) -> bool:
        return bool(self._raw.get("harnesses", {}).get(harness, {}).get("enabled", True))

    def method_enabled(self, method_id: str) -> bool:
        return bool(self._raw.get("methods", {}).get(method_id, {}).get("enabled", True))

    def argv(self, method_id: str) -> Optional[list]:
        override = self._raw.get("methods", {}).get(method_id, {}).get("argv")
        if isinstance(override, list) and override:
            return list(override)
        default = DEFAULT_ARGV.get(method_id)
        return list(default) if default else None

    def timeout(self, method_id: str) -> Optional[float]:
        override = self._raw.get("methods", {}).get(method_id, {}).get("timeout")
        if isinstance(override, (int, float)) and not isinstance(override, bool):
            return float(override)
        return DEFAULT_TIMEOUT.get(method_id)

    def hops_limit(self) -> int:
        val = self._raw.get("hops_limit")
        if isinstance(val, int) and val > 0:
            return val
        return DEFAULT_HOPS_LIMIT

    def opencode_server_url(self) -> str:
        return str(self._raw.get("opencode", {}).get("server_url", "") or "")


def load() -> DeliveryConfig:
    return DeliveryConfig()
