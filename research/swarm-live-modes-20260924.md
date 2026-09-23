# Huddle live mode pilot — 2026-09-24

## Verdict

Against the live HTTP MCP server at http://127.0.0.1:8014/mcp, council, relay, and team reached a real final from the required author. Three swarm rooms did not finalize. The Cohere/Nvidia room was blocked by OpenCode database locks and a recovery response tied to the wrong request ID. A separate room with two other enabled free profiles stopped before results: Nvidia reported the same lock and OpenAI returned an UnknownError. The Antigravity/Nvidia room produced both arithmetic answers, but Antigravity did not call the pilot completion tools.

This is live evidence, not simulated tests. The server was not restarted. Pilot goals prohibited shell/file tools and project file changes; captured successful participant traces used Huddle MCP. No pre-existing room was closed or deleted.

## Server and profiles

Live MCP initialize returned mcp-huddle 1.28.1; tools/list exposed all six swarm_pilot_* tools. /api/auth returned required=false.

Enabled registry profiles:

- OpenCode-cohere — openrouter/cohere/north-mini-code:free
- OpenCode-nvidia — openrouter/nvidia/nemotron-3-super-120b-a12b:free
- OpenCode-openai — openrouter/openai/gpt-oss-20b:free
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

## Conclusion

Council, relay, and team have end-to-end live evidence. Swarm remains
unproven. The Cohere/Nvidia roster hit an OpenCode database lock and the
recovery result was tied to the corrective request instead of the pilot request.
A different two-OpenCode roster also failed before results with a database lock
and an OpenAI runner `UnknownError`. The Antigravity/Nvidia roster produced two
real arithmetic results and one completed round, but Antigravity did not call
the pilot control tools, so the room did not publish a final. The OpenAI
`err_8c7b5165` cause is unknown without the matching server log; do not infer it
from the reference alone.
