# Contributing to mcp-huddle

Thanks for your interest in improving mcp-huddle! This is a small, focused
project — bug fixes, docs, and well-scoped features are all welcome.

## Development setup

Requires Python 3.10+.

```bash
git clone https://github.com/kolotovalexander/mcp-huddle
cd mcp-huddle
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Running the server locally

```bash
mcp-huddle --http            # HTTP + dashboard on http://127.0.0.1:8014/dashboard
mcp-huddle                   # stdio transport (for MCP clients)
```

Rooms are stored under `~/.mcp-huddle/` (override with `MCP_HUDDLE_HOME`). When
hacking, point `MCP_HUDDLE_HOME` at a throwaway directory so you don't touch your
real rooms:

```bash
MCP_HUDDLE_HOME=$(mktemp -d) mcp-huddle --http
```

## Running tests

```bash
.venv/bin/pytest tests/ -q
```

Please add or update tests for any behavior change. Tests should be hermetic —
use the provided fixtures and an isolated `MCP_HUDDLE_HOME`; do not depend on
network access or on any agent CLI (`codex`, `agy`, `mimo`) being installed.

## Pull request workflow

1. Fork and create a topic branch off `main`.
2. Keep changes focused; avoid unrelated refactors in the same PR.
3. Make sure `pytest` passes and the dashboard still loads.
4. Update `README.md` and `CHANGELOG.md` if you change user-facing behavior.
5. Open a PR with a clear description of the problem and the fix.

## Code style

- Standard library + the declared dependencies (`mcp`, `starlette`, `uvicorn`);
  avoid adding new runtime dependencies without discussion.
- Match the existing style: typed function signatures, small functions, and
  comments that explain *why* rather than *what*.
- Storage is the single source of truth — prefer file-backed state over
  in-process global state so stdio clients and the dashboard stay consistent.

## Reporting bugs

Open an issue at <https://github.com/kolotovalexander/mcp-huddle/issues> with
steps to reproduce, what you expected, and what happened (including relevant
stderr output).

By contributing, you agree that your contributions are licensed under the
project's [MIT License](LICENSE).
