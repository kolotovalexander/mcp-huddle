# Huddle live mode pilot — 2026-09-24

## Verdict

Against the live HTTP MCP server at http://127.0.0.1:8014/mcp, council, relay, team, and two post-restart swarms reached real finals from the required author. Other swarm combinations did not finalize. Two early OpenCode combinations hit database locks; a later Antigravity/Nvidia room got results but no completion from Antigravity. The 9router/Nvidia room was delayed by full-lifetime serialization, then completed after the 9router turn exited and Nvidia could run.

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
| 9router swarm, room_e01095e6 | 9router/Nvidia requests #1/#2; 9router result #3; operational stop request #4; delayed Nvidia result #5, system reporter reminder #6, 9router exit acknowledgement #7, system final request #8, Nvidia combined result #9 and final #10. | Both members in done; reporter/final author Nvidia; phase=completed. Completed 10m47s after room creation. No manual close was performed. |

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
- The 9router trace initially showed repeated Huddle polling while Nvidia had
  not started; full-lifetime serialization held the OpenCode slot. Organizer
  request #4 told 9router its result was durable and to end its CLI turn. The
  room later progressed without another organizer correction or any manual
  close.
- Exact late messages:
  - #5, OpenCode-nvidia result replying to #2: `23 + 19 = 42. Explanation:
    23 + 19 = 23 + (20 − 1) = 43 − 1 = 42.`
  - #6, system to all: `All members have completed the round, but no reporter
    is claimed. One member must claim the reporter responsibility with
    swarm_pilot_record(room_id, member, 'responsibility', 'reporter',
    '<description>').`
  - #7, OpenCode-9router result replying to #4: `Acknowledged. My result
    (23 + 19 = 42) and round_done are already durably recorded. I will stop
    polling and end this turn now, and wait for Huddle's separate final
    request to the reporter.`
  - #8, system to OpenCode-nvidia: `All members have completed the round. Read
    their results and publish the combined result with
    swarm_pilot_finish(room_id, member, result).`
  - #9, OpenCode-nvidia result replying to #6: `Combined result: 23 + 19 =
    42. Explanation: 23 + 19 = 23 + (20 - 1) = 43 - 1 = 42. Both agents
    independently verified this result.`
  - #10, OpenCode-nvidia final: `Combined result: 23 + 19 = 42. Explanation:
    23 + 19 = 23 + (20 - 1) = 43 - 1 = 42. Both agents independently
    verified this result.`
- Durable status now has both `OpenCode-9router` and `OpenCode-nvidia` in
  `done`; Nvidia claimed reporter and authored the final; `phase="completed"`.
  The room was created at Unix time 1790204725 and final #10 was recorded at
  1790205372: 647 seconds (10m47s) end to end. Both wake claims are null and
  `last_wake_rc=0`. This is delayed but complete live evidence. The room-close
  request had earlier been rejected by auto-review before execution; manual
  close was not performed, and the room completed on its own.

## Conclusion

All four modes have end-to-end live proof: council, relay, team, and swarm.
The post-restart Cohere/Nvidia room completed after one exact correction,
without a captured database lock. The 9router/Nvidia room also reached a real
final, but only after a 10m47s delay while the serialized 9router turn was
polling; after it exited, Nvidia completed the reporter and final steps. This
confirms completion but also shows that a waiting reporter can delay the next
serialized OpenCode member. The earlier database-lock errors and the
Antigravity completion gap remain as recorded above. The OpenAI
`err_8c7b5165` cause remains unknown without the matching server log.

## Post-restart serialized OpenCode lifecycle check — room_2d23acbe

One new disposable live swarm was created after the Huddle `:8014` restart,
using `OpenCode-cohere` (`openrouter/cohere/north-mini-code:free`) and
`OpenCode-nvidia` (`openrouter/nvidia/nemotron-3-super-120b-a12b:free`). The
toy goal was `18 + 24`; both participants were told to use Huddle MCP only and
not to use shell or file tools. No corrective request was needed.

Room and message evidence (timestamps below are Unix seconds; parenthetical
wall times are Indochina Time, UTC+7):

- Room `room_2d23acbe` created at `1790206531` (2026-09-24 06:35:31 ICT); both
  initial requests (#1 and #2) were dispatched at that timestamp.
- Cohere result #3 replied to request #1 at `1790206564` (06:36:04 ICT), then
  its durable `round_done` entry was recorded at `1790206570` (06:36:10 ICT).
- Nvidia result #4 replied to request #2 at `1790206601` (06:36:41 ICT), then
  its durable `round_done` entry was recorded at `1790206604` (06:36:44 ICT).
- System final request #5 went to Cohere at `1790206604`; Cohere published
  combined result #6 at `1790206635` (06:37:15 ICT) and final #7 at
  `1790206639` (06:37:19 ICT), 108 seconds after room creation.
- Final durable state: `phase="completed"`; both members are in `done`;
  `responsibilities.reporter.member="OpenCode-cohere"` and
  `final.member="OpenCode-cohere"`. Both current `wake_claim_id` values are
  null. Result #3 was `18 + 24 = 42`; #4 independently verified `18 + 24 =
  42`. Final #7: `All members completed the round. Both OpenCode-cohere
  (calculated) and OpenCode-nvidia (verified) independently confirmed that
  18 + 24 = 42. Combined result published.`

Lifecycle evidence and limits:

- Nvidia's recorded child PID was `54141`, launched/claimed at
  `1790206531`, with `last_wake_exit_at=1790206607` and `last_wake_rc=0`:
  three seconds after its `round_done` timestamp. Its `wake_fail_count=0`.
- Cohere's first child PID was `54138`. After its `round_done`, a live
  metadata snapshot observed the wake claim released and `last_wake_rc=-15`.
  That first exit timestamp was overwritten by Cohere's later reporter wake,
  so the exact delay from the first `round_done` to that exit cannot be
  recovered from current metadata. The later reporter PID `54793` had
  `last_wake_exit_at=1790206641`, two seconds after final #7, and
  `last_wake_rc=-15`.
- At the final snapshot Cohere had `wake_claim_id=null` and
  `wake_fail_count=2`, despite its successful result, `round_done`, and
  `finish`; Nvidia had no failures. This is a telemetry defect: successful
  pilot turns stopped by the lifecycle path are recorded as nonzero exits and
  counted as wake failures. No source change was made in this pilot.
- There was no observed 352-second stall: Nvidia's result arrived 70 seconds
  after room creation and its round completed 73 seconds after creation.
  However, the persisted start time is the wake claim time, not a timestamp
  for when the serialized child actually acquired its slot. The first Cohere
  exit timestamp was overwritten. Therefore this room does not prove the
  exact `~1.5s` child-exit target or the precise inter-child start delay.

This is a real completed Swarm run, not a simulated test. It confirms that
both inexpensive OpenCode profiles returned results, completed their round,
and produced the reporter final. It did not fully confirm the requested
process-lifecycle timing; use a per-wake exit history and a terminal outcome
that distinguishes an intentional stop from an execution failure to verify
that target reliably.

## Post-fix stop telemetry check — room_854fa51f

After the main checkout reported commit `c61be97cc2c6fce5fa0a901e32b782df9b9b9e5e`,
the live HTTP MCP endpoint returned `mcp-huddle` 1.28.1 and all six
`swarm_pilot_*` tools. One disposable Swarm used the enabled free profiles
`OpenCode-cohere` (`openrouter/cohere/north-mini-code:free`) and
`OpenCode-nvidia` (`openrouter/nvidia/nemotron-3-super-120b-a12b:free`). The
goal was `27 + 15`; participants were told to use Huddle pilot tools only and
make no shell, file, or system actions. No organizer correction was sent.

Timeline (Unix timestamps; wall times are Indochina Time, UTC+7):

- `room_854fa51f` and requests #1/#2 were created at `1790207392`
  (2026-09-24 06:49:52).
- Cohere result #3 replied to #1 at `1790207414` (06:50:14); its durable
  `round_done` timestamp is `1790207418` (06:50:18).
- Cohere PID `60738` exited at `1790207419` (06:50:19), one second after
  `round_done`, with `last_wake_rc=-15`, `wake_fail_count=0`, and its wake
  claim cleared. This is the requested normal SIGTERM stop telemetry.
- Nvidia result #4 replied to #2 at `1790207450` (06:50:50); its durable
  `round_done` timestamp is `1790207452` (06:50:52). PID `60741` exited at
  `1790207464` (06:51:04) with `last_wake_rc=0` and `wake_fail_count=0`.
- Standard system final request #5 went to Cohere at `1790207452`; Cohere
  published combined result #6 and final #7 (`42`) at `1790207487`
  (06:51:27). Its reporter PID `61297` exited at `1790207488` (06:51:28),
  with `last_wake_rc=-15` and `wake_fail_count=0`.
- Final state: `phase="completed"`; both agents are in `done`;
  `final.member="OpenCode-cohere"`; both `wake_claim_id` values are null;
  both `wake_fail_count` values are zero. The room completed 95 seconds after
  creation.

No system error message appeared in the room; #5 is the ordinary pilot final
request. Nvidia's event log does contain two participant-side validation
errors: it first omitted the required `member` argument to
`swarm_pilot_record`, then tried `status_set(phase="completed")`, which is
not an allowed phase. It retried its responsibility call correctly, posted
result #4, and completed `round_done`; neither error produced a system room
message or blocked the final. No files or system actions were performed by
the pilot participants.

Conclusion: the requested telemetry behavior is live-proven in this room:
the normal stop after a completed member turn recorded `rc=-15` with
`wake_fail_count=0`, both wake claims were cleared, both members completed,
and the reporter published a final. This is one run, using these two free
OpenCode profiles.
