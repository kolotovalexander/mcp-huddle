#!/usr/bin/env python3
"""Render a candidate launchd plist without changing the live host setup.

The existing dashboard plist is the template. All of its settings, including
custom environment variables and launchd limits, are preserved; only the
program arguments and source import path are set for the combined service.
The output path must not already exist.
"""
from __future__ import annotations

import argparse
import os
import plistlib
from pathlib import Path
import shlex
import stat
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", required=True, type=Path, help="existing dashboard plist")
    parser.add_argument("--python", required=True, help="absolute Python executable")
    parser.add_argument("--source", required=True, type=Path, help="checkout containing src/mcp_huddle")
    parser.add_argument("--output", required=True, type=Path, help="new candidate plist path")
    parser.add_argument("--backup", required=True, type=Path, help="unused path for original dashboard plist backup")
    parser.add_argument("--bridge-plist", required=True, type=Path, help="exact existing bridge plist path")
    parser.add_argument(
        "--bridge-label",
        default="com.kolotovalexander.toolhive.agentsync-pilot-vmcp.bridge.agentsync-huddle-bridge",
    )
    parser.add_argument(
        "--bridge-was-enabled",
        action="store_true",
        required=True,
        help="assert launchctl print-disabled confirms the existing bridge is enabled",
    )
    parser.add_argument("--dashboard-port", type=int, default=8014)
    parser.add_argument("--mcp-port", type=int, default=45111)
    args = parser.parse_args()

    source = args.source.expanduser().resolve()
    source_package = source / "src" / "mcp_huddle"
    if not source_package.is_dir():
        parser.error(f"source checkout has no src/mcp_huddle: {source}")
    python = str(Path(args.python).expanduser())
    if not Path(python).is_absolute():
        parser.error("--python must be an absolute executable path")
    if args.dashboard_port == args.mcp_port:
        parser.error("--dashboard-port and --mcp-port must differ")

    try:
        with args.template.expanduser().open("rb") as stream:
            plist = plistlib.load(stream)
    except (OSError, plistlib.InvalidFileException) as exc:
        parser.error(f"cannot read template plist: {exc}")
    if not isinstance(plist, dict) or not isinstance(plist.get("Label"), str):
        parser.error("template must be a launchd plist with a Label")

    environment = plist.get("EnvironmentVariables", {})
    if not isinstance(environment, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in environment.items()
    ):
        parser.error("template EnvironmentVariables must contain string keys and values")
    environment = dict(environment)
    source_path = str(source / "src")
    previous_pythonpath = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (source_path, previous_pythonpath) if part
    )

    plist["EnvironmentVariables"] = environment
    plist["ProgramArguments"] = [
        python,
        "-m",
        "mcp_huddle",
        "--http",
        "--port",
        str(args.dashboard_port),
        "--mcp-port",
        str(args.mcp_port),
    ]
    destination = args.output.expanduser()
    backup = args.backup.expanduser()
    bridge_plist = args.bridge_plist.expanduser()
    dashboard_plist = args.template.expanduser()
    protected = {dashboard_plist.resolve(), bridge_plist.resolve()}
    if destination.resolve() in protected or backup.resolve() in protected:
        parser.error("candidate output and backup must not overwrite either live plist")
    if destination.resolve() == backup.resolve():
        parser.error("candidate output and backup must use different paths")
    if os.path.lexists(backup):
        parser.error(f"backup already exists; refusing to overwrite: {backup}")
    if not bridge_plist.is_file():
        parser.error(f"bridge plist does not exist: {bridge_plist}")
    try:
        with bridge_plist.open("rb") as stream:
            bridge_config = plistlib.load(stream)
    except (OSError, plistlib.InvalidFileException) as exc:
        parser.error(f"cannot read bridge plist: {exc}")
    if not isinstance(bridge_config, dict) or bridge_config.get("Label") != args.bridge_label:
        parser.error(
            f"bridge plist label {(bridge_config.get('Label') if isinstance(bridge_config, dict) else None)!r} does not match "
            f"--bridge-label {args.bridge_label!r}"
        )
    try:
        candidate_fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(candidate_fd, "wb") as stream:
            plistlib.dump(plist, stream, fmt=plistlib.FMT_XML, sort_keys=False)
    except FileExistsError:
        parser.error(f"output already exists; refusing to overwrite: {destination}")
    except OSError as exc:
        parser.error(f"cannot write candidate plist: {exc}")
    print(f"Candidate plist written: {destination}")
    print(f"Launchd label preserved: {plist['Label']}")
    print(f"Source import path: {environment['PYTHONPATH']}")
    print("Live launchd jobs were not changed.")
    domain = f"gui/{os.getuid()}"
    dashboard_label = plist["Label"]
    metadata = dashboard_plist.stat()
    file_mode = stat.S_IMODE(metadata.st_mode)
    q = lambda path: shlex.quote(str(path))
    print("\n# Exact apply commands (review paths and labels before running):")
    print(f"cp -p {q(dashboard_plist)} {q(backup)}")
    bridge_target = f"{domain}/{args.bridge_label}"
    dashboard_target = f"{domain}/{dashboard_label}"
    print(f"launchctl disable {shlex.quote(bridge_target)}")
    print(f"launchctl bootout {shlex.quote(bridge_target)}")
    print(f"launchctl bootout {shlex.quote(dashboard_target)}")
    print(
        f"install -o {metadata.st_uid} -g {metadata.st_gid} -m {file_mode:o} "
        f"{q(destination)} {q(dashboard_plist)}"
    )
    print(f"launchctl bootstrap {domain} {q(dashboard_plist)}")
    print(f"launchctl kickstart -k {shlex.quote(dashboard_target)}")
    print("\n# Exact rollback commands:")
    print(f"launchctl bootout {shlex.quote(dashboard_target)}")
    print(f"cp -p {q(backup)} {q(dashboard_plist)}")
    print(f"launchctl bootstrap {domain} {q(dashboard_plist)}")
    print(f"launchctl enable {shlex.quote(bridge_target)}")
    print(f"launchctl bootstrap {domain} {q(bridge_plist)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
