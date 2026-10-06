---
name: huddle
description: Use persistent Huddle rooms for council, relay, team or swarm collaboration, or deliver a message between sessions when no suitable native route exists. Use native tools for ordinary internal subagent coordination.
---

# Huddle

First discover the Huddle MCP tools and their current schemas. MCP is the connection through which your agent calls Huddle tools. If unavailable, use `huddle-install`; never assume an installed Skill proves connectivity.

## Choose a workflow

- Internal subagents: use your own harness's native collaboration tools.
- Existing session: use native delivery when it can reach that exact recipient. Otherwise use `message_targets` then `message_send`; a room is unnecessary. Declare `sender_harness` and only provide `native_unavailable_reason` when a native route is actually unavailable. A permission denial is never a reason to switch transports.
- Ordinary room: persistent shared discussion without a managed execution mode.
- Council (`council`): sequential opinions, each sees preceding discussion; the organizer owns the final decision.
- Relay (`relay`): ordered handoffs, each participant continues the previous result.
- Team (`team`): parallel, assigned responsibilities; coordinate results in the room.
- Swarm (`swarm`): participants distribute responsibilities, discuss and produce the final result; organizer coordinates and handles escalation.

Use `swarm_room_proposal` / `swarm_plan_preview` when available to inspect suitable enabled profiles, then `swarm_pilot_create` with the selected mode, goal, organizer and members. Inspect the actual schemas; optional planners are not permissions. Set `start=False` to prepare without spending model usage. Use unique member names and session identities, not just model names.

Read the room before acting. Use `message_post(kind="request")` to request work; a `comment` does not wake a worker. Address the recipient explicitly. Track ownership of responsibilities and announce newly discovered unowned work. Use `swarm_pilot_record`, `swarm_pilot_status`, `swarm_pilot_round_done` and `swarm_pilot_finish` according to their schemas. Rounds end by explicit completion, not message counts. Do not repeatedly poll or respond to every status event.

Agent-to-agent communication is English. Human-facing results use the user's language. Preserve original evidence, code, IDs and paths.

## Permissions and completion

Read-only is the default. Shared writes require an explicitly approved local Git workspace and supported enforced profiles. Use `shared_only` for a shared copy; `allow_subworktrees` allows isolated local copies. Never weaken harness guards or claim all harnesses enforce read-only. Child agents require the room's explicit child-agent policy.

Distinguish accepted/queued/started from delivered and answered. Inspect delivery receipts and persisted room results; a process starting does not prove delivery. Authentication, model availability and quota are separate from binary discovery. Huddle is local to one device; multi-device execution is not yet supported.
