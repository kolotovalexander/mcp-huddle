#!/usr/bin/env python3
"""Create, verify, and rotate Huddle room-data snapshots using the stdlib."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath

MANIFEST = "manifest.json"
MANIFEST_VERSION = 1


def _regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except FileNotFoundError:
        return False


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise ValueError(f"Refusing symlink path component: {current}")


def _hash(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _open_parent_at(root_fd: int, relative: Path) -> tuple[int, str]:
    parts = relative.parts
    current = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) |
                            getattr(os, "O_NOFOLLOW", 0), dir_fd=current)
            os.close(current)
            current = child
        return current, parts[-1]
    except Exception:
        os.close(current)
        raise


def _copy_locked(home_fd: int, relative: Path, dst: Path, lock_name: str) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(dst.parent, 0o700)
    parent_fd, name = _open_parent_at(home_fd, relative)
    try:
        lock_fd = os.open(lock_name, os.O_RDWR | os.O_APPEND | os.O_CREAT |
                          getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=parent_fd)
        try:
            if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
                raise ValueError(f"Refusing non-regular lock for {relative}")
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            source_fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
            try:
                if not stat.S_ISREG(os.fstat(source_fd).st_mode):
                    raise ValueError(f"Refusing non-regular room data: {relative}")
                with os.fdopen(source_fd, "rb") as source, dst.open("xb") as target:
                    source_fd = -1
                    source_stat = os.fstat(source.fileno())
                    shutil.copyfileobj(source, target)
                    target.flush()
                    os.fsync(target.fileno())
            finally:
                if source_fd >= 0:
                    os.close(source_fd)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
    except Exception:
        try:
            dst.unlink()
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(parent_fd)
    # SQLite migration drift checks use size + mtime_ns. Preserve JSONL's
    # source timestamp so an intact snapshot restored in place stays valid.
    os.utime(dst, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
    os.chmod(dst, 0o600)


def _copy_sqlite_locked(home_fd: int, home: Path, relative: Path,
                        dst: Path, lock_relative: Path) -> None:
    """Use SQLite's online backup API so committed WAL data is included."""
    _reject_symlink_components(home / relative)
    source = home / relative
    if not _regular_file(source):
        raise ValueError(f"Refusing non-regular SQLite database: {relative}")
    dst.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(dst.parent, 0o700)
    lock_parent_fd, lock_name = _open_parent_at(home_fd, lock_relative)
    lock_fd = os.open(lock_name, os.O_RDWR | os.O_APPEND | os.O_CREAT |
                      getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=lock_parent_fd)
    try:
        if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
            raise ValueError(f"Refusing non-regular lock for {relative}")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        source_db = sqlite3.connect(source, timeout=30)
        target_db = sqlite3.connect(dst)
        try:
            source_db.backup(target_db)
        finally:
            target_db.close()
            source_db.close()
        with dst.open("rb") as target:
            os.fsync(target.fileno())
        os.chmod(dst, 0o600)
    except Exception:
        try:
            dst.unlink()
        except FileNotFoundError:
            pass
        raise
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
        os.close(lock_parent_fd)
def _room_files(home: Path):
    rooms = home / "rooms"
    if rooms.is_symlink():
        raise ValueError(f"Unsafe rooms directory: {rooms}")
    if not rooms.exists():
        return
    if not rooms.is_dir():
        raise ValueError(f"Unsafe rooms directory: {rooms}")
    for room in sorted(rooms.iterdir()):
        if room.is_symlink() or not room.is_dir():
            raise ValueError(f"Refusing unknown room entry: {room}")
        for name, lock_name in (("meta.json", "meta.lock"),
                                ("status.json", "status.lock"),
                                ("messages.jsonl", "messages.jsonl"),
                                ("messages.sqlite3", "messages.jsonl"),
                                ("notify_registry.json", "notify.lock")):
            source = room / name
            if _regular_file(source):
                yield source.relative_to(home), source, room / lock_name
            elif source.exists() or source.is_symlink():
                raise ValueError(f"Refusing unsafe room file: {source}")
        agents = room / "agents"
        if agents.exists() or agents.is_symlink():
            if agents.is_symlink() or not agents.is_dir():
                raise ValueError(f"Refusing unsafe agents directory: {agents}")
            for source in sorted(agents.iterdir()):
                if (source.name.endswith((".events.jsonl", ".last_message.txt"))
                        and _regular_file(source)):
                    yield source.relative_to(home), source, source
                elif source.name.endswith((".events.jsonl", ".last_message.txt")) and (source.exists() or source.is_symlink()):
                    raise ValueError(f"Refusing unsafe agent output: {source}")


