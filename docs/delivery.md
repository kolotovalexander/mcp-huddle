# Native cross-harness message delivery

`message_send` and `message_targets` hand a message to another agent session
that lives *outside* the current huddle room — a different Claude session, a
Codex thread, a Hermes peer, an OpenCode session, or an `agy` conversation.
There is no LLM in this path: huddle picks a deterministic "postman" per
harness and tries delivery methods in a fixed order, passing the caller's
text through unchanged. Implementation: `src/mcp_huddle/delivery/`.

## Tools

### `message_send(to, text, mode="auto", from_name="", reply_to="", idempotency_key="")`

Resolves `to`, wraps `text` in the envelope (below), and tries methods in
order until one succeeds or the order is exhausted. Returns a JSON string:

```json
{"msg_id": "...", "delivered": true, "method": "codex.native",
 "attempts": [{"method": "codex.native", "ok": true, "detail": "queued"}],
 "note": "delivered to the recipient's live session"}
```

`mode="auto"` (default) uses the harness's configured order. Any other value
is treated as a forced single method (either a bare token resolved against
the target's harness, e.g. `"resume"` -> `"codex.resume"`, or a full method
id, e.g. `"claude.native"`, or `"spool"`); no fallback is attempted for a
forced mode — if that one method isn't applicable, `delivered` is `false`.

### `message_targets(harness="")`

Lists resolvable targets: `{harness, id, name, live, methods}` per entry.
Only Claude and Codex have a discoverable local registry, so those are the
only harnesses this can enumerate; Hermes/OpenCode/agy targets can still be
addressed directly by id even though they won't show up in this list. Never
returns tokens, sockets, or file contents.

## `to` syntax

| Form | Meaning |
|---|---|
| `claude:<name\|sessionId>` | Claude session, by registry name or session id |
| `codex:<threadId\|thread_name>` | Codex thread, by id or name |
| `codex://threads/<id>` | Codex thread, by id (URI form) |
| `hermes:<peer[/agent]\|session>` | Hermes peer (optionally `/agent`) or session |
| `opencode:<sessionId>` | OpenCode session id |
| `agy:<conversationId>` | agy conversation id |
| `<bare name>` | searched across Claude + Codex; ambiguous match is an error listing every candidate as `harness:id` |

## Target resolution

- **Claude**: reads `~/.claude/sessions/<pid>.json` (override the directory
  with `MCP_HUDDLE_DELIVERY_CLAUDE_SESSIONS_DIR`, used by tests). Expected
  fields: `pid`, `sessionId`, `name`, `status`, `messagingSocketPath`, `cwd`,
  `entrypoint`, `kind`. A session is **live** only if `os.kill(pid, 0)`
  succeeds (or raises `PermissionError`, meaning the process exists but is
  owned by someone else) *and* `messagingSocketPath` exists on disk.
- **Codex**: reads `~/.codex/session_index.jsonl` (override with
  `MCP_HUDDLE_DELIVERY_CODEX_HOME`), lines of `{"id", "thread_name",
  "updated_at"}`. The last line for a given `id` wins. There is no live
  detection for Codex threads; `codex.native` is always attempted first
  regardless, and its own exit code decides success.

## Methods ("postmen")

Every method receives the same envelope text and returns `{ok, method,
detail}`. Binaries are resolved with `shutil.which`; a missing binary is
reported as `"binary not found"`, never raised. All subprocess calls use an
argv list — never `shell=True`.

