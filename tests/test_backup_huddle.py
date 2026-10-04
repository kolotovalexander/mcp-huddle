import importlib.util
import json
import sqlite3
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("backup_huddle", ROOT / "tools" / "backup_huddle.py")
backup = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = backup
SPEC.loader.exec_module(backup)


def make_home(tmp_path):
    home = tmp_path / "huddle-home"
    room = home / "rooms" / "room_abcd1234"
    agents = room / "agents"
    agents.mkdir(parents=True)
    (room / "meta.json").write_text('{"id":"room_abcd1234"}\n')
    (room / "status.json").write_text('{"codex":{"status":"online"}}\n')
    (room / "messages.jsonl").write_text('{"id":1,"body":"hello"}\n')
    (room / "notify_registry.json").write_text('{"codex":"notify.txt"}\n')
    (agents / "codex.events.jsonl").write_text('{"event":"done"}\n')
    (agents / "codex.last_message.txt").write_text("finished\n")
    (room / "meta.lock").touch()
    (room / "status.lock").touch()
    (room / "notify.lock").touch()
    (home / "registry.json").write_text('[{"name":"Codex","pass_env":["API_KEY"]}]\n')
    (home / "logs").mkdir()
    (home / "logs" / "private.log").write_text("not backed up\n")
    return home


def snapshots(destination):
    return sorted(destination.glob("snapshot-*"))


def test_backup_roundtrip_hashes_and_restricts_permissions(tmp_path):
    home = make_home(tmp_path)
    destination = home / "backups" / "automatic"

    snapshot = backup.create_backup(home, destination)

    assert backup.verify_snapshot(snapshot)
    manifest = json.loads((snapshot / "manifest.json").read_text())
    paths = {item["path"] for item in manifest["files"]}
    assert "rooms/room_abcd1234/messages.jsonl" in paths
    assert "rooms/room_abcd1234/notify_registry.json" in paths
    assert "rooms/room_abcd1234/agents/codex.last_message.txt" in paths
    assert "registry.json" in paths
    assert not (snapshot / "logs").exists()
    assert snapshot.stat().st_mode & 0o777 == 0o700
    assert (snapshot / "registry.json").stat().st_mode & 0o777 == 0o600

    (snapshot / "rooms/room_abcd1234/messages.jsonl").write_text("changed\n")
    try:
        backup.verify_snapshot(snapshot)
    except ValueError as exc:
        assert "mismatch" in str(exc)
    else:
        raise AssertionError("modified file passed manifest verification")


def test_backup_uses_sqlite_online_snapshot_including_committed_rows(tmp_path):
    home = make_home(tmp_path)
    room = home / "rooms" / "room_abcd1234"
    database = room / "messages.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, body TEXT)")
        db.execute("INSERT INTO messages VALUES (1, 'sqlite history')")
        db.commit()

    snapshot = backup.create_backup(home, home / "backups")
    copied = snapshot / "rooms" / "room_abcd1234" / "messages.sqlite3"
    original_jsonl = room / "messages.jsonl"
    copied_jsonl = snapshot / "rooms" / "room_abcd1234" / "messages.jsonl"
    assert copied_jsonl.stat().st_mtime_ns == original_jsonl.stat().st_mtime_ns
    with sqlite3.connect(copied) as db:
        assert db.execute("SELECT body FROM messages WHERE id=1").fetchone() == ("sqlite history",)
    assert backup.verify_snapshot(snapshot)


def test_room_inventory_ignores_only_regular_ds_store(tmp_path):
    home = make_home(tmp_path)
    (home / "rooms" / ".DS_Store").write_bytes(b"finder metadata")

    snapshot = backup.create_backup(home, home / "backups")

    manifest_paths = {item["path"] for item in
                      json.loads((snapshot / "manifest.json").read_text())["files"]}
    assert not any(path.endswith("/.DS_Store") for path in manifest_paths)

    symlink_home = make_home(tmp_path / "symlink-case")
    (symlink_home / "rooms" / ".DS_Store").symlink_to(tmp_path / "outside")
    try:
        backup.create_backup(symlink_home, symlink_home / "backups")
    except ValueError as exc:
        assert "unknown room entry" in str(exc)
    else:
        raise AssertionError("symlink .DS_Store was unexpectedly ignored")

    unknown_home = make_home(tmp_path / "unknown-case")
    (unknown_home / "rooms" / "unexpected.txt").write_text("keep strict inventory")
    try:
        backup.create_backup(unknown_home, unknown_home / "backups")
    except ValueError as exc:
        assert "unknown room entry" in str(exc)
    else:
        raise AssertionError("unknown room-container file was unexpectedly ignored")


def test_rotation_keeps_two_verified_snapshots(tmp_path):
    home = make_home(tmp_path)
    destination = home / "backups" / "automatic"
    first = backup.create_backup(home, destination)
    second = backup.create_backup(home, destination)
    for snapshot in (first, second):
        manifest_path = snapshot / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["created_at"] = time.time() - 40 * 86400
        manifest_path.write_text(json.dumps(manifest))
    third = backup.create_backup(home, destination, keep_days=21)

    remaining = snapshots(destination)
    assert third in remaining
    assert len(remaining) == 2
    assert all(backup.verify_snapshot(path) for path in remaining)


def test_failed_snapshot_does_not_rotate_existing_backup(tmp_path):
    home = make_home(tmp_path)
    destination = home / "backups" / "automatic"
    previous = backup.create_backup(home, destination)
    manifest_path = previous / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["created_at"] = time.time() - 40 * 86400
    manifest_path.write_text(json.dumps(manifest))
    source = home / "rooms" / "room_abcd1234" / "meta.json"
    source.unlink()
    source.symlink_to(tmp_path / "outside")

    try:
        backup.create_backup(home, destination, keep_days=1)
    except ValueError:
        pass
    else:
        raise AssertionError("unsafe source unexpectedly produced a snapshot")

    assert snapshots(destination) == [previous]
    assert backup.verify_snapshot(previous)
    missing = tmp_path / "missing-home"
    try:
        backup.create_backup(missing, destination)
    except ValueError as exc:
        assert "existing real directory" in str(exc)
    else:
        raise AssertionError("missing home unexpectedly produced a snapshot")

    empty = tmp_path / "empty-home"
    empty.mkdir()
    try:
        backup.create_backup(empty, destination)
    except ValueError as exc:
        assert "No Huddle room data" in str(exc)
    else:
        raise AssertionError("empty home unexpectedly produced a snapshot")

    assert snapshots(destination) == [previous]
