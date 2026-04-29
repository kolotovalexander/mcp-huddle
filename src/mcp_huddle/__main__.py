"""Entry point for `python -m mcp_huddle` and the `mcp-huddle` console script."""
import os

import uvicorn

from .server import build_app


def main() -> None:
    port = int(os.environ.get("PORT", 8014))
    print(f"mcp-huddle starting on :{port}", flush=True)
    print(f"Dashboard: http://127.0.0.1:{port}/dashboard", flush=True)
    uvicorn.run(build_app(), host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
