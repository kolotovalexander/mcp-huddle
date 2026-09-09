# CLAUDE.md — working on mcp-huddle

Guidance for AI agents (and humans) editing this repo. User-facing docs:
`README.md`; onboarding: `docs/ONBOARDING.md`; open work: `docs/TODO.md`.

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
- `src/mcp_huddle/child_processes.py` — process-local ownership of exact
  `Popen` objects. Persisted PIDs are diagnostics only: never signal, clear or
  replace an unknown foreign lease merely because its PID looks dead.
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
- Agents are **read-only discussants by default** where the CLI supports an
  enforced transform (`MCP_HUDDLE_READONLY`; currently Claude and Codex).
  Antigravity has no enforced read-only mode; MiMo instead uses a neutral temp
  cwd. Do not claim that environment scrubbing is an OS/filesystem sandbox.
- New agent slots: prefer a `~/.mcp-huddle/registry.json` entry or the
  `openai_compatible_runner` over hard-coding; cloud APIs use `--api-key-env`.
  Child environments are allowlisted: use `pass_env` for other required names,
  never values. The variable named by `--api-key-env` is copied automatically.
  Note: merging in `registry.json` is by `name` and REPLACES the whole entry —
  an override for an existing default agent must carry its full `cmd`.
- While the owning server is running, it announces the known ways a woken agent
  can fail to reply (rate-limit, spawn exception, error exit, silent exit, hung wake — see
  `server.py::_handle_rate_limit_on_exit` / `_announce_spawn_failure` /
  `_announce_noreply_on_exit` / `_check_stuck_wakes`). Don't build
  orchestrator-side polling/timeout logic for this — read the room instead.
- Dashboard JS: no framework, no bundler — edit the files directly. UI strings
  go through `t()` / `data-i18n`.
- Keep every HTTP data/action/MCP route behind the default-deny guard when
  `MCP_HUDDLE_TOKEN` is set. The dashboard may retain only its derived
  process-local credential in memory; never put the raw token in URLs, cookies
  or browser storage.
- All room/agent/notification file opens must use `bus.py` confined helpers.
  Notification targets are flat direct children of
  `$MCP_HUDDLE_HOME/notifications/`. Do not hold two file locks at once.
- Cross-process wake exclusion is the persisted claim under the meta lock; the
  in-process lock is only a local optimization. A warning or unsuccessful
  SIGTERM must not release that claim before exact child exit.

## Verify before claiming done
```bash
.venv/bin/pytest tests/ -q          # full suite
node --check src/mcp_huddle/static/dashboard.js   # JS syntax
.venv/bin/python -m compileall -q src/mcp_huddle
.venv/bin/python -m pip check
```
For dashboard changes, the server serves files fresh (no-cache) — just reload
the browser. Python changes (server/spawn) require a server restart to go live.

## Don't
- Don't break `bus.py` locking or `server.py` tool/route signatures.
- Don't add a build step or runtime dependencies to the dashboard.
- Don't enable agents that need interactive login (agy) by default.