def _source_files(home: Path):
    yield from _room_files(home)
    registry = home / "registry.json"
    if _regular_file(registry):
        # Registry entries refer to credential environment-variable names; actual
        # secret values are never stored by Huddle's registry format.
        yield Path("registry.json"), registry, registry
    elif registry.exists() or registry.is_symlink():
        raise ValueError(f"Refusing unsafe registry: {registry}")


def _safe_manifest_paths(snapshot: Path, entries: list[dict]) -> list[tuple[str, Path]]:
    result = []
    seen = set()
    for entry in entries:
        rel = entry.get("path") if isinstance(entry, dict) else None
        if not isinstance(rel, str) or rel in seen:
            raise ValueError("Invalid manifest path list")
        pure = PurePosixPath(rel)
        if (pure.is_absolute() or not pure.parts or rel == MANIFEST
                or any(part in ("", ".", "..") for part in pure.parts)):
            raise ValueError(f"Unsafe manifest path: {rel!r}")
        target = snapshot.joinpath(*pure.parts)
        cursor = snapshot
        for part in pure.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ValueError(f"Refusing symlink in snapshot: {cursor}")
        if not _regular_file(target):
            raise ValueError(f"Missing or non-regular snapshot file: {rel}")
        seen.add(rel)
        result.append((rel, target))
    return result


