# Native cross-harness message delivery

`message_send` and `message_targets` hand a message to another agent session
that lives *outside* the current huddle room — a different Claude session, a
Codex thread, a Hermes peer, an OpenCode session, or an `agy` conversation.
There is no LLM in this path: huddle picks a deterministic "postman" per
harness and tries delivery methods in a fixed order, passing the caller's
text through unchanged. Implementation: `src/mcp_huddle/delivery/`.

## Tools

### `message_send(to, text, mode="auto", from_name="", reply_to="", idempotency_key="", room_id="", native_routes=None)`

`room_id` lets
Huddle verify the caller's own identity when it's a swarm pilot member, so
the two guards below can decide whether to refuse. It is not otherwise used
for resolving `to`.

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

An optional `native_routes` list lets a caller declare a concrete native tool
for an exact target:

```json
[{"target":"codex:THREAD_ID","tool":"mcp__codex_app__send_message_to_thread",
  "reason":"This tool is available here and can reach that thread."}]
```

When `target` exactly matches the requested target (`harness:id` after
resolution, or the original `to` string), Huddle returns
`reason: "native_route_required"`, `suggested_tool`, and a short explanation.
It makes no delivery attempt, writes no spool, and does not fall back to a
different transport. The existing read-only-caller and Huddle-owned-session
checks run first.

This declaration is caller-provided and self-reported. The MCP protocol does
not tell Huddle which native tools the agent can currently call. Declare only
tools actually exposed in the current caller session, and only for the exact
destination they can reach. If native availability is unknown, omit the
declaration; Huddle keeps its configured cross-harness delivery behavior.
This guard covers `message_send` only. It does not block persistent room,
council, relay, team, or swarm workflows.

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
| `hermes:peer:<peer[/agent]>` | Hermes **peer** target (DM-able) -- `hermes.native` only |
| `hermes:session:<id>` | Hermes **session** target (resumable) -- `hermes.resume` only |
| `hermes:<peer[/agent]>` | Backward-compatible bare form; equivalent to `hermes:peer:<peer[/agent]>` |
| `opencode:<sessionId>` | OpenCode session id |
| `agy:<conversationId>` | agy conversation id |
| `<bare name>` | searched across Claude + Codex; ambiguous match is an error listing every candidate as `harness:id` |

A hermes **peer** name is never a session id and is never substituted for
one, and vice versa: a peer target only ever tries `hermes.native` (falling
through to `spool`, never `hermes.resume`), and a session target only ever
tries `hermes.resume` (never `hermes.native`, which has no peer to DM). Using
a peer name as a session id used to be possible and risked a double turn
against a live peer session -- see the "Honesty" and ambiguous-failure
sections below.

Every `<id>` (Claude sessionId, Codex thread id, Hermes peer/session,
OpenCode session id, agy conversation id) is validated against a strict
allowlist (`^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,199}$`, never a leading `-`)
before it's used anywhere, both at resolution time (for the harnesses with no
local registry to validate against: hermes/opencode/agy) and in every argv
template (every `{id}`/`{peer}` placeholder is bound the same way `{text}`
already was -- merged into a single `--flag=value` token, or placed after a
literal `--`, so it can never be parsed as a separate CLI flag no matter what
the caller passed in `to`).

A forced `mode` that names a full method id (e.g. `"claude.resume"`) is also
checked against the *resolved* target's harness before anything is
attempted: forcing a Claude method against a target that resolved to a
different harness (e.g. `to="agy:..."` with `mode="claude.resume"`) is
refused immediately, with an empty `attempts` list -- never dispatched.

## Target resolution

