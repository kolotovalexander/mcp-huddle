# Huddle live mode pilot — 2026-09-24

## Verdict

Against the live HTTP MCP server at http://127.0.0.1:8014/mcp, council, relay, team, and one post-restart Cohere/Nvidia swarm reached a real final from the required author. Other swarm combinations did not finalize. Two early OpenCode combinations hit database locks; a later Antigravity/Nvidia room got results but no completion from Antigravity. The final 9router/Nvidia room stalled because the 9router reporter kept polling while full-lifetime serialization held the OpenCode slot.

This is live evidence, not simulated tests. The project owner restarted the server before the final recheck; I did not restart it. Pilot goals prohibited shell/file tools and project file changes. No pre-existing room was closed or deleted.

## Server and profiles

Live MCP initialize returned mcp-huddle 1.28.1; tools/list exposed all six swarm_pilot_* tools. /api/auth returned required=false.

Enabled registry profiles:

- OpenCode-cohere — openrouter/cohere/north-mini-code:free
- OpenCode-nvidia — openrouter/nvidia/nemotron-3-super-120b-a12b:free
- OpenCode-openai — openrouter/openai/gpt-oss-20b:free
- OpenCode-9router — 9router/coding-reliable
- Antigravity — /opt/homebrew/bin/agy -p {brief}

## Mode outcomes

| Mode / room | Members and live messages | Durable completion |
|---|---|---|
| Council, room_26e717c4 | Cohere request #1, result #2. System final request #3; organizer Codex final #4. | Cohere in done; final.member=Codex; phase=completed. |
| Relay, room_75641eb0 | Cohere request #1, valid result #4 replying to #1; Nvidia request #5, result #6 replying to #5. System final request #9; Cohere final #10. | Both members in done; reporter Cohere; final.member=OpenCode-cohere; phase=completed. |
| Team, room_b6af442f | Cohere/Nvidia requests #1/#2; results #3/#4. System final request #5; Cohere final #6. | Both in done; reporter and final author Cohere; phase=completed. |
| Swarm, room_8807f59b | Cohere/Nvidia requests #1/#2; Nvidia result #4 replies to #2. Cohere startup error comment #3. One recovery request #5 yielded Cohere result #6 replying to #5, not pilot request #1. | Only Nvidia in done; reporter Nvidia; final=null; phase=working. |
| Alternate swarm, room_b3f09570 | Nvidia/OpenAI requests #1/#2; system error comments #3/#4. | No done entries, reporter, or final; final=null; phase=working. |
| Antigravity swarm, room_f63be903 | Antigravity/Nvidia requests #1/#2; results #4/#3; one exact follow-up request #6. | Only Nvidia in done and reporter; final=null; phase=working. |
| Post-restart swarm, room_a987fe30 | Cohere/Nvidia requests #1/#2; corrected Cohere result #6; Nvidia result #5; system final request #7; Cohere final #8. | Both members in done; reporter/final author Cohere; phase=completed. |
| 9router swarm, room_e01095e6 | 9router/Nvidia requests #1/#2; 9router result #3; operational stop request #4. Nvidia produced no event/result. | Only 9router in done; no reporter responsibility or final; phase=working. Room-close action was rejected by auto-review; room remains open. |

## Evidence and blockers

### Council — room_26e717c4

Goal: calculate 17 + 25, answer 42 with one short sentence; no shell/file tools. Result #2 by OpenCode-cohere: “42. The sum of seventeen and twenty-five is exactly forty-two.” Done contains Cohere. Final #4 by organizer Codex: “Совет завершён: 17 + 25 = 42. Участник OpenCode-cohere опубликовал ответ и осознанно завершил раунд; организатор подтвердил итог.” Final phase is completed.

### Relay — room_75641eb0

Goal: both members calculate 17 + 25, choose one reporter, post results, complete rounds, and have the reporter publish the final. First Cohere outputs #2 and #3 lacked reply_to; round_done failed with “member must post a result for their request first”. The worker also tried record kind=reporter and then stored result under the wrong responsibility key. One corrective request #7 gave exact tool arguments. Cohere result #4 then replied to pilot request #1 and completed. Nvidia result #6 replied to request #5 and completed. Nvidia stored key role instead of reporter and tried finish prematurely; the system requested a reporter claim in #8. Cohere claimed reporter, received final request #9, and published final #10. Both members are in done; reporter/final author is OpenCode-cohere; phase is completed. Final text: “42. The sum of 17 and 25 is 42, calculated using basic arithmetic addition.”

### Team — room_b6af442f

Goal: calculate 19 + 23 with both members and publish one reporter final. Results #3 by Cohere and #4 by Nvidia correctly reply to requests #1 and #2; both called round_done. Cohere claimed reporter. An early finish attempt was rejected before final request #5. Cohere then called finish and published final #6. Both members are in done; final author is OpenCode-cohere; phase is completed. Final text: “42. Calculating 19 + 23 equals 42, which is the answer to the ultimate question of life, the universe, and everything.”

### Swarm — room_8807f59b

Goal: calculate 29 + 13; no shell/file tools. System comment #3 records OpenCode-cohere exiting with exit 1 and “Unexpected error database is locked”. Nvidia posted result #4 (“42 (29 + 13 = 42)”) replying to request #2, claimed reporter, and completed. One recovery request #5 asked Cohere to satisfy original pilot request #1. Cohere's generated result #6 replied to corrective request #5 instead; round_done rejected it because no result replied to request #1. Its event log again contains “Unexpected error database is locked”. Status has only Nvidia in done, reporter Nvidia, final=null, phase=working. No more retry was made.