def verify_snapshot(snapshot: Path) -> bool:
    """Verify every file listed in a snapshot's manifest; raise on any mismatch."""
    if snapshot.is_symlink() or not snapshot.is_dir():
        raise ValueError(f"Not a real snapshot directory: {snapshot}")
    manifest_path = snapshot / MANIFEST
    if manifest_path.is_symlink() or not _regular_file(manifest_path):
        raise ValueError(f"Missing manifest: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("version") != MANIFEST_VERSION or not isinstance(manifest.get("files"), list):
        raise ValueError(f"Unsupported or invalid manifest: {manifest_path}")
    expected_by_path = {item.get("path"): item for item in manifest["files"]
                        if isinstance(item, dict)}
    for rel, path in _safe_manifest_paths(snapshot, manifest["files"]):
        expected = expected_by_path[rel]
        digest, size = _hash(path)
        if digest != expected.get("sha256") or size != expected.get("size"):
            raise ValueError(f"Hash or size mismatch: {rel}")
    return True


def _remove_owned_snapshot(snapshot: Path) -> None:
    manifest_path = snapshot / MANIFEST
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    owned = _safe_manifest_paths(snapshot, manifest["files"])
    # A snapshot is prunable only when it contains no unlisted data. This keeps
    # operator-added files and any unexpected entries intact.
    listed = {rel for rel, _ in owned} | {MANIFEST}
    owned_dirs = {snapshot}
    for rel, _ in owned:
        parent = (snapshot / rel).parent
        while parent != snapshot:
            owned_dirs.add(parent)
            parent = parent.parent
    for path in snapshot.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"Refusing to prune snapshot containing symlink: {path}")
        if path.is_file() and path.relative_to(snapshot).as_posix() not in listed:
            raise ValueError(f"Refusing to prune snapshot with unowned file: {path}")
        if path.is_dir() and path not in owned_dirs:
            raise ValueError(f"Refusing to prune snapshot with unowned directory: {path}")
        if not path.is_file() and not path.is_dir():
            raise ValueError(f"Refusing unknown snapshot entry: {path}")
    for _, path in sorted(owned, key=lambda item: len(item[0]), reverse=True):
        path.unlink()
    manifest_path.unlink()
    for path in sorted((p for p in snapshot.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        path.rmdir()
    snapshot.rmdir()


def create_backup(home: Path, destination: Path, keep_days: int = 21,
                  *, dry_run: bool = False) -> Path | None:
    if keep_days < 1:
        raise ValueError("--keep-days must be at least 1")
    raw_home = home.expanduser().absolute()
    raw_destination = destination.expanduser().absolute()
    _reject_symlink_components(raw_home)
    _reject_symlink_components(raw_destination)
    if not raw_home.is_dir():
        raise ValueError(f"Huddle home must be an existing real directory: {raw_home}")
    home = raw_home.resolve(strict=True)
    destination = raw_destination.resolve(strict=False)
    rooms = home / "rooms"
    if rooms.is_symlink():
        raise ValueError(f"Huddle rooms directory must not be a symlink: {rooms}")
    if destination == home or destination == rooms or rooms in destination.parents:
        raise ValueError("Backup destination must be outside the rooms directory")
    if dry_run:
        count = sum(1 for _ in _source_files(home))
        if count == 0:
            raise ValueError("No Huddle room data or registry found; refusing an empty snapshot")
        print(f"Would snapshot {count} file(s) from {home} into {destination}; keep {keep_days} days.")
        return None

    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(destination, 0o700)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    final = destination / f"snapshot-{stamp}-{os.getpid()}-{time.time_ns()}"
    stage = Path(tempfile.mkdtemp(prefix=".snapshot-staging-", dir=destination))
    os.chmod(stage, 0o700)
    try:
        files = []
        root_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        home_fd = os.open(home, root_flags)
        try:
            for rel, source, lock_path in _source_files(home):
                target = stage / rel
                if source.name == "messages.sqlite3":
                    _copy_sqlite_locked(
                        home_fd, home, rel, target, lock_path.relative_to(home),
                    )
                else:
                    _copy_locked(home_fd, rel, target, lock_path.name)
                digest, size = _hash(target)
                files.append({"path": rel.as_posix(), "sha256": digest, "size": size})
        finally:
            os.close(home_fd)
        if not files:
            raise ValueError("No Huddle room data or registry found; refusing an empty snapshot")
        manifest = {"version": MANIFEST_VERSION, "created_at": time.time(), "files": files}
        manifest_path = stage / MANIFEST
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(manifest_path, 0o600)
        with manifest_path.open("rb") as stream:
            os.fsync(stream.fileno())
        verify_snapshot(stage)
        os.rename(stage, final)
        verify_snapshot(final)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise

    # Rotation starts only after the new snapshot has been published and read back.
    cutoff = time.time() - keep_days * 86400
    verified = []
    for candidate in sorted(destination.glob("snapshot-*")):
        try:
            if verify_snapshot(candidate):
                created = json.loads((candidate / MANIFEST).read_text(encoding="utf-8"))["created_at"]
                verified.append((created, candidate))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    protected = {path for _, path in sorted(verified, reverse=True)[:2]}
    for created, candidate in verified:
        if candidate != final and created < cutoff and candidate not in protected:
            _remove_owned_snapshot(candidate)
    return final


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home() / ".mcp-huddle")
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--keep-days", type=int, default=21)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verify", type=Path, metavar="SNAPSHOT")
    args = parser.parse_args(argv)
    try:
        if args.verify:
            verify_snapshot(args.verify)
            print(f"Verified snapshot: {args.verify}")
        else:
            destination = args.destination or args.home / "backups" / "automatic"
            result = create_backup(args.home, destination, args.keep_days, dry_run=args.dry_run)
            if result:
                print(f"Created and verified snapshot: {result}")
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"backup_huddle: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
