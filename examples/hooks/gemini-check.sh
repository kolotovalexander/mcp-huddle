#!/usr/bin/env bash
# Huddle — on_tool_response hook for Gemini CLI.
# Same managed-directory/recoverable-claim logic as claude-check.sh.
notify_root="${MCP_HUDDLE_HOME:-$HOME/.mcp-huddle}/notifications"
python3 -c '
import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import time

root = sys.argv[1]
ending = sys.argv[2]
MAX_FILE = 65536
MAX_SCAN = 256
MAX_RECOVER = 32
MAX_NEW = 64
CLAIM_GRACE = 5
CLAIM_MAX_AGE = 300
new_claim = re.compile(r"^(agent-bus-.*-notify[.]json)[.]claim[.]v2[.](\d+)[.]([0-9a-f]{32})$")
old_claim = re.compile(r"^(agent-bus-.*-notify[.]json)[.]claim[.](\d+)[.](\d+)$")


def same_inode(left, right):
    return (left.st_dev, left.st_ino, left.st_mode) == (right.st_dev, right.st_ino, right.st_mode)


def lstat(name):
    return os.stat(name, dir_fd=root_fd, follow_symlinks=False)


def safe_unlink(name, expected):
    try:
        current = lstat(name)
        if same_inode(current, expected):
            os.unlink(name, dir_fd=root_fd)
    except OSError:
        pass


def consume(name):
    try:
        observed = lstat(name)
    except OSError:
        return
    fd = None
    data = None
    try:
        flags = (os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
                 | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
        fd = os.open(name, flags, dir_fd=root_fd)
        opened = os.fstat(fd)
        if not same_inode(observed, opened):
            return
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or opened.st_uid != os.geteuid() or opened.st_size > MAX_FILE):
            return
        with os.fdopen(fd, encoding="utf-8") as stream:
            fd = None
            value = json.load(stream)
        if isinstance(value, dict):
            data = value
    except (OSError, UnicodeError, ValueError):
        pass
    finally:
        if fd is not None:
            os.close(fd)
        safe_unlink(name, observed)
    if data is not None:
        clean = lambda value: " ".join(str(value).split())[:160]
        room = clean(data.get("room_id", "?"))
        sender = clean(data.get("from_agent", "?"))
        msg = clean(data.get("msg_id", "?"))
        print(f"💬 Huddle [{room}]: {sender} sent a request (msg #{msg}). Use messages_read({room!r}) {ending}.")


def names_matching(predicate, limit):
    result = []
    try:
        with os.scandir(root_fd) as entries:
            for index, entry in enumerate(entries):
                if index >= MAX_SCAN:
                    break
                if predicate(entry.name):
                    result.append(entry.name)
                    if len(result) >= limit:
                        break
    except OSError:
        pass
    return result


def owner_is_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True


def claim_details(name):
    match = new_claim.match(name)
    if match:
        return match.group(1), int(match.group(2))
    match = old_claim.match(name)
    if match:
        return match.group(1), int(match.group(3))
    return None


def open_private_dir(parent_fd, name):
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    flags = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
             | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    fd = os.open(name, flags, dir_fd=parent_fd)
    info = os.fstat(fd)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o022):
        os.close(fd)
        raise ValueError("unsafe notification lock directory")
    return fd


@contextlib.contextmanager
def target_locked(base_name):
    digest = hashlib.sha256(os.path.join(root_real, base_name).encode("utf-8")).hexdigest()
    flags = (os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_CLOEXEC", 0))
    fd = os.open(f"{digest}.lock", flags, 0o600, dir_fd=locks_fd)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid() or info.st_mode & 0o022):
            raise ValueError("unsafe notification lock")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


root_fd = home_fd = internal_fd = locks_fd = None
try:
    root_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    root_fd = os.open(root, root_flags)
    root_info = os.fstat(root_fd)
    if (not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid != os.geteuid()
            or root_info.st_mode & 0o022):
        raise ValueError("unsafe notification directory")
    root_real = os.path.realpath(root)
    if not same_inode(root_info, os.stat(root_real, follow_symlinks=False)):
        raise ValueError("notification directory changed")
    home_real = os.path.dirname(root_real)
    home_fd = os.open(home_real, root_flags)
    internal_fd = open_private_dir(home_fd, "internal")
    locks_fd = open_private_dir(internal_fd, "notification-locks")

    now = time.time()
    def stale_claim(name):
        details = claim_details(name)
        if details is None:
            return False
        _, owner = details
        try:
            age = max(0, now - lstat(name).st_mtime)
        except OSError:
            return False
        return age >= CLAIM_GRACE and (age >= CLAIM_MAX_AGE or not owner_is_alive(owner))

    for name in names_matching(stale_claim, MAX_RECOVER):
        base_name, _ = claim_details(name)
        with target_locked(base_name):
            if stale_claim(name):
                consume(name)

    is_notice = lambda name: name.startswith("agent-bus-") and name.endswith("-notify.json")
    for name in names_matching(is_notice, MAX_NEW):
        with target_locked(name):
            for _ in range(4):
                claim = f"{name}.claim.v2.{os.getpid()}.{secrets.token_hex(16)}"
                try:
                    os.link(name, claim, src_dir_fd=root_fd, dst_dir_fd=root_fd, follow_symlinks=False)
                    linked = lstat(claim)
                    safe_unlink(name, linked)
                    consume(claim)
                    break
                except FileExistsError:
                    continue
                except OSError:
                    break
except Exception:
    pass
finally:
    for fd in (locks_fd, internal_fd, home_fd, root_fd):
        if fd is not None:
            os.close(fd)
' "$notify_root" "when ready" 2>/dev/null
exit 0
