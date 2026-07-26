# Agent Lifecycle Status Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make Huddle expose trustworthy agent lifecycle state so participants wait for active generation/research and distinguish completion from failure.

**Architecture:** Keep the existing `status` string and wake lease compatible, while adding a persisted `phase` and task metadata to `status.json`. The server owns process-derived terminal states and exposes one `room_status` MCP read tool plus one `status_set` MCP self-report tool. Existing prompts are extended through the shared protocol block so cold spawns, wakes, custom briefs, and fresh follow-ups share the same lifecycle instructions.

**Tech Stack:** Python 3.11 standard library, file-locked JSON status store, FastMCP tools, pytest.

## Global Constraints

- Preserve `/Users/kolotovalexander/Apps Projects/AgentSync/mcp/huddle/src/mcp_huddle/bus.py` lock ordering and never hold two bus locks at once.
- Preserve existing `status` values and `get_status()` string output for dashboard/internal callers.
- Do not add runtime dependencies or a background polling loop beyond the existing watchdog.
- Keep spawned agents read-only by default and route room communication through Huddle MCP tools.
- Verify with `PYTHONPATH=src python3 -m pytest tests/ -q` and `node --check src/mcp_huddle/static/dashboard.js`.

---

### Task 1: Persist lifecycle phases and expose a room status snapshot

**Files:**
- Modify: `src/mcp_huddle/bus.py`
- Modify: `src/mcp_huddle/server.py`
- Test: `tests/test_bus_reliability.py`
- Test: `tests/test_phase1_2.py`

**Interfaces:**
- `bus.set_status(..., phase="working", task_id=..., detail=..., source=...)` remains backward-compatible with the existing five positional arguments.
- `bus.get_status_details(room_id)` returns normalized records with `status`, `phase`, `task_id`, `updated_at`, `expires_at`, and optional `detail`/`source`.
- MCP `status_set(room_id, agent, phase, task_id="", detail="", expires_in_sec=0, session_id="")` accepts only agent-reported `thinking`, `working`, and `responding` phases.
- MCP `room_status(room_id)` returns participants, per-agent process/wake health, pending requests with `waiting_for`, `wait_recommended`, and `all_terminal`.

- [ ] **Step 1: Write failing tests** for status phase persistence/normalization, `room_status` waiting on a live agent and pending request, and MCP exposure of `status_set`/`room_status`.
- [ ] **Step 2: Run the focused tests** and confirm they fail because the new phase/details/tool interfaces do not exist.
- [ ] **Step 3: Implement the locked status record extension** in `bus.py`, preserving old string reads and expiry behavior.
- [ ] **Step 4: Implement `room_status` and validated `status_set`** in `server.py`, deriving `waiting_for` from unanswered request messages and process health from existing wake metadata.
- [ ] **Step 5: Run the focused tests** and confirm they pass.

### Task 2: Drive lifecycle state from spawn, reply, and failure paths

**Files:**
- Modify: `src/mcp_huddle/server.py`
- Test: `tests/test_phase1_2.py`

**Interfaces:**
- Initial and wake spawns stamp `starting`/`working` with the relevant task ID.
- Reply posts stamp `completed` automatically.
- Existing rate-limit, spawn-failure, no-reply, dead-wake, and stuck-wake paths stamp `rate_limited`, `unavailable`, or `stuck` with a concise reason.

- [ ] **Step 1: Write failing tests** for reply-to-completed, initial/wake active phases, and terminal failure phases.
- [ ] **Step 2: Run those tests** and confirm the current implementation leaves status `online`/`busy` without a lifecycle phase.
- [ ] **Step 3: Add the smallest server lifecycle helpers** and call them at spawn, reply, rate-limit, no-reply, dead-wake, and stuck-wake boundaries.
- [ ] **Step 4: Run the focused lifecycle tests** and confirm they pass without changing wake deduplication.

### Task 3: Teach every spawned agent the wait/report protocol and update docs

**Files:**
- Modify: `src/mcp_huddle/server.py`
- Modify: `src/mcp_huddle/openai_compatible_runner.py`
- Modify: `src/mcp_huddle/mimo_runner.py`
- Modify: `README.md`
- Modify: `docs/orchestration-reliability.md`
- Test: `tests/test_phase1_2.py`

**Interfaces:**
- Cold-spawn, custom-brief, Codex wake, registry wake, and fresh follow-up prompts mention `status_set`, `room_status`, process-vs-completion semantics, and exact room delivery rules.
- Runner prompts explain that the runner posts the result and server lifecycle state is authoritative; they do not claim MCP tool access.

- [ ] **Step 1: Write failing prompt tests** asserting the lifecycle/wait rules appear in each prompt family.
- [ ] **Step 2: Run the prompt tests** and confirm they fail on the current prompts.
- [ ] **Step 3: Add one shared lifecycle protocol block** and include it in every CLI prompt; update runner prompts and user-facing docs.
- [ ] **Step 4: Run prompt tests, the full Python suite, and JS syntax verification.**

