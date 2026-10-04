from __future__ import annotations

import importlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

import mcp_huddle.bus as bus


@pytest.fixture()
def sqlite_bus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path))
    monkeypatch.setenv("MCP_HUDDLE_MESSAGE_STORE", "sqlite")
    reloaded = importlib.reload(bus)
    yield reloaded
    monkeypatch.delenv("MCP_HUDDLE_HOME", raising=False)
    monkeypatch.delenv("MCP_HUDDLE_MESSAGE_STORE", raising=False)
    importlib.reload(bus)


def test_sqlite_backend_migrates_existing_history_and_keeps_ids(sqlite_bus, tmp_path):
    sqlite_bus._SQLITE_MESSAGE_STORE = False
    room_id = sqlite_bus.create_room("Legacy", "Codex", 0, "/tmp", "session")
    messages = [
        {"id": i, "agent": "Codex", "kind": "comment", "timestamp": i,
         "body": f"legacy-{i}"}
        for i in range(1, 4)
    ]
    source = sqlite_bus._room_dir(room_id) / "messages.jsonl"
    source.write_text("".join(json.dumps(item) + "\n" for item in messages))
    sqlite_bus._SQLITE_MESSAGE_STORE = True

    assert sqlite_bus.migrate_room_to_sqlite(room_id) == 3
    page = sqlite_bus.read_message_page(room_id, limit=2)
    assert [item["id"] for item in page["messages"]] == [2, 3]
    assert page == {"messages": messages[1:], "has_more": True,
                    "oldest_id": 1, "latest_id": 3}
    assert source.read_text().count("legacy") == 3
    assert sqlite_bus.post_message(room_id, "Codex", "next", "comment") == 4


def test_sqlite_append_deduplicates_and_pages_without_loading_all(sqlite_bus):
    room_id = sqlite_bus.create_room("Pages", "Codex", 0, "/tmp", "session")
    for i in range(5):
        sqlite_bus.post_message(room_id, "Codex", f"m{i}", "comment",
                                idempotency_key=f"key-{i}")

    duplicate_id = sqlite_bus.post_message(
        room_id, "Codex", "ignored", "comment", idempotency_key="key-2",
    )
    assert duplicate_id == 3
    page = sqlite_bus.read_message_page(room_id, before_id=5, limit=2)
    assert [item["id"] for item in page["messages"]] == [3, 4]
    assert page["has_more"] is True
    assert page["oldest_id"] == 1 and page["latest_id"] == 5
    assert sqlite_bus.read_message_page(room_id, after_id=3, limit=2)["messages"] == [
        sqlite_bus._load_messages(room_id)[3], sqlite_bus._load_messages(room_id)[4],
    ]


def test_sqlite_page_does_not_read_or_parse_legacy_jsonl(sqlite_bus, monkeypatch):
    room_id = sqlite_bus.create_room("Bounded", "Codex", 0, "/tmp", "session")
    sqlite_bus.post_message(room_id, "Codex", "one", "comment")

    def fail_if_legacy_read(_path):
        raise AssertionError("SQLite page read touched legacy JSONL")

    monkeypatch.setattr(sqlite_bus, "_safe_read_text", fail_if_legacy_read)
    assert sqlite_bus.read_message_page(room_id, limit=1)["messages"][0]["body"] == "one"

    with pytest.raises(ValueError, match="from 1 to 500"):
        sqlite_bus.read_message_page(room_id, limit=501)


def test_migration_rejects_corrupt_jsonl_without_activating(sqlite_bus):
    sqlite_bus._SQLITE_MESSAGE_STORE = False
    room_id = sqlite_bus.create_room("Corrupt", "Codex", 0, "/tmp", "session")
    legacy = sqlite_bus._room_dir(room_id) / "messages.jsonl"
    legacy.write_text('{"id":1,"agent":"Codex","kind":"comment","timestamp":1,"body":"ok"}\nnot-json\n')
    original = legacy.read_bytes()

    with pytest.raises(ValueError, match="Invalid messages.jsonl line 2"):
        sqlite_bus.migrate_room_to_sqlite(room_id)

    assert legacy.read_bytes() == original
    assert not sqlite_bus._sqlite_storage.is_active(sqlite_bus._room_dir(room_id))


