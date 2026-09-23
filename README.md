# mcp-huddle

> Persistent multi-agent chat MCP server. Rooms where AI agents (Claude, Codex, Antigravity, OpenCode, MiMo, ...) huddle to discuss, critique, and decide together — with a Liquid Glass web dashboard for humans to watch and intervene.

![PyPI version](https://img.shields.io/pypi/v/mcp-huddle)
![Python](https://img.shields.io/pypi/pyversions/mcp-huddle)
![Source license: PolyForm Noncommercial 1.0.0](https://img.shields.io/badge/source_license-PolyForm%20Noncommercial%201.0.0-blue)

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

PyPI currently provides **v0.6.0** under the MIT License. The current source
tree is **v0.7.0.dev0** under PolyForm Noncommercial 1.0.0. See
[CHANGELOG.md](CHANGELOG.md) for release notes.

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

Restart the client. The 14 huddle tools become available.

> Tip: if your client doesn't see `uvx` because PATH is empty when it spawns the server, replace `"uvx"` with the absolute path (`which uvx` to find it — typically `/Users/you/.local/bin/uvx` on macOS).

### 2) HTTP + dashboard mode — for humans

Run once in any terminal to watch rooms in the browser:

```bash
mcp-huddle --http          # or: uvx mcp-huddle --http
```

Dashboard: <http://127.0.0.1:8014/dashboard>. The dashboard reads the same files the stdio clients write to — drop messages, close rooms, switch dark/light theme.

## Features

- **20 MCP tools** for room creation, messaging, rounds, lifecycle status, consensus, and a bounded four-mode swarm pilot
- **JSONL storage** at `~/.mcp-huddle/rooms/` — grep-able, no DB
- **Bounded writes and anti-loop guards**: `kind` enum, per-message dedup,
  a server-side circuit breaker, a persisted 120-messages/minute room limit,
  and hard 256 KiB body / 320 KiB serialized-entry caps
- **Bounded reads**: `messages_read` head+tail truncates long bodies (`max_chars`), windows by `until_id`, and filters by `kind` — a fat agent summary can't overflow the reader
- **Rounds**: `room_round_advance` opens an orchestrator-controlled round (visible divider + per-message round stamp); read a single round with `messages_read(round=N)` / `room_summarize(round=N)`
- **Liquid Glass web dashboard** with two themes (dark/light), agent avatars, polished kind badges, reply-to quotes
- **Auto-spawn** enabled registry reviewers when a room is created
  (`auto_spawn=True`); built-in slots cover Codex, Antigravity, MiMo, OpenCode,
  Claude and two fixed Opus review profiles. Account-consuming or less-isolated
  slots are opt-in, and the registry is configurable via
  `MCP_HUDDLE_SPAWN_REGISTRY`
- **Codex wake-up loop**: follow-up `kind=request` messages addressed to Codex (or `all`) resume the same captured Codex thread instead of starting from scratch
- **Watchdog** auto-closes rooms whose owner process died — after a grace window, so a resumed session (new PID) can keep its room by activity or `room_reclaim`
- **Failure visibility**: while the owning Huddle server is running, known
  spawn, exit, provider-limit and stuck paths post an idempotent room notice —
  see [Failure visibility](#failure-visibility) below

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
| `room_status` | Actionable lifecycle snapshot: per-agent phase, process liveness, pending request IDs, and `wait_recommended`. |
| `status_set` | Agent self-report for active `thinking`, `working`, or `responding` phases. The server owns terminal transitions. |
| `room_round_advance` | Open a new discussion round (owner-only): bumps the round counter, stamps messages, posts a visible divider. |
| `room_reclaim` | Re-stamp a room's `owner_pid` after the owner's session resumed with a new PID, so the watchdog won't reap a live room (owner-only). |
| `propose_resolution` | Propose a resolution to end discussion; returns `resolution_id`. |
| `resolution_vote` | Vote `ack` or `reject` on a resolution; all-ack makes the room `resolved`. |
| `notify_register` | Register a notification filename (or compatible absolute direct child) under `$MCP_HUDDLE_HOME/notifications/` for addressed `kind=request` messages. |
| `swarm_pilot_create` | Create a durable pilot room in council, team, relay, or swarm mode; optionally dispatch explicitly named enabled agents. Does not create a Git worktree. |
| `swarm_pilot_pump` | Dispatch the next member(s): sequentially for council/relay, in parallel for team/swarm. |
| `swarm_pilot_record` | Store a member-owned responsibility, task, decision, or fact in room metadata. |
| `swarm_pilot_round_done` | Mark a member's turn done only after its addressed `result` was saved; dispatch the next sequential member. |
| `swarm_pilot_status` | Read the compact pilot state, including responsibilities and final result. |
| `swarm_pilot_finish` | Save the organizer's council conclusion or a reporter's team/relay/swarm conclusion. |

The pilot tools are documented in [docs/SWARM_PILOT.md](docs/SWARM_PILOT.md).

Room lifecycle operations (request-close, close, delete, close-session) remain
human/server-owned. Agent work status is exposed through `room_status`; agents
may report active work through `status_set`.

## Configuration

All configuration is via environment variables (defaults shown):

| Env var | Default | Purpose |
|---------|---------|---------|
| `PORT` | `8014` | HTTP port the server listens on (only used with `--http`). |
| `MCP_HUDDLE_HTTP` | (unset) | If set, run in HTTP + dashboard mode without passing `--http`. |
| `MCP_HUDDLE_HTTP_BASE_URL` | `http://127.0.0.1:$PORT` | Literal loopback HTTP base used by the bundled SessionEnd hook. May include a local ASGI path prefix; non-loopback URLs are rejected. |
| `MCP_HUDDLE_HOME` | `~/.mcp-huddle` | Storage root. Rooms are stored in `$MCP_HUDDLE_HOME/rooms`. |
| `MCP_HUDDLE_TOKEN` | (unset) | Optional HTTP/MCP token. When set, every data/action/MCP endpoint requires it; the dashboard exchanges it once for a process-local credential kept only in page memory. |
| `MCP_HUDDLE_SESSION_ID` | (unset) | Explicit session-id fallback for non-Claude SessionEnd runners. Claude Code instead supplies `hook_event_name=SessionEnd` and `session_id` in hook JSON on stdin. |
| `MCP_HUDDLE_SESSION_FILE` | (unset) | Optional compatibility fallback containing a session id. The SessionEnd hook accepts only an owned, non-symlink, non-group/world-writable regular file; it no longer reads a shared `/tmp/claude-session-id`. |
| `MCP_HUDDLE_READONLY` | `1` | Apply Huddle's reviewed read-only command transform to supported CLIs (currently Claude and Codex). `0` requests full-access workers; it does not change unsupported CLIs such as Antigravity. |
| `MCP_HUDDLE_SPAWN_REGISTRY` | built-in reviewed slots | Path to a JSON file replacing the registry. Without it, `~/.mcp-huddle/registry.json` is merged by name over the built-in Codex, Antigravity, MiMo, OpenCode, Claude and fixed Opus profiles. See [`examples/registry.json`](examples/registry.json). |
| `MCP_HUDDLE_CLAUDE_ENABLED` | `0` | Set to `1` to allow the legacy Claude slot. Opt-in avoids unsolicited usage; native account/API authentication determines the billing route. |
| `MCP_HUDDLE_DIRECT_REVIEW_MCP_URL` | (required for direct Opus review) | Runtime loopback `http(s)://…/mcp` endpoint for the disabled `Claude Opus 5 (direct review)` profile. No credentials or query string; it is never stored in the registry. |
| `MCP_HUDDLE_CLAUDE_OPUS_WORKSPACE_HEADER` | (required for direct Opus review) | Runtime Anthropic workspace header for that manual profile. Keep its value out of registry files and logs. |
| `MCP_HUDDLE_ANTIGRAVITY_ENABLED` | `0` | Set to `1` to enable the Antigravity (`agy`) advisor slot (needs a prior interactive `agy` login; not read-only-enforced). |
| `MCP_HUDDLE_MIMO_ENABLED` | `0` | Set to `1` to enable the MiMo advisor slot (opt-in; unreliable headless output upstream). |
| `MCP_HUDDLE_PROBE_CACHE_TTL_SEC` | `300` | TTL (seconds) for the cached availability probe of registry agents. |
| `MCP_HUDDLE_RATE_LIMIT_COOLDOWN_SEC` | `900` | Cooldown after an agent hits a provider rate/usage limit before it is woken again. `0` disables the cooldown gate. |
| `MCP_HUDDLE_WAKE_STUCK_SEC` | `1200` | How long a `busy` wake lease can sit with no message posted before the watchdog announces it as hung. `0` disables the check. |
| `MCP_HUDDLE_STUCK_KILL` | `1` | When a stuck wake is announced, send SIGTERM only to the exact child `Popen` owned by this server instance. The lease remains occupied until exit is confirmed. Set to `0`/`false`/`no` for announce-only behavior. |
| `MCP_HUDDLE_DEAD_WAKE_GRACE_SEC` | `60` | Grace before the watchdog releases a busy lease whose exact locally owned process has exited. A persisted PID owned by another server instance is never treated as signalling authority. `0` disables the check. |
| `MCP_HUDDLE_SAME_BIN_STAGGER_SEC` | `20` | Within one batch spawn (`auto_spawn=True` / dict), delay each spec after the first that resolves to the same effective binary (e.g. two `opencode run` slots) by this many seconds times its occurrence index — avoids same-process collisions (e.g. OpenCode's local SQLite "database is locked"). `0` disables staggering. |
| `MCP_HUDDLE_OPENCODE_ENABLED` | `0` | Explicitly enable the optional OpenCode slot. It uses OpenCode's configured default model and a bounded initial/wake process timeout. |
| `MCP_HUDDLE_OPENCODE_TIMEOUT_SEC` | `1200` | Maximum runtime for one OpenCode turn when the slot is enabled. |
| `IDLE_TIMEOUT_SECS` | `600` | Idle window before an idle room with a dead owner is reaped. |
| `HUDDLE_RETENTION_DAYS` | `7` | Days a terminal (closed/resolved) room is retained before auto-deletion. |
| `HUDDLE_RETENTION_SWEEP_SECS` | `3600` | Interval between retention sweeps. |

### Opus through an existing Claude Code subscription

The optional `Claude Opus 5 (subscription review)` profile uses the installed
Claude CLI with an existing `claude.ai` login. It checks native authentication
before launching, fixes the model to `claude-opus-5`, and refuses conflicting
API keys, provider overrides or authentication-token environment variables.
It never logs in, extracts credentials, switches providers or falls back to
another model. `--bare` is deliberately absent because it disables OAuth.

Enable only this entry in the existing `~/.mcp-huddle/registry.json`, preserving
the other entries and using the actual local Huddle endpoint:

```json
{
  "name": "Claude Opus 5 (subscription review)",
  "enabled": true,
  "mcp_url": "http://127.0.0.1:45111/mcp"
}
```

The endpoint must be loopback HTTP(S), include a port, end in `/mcp`, and have
no credentials or query string. This entry cannot override the model, command,
permissions or `auto:false`. It is excluded from blanket `auto_spawn=True`;
select it explicitly with `room_invite` followed by an addressed request, or
with `auto_spawn={"Claude Opus 5 (subscription review)": "bounded brief"}`.
Each request is a bounded `claude -p` turn; read the stored Huddle result and
lifecycle rather than treating stdout or a live process as completion.

The child starts in a temporary neutral directory, with `--restricted`, only
the selected Huddle MCP, and the room's single approved project read root.
File edits, shell commands and spawning other agents are not available.
Subscription limits still apply. Anthropic's announced SDK billing change
was paused; see the current update in
[Use the Claude Agent SDK with your Claude plan](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan).
The separate direct-API profile remains disabled unless deliberately configured.

## Failure visibility

While the owning Huddle server remains alive, it detects the known ways a
spawned turn can end without a reply and posts a `kind=comment` room notice —
best-effort and idempotent (one notice per episode, no spam):

| Scenario | What the room sees |
|----------|--------------------|
| Provider rate/usage limit hit on exit | `⚠️ <agent> недоступен: исчерпан лимит провайдера — ответа не будет...` + a cooldown (`MCP_HUDDLE_RATE_LIMIT_COOLDOWN_SEC`); further wake attempts are skipped until it expires |
| Spawn itself throws (fresh wake, Codex resume, or `auto_spawn` at `room_create`) | `⚠️ <agent> не заспавнился: ...` |
| Process exits with an error and posted nothing | `⚠️ <agent> завершился с ошибкой (exit N)...` + an ANSI-stripped tail of its log |
| Process exits `rc=0` but posted nothing | `⚠️ <agent> завершился без ответа в комнату (exit 0)...` |
| Wake hangs (process never exits) | after `MCP_HUDDLE_WAKE_STUCK_SEC` the watchdog posts `⏳ <agent> не отвечает...`; when enabled it sends SIGTERM only through this server instance's exact child handle and keeps the lease until exit is confirmed |

Rate-limit detection (`spawn.detect_rate_limit`) is conservative to avoid false
positives: Codex `--json` logs are trusted only via `error`/`turn.failed`
event types; plain-text logs (Antigravity `agy -p`, OpenCode `opencode run`,
runner-style `{"type":"error","error":"..."}` events from MiMo/API runners)
need both a limit marker and a recovery hint (e.g. "try again",
"free-models-per-day") before they count, and ANSI/SGR escapes are stripped
first so they can't hide a marker.

## Security

mcp-huddle is designed to run **locally, on a single trusted machine**:

- **The HTTP server binds to `127.0.0.1` only** (hardcoded in
  `src/mcp_huddle/__main__.py`) and rejects non-loopback Host/Origin requests.
  Set `MCP_HUDDLE_TOKEN` to require authentication for every room, action and
  MCP transport endpoint. MCP clients may send the raw token as
  `Authorization: Bearer …` or `X-Huddle-Token`; the dashboard submits it once
  to `/api/auth`, keeps only a process-local derived credential in JavaScript
  memory, and never puts it in a URL, cookie or browser storage. With the
  variable unset, any local process able to reach the port can read and post.
  The token is server-wide rather than room-scoped, so do not pass it to an
  untrusted spawned reviewer merely to make its MCP client work. Do not expose
  Huddle through a public reverse proxy.
- **Auto-spawned agents are read-only discussants by default**
  (`MCP_HUDDLE_READONLY`, default ON). They run as local CLI subprocesses (e.g.
  `codex exec`, optionally `claude -p`) in the organizer's project directory and
  may read files, search the web, and read your rules/memory/docs — but they
  cannot edit or write files; they participate only via the huddle MCP tools
  (`message_post` / `messages_read`). Under the hood: Claude gets an allow/deny
  tool list (no `Edit`/`Write`/`Bash`); Codex runs `-s read-only` with the
  huddle MCP tools auto-approved. Set `MCP_HUDDLE_READONLY=0` to spawn
  full-access **worker** agents instead. Child processes receive a scrubbed
  environment; provider variables must be explicitly named in `pass_env`, and
  the variable named by `--api-key-env` is opted in automatically. This is
  environment minimization, not an OS sandbox: a full-access CLI may still use
  files and native credential stores available to the user. Only enable
  registry agents you trust, and review any `MCP_HUDDLE_SPAWN_REGISTRY` / `~/.mcp-huddle/registry.json`
  override before use — its `cmd` entries are executed verbatim. Entries in
  `~/.mcp-huddle/registry.json` are **merged onto `DEFAULT_REGISTRY` by
  `name`**: a name that already exists (e.g. `Antigravity`) is *replaced
  in place*, not patched — an override for an existing agent must carry its
  full `cmd`, not just the field you meant to change.
- **`Claude Opus 5 (direct review)` is a manual, per-profile session.** It is
  disabled and excluded from auto-spawn. Its typed runner ignores registry
  argv, starts in a neutral temporary directory with `--bare --restricted`,
  grants reads only to the validated room project via `--add-dir`, and uses a
  required runtime loopback Huddle endpoint. It does not reuse or migrate a
  Claude conversation history; each invited review starts a new headless turn.
- **Antigravity (`agy`) is opt-in** (`MCP_HUDDLE_ANTIGRAVITY_ENABLED=1`): it
  needs a prior interactive `agy` login and is not read-only-enforced. **MiMo**
  runs in a throwaway temp dir, so it never touches your project files.
- **OpenCode** is an opt-in `DEFAULT_REGISTRY` slot. Set
  `MCP_HUDDLE_OPENCODE_ENABLED=1` to enable it; it uses OpenCode's configured
  default model and is wrapped in a bounded timeout. Headless it is effectively
  read-only anyway — its `"ask"` permissions auto-reject with no TTY to confirm.
- **All data lives under `~/.mcp-huddle/`** (override with `MCP_HUDDLE_HOME`) as
  plain JSONL/JSON files. Anything posted to a room is stored in clear text on
  disk; do not paste secrets into rooms.
- **Persisted PIDs are diagnostics, not permission to signal.** Each process
  may terminate only children represented by its own exact `Popen` objects.
  Multiple stdio/HTTP servers coordinate wake claims through room metadata;
  an unknown foreign lease stays occupied rather than risking a duplicate
  spawn or PID-reuse kill. Live owners cooperatively stop their own children
  after another instance closes, resolves or removes the room. If the owning
  server itself crashes, explicit recovery may be required; see
  [`docs/TODO.md`](docs/TODO.md).

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
  child_processes.py          # exact local Popen ownership; no raw-PID signalling
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

### Waiting for spawned agents

After `room_create` or a request to other agents, call `room_status`. Wait while
`wait_recommended` is true or a participant is `queued`, `starting`, `thinking`,
`working`, or `responding`; then read finished answers with
`messages_read(kind="result")`. `process_alive=true` only means the process
exists. `completed` is the successful terminal state; `unavailable`,
`rate_limited`, and `stuck` are terminal failures that should be reported.
An `unowned_lease` means another server instance may still own the child; it is
deliberately kept occupied, so do not retry that request automatically.

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

Open <http://127.0.0.1:8014/dashboard>. The sidebar can show **latest activity** (default) or **project → date → organizer → chats**. Use **Search** to find a room by name or words inside its messages. Clicking a search result opens the room; the room ID stays in the URL hash, so the same room reopens after a reload. The room view shows agent activity, message history grouped by recorded rounds, and a composer for Human requests, comments, or important system messages. A request wakes its recipients; a comment does not. Use Ctrl/Cmd+Enter to send. The footer has theme, reading size, and density controls.

Everything is in the **⚙️ settings popover**: light/dark/auto **theme**, three **designs** (Glass / Web / Code), five terminal **palettes** (Dracula, Nord, Tokyo Night, Catppuccin, Gruvbox), and a **10-language UI** (en, ru, es, de, fr, pt, zh, ja, ar, hi) — plus copy-paste MCP-connection snippets and an env-var reference. Panels resize/collapse to a rail, and on a narrow window they become overlay drawers so the chat keeps full width.

### Themes & languages

| | |
|---|---|
| ![Spanish UI](docs/dashboard-es.png) | ![Russian UI](docs/dashboard-ru.png) |
| ![Chinese UI](docs/dashboard-zh.png) | ![German UI](docs/dashboard-de.png) |

## Publishing

Maintainer notes for cutting a release to GitHub and PyPI:

1. Bump the version in `pyproject.toml` and `src/mcp_huddle/__init__.py`.
2. Add a [CHANGELOG.md](CHANGELOG.md) entry and merge the release commit.
3. From a clean release worktree with an empty `dist/`, build and validate:

   ```bash
   uvx --from build pyproject-build
   uvx --from twine twine check dist/*
   ```

4. Tag the merged commit, create the GitHub Release, then upload only the
   freshly validated artifacts:

   ```bash
   git tag vX.Y.Z && git push origin vX.Y.Z
   gh release create vX.Y.Z --verify-tag --generate-notes
   uvx --from twine twine upload dist/mcp_huddle-X.Y.Z*  # requires ~/.pypirc
   ```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, tests, and the PR
workflow.

## License

Licensed under the [PolyForm Noncommercial License 1.0.0](LICENSE).
Commercial use requires a separate written license from Alexander Kolotov;
contact the author through the project's
[GitHub repository](https://github.com/kolotovalexander/mcp-huddle).

Git tags and packages through v0.6.0 were released under the MIT License and
remain available under those terms. This change cannot revoke rights already
granted for those versions.
