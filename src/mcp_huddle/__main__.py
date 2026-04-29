"""Entry point for `python -m mcp_huddle` and the `mcp-huddle` console script.

Two modes:
  default   stdio transport — for MCP clients (Claude Code, Codex, Gemini CLI,
            Claude Desktop). Spawned per-client; storage in ~/.mcp-huddle/rooms/
            is shared across processes via file locks.
  --http    HTTP server + Liquid Glass dashboard on :8014. Run once manually
            to watch rooms in a browser. Dashboard is the only difference —
            the MCP tools are the same.
"""
import os
import sys


def main() -> None:
    args = sys.argv[1:]
    use_http = "--http" in args or os.environ.get("MCP_HUDDLE_HTTP")

    if use_http:
        import uvicorn
        from .server import build_app

        port = int(os.environ.get("PORT", 8014))
        print(f"mcp-huddle (HTTP + dashboard) on :{port}", flush=True)
        print(f"Dashboard: http://127.0.0.1:{port}/dashboard", flush=True)
        uvicorn.run(build_app(), host="127.0.0.1", port=port, log_level="warning")
    else:
        # stdio transport — JSON-RPC over stdin/stdout. Default for MCP clients.
        from .server import mcp
        mcp.run()


if __name__ == "__main__":
    main()