def test_corrupt_sqlite_database_never_falls_back_to_jsonl(sqlite_bus):
    room_id = sqlite_bus.create_room("Corrupt DB", "Codex", 0, "/tmp", "session")
    sqlite_bus.post_message(room_id, "Codex", "sqlite", "comment")
    sqlite_bus._SQLITE_MESSAGE_STORE = False
    (sqlite_bus._room_dir(room_id) / "messages.sqlite3").write_bytes(b"not a database")

    with pytest.raises(RuntimeError, match="Cannot read SQLite message database"):
        sqlite_bus.post_message(room_id, "Codex", "must fail", "comment")


def test_sqlite_backend_fails_closed_when_legacy_writer_changes_jsonl(sqlite_bus):
    room_id = sqlite_bus.create_room("One source", "Codex", 0, "/tmp", "session")
    sqlite_bus.post_message(room_id, "Codex", "sqlite", "comment")
    legacy = sqlite_bus._room_dir(room_id) / "messages.jsonl"
    legacy.write_text(legacy.read_text() + '{"id":2,"agent":"Old","kind":"comment","timestamp":2,"body":"old writer"}\n')

    with pytest.raises(RuntimeError, match="mixed-writer"):
        sqlite_bus.post_message(room_id, "Codex", "must fail", "comment")


def test_rollback_export_preserves_sqlite_appends_as_jsonl(sqlite_bus):
    room_id = sqlite_bus.create_room("Rollback", "Codex", 0, "/tmp", "session")
    sqlite_bus.post_message(room_id, "Codex", "first", "comment")
    sqlite_bus.post_message(room_id, "Codex", "second", "comment")

    assert sqlite_bus.export_sqlite_history_to_jsonl(room_id) == 2
    sqlite_bus._SQLITE_MESSAGE_STORE = False
    assert [m["body"] for m in sqlite_bus._load_messages(room_id)] == ["first", "second"]
    assert sqlite_bus.post_message(room_id, "Codex", "file mode", "comment") == 3


def test_sqlite_backup_restores_with_legacy_fingerprint_intact(sqlite_bus, tmp_path, monkeypatch):
    home = tmp_path / "live-home"
    monkeypatch.setenv("MCP_HUDDLE_MESSAGE_STORE", "file")
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(home))
    sqlite_bus = importlib.reload(sqlite_bus)
    room_id = sqlite_bus.create_room("Restore", "Codex", 0, "/tmp", "session")
    sqlite_bus.post_message(room_id, "Codex", "legacy", "comment")
    sqlite_bus.migrate_room_to_sqlite(room_id)
    monkeypatch.setenv("MCP_HUDDLE_MESSAGE_STORE", "sqlite")
    sqlite_bus = importlib.reload(sqlite_bus)
    sqlite_bus.post_message(room_id, "Codex", "sqlite row", "comment")

    spec = importlib.util.spec_from_file_location(
        "backup_huddle_restore_test",
        Path(__file__).resolve().parents[1] / "tools" / "backup_huddle.py",
    )
    backup = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = backup
    spec.loader.exec_module(backup)
    snapshot = backup.create_backup(home, home / "backups")
    restored_home = tmp_path / "restored-home"
    shutil.copytree(snapshot / "rooms", restored_home / "rooms", copy_function=shutil.copy2)

    monkeypatch.setenv("MCP_HUDDLE_HOME", str(restored_home))
    monkeypatch.setenv("MCP_HUDDLE_MESSAGE_STORE", "sqlite")
    restored_bus = importlib.reload(sqlite_bus)
    page = restored_bus.read_message_page(room_id, limit=10)
    assert [item["body"] for item in page["messages"]] == ["legacy", "sqlite row"]
