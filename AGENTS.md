<!-- AGENTSYNC-GENERATED v1
canon_file: CLAUDE.md
canon_agent: claude
canon_hash: sha256:0ca55cc17f95d66cf7a5ad798e42e4867df8d6a8885e85b0fb387c1b8461fcdf
body_hash: sha256:4eb9c4cda79de525302ebede611d3bf606b3dd022581dacb6ca0cdaf199f1a9c
render_rules_hash: sha256:c05a3284711a2baf049f656149b22bf11e7c0df5443924bf48240c5c3184c624
generated_at: 2026-07-23T04:36:44Z
-->
# AGENTS.md — working on mcp-huddle

Guidance for AI agents (and humans) editing this repo. User-facing docs:
[README.md](README.md); onboarding: [docs/ONBOARDING.md](docs/ONBOARDING.md); open work: [docs/TODO.md](docs/TODO.md).

## What this is
A persistent multi-agent chat MCP server. AI agents join rooms and discuss;
a web dashboard lets humans watch/intervene. Two run modes (one binary):
`mcp-huddle` (stdio MCP) and `mcp-huddle --http` (HTTP MCP + dashboard on :8014).

## Layout (deep modules, clear seams)
- `src/mcp_huddle/server.py` — MCP tools + HTTP/SSE routes + spawn/wake
  orchestration. The public surface; keep tool/route signatures stable.
- `src/mcp_huddle/bus.py` — file-locked room/message/meta store with an
  in-process message cache. **Concurrency-critical**: never change lock
  ordering or acquire two locks at once; additions must stay defensive.
- `src/mcp_huddle/spawn.py` — agent registry + spawning. Agents are spawned
  one-shot per turn (`cd <project> && <cli> …`), re-woken per addressed message;
  Codex resumes its thread. `DEFAULT_REGISTRY`, read-only transform
  (`_apply_readonly`, default ON), on-disk registry merge.
- `src/mcp_huddle/openai_compatible_runner.py` / `mimo_runner.py` — runners for
  agents that don't speak MCP (cloud APIs / MiMo): read the room, call the
  model, post via the bus. Both validate output before posting.
- `src/mcp_huddle/__main__.py` — CLI (argparse): `--http`, `--port`,
  `--version`, `--install-hooks`.
- `src/mcp_huddle/static/{dashboard.html,css,js}` — the dashboard (vanilla JS,
  no build step). Themes axis `data-theme`, skin axis `data-skin`, palette axis
  `data-palette`, language `data-lang`; settings popover + i18n live in
  `dashboard.js`.

## Conventions
- Python 3.11+ stdlib only (no third-party in runtime code beyond `mcp` /
  starlette / uvicorn). Keep it dependency-light.
- Agents are **read-only discussants by default** (`MCP_HUDDLE_READONLY`); they
  read but never edit files and talk only via huddle MCP / the bus.
- New agent slots: prefer a `~/.mcp-huddle/registry.json` entry or the
  `openai_compatible_runner` over hard-coding; cloud APIs use `--api-key-env`.
  Note: merging in `registry.json` is by `name` and REPLACES the whole entry —
  an override for an existing default agent must carry its full `cmd`.
- The server itself announces every way a woken agent can fail to reply
  (rate-limit, spawn exception, error exit, silent exit, hung wake — see
  `server.py::_handle_rate_limit_on_exit` / `_announce_spawn_failure` /
  `_announce_noreply_on_exit` / `_check_stuck_wakes`). Don't build
  orchestrator-side polling/timeout logic for this — read the room instead.
- Dashboard JS: no framework, no bundler — edit the files directly. UI strings
  go through `t()` / `data-i18n`.

## Verify before claiming done
```bash
.venv/bin/pytest tests/ -q          # full suite (currently 119 passing)
node --check src/mcp_huddle/static/dashboard.js   # JS syntax
```
For dashboard changes, the server serves files fresh (no-cache) — just reload
the browser. Python changes (server/spawn) require a server restart to go live.

## Don't
- Don't break `bus.py` locking or `server.py` tool/route signatures.
- Don't add a build step or runtime dependencies to the dashboard.
- Don't enable agents that need interactive login (agy) by default.