- **Claude**: reads `~/.claude/sessions/<pid>.json` (override the directory
  with `MCP_HUDDLE_DELIVERY_CLAUDE_SESSIONS_DIR`, used by tests). Expected
  fields: `pid`, `sessionId`, `name`, `status`, `messagingSocketPath`, `cwd`,
  `entrypoint`, `kind`. Liveness is a **tri-state**, not a bool: `target.live`
  is `True` (alive -- `claude.native`-eligible), `False` (confirmed dead --
  `claude.resume`-eligible), or `None`/"unknown" (neither -- falls through to
  `spool`). Process-alive-ness and socket-reachability are checked
  separately: `os.kill(pid, 0)` raising `ProcessLookupError` is the *only*
  thing that counts as confirmed-dead; a `PermissionError` (or any other
  doubt) is unknown, never dead. A confirmed-alive process whose
  `messagingSocketPath` can't be confirmed to exist is also unknown, not
  dead -- this used to be misreported as "not live", which let
  `claude.resume` run against a session that might still be running. If the
  same `sessionId` appears in more than one registry entry (e.g. a stale file
  left behind alongside a fresh one), the entries are merged rather than
  treated as ambiguous, and the merged state is "alive" if *any* entry
  independently reads as alive. This merge applies identically whether the
  session was found by id or by name: resolving `claude:<name>` first finds
  every entry with a matching name, then merges *all* registry entries that
  share the resulting sessionId(s) -- not just the ones that still carry that
  name -- so a stale record under an old name can never shadow a live entry
  for the same session (e.g. after a rename).
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
| `codex.native` | `codex queue --thread=<id> --message=<envelope>`, waited up to 30s; ok iff exit 0. A timeout, or a nonzero exit whose stderr/stdout doesn't contain `"not loaded"` / `"no active session"` / `"unknown thread"`, is **ambiguous** (see below) and blocks the automatic fall-through to `codex.resume`. |
| `codex.resume` | Detached: `codex exec resume -- <id> <envelope>`. Auto mode never runs this right after an ambiguous `codex.native` failure. |
| `hermes.native` | Only for a **peer** target. `hermes peer dm <peer[/agent]> <envelope>`, waited up to 120s. A timeout, or any nonzero exit (there's no known-safe "peer unreachable" signal to distinguish from an ambiguous one), is **ambiguous** and blocks the paired `hermes.resume`. |
| `hermes.resume` | Only for a **session** target. Detached: `hermes --resume=<session> chat -q <envelope>`. **Unverified** — this argv is a config-default template, not something exercised against a real `hermes` binary. |
| `opencode.native` | Only if `opencode.server_url` is configured. Best-effort `GET {server_url}/session/status` (ignored on failure — see [upstream bug #46842](https://github.com/sst/opencode/issues/46842), a busy session can silently drop the turn), then `POST {server_url}/session/{id}/prompt_async` with `{"parts":[{"type":"text","text":<envelope>}]}` via `urllib`. A timeout, an HTTP error response (401/403/409/429/500/503/...), a dropped/reset connection, or any non-2xx status that doesn't come back as a definite HTTP error response, is **ambiguous** and blocks the paired `opencode.resume`. The only exception is a bare "connection refused" (`ECONNREFUSED`) — nothing is listening on `server_url` at all, which is proof there's no live server to collide with — that is not ambiguous and lets `auto` fall through to `opencode.resume`. |
| `opencode.resume` | Detached: `opencode run --session <id> <envelope>`. |
| `agy.resume` | Detached: `agy --conversation <id> -p <envelope>`. |
| `spool` | Always available, last resort for every harness. Atomically writes the envelope to `$MCP_HUDDLE_HOME/delivery/spool/<harness>/<target_id>/<msg_id>.md` for a harness-side hook to pick up later. |

A "detached" spawn uses `Popen(..., start_new_session=True)` with stdout/stderr
redirected to a log file under `$MCP_HUDDLE_HOME/delivery/logs/`. It watches
the process for a short grace window (`MCP_HUDDLE_DELIVERY_DETACHED_GRACE`,
default 3 s): a non-zero exit inside that window (e.g. `agy` "trajectory not
found" for an unknown conversation) is reported as `ok=False` with the last log
line, so auto mode falls through to the next method. Otherwise it reports
`ok=True` ("started") — the process is running or exited 0 — not that the turn
finished or was read.

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

**Double-writer guard**: Claude's live-check already makes `claude.resume`
safe (it's only tried when the session is provably not live). Codex has no
such live-check, so `codex.native`'s own exit signal must decide it instead:
a definite "not loaded" signal makes falling through to `codex.resume` safe;
anything else the failure could mean (a timeout, an unrecognized error) is
treated as ambiguous, and `auto` mode skips `codex.resume` entirely rather
than risk forking a live thread's history — it falls straight through to
`spool`. This generalizes to every harness whose native method reports a
failure as ambiguous: `hermes.native` and `opencode.native` do the same (see
the methods table above). Hermes's peer/session split means this rarely
comes up in practice for hermes specifically (a peer target's order never
includes `hermes.resume` to begin with), but the mechanism in `core.py` is
harness-agnostic and applies to any harness whose configured order still
puts a `*.native` immediately before a `*.resume` for the same target.

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
if the incoming `text` *is itself* an `<agent-message>` envelope (the tag
must open at the very start of `text`, modulo incidental leading whitespace
— not merely appear somewhere inside it) whose `hops` attribute is at or
past the limit, `message_send` refuses immediately and sends nothing
(`delivered: false`, `note` explains why). Otherwise a fresh send starts at
`hops="1"`; relaying an already-enveloped message increments it. Anchoring
the check at the start of `text` matters: a message that merely quotes or
discusses the envelope syntax (or a forged tag an attacker embeds mid-body)
must not be mistaken for a real forwarded envelope — that could either let a
message that should be refused slip past the loop guard, or spuriously
refuse an innocent one.

**Idempotency**: a repeated `idempotency_key` within 24h returns the exact
same result JSON as the first call and sends nothing new. This is enforced
with a per-key cross-process **lock** (`src/mcp_huddle/delivery/
idempotency.py`): every read and write of
`$MCP_HUDDLE_HOME/delivery/reservations/<sha256(key)>.json` happens while
holding an `fcntl.flock` on a sibling `<sha256(key)>.json.lock` file (created
once, never deleted, so every process locks the same inode), and every write
is published atomically (tmp file in the same directory, `fsync`,
`os.replace`). The first caller to see no existing entry (still under the
lock) writes `{"status": "reserved", "msg_id", "pid", "ts"}` and is the only
one that ever sends; this happens *before* any send is attempted. A
concurrent call with the same key while the first is still in flight (owner
pid confirmably alive) sends nothing and returns a different, minimal shape
instead of the usual result: `{"status": "in_progress", "msg_id": <the
in-flight call's msg_id>}`. Once the owner finishes, it overwrites the file
with `{"status": "done", "msg_id", "ts", "result"}` (but only if the entry
still names its own `msg_id` -- an owner check, so a reservation reassigned
out from under it is never clobbered), which is what a same-key retry gets
back verbatim (and which expires after 24h same as before).

If a `"reserved"` entry's owning pid is confirmably dead, the key moves to
`{"status": "unknown", "msg_id", "ts"}` -- **not** taken over and retried.
Whether that owner delivered the message before it died is indistinguishable
from a crash before ever sending, and guessing either way risks a silent
double-send or a silently dropped message; this used to auto-resend after a
5-minute staleness window, which could deliver the same message twice if the
first sender died just after a successful send but before recording it
(Codex review finding D). A same-key call against an `"unknown"` entry
returns `{"status": "unknown_outcome", "msg_id": <the original msg_id>,
"delivered": null, "note": "..."}` and this never changes on its own -- a
caller that needs a guaranteed resend must supply a **new**
`idempotency_key`.

**Harness enabled**: `delivery.json`'s `harnesses.<harness>.enabled` (see
Configuration below) is checked once the target resolves, before either auto
or forced mode picks a method — a disabled harness is refused immediately
with an empty `attempts` list, in both modes.

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
built-in default in `src/mcp_huddle/delivery/config.py`. `text` is arbitrary,
fully caller-controlled content, so every default template guards against it
being parsed as a CLI flag when it starts with `-`: a literal `--`
"end of options" token immediately before it where it's a trailing
positional argument, or a merged `--flag={text}` token where it's a named
option's value. A custom template overriding these should keep the same
guard.

## Logging

Every attempt appends one JSON line to
`$MCP_HUDDLE_HOME/delivery/log.jsonl`: `ts`, `msg_id`, `to`, `harness`,
`method`, `ok`, `detail`, `text_sha256`, `text_len`. The raw text is never
logged — only its hash and length.

## Swarm-pilot guards

Two policy checks close off `message_send` as a way to work around the
swarm pilot's own constraints (`src/mcp_huddle/delivery/caller.py` and
`ownership.py`). Both refuse before anything is attempted: `attempts: []`,
no spool write, no subprocess. The refusal JSON gains a `"reason"` key for
these two cases only (every pre-existing refusal keeps its old shape).

### Readonly-caller guard

`core.message_send(..., caller=None)` takes an optional `Caller` (a small
frozen dataclass: `verified_member: bool`, `readonly: bool | None`,
`room_id`, `agent`, `wake_id`). It is **fail-closed**:

- `caller is None` -- no Huddle member-token header was ever seen on this
  call, i.e. not an agent Huddle spawned (a human, or an external client).
  Allowed, unchanged from before this guard existed.
- `caller.readonly is False` -- Huddle positively knows this profile's
  read-only transform is not in effect. Allowed.
- `caller.readonly is True`, **or** `caller.readonly is None` (unknown --
  Huddle knows it spawned this caller because it carried the member-token
  header, but couldn't establish its read-only status, e.g. no `room_id`
  was given or the wake claim didn't resolve) -- refused with
  `reason: "readonly_caller"`, unless the target is in `delivery.json`'s
  `readonly_allowed_targets` (a list of `to` strings or resolved
  `harness:id`, default empty).

Building the `Caller` is the server.py tool wrapper's job, not this
module's -- see the hunk below. The wrapper's contract: any call that
carries Huddle's `X-Huddle-Member` header is *always* turned into a
`Caller` object, even when room/wake verification fails -- never silently
downgraded to `caller=None`, which would let a caller that omits or lies
about `room_id` launder itself into the allowed path.

### Huddle-owned-session guard

Independent of the caller check, `message_send` refuses (`reason:
"huddle_owned_session"`) when the resolved target is a **Codex thread**
Huddle itself currently owns: some room's `agent_meta[member]` carries both
a live `wake_claim_id` and a `thread_id` equal to the target id. This is
unconditional -- it applies even for `caller=None` -- because the risk
(two turns racing into one Codex thread history) doesn't depend on who's
asking.

**Limitation**: only Codex is checked. There is no persisted field mapping
a Huddle wake claim to the *Claude* session id it might correspond to (a
spawned/woken Claude member's `agent_meta` has no equivalent of
`thread_id`), so a `claude:...` target relies only on delivery's existing
liveness tri-state (`claude.native` vs `claude.resume`, above) -- guessing
an ownership mapping that isn't actually recorded would be worse than not
checking at all.

### server.py wrapper

The MCP tool wrapper accepts optional `room_id` and receives the request
`Context`. When the call carries `X-Huddle-Member`, it builds a `Caller` from
the verified room member and effective read-only policy. A missing or false
`room_id` stays `readonly=None`, which is refused by default. Calls without
that header keep the external-client behavior. This guard protects the
Huddle-issued member-token route; it does not authenticate arbitrary clients
that omit the header or use another server connection.

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
