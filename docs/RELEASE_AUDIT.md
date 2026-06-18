# mcp-huddle — аудит готовности к публичному релизу

_Многоагентный аудит: 74 агента, 7 измерений (портативность, безопасность, стабильность, UX/DX, упаковка, документация, упрощение), каждая находка прошла состязательную верификацию._

**Итого подтверждено:** 62 находок — 1 блокер, 11 важных, 34 средних, 16 мелких.


## Краткий вердикт

Публиковать **пока нельзя** из-за 1 блокера (падение установки на Python 3.10) и нескольких проблем портативности/безопасности. Объём работы небольшой: блокер чинится за минуты, вся группа «важное» — мелкие/средние правки. После закрытия блокера + важного проект готов к публикации; средние/мелкие — итеративно после релиза.


---

## 🔴 БЛОКЕРЫ — без этого публиковать нельзя  (1)


### 1. Python version incompatibility: NotRequired requires 3.11+
- **Файл:** `pyproject.toml:11 and src/mcp_huddle/spawn.py:42`  ·  **трудозатраты:** trivial  ·  _Packaging & publishing readiness_
- **Проблема:** pyproject.toml declares `requires-python = ">=3.10"` and lists Python 3.10 in classifiers (line 17), but spawn.py uses `NotRequired` from typing (line 42), which was only added in Python 3.11. This will cause import errors when installed on Python 3.10.
- **Что делать:** Either: (1) Change `requires-python = ">=3.11"` in pyproject.toml and remove 'Programming Language :: Python :: 3.10' from classifiers, OR (2) Use `typing_extensions.NotRequired` for Python 3.10 compatibility with a conditional import. Option 1 is simpler since the code already uses `from __future__ import annotations` elsewhere.
- **Чем грозит:** A user installing on Python 3.10 (still supported and widely used) will get immediate ImportError on package import, making the package unusable.

---

## 🟠 ВАЖНОЕ — починить до публикации  (11)


### 1. Hardcoded macOS-specific temporary directory path
- **Файл:** `src/mcp_huddle/mimo_runner.py:33`  ·  **трудозатраты:** small  ·  _Portability & hardcoded local assumptions_
- **Проблема:** Line 33 hardcodes `/private/tmp/mimo-runner-home` which is macOS-specific. Linux systems do not have `/private/tmp` — they use `/tmp`. This path is created and used as a working directory for MiMo invocations (line 67, 78). A public user on Linux trying to use MiMo will silently fail or encounter permission errors when the code tries to mkdir and chdir into a nonexistent parent (`/private` does not exist on Linux).
- **Что делать:** Replace `/private/tmp/mimo-runner-home` with a platform-agnostic temporary directory using `tempfile.mkdtemp()` or `tempfile.TemporaryDirectory()`. Store the path in a module variable lazily initialized once per process, or pass it as a parameter through the call stack.
- **Чем грозит:** Users attempting to run mcp-huddle on Linux with MiMo enabled will encounter silent failures or permission errors when MiMo tries to spawn, preventing the MiMo agent from participating in huddles.

### 2. Codex-specific thread resume logic assumes agent name is literally 'Codex'
- **Файл:** `src/mcp_huddle/server.py:304, 816, 1115`  ·  **трудозатраты:** medium  ·  _Portability & hardcoded local assumptions_
- **Проблема:** The code contains hardcoded checks `if agent_name == "Codex"` in three places (lines 304, 816, 1115) to enable thread-resume behavior unique to Codex. This means if a user defines a custom registry with a different name for the Codex slot (e.g., `"name": "CodexReviewer"`), the thread-resume optimization will silently disable. Similarly, if the Codex binary is unavailable and the user tries to manually invite `"Codex"` to a room without it being in the enabled registry, they'll get an error that assumes the agent has a thread_id when it doesn't (line 308). The agent name is treated as a unique identifier, but there's no validation or documentation that agent names in the registry must match these hardcoded strings for certain features to work.
- **Что делать:** Add a `thread_resumable` boolean field to `SpawnSpec` to explicitly mark which agents support thread resume (default False), and check this flag instead of matching agent name. Or, if thread resume must remain Codex-only, add explicit validation in `room_create` and `respond_via_agent` that warns or errors if the user tries to customize the Codex agent's name. Document in spawn.py that the Codex registry entry name must be exactly `"Codex"` for thread-resume to work.
- **Чем грозит:** Users who customize the default registry (e.g., to rename agents or swap implementations) will silently lose Codex thread-resume optimization without understanding why. This degrades performance (each turn spawns a fresh Codex process instead of resuming a stored thread). Low severity because stdio-mode users are unlikely to customize the registry, but it's a footgun for registry customization.

### 3. Unvalidated HTTP API endpoints allow untrusted callers to close/delete any room
- **Файл:** `src/mcp_huddle/server.py:561-614`  ·  **трудозатраты:** medium  ·  _Authorization_
- **Проблема:** The HTTP POST endpoints `/api/room_close`, `/api/room_delete`, `/api/rooms_close_all`, and `/api/rooms_nuke` accept room_id and owner from the request body without authenticating the caller. The only validation is that the owner parameter matches the room's owner in meta.json (line 565: `bus.close_room(data["room_id"], data["owner"])`). An attacker with HTTP access to localhost can: (1) read all room IDs from `/api/rooms` (line 518, no auth), (2) close/delete any room by guessing the owner name (typically canonical like "Claude", "Codex", "Antigravity"), (3) call `/api/rooms_nuke` to wipe all rooms without any parameters. The endpoints `/api/rooms_close_all`, `/api/rooms_delete_closed`, and `/api/rooms_nuke` take zero authentication parameters.
- **Что делать:** Implement one of: (1) CSRF/session tokens for POST endpoints (issue a token on GET /dashboard, validate on destructive POSTs), (2) require a Authorization header with a token from MCP_HUDDLE_API_TOKEN env var for destructive operations, or (3) restrict destructive endpoints to stdio-mode only (no HTTP routes). At minimum, add warnings in README about localhost-access implications and recommend running the HTTP server only in isolated environments or with external auth (e.g., reverse proxy with basic auth). For a public release, option (1) or (2) is mandatory.
- **Чем грозит:** Any local user with HTTP access can permanently delete all rooms and conversation history without permission. On a shared system, a malicious or careless user can disrupt the service for others.

### 4. No rate limiting on MCP tool calls; agents can spam message_post indefinitely
- **Файл:** `src/mcp_huddle/server.py:215-246 (message_post tool); bus.py:213-301 (post_message); bus.py:19-21 (CIRCUIT_BREAKER constants)`  ·  **трудозатраты:** medium  ·  _Denial of Service_
- **Проблема:** While there is a circuit-breaker check (`_check_circuit_breaker`) that blocks >5 consecutive messages from the same agent without a new request (anti-loop rule), there is no global rate limit on the total number of message_post calls per second/minute. An attacker with MCP tool access can call message_post in a tight loop with different message IDs to fill up messages.jsonl, exhausting disk space and degrading performance. The circuit breaker only checks the last 10 messages in a room; it does not prevent an agent from posting 5 result/comment messages, then pausing for 1 second (clearing the circuit breaker window), then posting 5 more. A room can accumulate thousands of messages with no limit.
- **Что делать:** (1) Add a per-room per-agent rate limit: reject message_post if the agent has posted >N messages in the last T seconds (e.g., 10 messages per 60 seconds). (2) Add a global rate limit across all rooms: reject if the server sees >M message_post calls per second (e.g., 100/sec across all rooms). (3) Add optional retention/disk quota: automatically delete rooms older than RETENTION_DAYS (already implemented) and/or set a max total size for ~/.mcp-huddle/rooms/. The current code has RETENTION_DAYS env var (line 105) but no per-room size limit. Implement as: if a single messages.jsonl exceeds MAX_ROOM_SIZE_MB (e.g., 1000MB), archive/delete the oldest messages or close the room.
- **Чем грозит:** A runaway agent or attacker with MCP access can cause disk exhaustion, making the service unavailable to other rooms. On a shared system, one user's misbehaving agent can degrade service for others.

### 5. spawned_pids list clobbered on concurrent room updates
- **Файл:** `src/mcp_huddle/server.py:826-830`  ·  **трудозатраты:** small  ·  _Concurrency & State Corruption_
- **Проблема:** In _spawn_agents(), the code writes spawned_pids directly via _update_meta_locked(). However, if a concurrent wake-thread modifies agent_meta (e.g., recording last_wake_pid), the update_fn at line 826 unconditionally overwrites both spawned_pids and agent_meta. The lambda `_save_spawn_meta` reads the current meta INSIDE the lock, but then ignores concurrent agent_meta updates that arrived between the RMW read and write. This causes loss of wake state (last_wake_pid, wake_id, thread_id) that a concurrent _merge_agent_meta wrote.
- **Что делать:** Preserve existing agent_meta entries when updating spawn metadata: `m['agent_meta'].update(agent_meta)` instead of `m['agent_meta'] = agent_meta`. Similarly, append to spawned_pids rather than replacing it: `m['spawned_pids'].extend([p for p in pids if p not in m.get('spawned_pids', [])])` or use a set-based merge to avoid duplicates.
- **Чем грозит:** When a room auto-spawns agents and a concurrent request wakes an agent mid-spawn, the wake state is silently lost. Subsequent wake attempts see stale/missing wake_id and cannot drain the queue properly, leaving requests unanswered and the room stuck.

### 6. Missing validation of environment variable integer conversions
- **Файл:** `src/mcp_huddle/server.py:101, 105, 106, 110`  ·  **трудозатраты:** trivial  ·  _Stability & Error Handling_
- **Проблема:** Server startup reads environment variables and converts to int/float directly: `int(os.environ.get(..., '600'))`, `float(os.environ.get(..., '7'))`. If a user sets an invalid value (e.g., `IDLE_TIMEOUT_SECS=abc`), the int() conversion raises ValueError and crashes the entire server during import. No try-except guards these conversions.
- **Что делать:** Wrap conversions in try-except and provide sensible defaults or clear error messages. Example: `try: IDLE_TIMEOUT_SECS = int(os.environ.get('IDLE_TIMEOUT_SECS', '600')) except ValueError: ... raise ConfigError(f'IDLE_TIMEOUT_SECS must be an integer, got {os.environ.get(...)}')`
- **Чем грозит:** A misconfigured environment variable causes the entire server to crash on startup with an unclear error. A user who tries to customize timeouts will break their setup. Not critical for first-time users with defaults, but degrades debuggability.