| Method | What it does |
|---|---|
| `claude.native` | Only if the target is live. Connects `AF_UNIX` to `messagingSocketPath` (5s timeout) and writes one line: `{"type":"user","message":{"role":"user","content":<envelope>}}\n`, then closes. This wire format is the one used by the [openmsg](https://github.com/steipete/openmsg) project; Anthropic's own docs (<https://code.claude.com/docs/en/cross-session-messaging>) document only the socket and an optional `{"type":"auth","token":...}` line, which we deliberately never send — we don't own the receiver's token. |
| `claude.resume` | Only if the target is **not** live. Detached: `claude -p --resume <sessionId> <envelope>`, cwd = the session's cwd. |
| `codex.native` | `codex queue --thread <id> --message <envelope>`, waited up to 30s; ok iff exit 0. |
| `codex.resume` | Detached: `codex exec resume <id> <envelope>`. |
| `hermes.native` | Only if a peer name is available. `hermes peer dm <peer[/agent]> <envelope>`, waited up to 120s. |
| `hermes.resume` | Detached: `hermes --resume <session> chat -q <envelope>`. **Unverified** — this argv is a config-default template, not something exercised against a real `hermes` binary. |
| `opencode.native` | Only if `opencode.server_url` is configured. Best-effort `GET {server_url}/session/status` (ignored on failure — see [upstream bug #46842](https://github.com/sst/opencode/issues/46842), a busy session can silently drop the turn), then `POST {server_url}/session/{id}/prompt_async` with `{"parts":[{"type":"text","text":<envelope>}]}` via `urllib`. |
| `opencode.resume` | Detached: `opencode run --session <id> <envelope>`. |
| `agy.resume` | Detached: `agy --conversation <id> -p <envelope>`. |
| `spool` | Always available, last resort for every harness. Atomically writes the envelope to `$MCP_HUDDLE_HOME/delivery/spool/<harness>/<target_id>/<msg_id>.md` for a harness-side hook to pick up later. |

A "detached" spawn uses `Popen(..., start_new_session=True)` with stdout/stderr
redirected to a log file under `$MCP_HUDDLE_HOME/delivery/logs/`; it reports
`ok=True` as soon as the process **starts**, not once it finishes.

### Default order per harness

```
claude:   [native, resume, spool]
codex:    [native, resume, spool]
hermes:   [native, resume, spool]
opencode: [native, resume, spool]
agy:      [resume, spool]           # no native transport exists for agy
```

`auto` mode walks this list, skipping (not erroring on) a method that isn't
applicable to the resolved target (e.g. `claude.native` when the session
isn't live, or `claude.resume` when it **is** live — a live Claude session
never gets resumed), stopping at the first method that succeeds.

## Envelope

```
<agent-message from="{from}" from_verified="false" via="huddle:{method}" id="{msg_id}" hops="{n}" reply_to="{reply_to}">
{text}
</agent-message>
```

`text` is never modified — only the surrounding attributes are generated
(and escaped). `from_verified` is always `"false"`: there is no
sender-identity contract yet (agreed with the parallel Codex-side work on
this repo), so a recipient must not treat `from` as authenticated.

**Hops limit** (default 4, configurable via `delivery.json`'s `hops_limit`):
if the incoming `text` already contains an `<agent-message>` envelope whose
`hops` attribute is at or past the limit, `message_send` refuses immediately
and sends nothing (`delivered: false`, `note` explains why). Otherwise a
fresh send starts at `hops="1"`; relaying an already-enveloped message
increments it.

**Idempotency**: a repeated `idempotency_key` within 24h returns the exact
same result JSON as the first call and sends nothing new.

## Configuration

Optional JSON at `$MCP_HUDDLE_HOME/delivery.json` (`MCP_HUDDLE_HOME` defaults
to `~/.mcp-huddle`, same as the rest of huddle):

```json
{
  "hops_limit": 4,
  "harnesses": {
    "codex": {"order": ["native", "resume", "spool"], "enabled": true}
  },
  "methods": {
    "codex.resume": {"argv": ["codex", "exec", "resume", "{id}", "{text}"], "timeout": 30, "enabled": true}
  },
  "opencode": {"server_url": "http://127.0.0.1:4096"}
}
```

Argv templates are always lists (`{id}`, `{peer}`, `{text}`, `{cwd}`
placeholders) — never a shell string. Anything omitted falls back to the
built-in default in `src/mcp_huddle/delivery/config.py`.

## Logging

Every attempt appends one JSON line to
`$MCP_HUDDLE_HOME/delivery/log.jsonl`: `ts`, `msg_id`, `to`, `harness`,
`method`, `ok`, `detail`, `text_sha256`, `text_len`. The raw text is never
logged — only its hash and length.

## Honesty

`delivered: true` does **not** mean the recipient read the message:

- `*.native` success for Claude/Codex/Hermes means the message was actually
  handed to a live process (socket write / queue call / peer DM).
- `*.resume` success only means a detached process was **started** — the
  agent may still fail to pick it up.
- `spool` success only means the envelope was written to disk for a
  harness-side hook to notice later.

`message_send`'s `note` field spells out which of these applies to the final
result.