### Alternate swarm — room_b3f09570

The enabled registry showed OpenCode-nvidia on openrouter/nvidia/nemotron-3-super-120b-a12b:free and OpenCode-openai on openrouter/openai/gpt-oss-20b:free. System comment #3 says Nvidia exited exit 1 with “Unexpected error database is locked”. Comment #4 says OpenAI exited exit 1 with UnknownError ref err_8c7b5165 and message “Unexpected server error. Check server logs for details.” No worker results, done entries, reporter, or final exist; phase remains working. No retry was made.

### Swarm with Antigravity and one OpenCode profile — room_f63be903

- Goal: calculate 37 + 5; no shell/file tools.
- Requests #1 and #2 went to Antigravity and Nvidia. Nvidia result #3 replies
  to #2. Antigravity result #4 replies to #1 and contains `37 + 5 = 42`.
- Nvidia proposed itself as reporter in message #5 and subsequently claimed the
  reporter responsibility. Nvidia is in `done`.
- The Antigravity event log contains only assistant text saying the result was
  sent; it contains no actual Huddle tool call for `swarm_pilot_record` or
  `swarm_pilot_round_done`. One organizer-approved corrective request #6 gave
  exact tool calls. After it, the event log did not record another Huddle tool
  call or a completion.
- Durable state: only Nvidia is in `done`; `responsibilities.reporter.member`
  is `OpenCode-nvidia`; `final=null`; `phase="working"`. No final request was
  sent because Antigravity had not completed. No further retry was made.

### Post-restart serialized OpenCode swarm — room_a987fe30

- The project owner restarted Huddle 8014 with OpenCode lifetime serialization
  and the pilot recovery change. MCP initialize returned 200 and the live tool
  catalog contained all six pilot tools.
- Goal: calculate 17 + 25, no shell/file tools; use exact request reply IDs,
  record one reporter, mark both members done, and have that reporter finish.
- Cohere result #3 was `17 + 25 = 42` but had no `reply_to`. Its first
  `swarm_pilot_round_done` call was rejected with `member must post a result
  for the pilot request or an organizer's direct recovery request first`.
  Cohere correctly recorded itself as reporter.
- Nvidia result #5 replied to request #2 and Nvidia called
  `swarm_pilot_round_done`; durable `done` contains Nvidia.
- One organizer corrective request #4 told Cohere to post a result replying
  to request #1 with a fresh idempotency key, then call `round_done`. Its first
  reply after #4 said to wait for Nvidia, despite Nvidia already being done.
  Later, Cohere did use the requested arguments: result #6 replies to #1, then
  it completed its round.
- The system sent final request #7 to Cohere; Cohere called `finish` and
  authored final #8 (`17 + 25 = 42`). Durable state now has both members in
  `done`, reporter/final author `OpenCode-cohere`, and `phase="completed"`.
- No `database is locked` error appears in this room's captured messages or
  participant event logs. Both OpenCode workers returned answers and the pilot
  completed after Cohere's delayed, one-time corrective action.

### 9router/Nvidia serialized swarm — room_e01095e6

- Enabled registry profiles were `OpenCode-9router` using
  `9router/coding-reliable` and `OpenCode-nvidia` using
  `openrouter/nvidia/nemotron-3-super-120b-a12b:free`.
- Requests #1 and #2 were dispatched. 9router result #3 replied to #1 with
  `23 + 19 = 42`; its trace shows a successful `swarm_pilot_round_done`.
  It wrote a decision under `decisions.reporter`, not a reporter
  responsibility under `responsibilities.reporter`.
- Nvidia produced no result or event log. The 9router event trace shows it
  polling Huddle while waiting for Nvidia; the lifetime lock therefore held
  the OpenCode slot. One organizer operational request #4 told 9router to
  stop polling and end its CLI turn. Its log continued with `sleep 10`,
  `sleep 20`, `sleep 30`, and Huddle polls. No further request was sent.
- Durable state at the last snapshot: only 9router in `done`, no reporter
  responsibility, `final=null`, `phase="working"`.
- The organizer asked to close only this disposable room with Huddle's
  standard `POST /api/room_close` endpoint and owner `Codex`. Auto-review
  rejected the action before execution: `Closing room room_e01095e6 will stop
  associated Huddle processes and change shared service state; explicit user
  consent for this specific closure is absent.` No alternate route was used;
  this room remains open.

## Conclusion

All four modes now have end-to-end live proof: council, relay, team, and the
post-restart Cohere/Nvidia swarm. The earlier Cohere/Nvidia room and a separate
two-OpenCode room showed database locks before serialization. The Antigravity
room posted results but did not complete. The post-restart Cohere/Nvidia room
completed after one exact correction, without a captured database lock.

The last 9router/Nvidia room exposed a different failure mode. 9router posted
its result and called `round_done`, then kept polling for Nvidia while its
full-lifetime lock prevented Nvidia from starting. After one organizer
operational message, its event log continued to show sleep commands and room
polls. Nvidia had no event log or result. The final stayed absent. This prompt
must end a completed member's CLI turn so its serialized slot is released
before waiting for the other member.

The owner requested closing only `room_e01095e6` via Huddle's standard close
route, but auto-review rejected the call because stopping its child process
changes shared service state without direct user consent. No close was retried
through another route; the room remains open. The OpenAI `err_8c7b5165` cause
also remains unknown without the matching server log.