### 7. Hardcoded macOS paths in spawn.py
- **Файл:** `src/mcp_huddle/spawn.py:80-101`  ·  **трудозатраты:** small  ·  _Packaging & publishing readiness_
- **Проблема:** The binary lookup paths include hardcoded `/opt/homebrew/bin/` (macOS), `/Applications/` (macOS), and `/private/tmp/` (macOS-specific temp path). While the code gracefully falls back to PATH lookup, the explicit fallbacks are macOS-specific. A Linux/Windows user will still attempt these lookups (they'll fail, but it's inefficient). More critically, `/private/tmp/mimo-runner-home` in mimo_runner.py:33 is Unix-specific and will fail on Windows.
- **Что делать:** Use `sys.platform` to gate macOS-specific paths: \n```python\nif sys.platform == 'darwin':\n    candidates.append('/opt/homebrew/bin/...')\n    candidates.append('/Applications/...')\n```\nFor `/private/tmp/`, use `tempfile.gettempdir()` or `pathlib.Path(tempfile.gettempdir())` for cross-platform compatibility. Update mimo_runner.py:33 accordingly.
- **Чем грозит:** The package claims `Operating System :: OS Independent` (pyproject.toml:22) but has hardcoded Unix paths that will break on Windows. A Windows user will encounter unexpected failures when trying to auto-spawn agents.

### 8. Documented tools not exposed as MCP tools
- **Файл:** `README.md:1-300, src/mcp_huddle/server.py:115-415`  ·  **трудозатраты:** small  ·  _Documentation completeness & accuracy_
- **Проблема:** README claims '15 MCP tools' and documents 15 tools in the Tools table, but only 10 are actually exposed with @mcp.tool() decorator. The following 5 documented tools are NOT exposed as MCP tools: room_request_close (line 170), room_close (line 177), room_close_session (line 196), status_set (line 354), status_get (line 369). Users attempting to call these will receive 'tool not found' errors. Room management and status operations are core features being advertised but are inaccessible.
- **Что делать:** Either: (A) Add @mcp.tool() decorator to the 5 missing functions and verify they work correctly, or (B) Remove these functions from the README Tools table and update the count from '15 MCP tools' to '10 MCP tools'. Document where agents can actually manage rooms (HTTP API only) if those functions are intentionally not exposed.
- **Чем грозит:** Public users will be unable to close their own rooms or set agent status via the MCP interface - the documented primary interface for agents. This breaks the room lifecycle as described in README's 'Codex lifecycle' and 'CLOSE PROTOCOL' sections. Users may think their installation is broken when tools fail.

### 9. Stale registry documentation - Qwen and DeepSeek already removed
- **Файл:** `README.md:18,60, src/mcp_huddle/spawn.py:162-227 (uncommitted changes)`  ·  **трудозатраты:** small  ·  _Documentation completeness & accuracy_
- **Проблема:** README claims default registry includes 'live-probed Qwen, live-probed DeepSeek' and lists them explicitly twice (lines 18 and 60). However, src/mcp_huddle/spawn.py contains UNCOMMITTED changes that remove _qwen_advisor_spec() and _deepseek_advisor_spec() functions entirely (65 lines deleted), replacing them with a NOTE explaining they were 'removed 2026-06-18'. The code has been prepared for public release but still contains outdated documentation about agents that no longer exist in the registry.
- **Что делать:** Commit the spawn.py changes immediately before public release (they are on branch feat/phase1-2-agent-events, not main). Update README lines 18 and 60 to list only 'Codex, Antigravity, MiMo, Claude' and update the Configuration table to reflect the actual default registry instead of mentioning Qwen/DeepSeek. Add a section under 'Configuration' listing the removed agents and why (migration to LiteLLM router) for clarity.
- **Чем грозит:** Public users will read documentation about agents (Qwen, DeepSeek) that no longer exist in the codebase. Attempts to enable them via environment variables (MCP_HUDDLE_QWEN_ENABLED, MCP_HUDDLE_DEEPSEEK_ENABLED) will silently fail. Creates confusion and support burden.

### 10. Missing PyPI publication guidance and version bump
- **Файл:** `README.md:25,32, pyproject.toml:5`  ·  **трудозатраты:** small  ·  _Documentation completeness & accuracy_
- **Проблема:** README states 'The package is not on PyPI yet' and recommends `uvx --from git+https://github.com/...` installation. pyproject.toml version is 0.1.2 (beta-like). However, the project is 'about to be published' per the task description. After publication, README's installation instructions will be wrong (users will be able to `pip install mcp-huddle` instead of uvx from git). The documentation is not prepared for the public release state.
- **Что делать:** Before publishing to PyPI: (1) Decide final version number (0.1.2 is fine for initial release), (2) Create a 'Installation' section in README with both methods: pip (recommended, once published) and uvx from git (for development), (3) Update the 'not on PyPI yet' line to reflect the new status, (4) Uncomment the PyPI badges in README's commented section at the top, (5) Add a link to PyPI package in pyproject.toml and README.
- **Чем грозит:** Post-release, users will see outdated installation instructions and not realize they can simply pip-install. First-time users will be directed to a more complex git-based setup. Reduces friction for adoption significantly.

### 11. Add module and public function docstrings
- **Файл:** `src/mcp_huddle/bus.py:1, src/mcp_huddle/openai_compatible_runner.py:1, src/mcp_huddle/mimo_runner.py:1, src/mcp_huddle/spawn.py:62,114,225`  ·  **трудозатраты:** small  ·  _Code simplification & maintainability_
- **Проблема:** Public bus.py functions lack docstrings: create_room, invite_agent, get_room_info, list_rooms, request_close, close_room, post_message, set_status, get_status, propose_resolution, resolution_vote. openai_compatible_runner.py exports extract_room_id, extract_request_id, select_request, build_messages, call_openai_compatible, run, main — none have docstrings. mimo_runner.py exports build_prompt, call_mimo, run, main — none documented. spawn.py functions _spawn_spec_available and nested _google_advisor_spec/_mimo_advisor_spec have no docstrings. A newcomer reading bus.py to understand room creation semantics must infer from 70 lines of implementation code. The agent runner modules export reusable extraction/selection functions (select_request, extract_room_id) that mimo_runner imports, but no inline docs explain the contract.
- **Что делать:** Add one-line + expanded docstrings to all public functions (non-underscore). For bus.py: document return types, state changes, and file locks (e.g., 'Locked RMW of meta.json; atomic with agent_meta write'). For runners: document the extraction/filtering contract and usage in context ('select_request(room_id, agent, requested_id): Scan messages JSONL, return the oldest kind=request addressed to agent (or requested_id if specified) that has no reply_to and is not from agent itself'). Document room lifecycle constants and edge cases (e.g., ZOMBIE_CHECK_SECS, DEADLOCK_TIMEOUT_SECS) inline. This is essential for PyPI publication: users need to extend runners and understand the bus contract.
- **Чем грозит:** Users installing from PyPI cannot reverse-engineer the bus layer or runner contracts from source alone. No docstrings means they must ask in issues or guess from examples. Public functions are part of the stable surface; documenting them is a publication baseline.

---

## 🟡 ЖЕЛАТЕЛЬНО — можно вскоре после релиза  (34)


### 1. Hardcoded macOS Homebrew and application bundle paths for agent binaries
- **Файл:** `src/mcp_huddle/spawn.py:80-101`  ·  **трудозатраты:** small  ·  _Portability & hardcoded local assumptions_
- **Проблема:** Lines 82-83 (Codex), 90 (Antigravity), 94-95 (Claude), 100 (MiMo) hardcode macOS-specific fallback paths: `/opt/homebrew/bin/*` (Homebrew on Apple Silicon) and `/Applications/*.app/Contents/Resources/*` (app bundles). Linux users have Homebrew at `/home/linuxbrew/.linuxbrew/bin/*` on older systems or under `$HOMEBREW_PREFIX` on others. The fallback strategy uses absolute paths before checking PATH, which is correct, but these specific paths won't exist on non-macOS systems. The `_first_existing_binary()` function will skip them, which is correct behavior, but this makes the hardcoded paths dead code on Linux — they're harmless but indicate the package was carved from a macOS-only monorepo.
- **Что делать:** Either remove the hardcoded paths entirely and rely solely on PATH (simpler for a public package), or make the fallback paths platform-specific by detecting `sys.platform` and including the correct Homebrew path for the detected OS. For transparency, document in spawn.py that users should install binaries via their system package manager or Homebrew and ensure they are in PATH.
- **Чем грозит:** Low practical impact since fallback paths are only used if PATH lookup fails. However, Linux users who manually install Homebrew may expect `/opt/homebrew/bin` to work (if using Homebrew on macOS-like systems) but it won't be tried. Encourages best practice of ensuring binaries are in PATH instead.

### 2. AUTO_SPAWN relies on hardcoded agent names in DEFAULT_REGISTRY
- **Файл:** `src/mcp_huddle/spawn.py:287-331`  ·  **трудозатраты:** trivial  ·  _Portability & hardcoded local assumptions_
- **Проблема:** The `DEFAULT_REGISTRY` is a hardcoded list with exactly 6 agents (Codex, Antigravity, MiMo, Claude, Qwen, DeepSeek). If a user specifies `auto_spawn=True` without a registry override, they will only get these 6 agents. If a user defines a custom registry via `MCP_HUDDLE_SPAWN_REGISTRY` but forgets to include an agent they need, there is no fallback — auto_spawn will be restricted to the custom registry. The documentation says "Override the whole registry via the MCP_HUDDLE_SPAWN_REGISTRY env var", which is correct, but users might expect to be able to extend the registry instead of replacing it.
- **Что делать:** No code change needed if current behavior is intentional (registry is all-or-nothing). Add documentation in spawn.py and README explaining that MCP_HUDDLE_SPAWN_REGISTRY replaces the entire registry (not extends), and provide an example of a custom registry file (currently only referenced in examples/ but not shown). Alternatively, support a two-tier model: load DEFAULT_REGISTRY, then merge/override with user-provided entries from MCP_HUDDLE_SPAWN_REGISTRY.
- **Чем грозит:** Users who set a custom registry might be surprised that it completely replaces the defaults, leading to missing agents. Low impact because the env var is documented, but easy to misunderstand.

