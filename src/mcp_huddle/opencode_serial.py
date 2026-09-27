"""Small exec wrapper that serializes Huddle-launched OpenCode processes.

This file is executed by absolute path from :mod:`spawn`, so it intentionally
uses only the standard library and does not import the application package.
"""
from __future__ import annotations

import fcntl
import os
import stat
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 3 or args[1] != "--" or not args[2:]:
        print("OpenCode serialization wrapper: expected LOCK_PATH -- COMMAND ...", file=sys.stderr)
        return 2

    lock_path = Path(args[0])
    command = args[2:]
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(lock_path, flags, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("lock path is not a regular file")
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        # Python opens descriptors non-inheritable by default. Retain this
        # exact open-file description across exec so the OS releases the lock
        # only when the actual CLI process (or its timeout wrapper) exits.
        os.set_inheritable(fd, True)
        os.execvp(command[0], command)
    except OSError as exc:
        print(f"OpenCode serialization wrapper: {exc}", file=sys.stderr)
        return 127
    return 127  # os.execvp returns only on failure (normally raised above).


if __name__ == "__main__":
    raise SystemExit(main())
