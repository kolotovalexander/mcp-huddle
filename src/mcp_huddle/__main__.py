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


def main() -> None:
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
        "--port",
        type=_port_arg,
        default=None,
        help=f"HTTP port (default: $PORT or {DEFAULT_PORT}); only used with --http",
    )
    args = parser.parse_args()

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

        config = uvicorn.Config(build_app(), log_level="warning")
        server = uvicorn.Server(config)
        server.run(sockets=[sock])
    else:
        # stdio transport — JSON-RPC over stdin/stdout. Default for MCP clients.
        from .server import mcp

        mcp.run()


if __name__ == "__main__":
    main()