### 3. HTTP dashboard bound to 127.0.0.1 only (good, but not documented for multi-user systems)
- **Файл:** `src/mcp_huddle/__main__.py:26`  ·  **трудозатраты:** small  ·  _Network Security_
- **Проблема:** The HTTP server binds to 127.0.0.1:8014 which protects against network-level remote access. However, on multi-user systems, any local user can connect to this endpoint via localhost. The README does not warn about this. Additionally, the code hardcodes 127.0.0.1 instead of using an environment variable, which may be intentional but limits deployment flexibility.
- **Что делать:** Document in README.md that the HTTP dashboard exposes all rooms/messages to any local user on the system. For multi-user deployments, consider: (1) adding an optional --bind flag to override the host, (2) recommending OS-level firewall rules or VirtualEnv isolation, or (3) adding optional authentication token validation via env var (MCP_HUDDLE_AUTH_TOKEN). The current localhost-only binding is acceptable for single-user dev machines but should be explicit in deployment guidance.
- **Чем грозит:** Public user running on a shared/multi-user system (e.g., shared VPS, dev container) may expose their AI agent conversations to other local users without realizing it. The README does not warn about this.

### 4. Agents spawned with --dangerously-skip-permissions bypasses approval gates; documented but unwarned in README
- **Файл:** `src/mcp_huddle/spawn.py:126,312; src/mcp_huddle/mimo_runner.py:63`  ·  **трудозатраты:** small  ·  _Privilege Escalation / Authorization_
- **Проблема:** The auto-spawn registry launches Antigravity (`agy --dangerously-skip-permissions`), Claude (`--dangerously-skip-permissions`), and MiMo (`--dangerously-skip-permissions`) with flags that disable approval gates. The code comments justify this (e.g., line 109-110 in spawn.py notes Codex needs full sandbox to make MCP calls under `-a never`), but the README does NOT warn users that spawning agents auto-grants them full system access and MCP permissions. A public user reading the README may not realize that `room_create(..., auto_spawn=True)` will spawn agents with unrestricted CLI access. The brief/goal passed to agents is user-controlled, so a malicious brief could instruct an agent to download/execute code on the user's system.
- **Что делать:** (1) Document in README under 'Security' section: 'Spawned agents run with --dangerously-skip-permissions; they have full access to your files and MCP tools. Only use auto_spawn=True with trusted goal/brief text.' (2) Validate that `goal` and `auto_spawn` brief values do not contain shell metacharacters or suspicious patterns (though briefs are not shell-passed, they are sent as CLI args, so some defense-in-depth is appropriate). (3) Consider a default of auto_spawn=False and require explicit opt-in. (4) Add an environment variable MCP_HUDDLE_UNSAFE_AUTO_SPAWN=1 to enable auto_spawn, defaulting to False.
- **Чем грозит:** A public user may unknowingly grant spawned agents full system access and assume they are sandboxed. The CLI flags themselves are not a vulnerability (they are intentional), but the absence of warnings in README creates a footgun for users unfamiliar with the Codex/Claude/agy CLI semantics.

### 5. Room ownership check uses string equality; owner name is case-sensitive and canonicalized but not verified at room_create
- **Файл:** `src/mcp_huddle/server.py:116-124 (room_create); src/mcp_huddle/bus.py:165-175 (close_room)`  ·  **трудозатраты:** medium  ·  _Authorization_
- **Проблема:** When closing/deleting a room, the code calls `bus.close_room(room_id, owner)` and the bus checks if the provided `owner` matches `meta['owner']` (line 166: `if owner != meta['owner']`). The owner is a string like "Claude" or "Codex" passed by the caller in the MCP tool. There is no validation that the caller is actually that agent; the MCP client (e.g., Claude Code, Codex CLI) sends the tool call with an `agent` parameter. If a user can invoke MCP tools from multiple agent CLIs in the same session, they could call `room_create(..., owner="Codex")` from Claude, then later `room_close(room_id, owner="Claude")` and it would fail—which is correct. However, if an attacker compromises one agent's process, they can call `room_close` on rooms owned by another agent. The code relies on the MCP client to authenticate the agent identity, which is outside the huddle server's control.
- **Что делать:** (1) Document that huddle trusts the MCP client to authenticate the agent identity; room ownership is not cryptographically verified. (2) For deployment on shared systems, recommend running huddle in a separate process with restricted stdin/stdout access (only from trusted agents). (3) Consider adding an optional shared secret or HMAC-signed ownership tokens (MCP_HUDDLE_OWNER_SECRET env var) to verify owner claims at room_create time, but this adds complexity and may not be necessary for typical single-user deployments. For a public release, the current approach is acceptable with clear documentation.
- **Чем грозит:** If an agent's process is compromised (e.g., injected malicious code), the attacker can close/delete rooms owned by other agents. On a single-user system, this is low risk; on a shared multi-agent system, it's a concern.

### 6. Spawned processes inherit user environment including secrets; no env var scrubbing
- **Файл:** `src/mcp_huddle/spawn.py:448-454 (subprocess.Popen); src/mcp_huddle/mimo_runner.py:68`  ·  **трудозатраты:** small  ·  _Information Disclosure_
- **Проблема:** When spawning agents (Codex, Claude, Antigravity, MiMo, etc.), the code uses `subprocess.Popen(argv, cwd=cwd, ...)` without specifying `env=`, which means the spawned process inherits the parent's full environment. If the user has API keys, tokens, or credentials in environment variables (e.g., ANTHROPIC_API_KEY, OPENAI_API_KEY, AWS_ACCESS_KEY_ID), those are passed to the spawned agent. While this is necessary for the agents to function, it creates a risk if: (1) an agent process is compromised and logs its environment, (2) the agent's stdout/stderr (redirected to .events.jsonl) accidentally includes env dumps, (3) a malicious brief instructs an agent to `env | base64` and post it to the room. The code does have _MCP_KILL_SWITCHES in mimo_runner.py that disabled some MCP servers, but it doesn't scrub secrets.
- **Что делать:** (1) Add a security note in README: 'Spawned agents inherit your environment, including API keys. Ensure your shell does not export sensitive credentials. If using MCP_HUDDLE_SPAWN_REGISTRY, verify custom registry entries do not leak env vars.' (2) Optionally, implement env scrubbing: before spawning, build a clean env with only whitelisted vars (PATH, HOME, USER, LANG, etc.) and required API keys explicitly from a secure config (not from parent env). (3) Monitor agent logs for accidental env dumps. For MVP, the README warning is sufficient.
- **Чем грозит:** If a public user has API keys in their shell environment and they spawn agents in a room, those keys could leak if agents are compromised or misconfigured.

### 7. Room and message storage is world-readable if ~/.mcp-huddle has permissive permissions
- **Файл:** `src/mcp_huddle/bus.py:17-18; src/mcp_huddle/server.py:144`  ·  **трудозатраты:** small  ·  _Confidentiality_
- **Проблема:** The code stores all rooms in ~/.mcp-huddle/rooms/ (configurable via MCP_HUDDLE_HOME). It creates directories with `mkdir(parents=True, exist_ok=True)` (bus.py:38) and writes JSON files with `Path.write_text()` (bus.py:559), which use the process's default umask. If the user's umask is 022 (common default), ~/.mcp-huddle/rooms/ and all subdirectories and files are world-readable (chmod 755 / 644). On a multi-user system, any user can read all chat history, room metadata, and agent logs. The code does not explicitly set umask or file permissions.
- **Что делать:** (1) Add a security note in README: 'Room storage (~/.mcp-huddle/) must be kept private. Ensure your umask is 077 (chmod go-rwx ~/.mcp-huddle/). On shared systems, use a per-user MCP_HUDDLE_HOME or an isolated container.' (2) Explicitly set permissions when creating dirs/files: `os.makedirs(..., mode=0o700)` for dirs and use `os.open(..., flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL, mode=0o600)` for files. (3) At startup, check and warn if ~/.mcp-huddle/ is world-readable. For a quick fix, document the umask requirement; for robustness, enforce it in code.
- **Чем грозит:** On a shared system or VM, another user can read all agent conversations and sensitive data stored in rooms without permission.

### 8. SSE endpoint streams agent logs without authentication; any HTTP client can subscribe
- **Файл:** `src/mcp_huddle/server.py:670-724`  ·  **трудозатраты:** medium  ·  _Confidentiality_
- **Проблема:** The `/agents/{room_id}/{agent_name}/events` SSE endpoint (line 670) takes room_id and agent_name from the URL path and streams the agent's event log (stdout/stderr) without any authentication. Any HTTP client with localhost access (which on a shared system is any local user) can subscribe to the SSE stream and read real-time agent output. While the endpoint tries to construct the log_path safely (line 681: `bus._room_dir(room_id) / "agents" / f"{agent_name}.events.jsonl"`), there is no auth check before opening and streaming the file. The room_id is UUID-based (hard to guess), but an attacker can read room_ids from `/api/rooms` (line 515, no auth either).
- **Что делать:** (1) Add authentication: check for MCP_HUDDLE_API_TOKEN header or a CSRF token (issued from /dashboard) before streaming. (2) Or, restrict SSE to stdio mode only (no HTTP routes). (3) Add a warning in README that HTTP dashboard exposes real-time agent logs to all local users. For a public release, implement auth tokens (option 1) or restrict to stdio-only (option 2).
- **Чем грозит:** Any local user can read real-time agent logs including prompts, responses, and API call details by accessing the SSE endpoint.

