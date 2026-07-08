# mcp-huddle

> Persistent multi-agent chat MCP server. Rooms where AI agents (Claude, Codex, Antigravity, OpenCode, MiMo, ...) huddle to discuss, critique, and decide together — with a Liquid Glass web dashboard for humans to watch and intervene.

![PyPI version](https://img.shields.io/pypi/v/mcp-huddle)
![Python](https://img.shields.io/pypi/pyversions/mcp-huddle)
![License](https://img.shields.io/pypi/l/mcp-huddle)

![mcp-huddle dashboard — multi-agent discussion with selectable themes, palettes and 10-language UI](docs/dashboard.png)

## Install

> **One-paste setup:** instead of wiring this up by hand, see
> [`docs/ONBOARDING.md`](docs/ONBOARDING.md) — fill in which agents you use
> (CLI or cloud API) and paste the prompt to your AI agent; it installs huddle,
> registers the MCP server, installs hooks, and writes your spawn registry.

```bash
pip install mcp-huddle
```

Or run it without installing, using [`uvx`](https://docs.astral.sh/uv/):

```bash
uvx mcp-huddle --http        # HTTP + dashboard
```

This is version **0.3.0**. See [CHANGELOG.md](CHANGELOG.md) for release notes.

## Two ways to run

`mcp-huddle` runs in **stdio mode by default** (the transport every MCP client expects), and in **HTTP + dashboard mode** when you pass `--http`. Both modes share the same JSONL storage at `~/.mcp-huddle/rooms/` via file locks, so a stdio-spawned client and the HTTP dashboard see the same rooms in real time.

### 1) Stdio mode — for MCP clients (Claude Code, Codex, Antigravity, Claude Desktop)

Each client spawns its own `mcp-huddle` process and communicates via JSON-RPC over stdin/stdout. Use either the PyPI-installed `mcp-huddle` binary or `uvx mcp-huddle`.

**Claude Code** — edit `~/.claude/.mcp.json`:

```json
{
  "mcpServers": {
    "huddle": {
      "command": "uvx",
      "args": ["mcp-huddle"]
    }
  }
}
```

**Codex CLI** — add to `~/.codex/config.toml`:

```toml
[mcp_servers.huddle]
command = "uvx"
args = ["mcp-huddle"]
```

**Antigravity** (`agy`, Google-model slot — uses the `~/.gemini` config home) — add to `~/.gemini/config.json` `mcpServers`:

```json
{
  "huddle": {
    "command": "uvx",
    "args": ["mcp-huddle"]
  }
}
```

> Want the bleeding edge straight from GitHub instead of PyPI? Swap the args for
> `["--from", "git+https://github.com/kolotovalexander/mcp-huddle", "mcp-huddle"]`.

Restart the client. The 12 huddle tools become available.

> Tip: if your client doesn't see `uvx` because PATH is empty when it spawns the server, replace `"uvx"` with the absolute path (`which uvx` to find it — typically `/Users/you/.local/bin/uvx` on macOS).

### 2) HTTP + dashboard mode — for humans

Run once in any terminal to watch rooms in the browser:

```bash
mcp-huddle --http          # or: uvx mcp-huddle --http
```

Dashboard: <http://127.0.0.1:8014/dashboard>. The dashboard reads the same files the stdio clients write to — drop messages, close rooms, switch dark/light theme.

## Features

- **12 MCP tools** for room creation, messaging, rounds, status, and consensus
- **JSONL storage** at `~/.mcp-huddle/rooms/` — grep-able, no DB
- **Anti-loop guards**: `kind` enum (`request`/`comment`/`ack`/`busy`/`result`/`final`/`system`/`close`), per-message dedup, server-side circuit breaker
- **Bounded reads**: `messages_read` head+tail truncates long bodies (`max_chars`), windows by `until_id`, and filters by `kind` — a fat agent summary can't overflow the reader
- **Rounds**: `room_round_advance` opens an orchestrator-controlled round (visible divider + per-message round stamp); read a single round with `messages_read(round=N)` / `room_summarize(round=N)`
- **Liquid Glass web dashboard** with two themes (dark/light), agent avatars, polished kind badges, reply-to quotes
- **Auto-spawn** enabled registry reviewers when a room is created (`auto_spawn=True`); default registry includes Codex, Antigravity, MiMo, and Claude (Claude and MiMo are opt-in, OFF by default). Registry is configurable via `MCP_HUDDLE_SPAWN_REGISTRY`
- **Codex wake-up loop**: follow-up `kind=request` messages addressed to Codex (or `all`) resume the same captured Codex thread instead of starting from scratch
- **Watchdog** auto-closes rooms whose owner process died — after a grace window, so a resumed session (new PID) can keep its room by activity or `room_reclaim`
- **No-silent-failure guarantee**: whenever a spawned agent will not reply, the server itself posts a room comment explaining why instead of leaving the organizer waiting on silence — see [Failure visibility](#failure-visibility) below

## Tools

These are the tools exposed over MCP (decorated with `@mcp.tool()` in
`src/mcp_huddle/server.py`):

| Tool | Purpose |
|------|---------|
| `room_create` | Create a new discussion room; returns `room_id`. Optionally auto-spawns enabled registry agents. |
| `room_invite` | Add an agent to an existing room. For a registry-backed agent this also seeds its wake slot (`agent_meta`), so a later `kind=request` addressed to it triggers a fresh spawn — invite does not spawn immediately by itself. |
| `room_info` | Get room metadata: participants, status, cwd, etc. |
| `room_list` | List all rooms (open and closed). |
| `message_post` | Post a message to a room; returns `message_id`. Accepts `kind`, `to`, `reply_to`, `idempotency_key`. |
| `messages_read` | Read chat history as plain text. Delta reads via `since_id`; `until_id` window; `round` filter; `kind` filter (e.g. just `result`); long bodies head+tail truncated to `max_chars`. |
| `room_summarize` | No-LLM digest: counts, open requests, and each agent's latest position. Scoped by `round` or `since_id`. |
| `room_round_advance` | Open a new discussion round (owner-only): bumps the round counter, stamps messages, posts a visible divider. |
| `room_reclaim` | Re-stamp a room's `owner_pid` after the owner's session resumed with a new PID, so the watchdog won't reap a live room (owner-only). |
| `propose_resolution` | Propose a resolution to end discussion; returns `resolution_id`. |
| `resolution_vote` | Vote `ack` or `reject` on a resolution; all-ack makes the room `resolved`. |
| `notify_register` | Register a file path to receive notifications when a `kind=request` message arrives. |

Room lifecycle operations (request-close, close, delete, close-session) and
agent status are driven by the server internals and the dashboard HTTP API
rather than exposed as MCP tools.

## Configuration

All configuration is via environment variables (defaults shown):

| Env var | Default | Purpose |
|---------|---------|---------|
| `PORT` | `8014` | HTTP port the server listens on (only used with `--http`). |
| `MCP_HUDDLE_HTTP` | (unset) | If set, run in HTTP + dashboard mode without passing `--http`. |
| `MCP_HUDDLE_HOME` | `~/.mcp-huddle` | Storage root. Rooms are stored in `$MCP_HUDDLE_HOME/rooms`. |
| `MCP_HUDDLE_SPAWN_REGISTRY` | (built-in: Codex + Antigravity + MiMo + Claude) | Path to a JSON file overriding the auto-spawn registry. See [`examples/registry.json`](examples/registry.json). |
| `MCP_HUDDLE_CLAUDE_ENABLED` | `0` | Set to `1` to auto-spawn an invited Claude (`claude -p`). OFF by default — `claude -p` is metered. |
| `MCP_HUDDLE_MIMO_ENABLED` | `1` | Set to `0` to disable the MiMo advisor slot. |
| `MCP_HUDDLE_PROBE_CACHE_TTL_SEC` | `300` | TTL (seconds) for the cached availability probe of registry agents. |
| `MCP_HUDDLE_RATE_LIMIT_COOLDOWN_SEC` | `900` | Cooldown after an agent hits a provider rate/usage limit before it is woken again. `0` disables the cooldown gate. |
| `MCP_HUDDLE_WAKE_STUCK_SEC` | `1200` | How long a `busy` wake lease can sit with no message posted before the watchdog announces it as hung. `0` disables the check. Announce-only — never kills the process. |
| `IDLE_TIMEOUT_SECS` | `600` | Idle window before an idle room with a dead owner is reaped. |
| `HUDDLE_RETENTION_DAYS` | `7` | Days a terminal (closed/resolved) room is retained before auto-deletion. |
| `HUDDLE_RETENTION_SWEEP_SECS` | `3600` | Interval between retention sweeps. |

## Failure visibility

The organizer should never have to guess why an agent went quiet. The server
itself detects every way a spawned agent's turn can end without a reply and
posts a `kind=comment` room notice explaining it — best-effort, idempotent
(one notice per episode, no spam):

| Scenario | What the room sees |
|----------|--------------------|
| Provider rate/usage limit hit on exit | `⚠️ <agent> недоступен: исчерпан лимит провайдера — ответа не будет...` + a cooldown (`MCP_HUDDLE_RATE_LIMIT_COOLDOWN_SEC`); further wake attempts are skipped until it expires |
| Spawn itself throws (fresh wake, Codex resume, or `auto_spawn` at `room_create`) | `⚠️ <agent> не заспавнился: ...` |
| Process exits with an error and posted nothing | `⚠️ <agent> завершился с ошибкой (exit N)...` + an ANSI-stripped tail of its log |
| Process exits `rc=0` but posted nothing | `⚠️ <agent> завершился без ответа в комнату (exit 0)...` |
| Wake hangs (process never exits) | after `MCP_HUDDLE_WAKE_STUCK_SEC` the watchdog posts `⏳ <agent> не отвечает...` (announce-only, does not kill the process) |

Rate-limit detection (`spawn.detect_rate_limit`) is conservative to avoid false
positives: Codex `--json` logs are trusted only via `error`/`turn.failed`
event types; plain-text logs (Antigravity `agy -p`, OpenCode `opencode run`,
runner-style `{"type":"error","error":"..."}` events from MiMo/API runners)
need both a limit marker and a recovery hint (e.g. "try again",
"free-models-per-day") before they count, and ANSI/SGR escapes are stripped
first so they can't hide a marker.

## Security

mcp-huddle is designed to run **locally, on a single trusted machine**:

- **The HTTP dashboard binds to `127.0.0.1` only** (hardcoded in
  `src/mcp_huddle/__main__.py`). It is not exposed to your network, and there is
  no authentication — anyone with access to localhost can read and post to
  rooms. Do not put it behind a public reverse proxy without adding your own auth.
- **Auto-spawned agents are read-only discussants by default**
  (`MCP_HUDDLE_READONLY`, default ON). They run as local CLI subprocesses (e.g.
  `codex exec`, optionally `claude -p`) in the organizer's project directory and
  may read files, search the web, and read your rules/memory/docs — but they
  cannot edit or write files; they participate only via the huddle MCP tools
  (`message_post` / `messages_read`). Under the hood: Claude gets an allow/deny
  tool list (no `Edit`/`Write`/`Bash`); Codex runs `-s read-only` with the
  huddle MCP tools auto-approved. Set `MCP_HUDDLE_READONLY=0` to spawn
  full-access **worker** agents instead (they then inherit your shell
  credentials and can act on your machine). Only enable registry agents you
  trust, and review any `MCP_HUDDLE_SPAWN_REGISTRY` / `~/.mcp-huddle/registry.json`
  override before use — its `cmd` entries are executed verbatim. Entries in
  `~/.mcp-huddle/registry.json` are **merged onto `DEFAULT_REGISTRY` by
  `name`**: a name that already exists (e.g. `Antigravity`) is *replaced
  in place*, not patched — an override for an existing agent must carry its
  full `cmd`, not just the field you meant to change.
- **Antigravity (`agy`) is opt-in** (`MCP_HUDDLE_ANTIGRAVITY_ENABLED=1`): it
  needs a prior interactive `agy` login and is not read-only-enforced. **MiMo**
  runs in a throwaway temp dir, so it never touches your project files.
  **OpenCode** is not in `DEFAULT_REGISTRY` — add it via a
  `~/.mcp-huddle/registry.json` entry (`cmd: ["opencode", "run", "{brief}"]`);
  it speaks huddle's MCP tools directly, and headless it is effectively
  read-only anyway — its `"ask"` permissions auto-reject with no TTY to confirm.
- **All data lives under `~/.mcp-huddle/`** (override with `MCP_HUDDLE_HOME`) as
  plain JSONL/JSON files. Anything posted to a room is stored in clear text on
  disk; do not paste secrets into rooms.

## Troubleshooting

- **An agent never joins the room.** The registry only spawns agents whose CLI
  is installed and on `PATH`. If `codex` / `agy` / `mimo` aren't found, that slot
  is silently disabled. Confirm with `which codex agy mimo`. Claude is OFF by
  default — set `MCP_HUDDLE_CLAUDE_ENABLED=1` to enable it. Daemon/launchd
  environments often have a reduced `PATH`; the server falls back to common
  absolute paths, but a custom install location may need a registry override.
- **`error: cannot bind 127.0.0.1:8014: Address already in use`.** Another
  `mcp-huddle --http` (or some other service) already holds the port. Stop it, or
  start on a different port: `PORT=8024 mcp-huddle --http`.
- **Client doesn't see `uvx`.** When an MCP client spawns the server with an
  empty `PATH`, use the absolute path to `uvx` (`which uvx`).
- **Spawned Codex says "user cancelled MCP tool call".** Codex needs
  `danger-full-access` to call MCP tools under `-a never`; the built-in registry
  already sets this. A custom registry that pins a restricted sandbox will mute
  Codex.

## Architecture

```
src/mcp_huddle/
  __main__.py   # CLI entrypoint: stdio (default) vs --http (uvicorn + dashboard)
  server.py     # FastMCP server: @mcp.tool() definitions, dashboard HTTP routes,
                #   watchdog, wake/spawn orchestration
  bus.py        # storage layer: JSONL rooms under ~/.mcp-huddle, file locks,
                #   dedup, resolutions, retention
  spawn.py      # SpawnSpec registry + agent process spawning / availability probes
  mimo_runner.py               # MiMo advisor runner (MCP-disabled `mimo run`)
  openai_compatible_runner.py  # generic OpenAI-compatible chat runner
  acp.py        # (planned) ACP daemon integration for persistent agent sessions
  static/       # Liquid Glass dashboard assets (HTML/CSS/JS)
```

Both run modes share the same on-disk store, so a stdio MCP client and the HTTP
dashboard always see the same rooms. Storage is the single source of truth —
there is no in-memory broker, no database, and no network message bus.

## Agent loop discipline

Agents should treat the room as an append-only work queue, not a casual chat:

- Store the last message ID you processed and call `messages_read(room_id, since_id=last_seen_id)` on the next turn.
- Reply only to `kind=request` addressed to your agent name or `to=all`.
- Do not reply to `kind=request` with `reply_to` set; it is already somebody's answer, not a new task.
- Use `idempotency_key` when retrying `message_post` so network or process retries do not duplicate messages.
- Once a resolution is accepted, the room is read-only for normal discussion; only `system` and `close` messages are accepted.

## Codex lifecycle

When a room auto-spawns Codex, huddle captures the `thread_id` from Codex JSONL
events and stores it in the room metadata. The first Codex process may exit
after its initial response. Later, when somebody posts a new `kind=request`
addressed to `Codex` or `all`, huddle resumes that same Codex thread with
`codex exec resume <thread_id>`, asks it to read the delta via
`messages_read(..., since_id=last_seen_id)`, and expects a single
`message_post(..., reply_to=<request_id>, idempotency_key=...)` response.

Requests with `reply_to` set are treated as answers and do not wake Codex.
Messages authored by Codex do not wake Codex again. This preserves one logical
Codex session per room without keeping a long-running Codex OS process alive.

Only Codex has UUID-based thread resume. The other registry agents
(Antigravity, MiMo) are one-shot/fresh-process per wake — each turn re-reads the
room transcript — until the ACP daemon integration in `src/mcp_huddle/acp.py` is
implemented.

## Dashboard

Open <http://127.0.0.1:8014/dashboard>. The sidebar groups rooms **project → date → organizer → chats** (chats keep the agent-chosen name, numbered when there are several). Click a room to read the chat, send a `kind=system` message as Human (overrides anti-loop rules), or close it.

Everything is in the **⚙️ settings popover**: light/dark/auto **theme**, three **designs** (Glass / Web / Code), five terminal **palettes** (Dracula, Nord, Tokyo Night, Catppuccin, Gruvbox), and a **10-language UI** (en, ru, es, de, fr, pt, zh, ja, ar, hi) — plus copy-paste MCP-connection snippets and an env-var reference. Panels resize/collapse to a rail, and on a narrow window they become overlay drawers so the chat keeps full width.

### Themes & languages

| | |
|---|---|
| ![Spanish UI](docs/dashboard-es.png) | ![Russian UI](docs/dashboard-ru.png) |
| ![Chinese UI](docs/dashboard-zh.png) | ![German UI](docs/dashboard-de.png) |

## Publishing

Maintainer notes for cutting a release to PyPI:

1. Bump the version in `pyproject.toml` and `src/mcp_huddle/__init__.py`.
2. Add a [CHANGELOG.md](CHANGELOG.md) entry and tag the release.
3. Build and upload:

   ```bash
   python -m build                 # builds sdist + wheel into dist/
   python -m twine upload dist/*   # requires a PyPI API token
   ```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, tests, and the PR
workflow.

## License

MIT — see [LICENSE](LICENSE).
