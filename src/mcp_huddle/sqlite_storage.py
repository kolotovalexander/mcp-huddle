"""SQLite message history storage used by the opt-in bus backend.

Each room keeps its database beside the legacy JSONL file. The bus uses the
legacy file lock to serialize cooperating processes and checks the frozen
JSONL file's size and mtime on SQLite operations. Old clients can still append
there; the next SQLite operation then fails closed. Stop every Huddle process
before migration or rollback.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable


DB_NAME = "messages.sqlite3"


def _legacy_fingerprint(room_dir: Path) -> tuple[int, int]:
    source = room_dir / "messages.jsonl"
    try:
        st = source.lstat()
    except FileNotFoundError:
        return 0, 0
    if not stat.S_ISREG(st.st_mode):
        raise ValueError(f"Unsafe legacy message file: {source}")
    return st.st_size, st.st_mtime_ns


def database_path(room_dir: Path) -> Path:
    path = room_dir / DB_NAME
    if path.is_symlink():
        raise ValueError(f"Unsafe SQLite message database symlink: {path}")
    return path


def is_active(room_dir: Path) -> bool:
    path = database_path(room_dir)
    if not path.exists():
        return False
    try:
        with _connection(path) as db:
            tables = {row["name"] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'",
            )}
            if not {"messages", "storage_meta"}.issubset(tables):
                raise RuntimeError(f"SQLite message database has an invalid schema: {path}")
            row = db.execute(
                "SELECT value FROM storage_meta WHERE key='migration_complete'",
            ).fetchone()
        if not row or row["value"] not in ("0", "1"):
            raise RuntimeError(f"SQLite message database has no valid activation state: {path}")
        return row["value"] == "1"
    except sqlite3.DatabaseError as exc:
        raise RuntimeError(f"Cannot read SQLite message database {path}: {exc}") from exc


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=30)
    os.chmod(path, 0o600)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


@contextmanager
def _connection(path: Path):
    """Commit or roll back and always close the SQLite connection."""
    db = _connect(path)
    try:
        yield db
        if db.in_transaction:
            db.commit()
    except Exception:
        if db.in_transaction:
            db.rollback()
        raise
    finally:
        db.close()


def ensure_room(room_dir: Path, room_id: str, legacy_messages: Iterable[dict]) -> None:
    """Create the database or atomically migrate the supplied JSONL history."""
    path = database_path(room_dir)
    with _connection(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("BEGIN IMMEDIATE")
        db.execute("""CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY,
            timestamp INTEGER NOT NULL,
            agent TEXT NOT NULL,
            kind TEXT NOT NULL,
            reply_to INTEGER,
            idempotency_key TEXT,
            payload TEXT NOT NULL
        )""")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS messages_idempotency_key "
                   "ON messages(idempotency_key) WHERE idempotency_key IS NOT NULL")
        db.execute("CREATE INDEX IF NOT EXISTS messages_timestamp ON messages(timestamp)")
        db.execute("CREATE TABLE IF NOT EXISTS storage_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        row = db.execute("SELECT value FROM storage_meta WHERE key='room_id'").fetchone()
        if row and row["value"] != room_id:
            raise ValueError("SQLite message database belongs to a different room")
        db.execute("INSERT OR IGNORE INTO storage_meta(key,value) VALUES('room_id',?)", (room_id,))
        migrated = db.execute("SELECT value FROM storage_meta WHERE key='migration_complete'").fetchone()
        if migrated and migrated["value"] == "1":
            _check_legacy_fingerprint(db, room_dir)
            return
        try:
            # An explicit rollback export writes the complete SQLite history
            # to JSONL and marks this database inactive. Re-enabling SQLite
            # starts from that exported snapshot rather than duplicating rows.
            db.execute("DELETE FROM messages")
            size, mtime = _legacy_fingerprint(room_dir)
            for message in legacy_messages:
                _insert(db, message)
            db.execute("INSERT OR REPLACE INTO storage_meta(key,value) VALUES('migration_complete','1')")
            db.execute("INSERT OR REPLACE INTO storage_meta(key,value) VALUES('legacy_size',?)", (str(size),))
            db.execute("INSERT OR REPLACE INTO storage_meta(key,value) VALUES('legacy_mtime_ns',?)", (str(mtime),))
            db.commit()
        except Exception:
            db.rollback()
            raise


def _check_legacy_fingerprint(db: sqlite3.Connection, room_dir: Path) -> None:
    size, mtime = _legacy_fingerprint(room_dir)
    recorded = {row["key"]: row["value"] for row in db.execute(
        "SELECT key,value FROM storage_meta WHERE key IN ('legacy_size','legacy_mtime_ns')",
    )}
    if (str(size) != recorded.get("legacy_size")
            or str(mtime) != recorded.get("legacy_mtime_ns")):
        raise RuntimeError(
            "Legacy messages.jsonl changed after SQLite migration; refusing mixed-writer history. "
            "Stop legacy Huddle processes and reconcile the JSONL additions before retrying."
        )


def validate_legacy_fingerprint(room_dir: Path) -> None:
    """Verify no legacy writer changed JSONL without reading its contents."""
    with _connection(database_path(room_dir)) as db:
        _check_legacy_fingerprint(db, room_dir)


def _insert(db: sqlite3.Connection, message: dict) -> None:
    payload = json.dumps(message, ensure_ascii=False, allow_nan=False)
    db.execute(
        "INSERT INTO messages(id,timestamp,agent,kind,reply_to,idempotency_key,payload) "
        "VALUES(?,?,?,?,?,?,?)",
        (int(message["id"]), int(message.get("timestamp", 0)),
         str(message.get("agent", "")), str(message.get("kind", "")),
         message.get("reply_to"), message.get("idempotency_key"), payload),
    )


def append(room_dir: Path, message: dict) -> None:
    with _connection(database_path(room_dir)) as db:
        _insert(db, message)


def find_idempotency(room_dir: Path, key: str) -> int | None:
    with _connection(database_path(room_dir)) as db:
        row = db.execute("SELECT id FROM messages WHERE idempotency_key=?", (key,)).fetchone()
        return int(row["id"]) if row else None


def next_id(room_dir: Path) -> int:
    with _connection(database_path(room_dir)) as db:
        row = db.execute("SELECT COALESCE(MAX(id),0)+1 AS next_id FROM messages").fetchone()
        return int(row["next_id"])


def recent(room_dir: Path, limit: int) -> list[dict]:
    with _connection(database_path(room_dir)) as db:
        rows = db.execute("SELECT payload FROM messages ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [json.loads(row["payload"]) for row in reversed(rows)]


def since_time(room_dir: Path, timestamp: int) -> list[dict]:
    with _connection(database_path(room_dir)) as db:
        rows = db.execute("SELECT payload FROM messages WHERE timestamp>=? ORDER BY id", (timestamp,)).fetchall()
    return [json.loads(row["payload"]) for row in rows]


def get(room_dir: Path, message_id: int) -> dict | None:
    with _connection(database_path(room_dir)) as db:
        row = db.execute("SELECT payload FROM messages WHERE id=?", (message_id,)).fetchone()
    return json.loads(row["payload"]) if row else None


def has_terminal_reply(room_dir: Path, target_id: int, agent: str) -> bool:
    with _connection(database_path(room_dir)) as db:
        row = db.execute(
            "SELECT 1 FROM messages WHERE reply_to=? AND agent=? AND kind IN ('result','final') LIMIT 1",
            (target_id, agent),
        ).fetchone()
    return row is not None


def page(room_dir: Path, *, before_id: int | None, after_id: int, limit: int) -> dict:
    """Read one ordered page; SQL LIMIT bounds rows decoded into memory."""
    with _connection(database_path(room_dir)) as db:
        bounds = db.execute("SELECT MIN(id) AS oldest, MAX(id) AS latest FROM messages").fetchone()
        if before_id is not None:
            rows = db.execute(
                "SELECT id,payload FROM messages WHERE id<? ORDER BY id DESC LIMIT ?",
                (before_id, limit + 1),
            ).fetchall()
            has_more = len(rows) > limit
            rows = list(reversed(rows[:limit]))
        elif after_id:
            rows = db.execute(
                "SELECT id,payload FROM messages WHERE id>? ORDER BY id LIMIT ?",
                (after_id, limit + 1),
            ).fetchall()
            has_more = len(rows) > limit
            rows = rows[:limit]
        else:
            rows = db.execute(
                "SELECT id,payload FROM messages ORDER BY id DESC LIMIT ?",
                (limit + 1,),
            ).fetchall()
            has_more = len(rows) > limit
            rows = list(reversed(rows[:limit]))
        messages = [json.loads(row["payload"]) for row in rows]
        return {
            "messages": messages,
            "has_more": has_more,
            "oldest_id": int(bounds["oldest"]) if bounds["oldest"] is not None else None,
            "latest_id": int(bounds["latest"]) if bounds["latest"] is not None else None,
        }


def load_all(room_dir: Path) -> list[dict]:
    with _connection(database_path(room_dir)) as db:
        rows = db.execute("SELECT payload FROM messages ORDER BY id").fetchall()
    return [json.loads(row["payload"]) for row in rows]


def mark_exported(room_dir: Path, legacy_size: int, legacy_mtime_ns: int) -> None:
    with _connection(database_path(room_dir)) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE storage_meta SET value='0' WHERE key='migration_complete'")
        db.execute("INSERT OR REPLACE INTO storage_meta(key,value) VALUES('legacy_size',?)",
                   (str(legacy_size),))
        db.execute("INSERT OR REPLACE INTO storage_meta(key,value) VALUES('legacy_mtime_ns',?)",
                   (str(legacy_mtime_ns),))