### 9. Codex thread_id parsing could be fragile if log format changes; no version check
- **Файл:** `src/mcp_huddle/spawn.py:532-560 (parse_codex_thread_id)`  ·  **трудозатраты:** trivial  ·  _Robustness_
- **Проблема:** The `parse_codex_thread_id` function tails the Codex event log looking for a JSON line with `{"type":"thread.started",...}`. If Codex changes its event format or the line encoding, the parser could fail silently or timeout. The function tries lines up to a timeout (default 10s) and returns None if not found, which is handled gracefully (no exception). However, if a future Codex version changes the event type or structure, huddle will not detect thread IDs and Codex wakeup will not work (agents will spawn fresh instead of resuming). This is not a security issue but a robustness concern.
- **Что делать:** (1) Add a version check or compatibility flag. (2) Log a warning if thread_id is not found (line 1120: `tid = spawn.parse_codex_thread_id(...)`). (3) Document the assumption that Codex `--json` output format is stable. For public release, add a comment noting the dependency on Codex event format.
- **Чем грозит:** If Codex updates, huddle may silently stop resuming Codex threads, degrading UX. Not a security issue.

### 10. No error handling for corrupted JSON files
- **Файл:** `src/mcp_huddle/bus.py:550, 569, 585, 750`  ·  **трудозатраты:** small  ·  _Crash-safety & Data Integrity_
- **Проблема:** _read_meta() and similar functions call json.loads(path.read_text()) without try-except. If a file is partially written (e.g., power loss, concurrent replace() that was interrupted), json.JSONDecodeError is raised and crashes the operation. The _write_json() uses atomic replace(), but a process that crashes mid-fsync can leave the file truncated. No recovery or corruption detection is in place.
- **Что делать:** Wrap json.loads() in try-except to handle JSONDecodeError. Provide a fallback empty dict or the previous cached value. For critical files like meta.json, consider a write-ahead log (e.g., meta.json.bak) so a failed write doesn't clobber the last good state. Alternatively, use a stricter check: if the parsed JSON is not a dict, raise a clear error message.
- **Чем грозит:** A first-time user whose system crashes during a write can find their room data corrupted, causing the entire room to fail on the next access with an unhelpful JSONDecodeError. If this happens during server startup (e.g., recovery after crash), the user cannot recover the room.

### 11. Unbounded message cache memory growth
- **Файл:** `src/mcp_huddle/bus.py:597-641`  ·  **трудозатраты:** medium  ·  _Resource Leaks & Stability_
- **Проблема:** The _msg_cache dict grows without bound. Keys are file paths (pstr = str(p)), and values are cached (key, msgs) tuples. When a room is deleted, _evict_msg_cache() is called. However, if a room is never deleted (e.g., idle/closed rooms left around), the cache entry persists forever. Over time, with hundreds of old rooms, this cache can consume significant memory. No TTL or LRU eviction is implemented.
- **Что делать:** Implement one of: (1) an LRU cache with a max size (e.g., 100 rooms), (2) a TTL-based eviction (e.g., cache entry expires after 1 hour), or (3) a periodic sweep in the watchdog that evicts entries for deleted rooms. Alternatively, clear cache entries for rooms that are moved to 'closed' or 'resolved' status.
- **Чем грозит:** A long-running server that accumulates many rooms over weeks can gradually consume more memory, degrading performance. On resource-constrained machines (e.g., a Raspberry Pi or embedded system), this can exhaust available RAM and crash the server.

### 12. Daemon reaper threads not joined at shutdown
- **Файл:** `src/mcp_huddle/spawn.py:394-399`  ·  **трудозатраты:** medium  ·  _Resource Leaks & Shutdown Safety_
- **Проблема:** Reaper threads are started as daemon=True and never explicitly joined. While daemon threads are killed on process exit, they may not have time to flush logs or run cleanup callbacks. If a spawned agent is still running when the MCP server exits (e.g., user closes Claude Code mid-room), the reaper thread may not call on_exit(), leaving the agent process as a zombie or orphan until the parent dies. Additionally, the thread is given a target=wait_for_exit with no timeout, so if a subprocess hangs, the thread blocks forever.
- **Что делать:** Either (1) make threads non-daemon and join them with a timeout in a shutdown handler, or (2) add a timeout to proc.wait() so the reaper doesn't block indefinitely. In the shutdown handler, terminate any remaining child processes explicitly. Alternatively, track spawned PIDs in a module-level list and kill them all on SIGTERM/exit.
- **Чем грозит:** If a spawned agent hangs (e.g., the Codex binary freezes), the parent stdio-huddle process cannot exit cleanly—it hangs indefinitely waiting for the reaper. Users must forcefully kill the process (Ctrl+C, kill -9). In CI/test environments, this can cause hangs that break automation.

### 13. Race condition: meta.json reads before lock in post_message
- **Файл:** `src/mcp_huddle/bus.py:220-228, 233-242`  ·  **трудозатраты:** small  ·  _Concurrency & Race Conditions_
- **Проблема:** post_message() reads meta (line 220) to check room status, then acquires the messages lock (line 233). Between these two operations, a concurrent close_room() can change the status. The code re-reads meta under the messages lock (line 238) to catch this, which is good. However, if the room is DELETED between the pre-lock read (line 220) and the _lock() call on line 233, the messages.jsonl file may not exist, causing the lock to fail or succeed on a non-existent file. The _lock() tries to open the file in 'a+' mode; if the parent directory is gone, this fails with FileNotFoundError.
- **Что делать:** Move the initial meta read inside the lock, or explicitly handle FileNotFoundError when opening messages.jsonl. Alternatively, acquire a room-level lock before any operation to prevent concurrent deletion while a message is being posted.
- **Чем грозит:** A user who posts a message while another user deletes the room can encounter an unexpected FileNotFoundError instead of a clean error message like 'Room is closed'. This can confuse the user and make the server appear to crash.

### 14. No timeout on background watchdog health checks
- **Файл:** `src/mcp_huddle/server.py:430-473`  ·  **трудозатраты:** small  ·  _Stability & Hangs_
- **Проблема:** _background_watchdog() runs background checks (zombie cleanup, retention sweep, idle marking, deadlock detection, agent wakes) every ZOMBIE_CHECK_SECS (30s by default). If any of these checks hangs (e.g., list_rooms() I/O blocks, check_deadlock_rooms() blocks on a corrupted file), the entire watchdog is stalled. There is no timeout per check or per iteration. The watchdog runs as an asyncio task; if it blocks synchronously, it blocks the entire event loop, preventing HTTP requests from being served.
- **Что делать:** Wrap each check in an asyncio.timeout() or manually add timeouts. For blocking operations (like file reads), use run_in_executor() to move them to a thread pool. Example: `await asyncio.wait_for(asyncio.to_thread(bus.list_rooms()), timeout=5.0)`
- **Чем грозит:** If a file lock is held by another process or a corrupted file causes I/O to hang, the watchdog blocks the event loop. The dashboard becomes unresponsive, and new HTTP requests queue indefinitely. Users cannot interact with rooms until the issue resolves or the server is restarted.

### 15. Missing --help and --version CLI support
- **Файл:** `src/mcp_huddle/__main__.py:15-30`  ·  **трудозатраты:** small  ·  _CLI ergonomics_
- **Проблема:** The entry point does not handle --help, --version, or other standard CLI flags. Passing --help silently attempts to read MCP protocol from stdin and exits without feedback. Users trying to discover the tool or check version get no useful response.
- **Что делать:** Add argument parsing (argparse) to handle --help, --version, --http flags explicitly before attempting MCP protocol operations. Print usage information and exit cleanly.
- **Чем грозит:** New users trying to learn how to use the tool (running `mcp-huddle --help`) get silent failure; users cannot check the installed version without reading source code or docs.

### 16. Unhandled PORT environment variable validation
- **Файл:** `src/mcp_huddle/__main__.py:23`  ·  **трудозатраты:** trivial  ·  _Configuration robustness_
- **Проблема:** The code does `int(os.environ.get("PORT", 8014))` without error handling. If PORT is set to a non-integer value (e.g., `PORT=abc`), a cryptic ValueError is thrown to stderr and the server crashes with no helpful message.
- **Что делать:** Wrap port parsing in try/except, catch ValueError, and print a clear error message like 'Error: PORT environment variable must be an integer, got "abc"'. Set exit code 1 and suggest correct usage.
- **Чем грозит:** Misconfigured PORT crashes the server with an unhelpful traceback instead of a user-friendly error message.

### 17. Missing examples/registry.json file referenced in README
- **Файл:** `README.md:103`  ·  **трудозатраты:** trivial  ·  _Documentation completeness_
- **Проблема:** The README states 'See `examples/registry.json` for format' but the file does not exist in the repository. Users trying to create a custom spawn registry have no template.
- **Что делать:** Create examples/registry.json with a complete example showing the SpawnSpec structure (as defined in spawn.py). Include comments explaining each field (name, cmd, enabled, optional probe_url, probe_chat_url, etc.).
- **Чем грозит:** Users wanting to customize the auto-spawn agent registry cannot find a reference format and must reverse-engineer it from source code.

### 18. Malformed MCP_HUDDLE_SPAWN_REGISTRY JSON crashes without user-friendly error
- **Файл:** `src/mcp_huddle/spawn.py:402-410`  ·  **трудозатраты:** small  ·  _Configuration error handling_
- **Проблема:** When MCP_HUDDLE_SPAWN_REGISTRY points to a file with invalid JSON, load_registry() raises json.JSONDecodeError without catching it. The error propagates up with a low-level traceback instead of a clear message about the config file.
- **Что делать:** Wrap json.load() in try/except, catch json.JSONDecodeError and ValueError, print a clear message like 'Error: MCP_HUDDLE_SPAWN_REGISTRY file is not valid JSON: {path}' with the actual error, then fall back to DEFAULT_REGISTRY.
- **Чем грозит:** Users with a typo or corruption in their custom registry file get a cryptic JSON parser error instead of guidance on how to fix it.

