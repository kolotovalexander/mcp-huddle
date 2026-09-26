"""24h idempotency cache for ``message_send``, keyed by caller-supplied key.

A repeated ``idempotency_key`` within the window returns the earlier result
verbatim and sends nothing. Best-effort file locking (module-level lock plus
atomic replace) is enough here: this guards against accidental double-sends
from a retrying caller, not a hard multi-process transaction.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Optional

from . import config as delivery_config

WINDOW_SECONDS = 24 * 60 * 60

_LOCK = threading.Lock()


def _store_path() -> Path:
    return delivery_config.state_dir() / "idempotency.json"


def _load(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def get_cached(key: str) -> Optional[str]:
    """Return the previously stored result JSON for ``key``, or ``None`` if
    there isn't one, it's expired, or ``key`` is empty."""
    if not key:
        return None
    with _LOCK:
        entry = _load(_store_path()).get(key)
    if not entry:
        return None
    if time.time() - entry.get("ts", 0) > WINDOW_SECONDS:
        return None
    return entry.get("result")


def store(key: str, result_json: str) -> None:
    """Record ``result_json`` under ``key`` and opportunistically prune
    entries older than the window so the file doesn't grow unbounded."""
    if not key:
        return
    path = _store_path()
    with _LOCK:
        data = _load(path)
        cutoff = time.time() - WINDOW_SECONDS
        data = {k: v for k, v in data.items() if v.get("ts", 0) >= cutoff}
        data[key] = {"ts": time.time(), "result": result_json}
        _save(path, data)
