#!/usr/bin/env python3
"""Explicit offline migration/export for Huddle SQLite message history.

Stop every Huddle process before running this tool. Old stdio clients do not
know about SQLite; the bus detects later JSONL drift and fails closed, but it
cannot prevent an old process from appending before it notices the migration.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home() / ".mcp-huddle")
    parser.add_argument(
        "--export-jsonl", action="store_true",
        help="export current SQLite rows to JSONL and deactivate SQLite for rollback",
    )
    args = parser.parse_args(argv)
    if not args.home.expanduser().is_dir():
        print(f"Huddle home does not exist: {args.home}", file=sys.stderr)
        return 1
    os.environ["MCP_HUDDLE_HOME"] = str(args.home.expanduser().absolute())
    os.environ.pop("MCP_HUDDLE_MESSAGE_STORE", None)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from mcp_huddle import bus

    rooms = [item.get("id") for item in bus.list_rooms() if item.get("id")]
    try:
        for room_id in rooms:
            if args.export_jsonl:
                if not bus._sqlite_storage.is_active(bus._room_dir(room_id)):
                    print(f"Skipped room without active SQLite history: {room_id}")
                    continue
                count = bus.export_sqlite_history_to_jsonl(room_id)
                action = "Exported"
            else:
                count = bus.migrate_room_to_sqlite(room_id)
                action = "Migrated"
            print(f"{action} {count} messages: {room_id}")
    except Exception as exc:
        print(f"migrate_huddle_messages: {exc}", file=sys.stderr)
        return 1
    print(f"Processed {len(rooms)} room(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
