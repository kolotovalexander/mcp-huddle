# Four-mode swarm pilot

This is a bounded mechanical pilot, not the full swarm design. It uses the
existing room bus and one-shot CLI wake mechanism. Normal rooms keep their
existing behavior.

## Start

Use `swarm_pilot_create(name, organizer, goal, mode, members, cwd,
workspace_strategy, start, write_policy="read_only")` with:

- `mode`: `council`, `team`, `relay`, or `swarm`.
- `members`: unique, enabled registry profile names; the organizer is separate.
- `cwd`: an already prepared shared working directory. For a write room it must
  be the exact canonical Git worktree root in `MCP_HUDDLE_WRITE_ROOTS`.
- `workspace_strategy`: `shared_only` or `allow_subworktrees`. With explicit
  `write_policy="shared_write"`, the latter creates one detached local Git
  worktree per member. Members coordinate their own changes and integration.
- `write_policy`: `read_only` by default. `shared_write` requires the single
  admin-approved root and supported Claude or Codex write profiles. Claude
  additionally requires `MCP_HUDDLE_CLAUDE_GUARD_COMMAND` to match its
  configured protective hook for both `Edit` and `Write`.
- `start=False`: create state without dispatching agents. `start=True`: check
  the named profiles and send the initial addressed request(s).

`council` and `relay` dispatch one member at a time. `team` and `swarm`
dispatch every named member. An agent posts its answer with `message_post`
(`kind="result"`, `reply_to=<its request id>`), then calls
`swarm_pilot_round_done`. The next council/relay member is dispatched only
after that explicit completion. The organizer calls `swarm_pilot_finish` for
council; in the other modes one member must first claim the `reporter`
responsibility with `swarm_pilot_record` and then publish the final.
When every member has finished, Huddle sends a final request to that reporter
(or requests members to claim one if missing); in council Huddle sends a final
request to the organizer to keep the final word.
`swarm_pilot_finish` accepts the final only after that addressed request is
present in the room; a concurrent early finish cannot leave a stale request
after the room is completed. A late reporter claim returns `final_request`.

A claimed responsibility moves only through `swarm_pilot_transfer(room_id,
member, key, to_member, reason)`. The owner may hand it to another member at
any time. Another member may take it over for themselves only after every
member finished the round and the owner is not running a turn (no active wake
claim). Both sides must be room members, the room must still be working, and
each move is appended to `transfers`, with a system message in the room. A
reporter transfer sends the new reporter a fresh final request (key suffix
`:transfer-<n>`); the old reporter's request is superseded and its
`swarm_pilot_finish` is refused. Council does not have a transferable
reporter: the organizer's final request stays in the room until the organizer
publishes the final word, and Huddle does not wake the organizer.

Use `swarm_pilot_status` for responsibilities, tasks, facts, decisions, and the
final result. The pilot records one deliberate round. Other participants can
communicate with the existing `message_post` and `messages_read` tools. This
does not stream new user input into a busy model process; a request received
mid-turn is queued for a later wake. The member's asserted name is not yet
cryptographically bound to a native CLI session, so do not use this pilot to
delegate new permissions or run untrusted participants.

The pilot stores its state in the existing room `meta.json`, alongside the
room's `messages.jsonl` and agent event logs. It sets `owner_pid=0` and an empty
`session_id` so the organizer's process ending does not close this pilot room.
No background service is activated by importing these tools; Python server
changes take effect after the server running this checkout is started.

## Pinning a Codex member's Huddle URL

A Codex registry profile may set `"mcp_url": "http://127.0.0.1:<port>/mcp"`
(loopback host, explicit port, path `/mcp`, no credentials or query). At
launch Huddle then passes one `-c mcp_servers={huddle={url=...,
default_tools_approval_mode="approve"}}` override, after the read-only and
room write transforms. This pins the child's `huddle` MCP server to that URL.
It does not remove other MCP servers from the user's Codex configuration:
`codex mcp list` still shows them, so an agent can still reach another Huddle
through an aggregator. `~/.codex/config.toml` is not changed; sandbox,
approval, model and effort arguments stay as enforced. Without `mcp_url` the
Codex command is unchanged. The URL's digest is part of `spec_fingerprint`,
so changing it in a pinned room is spec drift. Resumed turns of the default
`Codex` profile (`codex exec resume`) do not yet read this field.

## Bounded replacement after a failed turn

A registry profile is eligible as a backup only when its full entry is enabled,
available, and sets `"swarm_replacement": true`. A profile override in
`registry.json` replaces the whole entry, so retain its complete `cmd`. After
an owned pilot member process exits without an answer, Huddle classifies the
failure and may use one eligible backup, at most three route attempts per
member. The backup keeps the original room member name, ID, responsibility,
request, and log path. It starts a new CLI turn; a launch alone does not count
as an answer. The old failure record remains visible. Unrecognized failures,
an unproved process exit, and exhausted routes do not trigger blind retries.

In a `shared_write` room, the backup must pass the same room workspace policy
before launch; it inherits that room's write rights. A native permission or
user-input wait is never automatically approved. The current pilot does not
yet extract such waits from every CLI, so this state must be surfaced by the
harness integration before that specific branch can be relied on. Backup
routes are an opt-in capability, not a claim that provider quota or model
availability has been checked by a real model request.

## Room proposal checks

`swarm_room_proposal` does not create rooms or launch task work. Its normal
login check verifies only that native Claude/Codex CLI recognizes a local
login; it does not prove model availability. Set `check_exact_model=true` only
when that extra evidence is useful. It makes at most one cached, 45-second
request for each selected Claude/Codex model+effort route, using a fixed
`HUDDLE PREFLIGHT OK` sentinel from a temporary directory. It sends no room
goal, repository files, registry credentials, or `pass_env` values. Claude is
run with tools disabled and permission prompts denied; Codex runs ephemeral in
a read-only sandbox. A result is `passed` only when that exact sentinel comes
back. Missing explicit model or effort, CLI errors, and timeouts block
`create_args`; Huddle does not silently choose a different model or ask Jev
again. Other harnesses are reported as unsupported and also block this strict
proposal. Current local registry entries for Codex and the subscription Opus
profile do not pin both model and effort, so this optional check will mark
them unsupported until those exact settings are configured.

## Mechanical verification

`PYTHONPATH=src python3 -m pytest tests/test_swarm_pilot.py -q`

These tests simulate agent messages. A successful run confirms the four modes'
dispatch order and stored transitions. It does not confirm provider access,
model quality, file editing, a live dashboard, or a real multi-agent run.
