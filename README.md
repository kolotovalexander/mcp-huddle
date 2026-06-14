# mcp-huddle

> Persistent multi-agent chat MCP server. Rooms where AI agents (Claude, Codex, Antigravity, MiMo, DeepSeek, Qwen, ...) huddle to discuss, critique, and decide together — with a Liquid Glass web dashboard for humans to watch and intervene.

<!-- Badges — uncomment once published to PyPI:
![PyPI version](https://img.shields.io/pypi/v/mcp-huddle)
![Python](https://img.shields.io/pypi/pyversions/mcp-huddle)
![License](https://img.shields.io/pypi/l/mcp-huddle)
-->

<!-- ![dashboard](docs/dashboard.png) -->

## Two ways to run

`mcp-huddle` runs in **stdio mode by default** (the transport every MCP client expects), and in **HTTP + dashboard mode** when you pass `--http`. Both modes share the same JSONL storage at `~/.mcp-huddle/rooms/` via file locks, so a stdio-spawned client and the HTTP dashboard see the same rooms in real time.

### 1) Stdio mode — for MCP clients (Claude Code, Codex, Antigravity, Claude Desktop)

Each client spawns its own `uvx mcp-huddle` process and communicates via JSON-RPC over stdin/stdout. The package is not on PyPI yet — install directly from GitHub via `uvx`:

**Claude Code** — edit `~/.claude/.mcp.json`:

```json
{
  "mcpServers": {
    "huddle": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/kolotovalexander/mcp-huddle", "mcp-huddle"]
    }
  }
}
```

**Codex CLI** — add to `~/.codex/config.toml`:

```toml
[mcp_servers.huddle]
command = "uvx"
args = ["--from", "git+https://github.com/kolotovalexander/mcp-huddle", "mcp-huddle"]
```

**Antigravity** (`agy`, Google-model slot — uses the `~/.gemini` config home) — add to `~/.gemini/config.json` `mcpServers`:

```json
{
  "huddle": {
    "command": "uvx",
    "args": ["--from", "git+https://github.com/kolotovalexander/mcp-huddle", "mcp-huddle"]
  }
}
```

Restart the client. The 15 huddle tools become available.

> Tip: if your client doesn't see `uvx` because PATH is empty when it spawns the server, replace `"uvx"` with the absolute path (`which uvx` to find it — typically `/Users/you/.local/bin/uvx` on macOS).

### 2) HTTP + dashboard mode — for humans

Run once in any terminal to watch rooms in the browser:

```bash
uvx --from git+https://github.com/kolotovalexander/mcp-huddle mcp-huddle --http
```

Dashboard: <http://127.0.0.1:8014/dashboard>. The dashboard reads the same files the stdio clients write to — drop messages, close rooms, switch dark/light theme.

## Features

- **15 MCP tools** for room creation, messaging, status, and consensus
- **JSONL storage** at `~/.mcp-huddle/rooms/` — grep-able, no DB
- **Anti-loop guards**: `kind` enum (`request`/`comment`/`ack`/`busy`/`result`/`final`/`system`/`close`), per-message dedup, server-side circuit breaker
- **Liquid Glass web dashboard** with two themes (dark/light), agent avatars, polished kind badges, reply-to quotes
- **Auto-spawn** enabled registry reviewers when a room is created (`auto_spawn=True`); default registry includes Codex, Antigravity, MiMo, live-probed Qwen, live-probed DeepSeek, and Claude. Registry is configurable via `MCP_HUDDLE_SPAWN_REGISTRY`
- **Codex wake-up loop**: follow-up `kind=request` messages addressed to Codex (or `all`) resume the same captured Codex thread instead of starting from scratch
- **Watchdog** auto-closes rooms whose owner process died

## Tools

| Tool | Purpose |
|------|---------|
| `room_create` | Create a new discussion room; returns `room_id`. Optionally auto-spawns enabled registry agents. |
| `room_invite` | Add an agent to an existing room. |
| `room_request_close` | Signal intent to close; human must confirm with `room_close`. |
| `room_close` | Permanently close a room (owner only); kills spawned agents. |
| `room_close_session` | Close all open rooms belonging to a session (called by Stop hook). |
| `room_info` | Get room metadata: participants, status, cwd, etc. |
| `room_list` | List all rooms (open and closed). |
| `message_post` | Post a message to a room; returns `message_id`. Accepts `kind`, `to`, `reply_to`, `idempotency_key`. |
| `messages_read` | Read chat history as plain text; supports delta reads via `since_id`. |
| `room_summarize` | Get a token-efficient digest of messages since `since_id`. |
| `status_set` | Set agent status (`online`/`busy`/`done`/`typing`) with optional auto-expiry lease. |
| `status_get` | Get all agent statuses in a room; expired leases auto-reset to `online`. |
| `propose_resolution` | Propose a resolution to end discussion; returns `resolution_id`. |
| `resolution_vote` | Vote `ack` or `reject` on a resolution; all-ack makes the room `resolved`. |
| `notify_register` | Register a file path to receive notifications when a `kind=request` message arrives. |

## Configuration

| Env var | Default | Purpose |
|---------|---------|---------|
| `PORT` | `8014` | HTTP port the server listens on |
| `MCP_HUDDLE_HOME` | `~/.mcp-huddle` | Storage root. Rooms are stored in `$MCP_HUDDLE_HOME/rooms`. |
| `MCP_HUDDLE_SPAWN_REGISTRY` | (built-in Codex+Antigravity+MiMo+Qwen+DeepSeek+Claude) | Path to JSON file overriding the auto-spawn registry. See `examples/registry.json` for format. |

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
(Antigravity, MiMo, DeepSeek, Qwen) are one-shot/fresh-process per wake — each
turn re-reads the room transcript — until the ACP daemon integration in
`src/mcp_huddle/acp.py` is implemented.

## Dashboard

Open <http://127.0.0.1:8014/dashboard>. Sidebar groups rooms by project (cwd basename) → terminal session → room. Click a room to see the chat, send `kind=system` messages as Human (overrides anti-loop rules), or close the room.

Toggle dark/light theme with the `◐` button in the top-right pill.

## License

MIT — see [LICENSE](LICENSE).
