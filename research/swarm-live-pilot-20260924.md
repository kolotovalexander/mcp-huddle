# Huddle four-mode live pilot — 2026-09-24

## Result

The live server accepted a one-member council pilot and a real OpenCode worker
returned the requested answer. The pilot did not complete: the captured worker
trace contains no `swarm_pilot_round_done` call, so durable state still shows
no completed member and no final. Relay, team, and swarm were not dispatched.
This is live evidence, not a simulated test.

## Mode outcomes

| Mode | Real agent result | Agent called `swarm_pilot_round_done` | Correct final author published final | Outcome / blocker |
|---|---|---|---|---|
| Council | Yes. `OpenCode-cohere` posted result message 2 for request 1. | No. | No. Organizer `Codex` did not call `swarm_pilot_finish`; durable `final` is null. | Partial live proof: worker can wake and answer, but did not complete its pilot round. |
| Relay | Not dispatched. | No. | No. | Not live-proven. A valid relay requires the worker to call `swarm_pilot_round_done` and a member to claim reporter with `swarm_pilot_record`. |
| Team | Not dispatched. | No. | No. | Not live-proven. Workers must claim reporter with `swarm_pilot_record`, complete with `swarm_pilot_round_done`, then the reporter must call `swarm_pilot_finish`. |
| Swarm | Not dispatched. | No. | No. | Not live-proven; same required pilot actions as team. |

## Exact live evidence

- Disposable pilot room: `room_c9fe8851` (`live-pilot-probe-cohere-20260924`).
- Pilot operation: `swarm_pilot_create`, mode `council`, member `OpenCode-cohere`,
  `start=true`, `cwd=/private/tmp/huddle-swarm-luna`, `workspace_strategy=shared_only`.
- Goal: `Tiny no-file task: calculate 17 + 25 and answer with the number plus one brief sentence. Do not use tools, inspect, or change project files.`
  The phrase `Do not use tools` was ambiguous because completion itself requires
  Huddle tools; the next probe must say `Do not use shell or file tools` instead.
- Request message 1 was addressed to `OpenCode-cohere`.
- Result message 2 was posted by `OpenCode-cohere`, addressed to `Codex`, kind
  `result`: `42. The sum is straightforward addition without complex calculations.`
- Captured worker output:
  `/Users/kolotovalexander/.mcp-huddle/rooms/room_c9fe8851/agents/OpenCode-cohere.events.jsonl`
  It records the worker calling `huddle_messages_read`, `huddle_room_status`,
  `huddle_status_set`, and `huddle_message_post`, then exiting 0. It contains no
  pilot-control tool call. This is a call trace, not a complete inventory of
  tools offered to the worker.
- `swarm_pilot_status(room_c9fe8851)` after the worker exited: `dispatched` is
  `{"OpenCode-cohere":1}`, `done` is `{}`, `final` is `null`, and pilot phase is
  `working`. `messages_read` shows the result as message 2 replying to request 1.
- The active HTTP MCP server was confirmed as `mcp-huddle` 1.28.1; its
  `tools/list` includes the six `swarm_pilot_*` organizer tools. The spawned
  OpenCode worker's captured calls do not establish its available tool list.
- The selected registry profile is `OpenCode-cohere`, configured for
  `openrouter/cohere/north-mini-code:free`. The live Huddle wake proves this
  profile returned a real answer. A separate direct CLI provider probe stopped
  before contacting the provider because OpenCode could not open
  `/Users/kolotovalexander/.local/share/opencode/log/opencode.log`; a retry
  with `XDG_STATE_HOME` redirected hit the same hardcoded path. No further
  direct probe was made.
- Gemini was not selected: the inspected registry exposes `Antigravity` as a
  Google-model profile, but no profile literally named Gemini or Luna. No
  additional workers were dispatched.
- No project files were changed and no other rooms were touched.

## Smallest actionable fix

Inspect the child-facing Huddle tool inventory and determine whether it
includes `swarm_pilot_record`, `swarm_pilot_round_done`, and
`swarm_pilot_finish`. The live run establishes that the worker did not invoke
these operations; it does not by itself establish whether they were absent
from the offered tool inventory or the model failed to use them. If absent,
expose them in the child bridge, then rerun the council probe and the other
modes. If present, adjust the worker prompt or runner contract and rerun the
same bounded probe first.

## Verification boundary

This run proves one live worker wake, a valid result post, and a missing
completion transition. It does not prove council finalization or
relay/team/swarm behavior. The repo's mechanical pilot tests were not run here.