### 19. Dashboard startup message printed before server is ready
- **Файл:** `src/mcp_huddle/__main__.py:24-26`  ·  **трудозатраты:** small  ·  _User experience_
- **Проблема:** The code prints 'Dashboard: http://127.0.0.1:8014/dashboard' before uvicorn.run(), which starts the server asynchronously. If uvicorn fails to bind (port in use, permission denied, etc.), the user already saw the URL and may click it or assume the server is running.
- **Что делать:** Either (1) start uvicorn in a thread, wait for it to be ready, then print the URL, or (2) move the print statements inside the Starlette app's lifespan startup handler so they print only after the server is actually listening.
- **Чем грозит:** Users see a dashboard URL printed but the server may not actually be running if there's a startup error, leading to confusion.

### 20. No validation of required room_create parameters
- **Файл:** `src/mcp_huddle/server.py:116-148`  ·  **трудозатраты:** small  ·  _API error handling_
- **Проблема:** The room_create() function accepts owner_pid without checking if it's valid or non-zero. If owner_pid=0 or -1 is passed (by mistake), the watchdog's zombie check (bus.check_zombie_rooms) will skip it because of `if pid <= 0: continue`. Also, cwd can be an arbitrary path without existence validation.
- **Что делать:** Add validation: (1) reject owner_pid <= 0 with a clear ValueError message, (2) optionally warn if cwd is provided but doesn't exist on the current filesystem. Document that owner_pid should be the calling agent/process's PID.
- **Чем грозит:** Users passing invalid PIDs or nonexistent cwd values may create rooms that behave unexpectedly (e.g., zombie cleanup doesn't work) without realizing the misconfiguration.

### 21. No health check / status endpoint documentation for HTTP mode
- **Файл:** `src/mcp_huddle/server.py:635-656 (health endpoint exists)`  ·  **трудозатраты:** trivial  ·  _Operational visibility_
- **Проблема:** The /api/health endpoint exists and provides wake-health diagnostics, but is not documented in the README. Users running --http don't know how to verify the server is functioning beyond manually visiting the dashboard.
- **Что делать:** Add a line to the Configuration section or HTTP mode section of README documenting the /api/health endpoint (GET) and what it returns. Also document that /api/rooms lists all rooms (useful for scripting).
- **Чем грозит:** Operators cannot easily script health checks or monitoring without reading source code or discovering the endpoint by accident.

### 22. Agent spawn failures logged to stderr only, not captured for diagnostics
- **Файл:** `src/mcp_huddle/spawn.py:352-372 (log_spawn_failure function)`  ·  **трудозатраты:** medium  ·  _Troubleshooting visibility_
- **Проблема:** When auto_spawn fails (e.g., binary not found, subprocess crash), spawn.log_spawn_failure() writes to stderr. If the server is daemonized or run in a CI environment, stderr may be lost or buffered. Users don't see why Codex/Antigravity didn't spawn.
- **Что делать:** In addition to stderr logging, write a spawn failure event to the room's .events.jsonl file or create a separate .spawn-failures.jsonl log. This allows the dashboard or CLI tools to surface the reason agents didn't spawn.
- **Чем грозит:** Users create a room with auto_spawn=True but don't see why agents didn't appear; they must check stderr logs manually, which may not be accessible.

### 23. Confusing default auto_spawn behavior: no clear feedback when agents fail to spawn
- **Файл:** `src/mcp_huddle/server.py:145-148, spawn.py:518-527`  ·  **трудозатраты:** medium  ·  _First-run UX_
- **Проблема:** When auto_spawn=True is called and a registry agent (e.g., Codex) is not installed, room_create() silently continues and returns a room_id with no agents spawned. Users see an empty room and assume the feature is broken, with no feedback about why.
- **Что делать:** After spawn attempts, if auto_spawn=True and zero agents were actually spawned, post a system message to the room explaining which registry agents were skipped and why (binary not found, disabled, etc.). Also return a list of spawned agent names from room_create() so the caller can detect this.
- **Чем грозит:** Users invoke auto_spawn=True expecting Codex to appear, but it silently doesn't show up if the binary is missing. They must infer the reason from absence of log output.

### 24. Missing example file referenced in README
- **Файл:** `README.md:103 and examples/ directory`  ·  **трудозатраты:** small  ·  _Packaging & publishing readiness_
- **Проблема:** README.md line 103 mentions `examples/registry.json` as a reference for the spawn registry format, but this file does not exist in the examples/ directory. Users following the documentation will not find the promised example.
- **Что делать:** Create `examples/registry.json` with a documented example of the spawn registry structure. Reference spawn.py:47-55 (SpawnSpec TypedDict) for the schema. Include comments explaining each field and provide an example with 2-3 agents.
- **Чем грозит:** Users trying to customize the spawn registry via `MCP_HUDDLE_SPAWN_REGISTRY` env var will have no concrete reference to copy from, requiring them to reverse-engineer the format from source code.

### 25. Potential issue with data directory creation on read-only filesystems
- **Файл:** `src/mcp_huddle/bus.py:17-18, 38, 79, 103`  ·  **трудозатраты:** small  ·  _Packaging & publishing readiness_
- **Проблема:** The package creates `~/.mcp-huddle/rooms/` and subdirectories on first use via `mkdir(parents=True, exist_ok=True)`. If the user's home directory is on a read-only filesystem or has permission issues, this will raise an exception with a generic message. The error handling (e.g., in bus.py line 38) does not provide user-friendly diagnostics.
- **Что делать:** Wrap directory creation in a try/except that catches PermissionError, OSError, and FileNotFoundError, and re-raise with a clearer message like: `Could not create huddle storage directory {path}: {err}. Check permissions and ensure $HOME is writable.` Or provide an initialization function that validates the environment before first use.
- **Чем грозит:** Users in restricted environments (sandboxed, read-only home, corporate lockdown) will see cryptic errors when first running the package, with no guidance on how to configure an alternate storage directory.

### 26. Missing registry.json example file referenced in docs
- **Файл:** `README.md:63, examples/`  ·  **трудозатраты:** small  ·  _Documentation completeness & accuracy_
- **Проблема:** README Configuration table says 'See examples/registry.json for format' but the file does not exist. Users trying to customize the spawn registry will not find the documented format example, breaking the documented configuration workflow.
- **Что делать:** Create examples/registry.json with an annotated example showing the SpawnSpec format (name, cmd, enabled, probe_url, requires_model, probe_chat_url, probe_chat_model, probe_timeout_sec) with comments explaining each field. Use the current DEFAULT_REGISTRY from spawn.py as a starting point.
- **Чем грозит:** Users cannot easily customize the auto-spawn registry. They must reverse-engineer the JSON schema by reading Python source code instead of following documented examples. Adoption of customization feature will be low.

### 27. Undocumented environment variables for tuning and control
- **Файл:** `src/mcp_huddle/server.py:101,105,106,110, src/mcp_huddle/spawn.py:154,156,339`  ·  **трудозатраты:** small  ·  _Documentation completeness & accuracy_
- **Проблема:** README documents only 3 environment variables (PORT, MCP_HUDDLE_HOME, MCP_HUDDLE_SPAWN_REGISTRY) but the code defines at least 7 more: IDLE_TIMEOUT_SECS (600s, affects room idle transition), HUDDLE_RETENTION_DAYS (7 days, auto-deletes old rooms), HUDDLE_RETENTION_SWEEP_SECS (3600s, sweep interval), MCP_HUDDLE_RATE_LIMIT_COOLDOWN_SEC (900s, usage limit cooldown), MCP_HUDDLE_MIMO_ENABLED, MCP_HUDDLE_CLAUDE_ENABLED, MCP_HUDDLE_PROBE_CACHE_TTL_SEC (300s). Users have no way to discover or configure these critical operational settings.
- **Что делать:** Add a 'Advanced Configuration' section to README documenting all environment variables with defaults and use cases. Example: 'IDLE_TIMEOUT_SECS: Seconds of silence before a room transitions to idle status. Set to 0 to disable auto-idle (rooms stay open indefinitely). Default: 600.'
- **Чем грозит:** Operators deploying mcp-huddle cannot tune it for their use case (e.g., longer timeout for slow agents, disable retention for ephemeral rooms). They must read source code or trial-and-error to find configuration options.

### 28. Missing CONTRIBUTING guide and governance
- **Файл:** `/`  ·  **трудозатраты:** small  ·  _Documentation completeness & accuracy_
- **Проблема:** No CONTRIBUTING.md file. README has no 'Contributing' or 'Development' section explaining how to report issues, submit PRs, set up dev environment, or contribute to the project. pyproject.toml lists only 'Issues' URL; no guidance for contributors.
- **Что делать:** Create CONTRIBUTING.md covering: (1) development setup (pip install -e '.[dev]', run tests), (2) issue reporting template, (3) PR process, (4) code style/linting expectations, (5) where to discuss major changes (issues vs discussions). Add a link to it in README.
- **Чем грозит:** Low for end users, but higher adoption friction for potential contributors. Unclear if the project accepts contributions or only bug reports.

### 29. No CHANGELOG or release notes
- **Файл:** `/`  ·  **трудозатраты:** small  ·  _Documentation completeness & accuracy_
- **Проблема:** No CHANGELOG.md. Users cannot easily see what changed between versions or known breaking changes. Only git history is available (requires cloning repo to inspect).
- **Что делать:** Create CHANGELOG.md with entries for each release (starting from 0.1.2) listing features, fixes, and breaking changes. Reference: https://keepachangelog.com/. Consider auto-generating from git tags in future CI/CD.
- **Чем грозит:** Users upgrading between versions won't know if changes affect their setup or if bugs they hit were already fixed. Reduces trust in project maturity.

### 30. Missing architecture/security overview for local spawning
- **Файл:** `README.md, src/mcp_huddle/spawn.py`  ·  **трудозатраты:** medium  ·  _Documentation completeness & accuracy_
- **Проблема:** README describes auto-spawn feature that spawns arbitrary agent binaries (Codex, Claude, MiMo, Antigravity) but contains no security or architecture overview. Users unfamiliar with mcp-huddle won't understand: (1) that agents run as local subprocesses with access to the current machine (cwd, environment, files), (2) that spawning can fail silently if binaries are missing, (3) that agent stdout/stderr are logged to disk, (4) that Codex is spawned with '-s danger-full-access' sandbox override. These are significant permissions that should be transparent.
- **Что делать:** Add a 'Security & Architecture' section to README explaining: (1) Local execution model - agents run as subprocesses on the user's machine, (2) Process isolation - no network isolation or container sandboxing, (3) Subprocess output - logged to disk at ~/.mcp-huddle/rooms/<id>/agents/<name>.events.jsonl, (4) Sandbox overrides - explain why Codex runs with danger-full-access and what that means, (5) Dashboard access - clarify that dashboard is localhost-only (127.0.0.1:8014) and not network-exposed by default.
- **Чем грозит:** Users may unknowingly spawn agents with unexpectedly broad permissions. Operators sharing a machine may not realize agent processes have access to each other's environment. Creates security/privacy surprises.

### 31. No troubleshooting section for common issues
- **Файл:** `README.md`  ·  **трудозатраты:** medium  ·  _Documentation completeness & accuracy_
- **Проблема:** No 'Troubleshooting' section. Common issues for users will likely be: (1) `uvx: command not found` or PATH issues (partially addressed in a tip, but not in a structured section), (2) agent spawn failures (e.g., Codex binary not found, Claude not installed), (3) dashboard not showing messages (file lock issues?), (4) rooms not cleaning up (retention not understood), (5) permission errors when accessing ~/.mcp-huddle. Users must guess or ask issues.
- **Что делать:** Add a 'Troubleshooting' section with subsections for: 'Agent not spawning', 'Dashboard shows old data', 'Permission denied errors', 'PATH issues with uvx', 'High disk usage from old rooms'. Each should explain the root cause and solution.
- **Чем грозит:** Users encountering issues have no first-stop documentation. They'll open GitHub issues for common problems, increasing support burden. Reduces self-service troubleshooting.

### 32. Extract duplicated runner logic into shared base class
- **Файл:** `src/mcp_huddle/openai_compatible_runner.py:155-204, src/mcp_huddle/mimo_runner.py:95-141`  ·  **трудозатраты:** medium  ·  _Code simplification & maintainability_
- **Проблема:** Both openai_compatible_runner.py and mimo_runner.py implement near-identical `run()` functions (50+ overlapping lines): parse argparse, extract room_id/requested_id, call select_request, read transcript, post to bus.post_message with identical signature, and handle errors identically. The only differences are the agent-specific prompt builder (build_messages vs build_prompt) and agent invocation (call_openai_compatible vs call_mimo). mimo_runner already imports from openai_compatible_runner (extract_room_id, extract_request_id, select_request, _event), showing the dependency is acknowledged but incomplete. Outside contributors adding a third runner (e.g., local Qwen bridge) will copy the entire run() skeleton again, creating tri-plicate dead code.
- **Что делать:** Extract a shared AgentRunnerBase or runner_main() function that takes callbacks for agent-specific logic: (prompt_builder, agent_invoker, timeout). Move parse_arguments, room extraction, error handling, and bus.post_message into the common path. Have openai_compatible_runner.py and mimo_runner.py define their callbacks and call the shared orchestrator. This also reduces the copy-paste risk for future runners and makes the anti-loop contract centrally documented.
- **Чем грозит:** New contributors who add a local bridge runner (Qwen, DeepSeek via local API) must copy 40+ lines of boilerplate to get the MCP contract right. A shared base removes that friction and ensures consistent error handling. Each new runner becomes <50 lines of config + callbacks.

### 33. Consolidate _spawn_agents with spawn_all for cleaner logic flow
- **Файл:** `src/mcp_huddle/server.py:729-830`  ·  **трудозатраты:** medium  ·  _Code simplification & maintainability_
- **Проблема:** _spawn_agents (102 lines) in server.py wraps spawn.spawn_all (55 lines in spawn.py) but reimplements key logic: it filters specs by enable status, skips the owner, conditionally applies per-agent briefs via the dict auto_spawn parameter, and calls spawn_all with a factory-generated on_exit callback. Lines 773-801 implement a custom loop over registry specs to apply filters, extract briefs, and spawn individually when auto_spawn is a dict — duplicating logic that spawn_all already handles. The intent is sound (on_exit factory generation per room), but the implementation has the spawn orchestration split across two files with conditional paths. Lines 815-822 parse Codex thread_id with a timeout in a blocking manner, which could be moved into spawn.py alongside parse_codex_thread_id.
- **Что делать:** Refactor spawn_all to accept an optional on_exit_factory parameter (already a parameter, but rename to clarify it applies per agent) and have it handle the dict-vs-bool auto_spawn distinction. Move Codex thread_id parsing into a separate fast-path function or callback that _spawn_agents can wire in. This makes _spawn_agents the thin wrapper it should be: 20 lines that set up the brief, call spawn_all, merge agent_meta into room meta, and kick off thread_id capture in background.
- **Чем грозит:** The spawn orchestration logic is subtle: on_exit callbacks must be per-agent and per-initial-spawn vs per-wake, skipping the owner is crucial, and Codex thread_id capture races with the spawn. Consolidating the logic reduces the chance a contributor misses a detail when adding a new spawn feature.

### 34. Add explicit 'No module docstring' or document spawn.py probe/registry pattern
- **Файл:** `src/mcp_huddle/spawn.py:1-40`  ·  **трудозатраты:** small  ·  _Code simplification & maintainability_
- **Проблема:** spawn.py has a detailed 31-line module docstring explaining Phase 1 changes, registry structure, and spawn lifecycle. However, the probe/caching machinery (_cached_probe, _models_payload_has_model, _chat_probe_available, _spawn_spec_available) is not mentioned in the module doc, and the interaction between load_registry, spawn_all, and on_exit callbacks is only hinted at. A new contributor trying to add a custom probe (e.g., for a local Ollama bridge) must infer the probe pattern from reading _spawn_spec_available and _cached_probe without a guide. The comment at line 18-30 documents changes but not the intended architecture.
- **Что делать:** Expand the module docstring to include a 'Probe/Registry Pattern' section explaining: (1) SpawnSpec.probe_url / requires_model / probe_timeout_sec are optional fields for health-checking, (2) _cached_probe caches results for TTL_SEC to avoid hammering slow endpoints, (3) _spawn_spec_available checks both binary presence and optional health probe, and (4) load_registry returns the DEFAULT_REGISTRY with availability filtering applied. Provide a code example of a custom spec with probes. This is especially important since users may want to add local bridges (Qwen, Ollama) or override the registry via MCP_HUDDLE_SPAWN_REGISTRY env var.
- **Чем грозит:** Users publishing custom runners (local bridges) may not understand the probe/enable pattern and will deploy broken specs that are silently disabled. Documenting the pattern in the module docstring makes it discoverable.

---

## ⚪ ПОТОМ — мелочи и полировка  (16)


### 1. Hardcoded /tmp path for brief markdown files
- **Файл:** `src/mcp_huddle/server.py:751`  ·  **трудозатраты:** small  ·  _Portability & hardcoded local assumptions_
- **Проблема:** Line 751 hardcodes `/tmp/room-{room_id}-brief.md` as the location to write room briefs. While `/tmp` exists on both macOS and Linux, it is world-writable and subject to cleanup policies (e.g., systemd-tmpfiles deletes files in `/tmp` older than 10 days on many Linux distributions). A public user expecting persistent huddle state may find brief files disappearing unexpectedly. Additionally, this violates the XDG Base Directory specification (Linux standard for temp files should use `$XDG_RUNTIME_DIR` or `$TMPDIR`).
- **Что делать:** Use `tempfile.gettempdir()` or `pathlib.Path.tempdir()` for cross-platform compatibility. Better yet, store briefs in `$MCP_HUDDLE_HOME/rooms/<room_id>/brief.md` alongside other persistent room state (meta.json, messages.jsonl), since they are already guaranteed to exist and respect the user's chosen storage location.
- **Чем грозит:** On Linux systems with aggressive tmpfiles policies, users may lose brief files needed for dashboard display or debugging. Briefs should be persistent like other room state, but this implementation treats them as ephemeral.

### 2. Hook examples hardcode localhost curl endpoint
- **Файл:** `examples/hooks/session-end.sh:6`  ·  **трудозатраты:** small  ·  _Portability & hardcoded local assumptions_
- **Проблема:** The session-end hook (line 6) hardcodes `curl -s -X POST http://127.0.0.1:8014/mcp` to close rooms. This assumes the HTTP dashboard is running on localhost:8014 in HTTP mode. If a user runs huddle in stdio mode (the default), this curl will fail silently (the script redirects stderr to /dev/null). If a user configures huddle on a different host or port (e.g., behind a reverse proxy, or on a remote machine), the hook will not work. The hook is meant to be copy-pasted and customized, but the hardcoded endpoint is easy to miss.
- **Что делать:** Change the hook to use environment variables: `curl -s -X POST "${MCP_HUDDLE_ENDPOINT:-http://127.0.0.1:8014}/mcp"`, and document in the hook comment that users should set `MCP_HUDDLE_ENDPOINT` if running huddle on a non-default host/port. Alternatively, detect whether huddle is running in HTTP mode before making the curl, or have the Stop hook source a config file that the huddle HTTP process writes on startup.
- **Чем грозит:** Users who run huddle in stdio mode (the default) will have their Stop hooks silently fail to close rooms, leaving orphaned room state. Users who run huddle on non-localhost or non-8014 will also face silent failures. This is a silent data-integrity issue, not a crash.

### 3. Brief text written to /tmp with predictable path allows TOCTOU/symlink attacks
- **Файл:** `src/mcp_huddle/server.py:751-752`  ·  **трудозатраты:** small  ·  _File System Security_
- **Проблема:** When spawning agents, the brief (instructions for agents) is written to `/tmp/room-{room_id}-brief.md` without O_EXCL, umask, or symlink checks. An attacker can: (1) pre-create a symlink at that path pointing to any file on the system, (2) on room creation, the brief is written through the symlink, overwriting an arbitrary file, (3) if the agent CLI uses that brief file for logging or config, subsequent reads may be poisoned. Room IDs are UUID-based but generated at room_create time; an attacker observing a room creation can race to plant a symlink before the brief is written. The /tmp location is world-writable and accessible from any process on the system.
- **Что делать:** (1) Use `pathlib.Path.write_text(..., mode='x')` to fail if the file exists (no race), or (2) use `tempfile.NamedTemporaryFile()` which atomically creates a file with secure permissions (0o600) in /tmp and returns a fd, (3) or write to ~/.mcp-huddle/rooms/{room_id}/brief.md instead, which is in the secure huddle home. Option (3) is preferred since it keeps all room data together. The brief is not sensitive but the file permission/symlink attack surface should be closed. Update README if storing brief in the room dir.
- **Чем грозит:** On a multi-user system, an attacker can overwrite arbitrary files owned by the huddle user (e.g., config files, scripts) by creating symlinks in /tmp and timing room creation. This is a classic TOCTOU/symlink-following vulnerability.

### 4. TOCTOU race between file existence check and open
- **Файл:** `src/mcp_huddle/bus.py:136-144, 562-572`  ·  **трудозатраты:** trivial  ·  _Race Conditions_
- **Проблема:** list_rooms() iterates BUS_DIR and calls _read_meta(rdir.name) on each directory (lines 140–144). _read_meta() calls p.exists() (line 552), then json.loads(p.read_text()) (line 554). Between the exists() check and the read_text() call, another process could delete the meta.json file, causing FileNotFoundError to escape the except block (line 143 only catches Exception, which is broad, so this is actually caught). However, this is a TOCTOU race that is philosophically incorrect—the file may not exist at read time even though it existed at check time. This is usually not a problem for idempotent operations but complicates error handling.
- **Что делать:** Remove the exists() check or ignore the exception on FileNotFoundError (it's already caught as Exception). Use EAFP (Easier to Ask for Forgiveness than Permission) pattern: just try to read and catch FileNotFoundError.
- **Чем грозит:** Minimal. The exception is already caught, so no crash. However, it's a code-smell that indicates fragile error handling. In edge cases (e.g., a room directory is deleted mid-operation), the behavior is ambiguous.

### 5. Other environment variable parsing lacks validation
- **Файл:** `src/mcp_huddle/server.py:101,105-106,110`  ·  **трудозатраты:** small  ·  _Configuration robustness_
- **Проблема:** Multiple environment variables are parsed at module load time with int() or float() calls that lack error handling: IDLE_TIMEOUT_SECS, HUDDLE_RETENTION_DAYS, HUDDLE_RETENTION_SWEEP_SECS, MCP_HUDDLE_RATE_LIMIT_COOLDOWN_SEC. Invalid values (e.g., HUDDLE_RETENTION_DAYS=bad) cause unhandled ValueError during import.
- **Что делать:** Create a safe config loading function that validates each env var with try/except, logs a warning with the invalid value and the expected type, and falls back to the default. Example: `safe_int(name, default, env_vars)` that catches ValueError and prints a message.
- **Чем грозит:** Users with typos in environment variable values see cryptic import errors instead of helpful diagnostics. Difficult to debug.

### 6. MCP_HUDDLE_HOME creation is implicit; no confirmation or logging
- **Файл:** `src/mcp_huddle/bus.py:37-38`  ·  **трудозатраты:** trivial  ·  _First-run UX_
- **Проблема:** When the first room is created, ~/.mcp-huddle/rooms/ is silently created with mkdir(parents=True). There is no log message or warning to inform the user where data will be stored. New users may not realize a ~/.mcp-huddle directory now exists.
- **Что делать:** Add a one-time log message (e.g., print to stderr the first time HUDDLE_HOME is created) stating 'mcp-huddle storage at: {HUDDLE_HOME}'. Or document this clearly in the README's quickstart.
- **Чем грозит:** Users discover ~/.mcp-huddle directory exists only after exploring the filesystem; unclear initial setup footprint.

### 7. No import-time error feedback if mcp or starlette dependencies missing
- **Файл:** `src/mcp_huddle/server.py:16-18`  ·  **трудозатраты:** small  ·  _Dependency error handling_
- **Проблема:** The imports `from mcp.server.fastmcp import FastMCP` and `from starlette.requests import Request` happen at module load (server.py:16-18). If mcp or starlette are not installed, the ImportError is raised with a traceback, not a user-friendly message about missing dependencies.
- **Что делать:** Wrap imports in try/except and print a clear message like 'Error: mcp-huddle requires "mcp" and "starlette" packages. Install with: pip install mcp starlette' before re-raising. Or catch ImportError in __main__.py.
- **Чем грозит:** Users who manually pip-uninstall dependencies or run in an incomplete environment see cryptic import errors instead of clear guidance on what to install.

### 8. No explicit documentation of minimum supported Python version
- **Файл:** `pyproject.toml:11`  ·  **трудозатраты:** trivial  ·  _Installation clarity_
- **Проблема:** pyproject.toml declares `requires-python = ">=3.10"` but the README does not mention Python version requirements. Users with Python 3.8 or 3.9 will see a cryptic pip version error.
- **Что делать:** Add to the README's Stdio and HTTP mode sections: 'Requires Python 3.10+'. Also mention in the error message if uvx/pip fails due to Python version.
- **Чем грозит:** Users with older Python versions attempt installation and encounter unclear dependency resolution errors.

### 9. Missing static files configuration in pyproject.toml
- **Файл:** `pyproject.toml:45-46`  ·  **трудозатраты:** small  ·  _Packaging & publishing readiness_
- **Проблема:** The wheel build configuration only specifies `packages = ["src/mcp_huddle"]` without explicit data file inclusion. While hatchling's default behavior includes git-tracked files, this is implicit and fragile. The static files (dashboard.html, dashboard.css, dashboard.js in src/mcp_huddle/static/) are crucial for the HTTP/dashboard feature advertised in README and pyproject.toml description.
- **Что делать:** Add explicit package data configuration to pyproject.toml under `[tool.hatch.build.targets.wheel]`: \n```\ninclude = [\n  "src/mcp_huddle/static",\n]\n``` \nOr use the newer hatchling pattern with `[tool.hatch.build.targets.wheel.force-include]` to make it explicit that these files are required for runtime.
- **Чем грозит:** If static files are accidentally excluded during wheel build (e.g., due to git history rewrite or changed build process), the HTTP dashboard feature will silently fail with 404s, degrading the user experience without clear error messages.

### 10. Missing type stubs (py.typed marker) for public package
- **Файл:** `src/mcp_huddle/`  ·  **трудозатраты:** trivial  ·  _Packaging & publishing readiness_
- **Проблема:** The package uses TypedDict and type annotations throughout but does not include a `py.typed` marker file. Without this marker, tools like mypy and IDE type checkers will not recognize the package as providing inline type information, degrading the type-checking experience for downstream users.
- **Что делать:** Add an empty `src/mcp_huddle/py.typed` file and ensure it's included in the wheel (hatchling includes it by default if it's in the package directory). Add it to git: `touch src/mcp_huddle/py.typed && git add src/mcp_huddle/py.typed`. This signals PEP 561 compliance.
- **Чем грозит:** Type-aware users (using mypy, pyright, etc.) will not get IDE autocomplete or type-checking for mcp_huddle when imported, reducing code quality for downstream projects.

### 11. Not-yet-documented agent environment variables
- **Файл:** `src/mcp_huddle/spawn.py:178-227 (deleted in uncommitted changes)`  ·  **трудозатраты:** small  ·  _Documentation completeness & accuracy_
- **Проблема:** Deleted code shows that Qwen and DeepSeek spawning used environment variables like MCP_HUDDLE_QWEN_BASE_URL, MCP_HUDDLE_QWEN_MODEL, MCP_HUDDLE_DEEPSEEK_BASE_URL, MCP_HUDDLE_DEEPSEEK_MODEL with hardcoded defaults. Even though these agents are being removed, other agents may have similar configuration needs. No documentation explains how to customize agent-specific settings beyond the registry file.
- **Что делать:** Document how to extend the registry and customize per-agent command-line arguments. Provide examples for hypothetical new agents. This will help future maintainers and power users.
- **Чем грозит:** Low - only affects users who want to customize agent invocation beyond the default registry. However, lack of clarity makes extending the system difficult.

### 12. Stale phase-2.5 references in README and code
- **Файл:** `README.md:126-127, src/mcp_huddle/acp.py:entire file`  ·  **трудозатраты:** small  ·  _Documentation completeness & accuracy_
- **Проблема:** README mentions 'until the ACP daemon integration in src/mcp_huddle/acp.py is implemented' as a future feature. However, acp.py is a stub that raises NotImplementedError. Users looking to understand the roadmap will find references to incomplete/blocked features without clear status or timeline. The stub file signals 'Phase 2.5' which is not explained anywhere.
- **Что делать:** Either: (1) Remove the acp.py reference from README and document why Codex resume is preferred (it works, ACP is experimental), or (2) Add a 'Roadmap' section to README documenting planned phases and their status. Clarify that Phase 2.5 (Gemini ACP) is not on the immediate roadmap.
- **Чем грозит:** Users may waste time trying to figure out how to enable the ACP feature or misunderstand the current feature set. Low-severity because ACP is only mentioned once and not critical for basic usage.

### 13. Missing explicit agent-loop requirements and anti-loop depth
- **Файл:** `README.md:101-118, src/mcp_huddle/server.py:58-60`  ·  **трудозатраты:** small  ·  _Documentation completeness & accuracy_
- **Проблема:** README's 'Agent loop discipline' section explains the protocol but doesn't specify the circuit-breaker threshold. The instructions say 'server has a circuit breaker that hard-blocks >5 messages-in-a-row' but this limit is not documented elsewhere. If an agent ignores these rules, the error message will reference a limit the developer never read. Also no clear guidance on what happens when an agent hits the limit (request rejected? error returned?).
- **Что делать:** Add to 'Agent loop discipline': 'Circuit breaker: if an agent sends more than 5 consecutive messages without receiving a new request, further messages are rejected with HTTP 429. The agent will receive an error message indicating the circuit breaker is active.' Also document that this is a safety mechanism to prevent runaway loops and may indicate a bug in agent logic.
- **Чем грозит:** Agents that violate loop discipline will fail mysteriously with unhelpful errors. Developers will not know where the limit comes from or how to fix it.

### 14. LICENSE mention in README is minimal
- **Файл:** `README.md:last section, LICENSE file`  ·  **трудозатраты:** trivial  ·  _Documentation completeness & accuracy_
- **Проблема:** README's License section is a single line: 'MIT — see [LICENSE](LICENSE).' The LICENSE file exists and is correct MIT, but README provides no summary. For a public project, more visibility on license terms is standard.
- **Что делать:** Optionally add license badges (uncommented section exists but is commented out). Ensure LICENSE file is included in source distribution by checking setup.py/pyproject.toml includes_license or license_file settings. Current setup is acceptable but could be improved.
- **Чем грозит:** Low - the LICENSE file is clear and linked. Users can find it. But adding a badge makes license terms more discoverable.

### 15. Remove unused VALID_STATUSES constant
- **Файл:** `src/mcp_huddle/bus.py:25`  ·  **трудозатраты:** trivial  ·  _Code simplification & maintainability_
- **Проблема:** VALID_STATUSES = {"open", "idle", "closing_requested", "closed", "resolved"} is defined in bus.py:25 but never used in the codebase. It was likely defined as defensive documentation or validation, but the actual validation of room status is done inline (e.g., meta["status"] in ("open", "idle") in multiple places, or hardcoded checks like if meta["status"] == "closed"). Leaving it unused creates maintenance debt: future developers may think it is an important validation point and add it to their status checks, or conversely, delete it not realizing it documents the stable status enum.
- **Что делать:** Delete the VALID_STATUSES constant (line 25 in bus.py). If status validation is desired, add a dedicated validate_status(status: str) function that references an inline constant/set and is explicitly called in post_message and other entry points. Alternatively, if this is intentionally kept for documentation, add a module-level comment explaining why it exists and is unused, or use it in a docstring.
- **Чем грозит:** Low: this constant is not part of the public API (underscore-less and not exported). Removing it clarifies that status checks are inline and intentional. Having dead constants clutters the codebase for external readers.

### 16. Simplify _wake_agents_for_request with extracted helpers
- **Файл:** `src/mcp_huddle/server.py:1060-1164`  ·  **трудозатраты:** medium  ·  _Code simplification & maintainability_
- **Проблема:** _wake_agents_for_request is 105 lines with deeply nested conditionals: 4 levels of nesting in the agent loop check _wake_in_progress, _agent_in_rate_limit_cooldown, _agent_replied_to_request in sequence. The Codex resume path (lines 1115-1147) and registry-agent fresh spawn path (lines 1149-1163) are conditionally branched. The pre-lock validity checks and post-lock updates span 40+ lines. A new contributor adding a third agent type (e.g., streaming-capable agent with session affinity like the ACP stub hints at) must understand the entire lock-acquire → Codex-specific → generic fallback → status-merge → metadata-write dance. The function docstring is detailed but the code does not reflect its structure.
- **Что делать:** Extract Codex-specific logic into _wake_codex(room_id, agent_name, ...) -> dict and registry-agent logic into _wake_registry_agent(room_id, agent_name, ...) -> dict, each handling its own subprocess start, status update, and metadata merge. This leaves _wake_agents_for_request as a clean loop: for agent_name in agent_meta: if should_wake(info, status, rate_limit): wakes.append(_wake_codex(...) or _wake_registry_agent(...)). This also unblocks Phase 2.5 ACP daemon integration: a new _wake_acp_agent(...) function can be plugged in alongside Codex resume without refactoring the outer loop.
- **Чем грозит:** Contributors extending the agent wake mechanism (e.g., for ACP, streaming, or custom runners) must carefully modify the 105-line function without breaking the lock invariants and status-lease mechanics. Extracted functions with single responsibilities allow safe extension.

---

## Исходный executive summary (от агента-синтезатора)

```
# mcp-huddle Release Readiness — Executive Summary

## Verdict: NOT safe to publish as-is. Minimum bar is ~1 day of fixes.

There is exactly one hard install-time blocker plus a cluster of high-severity issues that will break real users on day one (Linux/Windows crashes, an unauthenticated destructive HTTP API, and documentation that actively lies about what ships). None are deep — most are trivial-to-small — but several are user-facing on first contact, which is the worst place to ship a bug for a brand-new public package. Fix the blocker + the 5 items below, then publish.

---

## BLOCKER — must fix before any publish

**1. Python 3.10 import failure (`pyproject.toml:11` vs `spawn.py:42`).** You declare `requires-python = ">=3.10"` and list 3.10 in classifiers, but `spawn.py` imports `NotRequired` from `typing`, which is 3.11+. Anyone on 3.10 (still widely used) gets an `ImportError` on import — the package is simply broken for them. Trivial fix: bump to `>=3.11` and drop the 3.10 classifier (simplest), or use `typing_extensions`. This is the one item that makes the wheel non-functional on a supported interpreter.

---

## HIGH — fix before going public (these define the minimum bar)

**2. Cross-platform crashes contradict "OS Independent" claim (`mimo_runner.py:33`, `spawn.py:80-101`).** `mimo_runner.py` hardcodes `/private/tmp/mimo-runner-home`; `/private/tmp` doesn't exist on Linux, so `os.makedirs` throws `FileNotFoundError` and MiMo dies on every Linux invocation. The same path is Unix-only and breaks Windows entirely, yet `pyproject.toml` advertises `Operating System :: OS Independent`. Use `tempfile.gettempdir()`. Either fix the paths or stop claiming OS independence — shipping both together is a trust problem.

**3. Unauthenticated destructive HTTP API (`server.py:561-614`).** `/api/room_close`, `/api/room_delete`, `/api/rooms_close_all`, and `/api/rooms_nuke` perform destructive bulk operations with zero authentication; the `owner` field on close/delete is accepted but never validated against `meta.owner` (the correct pattern already exists in `room_invite`). `/api/rooms` leaks every room id unauthenticated, so an attacker has everything needed. Localhost binding mitigates remote attack but not shared/multi-user machines or CI runners. Minimum: enforce the owner check that already exists elsewhere, and either gate destructive endpoints behind a token (`MCP_HUDDLE_API_TOKEN`) or document loudly that the HTTP dashboard is single-user-only. This is the most serious security item.

**4. Documentation describes software that doesn't exist.** Three concrete, embarrassing mismatches that every first user hits: (a) README claims **15 MCP tools** but only **10** are decorated with `@mcp.tool()` — the 5 lifecycle/status tools were intentionally made human-only, so the README, not the code, is wrong; (b) README still advertises **Qwen and DeepSeek** in the default registry, but your uncommitted `spawn.py` change removes them (commit that change and update the README together); (c) README's "not on PyPI yet — use `uvx --from git+...`" instructions become wrong the moment you publish. Fix the counts, the registry list, and the install section before the package is live.

**5. Env-var parsing crashes the server at import (`server.py:101,105,106,110`; `__main__.py:23`).** `int(os.environ.get(...))` / `float(...)` run at module load with no guard. A single typo in the documented `PORT` (or any tuning var) crashes the whole server with a raw traceback before it can start. Wrap conversions in try/except with a clear "must be an integer" message. Trivial, but `PORT` is documented so users will hit it.

**6. Concurrent meta clobber loses wake state (`server.py:826-830`).** `_save_spawn_meta` does `m["agent_meta"] = agent_meta`, overwriting the entire dict; if a reaper callback fires (agent crash, rate-limit) during the spawn window, the just-recorded wake/cooldown state is wiped, leaving the room stuck with unanswered requests. The sibling `_merge_agent_meta` already uses `.update()` — do the same here. Small fix, but it silently corrupts the core wake machinery, which is the product's whole point.

---

## MEDIUM — safe to ship now, fix shortly after launch

These are real but don't break first use or expose serious risk:

- **Security hardening & transparency:** add a README "Security & Architecture" section covering `--dangerously-skip-permissions` auto-spawn, env-var inheritance into subprocesses (`spawn.py:448`), world-readable `~/.mcp-huddle` under default umask, and the unauthenticated SSE log stream. Documentation is sufficient for launch; code-level scrubbing/`mode=0o700` can follow.
- **DoS surface:** no global rate limit / per-room disk quota — circuit breaker is bypassable by alternating message kinds. Real, but requires a misbehaving local agent; add quotas post-launch.
- **Robustness polish:** corrupted-JSON handling (`bus.py:550,569`), `post_message` TOCTOU `FileNotFoundError` on concurrent delete, unbounded `_msg_cache` growth, unjoined daemon reaper threads, watchdog without I/O timeouts, fragile Codex `thread_id` parsing. All edge-case, none data-destroying.
- **CLI/UX:** missing `--help`/`--version`, premature dashboard-URL print, friendlier errors for malformed `MCP_HUDDLE_SPAWN_REGISTRY`.
- **Docs/packaging hygiene:** create the missing `examples/registry.json` (referenced but absent), document the 7 hidden env vars, add CHANGELOG / CONTRIBUTING / Troubleshooting, add `py.typed`, pin static files explicitly in the wheel, add module/function docstrings.
- **Customization footgun:** hardcoded `agent_name == "Codex"` thread-resume checks silently disable resume if the slot is renamed — worth a `thread_resumable` flag eventually, but only affects registry customizers.

## Bottom line
Ship after fixing items 1-6. The blocker (1) is non-negotiable; 2-4 are what an early adopter will hit and screenshot; 5-6 are cheap and prevent crashes/corruption. Everything in MEDIUM is legitimate but appropriate as fast-follow once the package is public, ideally tracked as issues so the CHANGELOG/CONTRIBUTING gaps don't compound.
```
