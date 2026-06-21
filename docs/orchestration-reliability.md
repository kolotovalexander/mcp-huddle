# Orchestration reliability — changes & follow-ups

From a multi-agent orchestrator's session report. Split into what huddle fixed
(shipped) and what is harness/cmux-level (research-backed guidance, not huddle code).

## Shipped in huddle

| Pain | Change | Tool surface |
|------|--------|-------------|
| Whole-room reads overflow (60–130 KB) | per-body **head+tail** truncation (`max_chars`, default 2000) + `until_id` window + `kind` filter | `messages_read(until_id, max_chars, kind, round)` |
| No round structure; agents don't see boundaries | `current_round` on meta, every message stamped, visible `━━━ Round N ━━━` divider | `room_round_advance(room_id, owner, label)`, `messages_read(round=N|-1)`, `room_summarize(round=…)` |
| Mechanical digest too thin | digest now reports **latest position per agent** + still-open requests, round-scoped | `room_summarize(round=…)` |
| resume → new PID → zombie-watchdog closes live room | grace window (open/closing rooms reaped only after silence) + explicit re-stamp | `room_reclaim(room_id, owner, owner_pid)` |
| MiMo advisor dead (empty headless output) | default OFF, opt-in | `MCP_HUDDLE_MIMO_ENABLED=1` |

**Truncation = head+tail (60/40), not head-only.** "Lost in the middle" research +
Hermes/Claude-Code production: conclusions live in the tail, so the middle is the
safe place to drop. Validated live (round-1 capped read kept each agent's closing
sentence). Sources: arxiv 2305.14325 (multi-agent debate), mem0.ai Hermes/Claude
compression.

**Rounds = orchestrator-driven re-dispatch, not live wakeup.** Verified with a
3-agent × 2-round Sonnet debate against this code: advance → dispatch fresh workers
seeded with the prior round (read `round=N-1`) → collect their `kind=result` →
advance. Matches AutoGen GroupChatManager / LangGraph supervisor. The debate
converged (incl. an agent flipping its stance) — round-scoped reads gave correct
per-round context.

> Design note: we deliberately did **not** add server-side off-round post rejection
> (keeps the bus a dumb append-only log — sinks-not-pipes). The test debate itself
> converged on this: soft-annotate off-round, leave hard enforcement to the
> orchestrator's prompt seeding. Revisit only if a concrete need appears.

## Harness / cmux level — not huddle code (use these patterns)

These were misattributed to huddle; the real fix is in the Claude Code harness or
cmux. Captured so the findings aren't lost.

### #1 Reliable final result from background agents
- The Task tool's free-text return is non-deterministic ("idle_notification with no
  content"). **In-band fix (do this):** instruct every worker to
  `message_post(kind="result")` to the room **before returning** — the deliverable
  is then durable in the bus; read it via `messages_read(kind="result")`, never the
  transcript. (This is why we added the `kind` filter.)
- **Out-of-band safety net (follow-up, not shipped):** a `SubagentStop` hook gets
  `last_assistant_message` in its input JSON → write it into the room as
  `kind=result`. Deterministic, no transcript parsing. huddle already ships example
  hooks (`--install-hooks`); a `subagent-result-capture.sh` could be added there.
- Agent SDK path: pass an `output_format` JSON schema and read `structured_output`
  from the result message (`subtype=error_max_structured_output_retries` = typed
  fail). Source: code.claude.com/docs/en/agent-sdk/structured-outputs.

### #2 Waking background agents
- huddle wakes only agents IT spawns. Background Agent-tool workers are harness-owned
  — don't fight it. Mirror AutoGen/LangGraph: re-dispatch a fresh one-shot worker per
  round, seeded from room state. (Same model huddle already uses for CLI agents.)

### #7 Idle-notification spam
- Claude Code `Notification` (`idle_prompt`) has **no debounce/coalesce** by design,
  and `BackgroundTasksIdle` was declined (issue #45781). Fix in a `Notification`
  hook: edge-triggered state file — emit "idle" once on the rising edge after a
  confirmation window (8–15 s), drop repeats while already idle. Sources:
  code.claude.com/docs/en/hooks, gh anthropics/claude-code#45781.

### #6 cmux
- **Scrollback read** is DEBUG-only in cmux (`read-screen`/`surface.read_text`,
  issue manaflow-ai/cmux#152) — no prod path. Route worker output through the huddle
  bus instead of scraping the terminal. tmux fallback: `capture-pane -p`.
- **`surface.close` returns a new ref** because closing the last surface triggers
  `createReplacementTerminalPanel()`. Close the **Workspace** (`workspace.close`),
  or keep ≥1 other surface; re-query `surface.list` to verify.

### "Clean" subagent (no CLAUDE.md / memory)
- `CLAUDE_DISABLE_HOOKS=1` never touched memory. Use **`claude --bare -p`** (drops
  CLAUDE.md + auto-memory + skills/plugins/hooks/MCP) or `--safe-mode`. Surgical:
  `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` + `claudeMdExcludes`. Managed-policy CLAUDE.md
  survives all of these. Source: code.claude.com/docs/en/cli-reference, /memory.
