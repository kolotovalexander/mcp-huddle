"""Entry point for `python -m mcp_huddle` and the `mcp-huddle` console script.

Two modes:
  default   stdio transport — for MCP clients (Claude Code, Codex, Antigravity,
            Claude Desktop). Spawned per-client; storage in ~/.mcp-huddle/rooms/
            is shared across processes via file locks.
  --http    HTTP server + Liquid Glass dashboard on :8014. Run once manually
            to watch rooms in a browser. Dashboard is the only difference —
            the MCP tools are the same.
"""
import argparse
import os
import socket
import sys

DEFAULT_PORT = 8014


def _version() -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("mcp-huddle")
        except PackageNotFoundError:
            pass
    except Exception:
        pass
    try:
        from . import __version__

        return __version__
    except Exception:
        return "unknown"


def _resolve_port(cli_port: "int | None") -> int:
    """Resolve the HTTP port from --port, then $PORT, then the default.

    An invalid value never crashes: it prints a clear warning and falls back
    to the default port.
    """
    if cli_port is not None:
        # argparse already validated/typed this.
        return cli_port

    raw = os.environ.get("PORT")
    if raw is None or raw == "":
        return DEFAULT_PORT
    try:
        port = int(raw)
    except (TypeError, ValueError):
        print(
            f"warning: invalid PORT={raw!r}, using default {DEFAULT_PORT}",
            file=sys.stderr,
            flush=True,
        )
        return DEFAULT_PORT
    if not (1 <= port <= 65535):
        print(
            f"warning: PORT={port} out of range 1-65535, using default {DEFAULT_PORT}",
            file=sys.stderr,
            flush=True,
        )
        return DEFAULT_PORT
    return port


def _port_arg(value: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"invalid port {value!r}: must be an integer")
    if not (1 <= port <= 65535):
        raise argparse.ArgumentTypeError(f"port {port} out of range 1-65535")
    return port


def _install_hooks(dest: "str | None") -> None:
    """Copy the bundled example hooks (Claude Code PostToolUse / Stop) to a
    directory and print how to wire them in. pip can't run post-install code
    safely, so this is the explicit opt-in step a user runs after installing.
    """
    import shutil
    from pathlib import Path

    src_dir = Path(__file__).parent / "hooks"
    target = Path(dest).expanduser() if dest else (Path.home() / ".mcp-huddle" / "hooks")
    target.mkdir(parents=True, exist_ok=True)
    copied = []
    for sh in sorted(src_dir.glob("*.sh")):
        out = target / sh.name
        shutil.copyfile(sh, out)
        out.chmod(0o755)
        copied.append(out)
    print(f"Installed {len(copied)} hook(s) to {target}:")
    for c in copied:
        print(f"  {c}")
    example = """
Wire them into Claude Code (~/.claude/settings.json), e.g.:
  "hooks": {
    "PostToolUse": [{"hooks": [{"type":"command","command":"__T__/claude-check.sh"}]}],
    "Stop":        [{"hooks": [{"type":"command","command":"__T__/session-end.sh"}]}]
  }
claude-check.sh surfaces pending huddle requests; session-end.sh closes this session's rooms on exit."""
    print(example.replace("__T__", str(target)))


def _cmd_post(args: argparse.Namespace) -> None:
    """Post one message to a room from any headless process.

    Calls bus.post_message directly, so it inherits kind validation, the
    closed/resolved guards, and the circuit breaker for free. It does NOT
    auto-wake addressed agents (that lives in the HTTP/MCP server layer) —
    this is for orchestrator-driven loops, not a replacement for auto-spawn.
    """
    from . import bus

    try:
        msg_id = bus.post_message(args.room, args.agent, args.body, args.kind, to=args.to)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
    print(msg_id)


def _cmd_read(args: argparse.Namespace) -> None:
    from . import bus

    try:
        print(bus.read_messages(args.room, since_id=args.since_id, limit=args.limit))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)


def _cmd_rooms(args: argparse.Namespace) -> None:
    from . import bus

    rooms = bus.list_rooms()
    if not rooms:
        print("(no rooms)")
        return
    for r in rooms:
        print(f"{r.get('id', '?')}  {r.get('status', '?'):8}  {r.get('name', '')}")


def main(argv: "list[str] | None" = None) -> None:
    parser = argparse.ArgumentParser(
        prog="mcp-huddle",
        description=(
            "Persistent multi-agent coordination rooms over MCP. "
            "Default mode is stdio transport for MCP clients; --http serves a "
            "browser dashboard."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"mcp-huddle {_version()}",
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help="run the HTTP server + dashboard instead of stdio transport",
    )
    parser.add_argument(
        "--install-hooks",
        nargs="?",
        const="",
        default=None,
        metavar="DIR",
        help="copy the bundled Claude Code hooks to DIR (default ~/.mcp-huddle/hooks) and exit",
    )
    parser.add_argument(
        "--port",
        type=_port_arg,
        default=None,
        help=f"HTTP port (default: $PORT or {DEFAULT_PORT}); only used with --http",
    )

    # Optional subcommands let any headless process talk to a room without MCP
    # or a running HTTP server — they hit bus.py (file locks) directly. No
    # subcommand keeps the legacy behaviour (stdio MCP, or --http), so MCP
    # clients that exec `mcp-huddle` bare are untouched.
    sub = parser.add_subparsers(dest="command")

    p_post = sub.add_parser("post", help="post one message to a room")
    p_post.add_argument("--room", required=True)
    p_post.add_argument("--agent", required=True)
    p_post.add_argument("--body", required=True)
    p_post.add_argument("--kind", default="comment", help="message kind (default: comment)")
    p_post.add_argument("--to", default=None, help="addressee agent name, or 'all'")
    p_post.set_defaults(func=_cmd_post)

    p_read = sub.add_parser("read", help="print a room's chat log")
    p_read.add_argument("--room", required=True)
    p_read.add_argument("--since-id", type=int, default=0, dest="since_id")
    p_read.add_argument("--limit", type=int, default=20)
    p_read.set_defaults(func=_cmd_read)

    p_rooms = sub.add_parser("rooms", help="list rooms (id / status / name)")
    p_rooms.set_defaults(func=_cmd_rooms)

    args = parser.parse_args(argv)

    if getattr(args, "func", None) is not None:
        args.func(args)
        return

    if args.install_hooks is not None:
        _install_hooks(args.install_hooks or None)
        return

    use_http = args.http or bool(os.environ.get("MCP_HUDDLE_HTTP"))

    if use_http:
        import uvicorn

        from .server import build_app

        host = "127.0.0.1"
        port = _resolve_port(args.port)

        # Bind first so the "ready" message only prints once we are actually
        # listening — never advertise a URL the server failed to bind.
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError as exc:
            sock.close()
            print(f"error: cannot bind {host}:{port}: {exc}", file=sys.stderr, flush=True)
            sys.exit(1)
        sock.listen()

        print(f"mcp-huddle (HTTP + dashboard) on :{port}", flush=True)
        print(f"Dashboard: http://{host}:{port}/dashboard", flush=True)

        # One-line-per-agent discovery summary (which agents are enabled / why
        # disabled) so the operator can see the roster at a glance.
        try:
            from . import spawn

            spawn.log_discovery_summary()
        except Exception:
            pass

        config = uvicorn.Config(build_app(), log_level="warning")
        server = uvicorn.Server(config)
        server.run(sockets=[sock])
    else:
        # stdio transport — JSON-RPC over stdin/stdout. Default for MCP clients.
        from .server import mcp

        mcp.run()


if __name__ == "__main__":
    main()
