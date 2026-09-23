# Four-mode swarm pilot

This is a bounded mechanical pilot, not the full swarm design. It uses the
existing room bus and one-shot CLI wake mechanism. Normal rooms keep their
existing behavior.

## Start

Use `swarm_pilot_create(name, organizer, goal, mode, members, cwd,
workspace_strategy, start)` with:

- `mode`: `council`, `team`, `relay`, or `swarm`.
- `members`: unique, enabled registry profile names; the organizer is separate.
- `cwd`: an already prepared shared working directory. Huddle does not create a
  Git worktree for the pilot.
- `workspace_strategy`: `shared_only` or `allow_subworktrees`. The pilot records
  this choice but does not create sub-worktrees or merge code automatically.
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

## Mechanical verification

`PYTHONPATH=src python3 -m pytest tests/test_swarm_pilot.py -q`

These tests simulate agent messages. A successful run confirms the four modes'
dispatch order and stored transitions. It does not confirm provider access,
model quality, file editing, a live dashboard, or a real multi-agent run.
