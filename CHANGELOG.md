# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- CI on Python 3.11–3.14, including the declared dependency floors, dashboard
  syntax, dependency consistency, source compilation, CLI version and wheel
  build checks. Third-party GitHub Actions are pinned to immutable commits.
- Persisted per-room write admission (120 messages per 60 seconds), a 256 KiB
  body cap and a 320 KiB complete serialized-message cap.
- Process-local exact-child ownership plus persisted, atomic wake claims for
  safe coordination between simultaneous stdio and HTTP server instances.
- Dashboard token entry and authenticated fetch-based live event streams with
  reconnect support. The raw token is exchanged once and retained only as a
  process-local derived credential in page memory.

### Changed

- Releases starting with v0.7.0 are licensed under PolyForm Noncommercial
  1.0.0. Commercial use requires a separate written license from Alexander
  Kolotov. Previously
  released versions through v0.6.0 remain under the MIT License.
- Runtime minimums now stay on patched compatible lines: MCP SDK 1.28.1+,
  Starlette 1.0.1+, Uvicorn 0.32+, with upper bounds for their next breaking
  major releases.
- Spawned processes receive a small non-secret environment allowlist. Registry
  entries opt in additional names with `pass_env`; `--api-key-env` opts in only
  the named provider key. Fixed subscription/direct-review profiles fail closed
  when their authentication contract is not satisfied.
- Notification files are confined to direct children of
  `$MCP_HUDDLE_HOME/notifications/`; hook consumers atomically claim them before
  reading.
- Persisted PIDs are diagnostic only. Huddle signals only exact `Popen` objects
  owned by the current server; foreign/unknown leases stay occupied and live
  owners cooperatively stop their own children when room state becomes terminal.

### Fixed

- Direct and symlink path traversal, final-component swaps and unsafe file
  opens across room metadata, messages, locks, agent logs and notifications.
- Concurrent post admission, close/delete/retention transitions, late child
  registration and bulk-close lifecycle races. Terminal rooms now stop owned
  children before their data is removed, and a failed close side effect no
  longer leaves an otherwise recoverable room stranded in `closing`.
- Zombie cleanup now revalidates owner PID, session, lifecycle and activity in
  the atomic close claim, so a concurrent room reclaim or fresh message cannot
  be closed from a stale watchdog snapshot.
- Duplicate agent launches from simultaneous server instances, from requests
  arriving during initial auto-spawn, and after a stuck foreign wake warning.
- HTTP access is default-deny outside the dashboard assets and token exchange;
  loopback Host/Origin, body type/size, root-path and malformed credential cases
  fail closed. Authentication covers REST, SSE and MCP transport data/actions.
- Agent-event SSE refuses symlinked room/log paths and reconnects after
  transient EOF/network failures without replaying already displayed events.
- API and MiMo runners publish only validated non-empty terminal results; error
  messages no longer echo provider payloads or secret values.
- Spawn setup failures stop and reap the exact new child or retain an exact
  fallback reaper; standalone logs reject traversal, symlinks and hardlinks.
- Bundled notification hooks recover abandoned bounded claims without deleting
  a live or concurrently replaced notification. The SessionEnd hook now
  consumes the caller's session id only for a verified SessionEnd event, safely
  bounds delayed or partial stdin and the complete loopback request, supports
  authenticated HTTP, and no longer depends on a shared predictable `/tmp`
  file or a non-exported MCP tool.
- `mcp-huddle --version` reports the checked-out source version in editable
  development trees instead of stale installed-package metadata.

## [0.6.0] - 2026-08-01

### Added

- Persisted agent lifecycle reporting through the new `status_set` and
  `room_status` MCP tools, including terminal states and wait guidance for
  organizers.
- An evidence/rubric contract across every built-in spawn prompt and runner:
  verifiable factual claims should cite a URL, `file:line`, test result, or room
  message ID; unsupported claims must be marked as inference or unknown. This
  is prompt-level enforcement and does not automatically validate links.

### Changed

- The watchdog now runs in stdio mode as well as HTTP mode. Stuck wake
  processes are terminated by default after their configured timeout; set
  `MCP_HUDDLE_STUCK_KILL=0` for announce-only behavior.
- Auto-spawn uses a curated roster, staggers same-binary launches, and checks
  room state again before a delayed spawn fires.
- OpenCode is a built-in but opt-in slot
  (`MCP_HUDDLE_OPENCODE_ENABLED=1`) that uses the configured default model and
  has a bounded timeout.
- Completion waits distinguish substantive `result` messages from lifecycle
  notices and stop cleanly on terminal agent states.

### Fixed

- Initial spawn failures, clean no-reply exits, and idle-room pending wakes are
  surfaced instead of leaving organizers waiting silently.
- `auto: false` requests, delayed-spawn process tracking, and terminal wait
  handling no longer leave stale or misleading room state.
- Invalid, zero, or negative `MCP_HUDDLE_OPENCODE_TIMEOUT_SEC` values fall back
  safely instead of crashing import or disabling the timeout.
- The MCP SDK dependency stays on the compatible `1.x` line until huddle is
  migrated to the breaking `2.x` server API.

## [0.3.0] - 2026-06-19

### Added

- **Read-only discussant agents by default** (`MCP_HUDDLE_READONLY`, default ON;
  set `=0` for full-access workers). Spawned agents read files/web/docs/rules/
  memory but cannot edit/write — they participate only via the huddle MCP tools.
  Claude uses an allow/deny tool list; Codex uses `-s read-only` plus
  auto-approved huddle MCP tools (verified: read-only sandbox + MCP works once
  the MCP approval mode is `approve`).
- **Cloud-API agents**: `openai_compatible_runner --api-key-env` lets any
  OpenAI-compatible cloud API (OpenAI/OpenRouter/vLLM/proxied Anthropic) join as
  a read-only discussant via a registry entry — no CLI, no MCP on the agent side.
- **Paste-a-prompt onboarding** (`docs/ONBOARDING.md`): fill in which agents you
  use (CLI or cloud API) and your AI agent installs huddle, registers the MCP
  server, installs hooks, and writes `~/.mcp-huddle/registry.json`.
- `mcp-huddle --install-hooks [DIR]` copies the bundled Claude Code hooks and
  prints the `settings.json` wiring.
- Optional on-disk registry `~/.mcp-huddle/registry.json` (merged with defaults;
  precedence env > file > defaults) + a startup agent-discovery summary.
- Endpoint auth: `_require_local` enforces loopback on mutating HTTP endpoints +
  SSE, with an optional `MCP_HUDDLE_TOKEN` bearer (no-op when unset).
- Dashboard: 3 skins (Glass/Web/Code), 5 terminal palettes, 10-language i18n
  (incl. Arabic RTL), a single ⚙️ settings popover (theme × design × palette ×
  language) with `?` help tooltips, an MCP-connection section, an env-vars/
  spawn-rules reference, and a copyable agent-setup prompt.
- Room tree regrouped: project → date → organizer → numbered chats.
- Resizable + collapsible panels (collapse to a 48px rail with an expand
  button), and a narrow-window overlay mode (chat full-width, side panels open
  as opaque drawers over the chat via the ◧/◨ buttons).
- `CLAUDE.md` contributor/agent guide; README hero + theme/language gallery.
- PEP 561 `py.typed`; `--help` / `--version` CLI.

### Changed

- `requires-python` raised to `>=3.11` (the code uses `typing.NotRequired`).
- Antigravity (`agy`) is now opt-in (`MCP_HUDDLE_ANTIGRAVITY_ENABLED=1`,
  default OFF): it needs a prior interactive `agy` login (headless can't sign
  in) and exposes no read-only flag. MiMo runs in a temp dir (never touches the
  project), so it is effectively read-only with respect to your files.

### Fixed

- Portability: removed macOS-only hardcoded paths (`mimo_runner` temp dir →
  `tempfile`; agent binaries resolved via `shutil.which`).
- `server.py`: safe env-int parsing, `spawned_pids` merge under the meta lock,
  `tempfile` brief (closes a `/tmp` TOCTOU), centralized Codex thread-resume.
- `bus.py`: corrupt-JSON resilience, `0700` data-dir perms, bounded message cache.
- MiMo runner validates output so a provider error (e.g. 403) is never posted as
  a reply. Palette × light-theme clash fixed (palettes force dark structure).

## [0.2.0] - 2026-06-18

### Added

- Liquid Glass web dashboard with light/dark themes, agent avatars, kind badges,
  and reply-to quotes.
- Configurable auto-spawn registry via `MCP_HUDDLE_SPAWN_REGISTRY` (JSON file);
  see `examples/registry.json`.
- Codex wake-up loop: follow-up `kind=request` messages resume the same captured
  Codex thread via `codex exec resume`.
- MiMo Code advisor slot (toggle with `MCP_HUDDLE_MIMO_ENABLED`).
- Watchdog that auto-closes rooms whose owner process died, plus retention sweep
  for terminal rooms (`HUDDLE_RETENTION_DAYS` / `HUDDLE_RETENTION_SWEEP_SECS`).
- Rate-limit / usage-limit detection with cooldown and an in-room notice instead
  of a silent agent death (`MCP_HUDDLE_RATE_LIMIT_COOLDOWN_SEC`).
- `CONTRIBUTING.md`, this `CHANGELOG.md`, and an expanded README (configuration
  reference, security note, troubleshooting, and architecture overview).

### Changed

- Default spawn registry is now Codex, Antigravity, MiMo, and Claude. Claude is
  opt-in and OFF by default (`MCP_HUDDLE_CLAUDE_ENABLED=1` to enable) because
  headless `claude -p` is metered.
- The HTTP dashboard binds to `127.0.0.1` only.
- Crash-safe lock release; malformed request bodies now return `400` instead of
  `500`.

### Removed

- Retired the local Qwen and DeepSeek advisor slots and the reverse-API
  browser-session bridges they fronted.
- Retired the Gemini CLI slot; the Google-model advisor now runs exclusively on
  Antigravity (`agy`).

## [0.1.2]

### Added

- Initial public-ish release: FastMCP server with persistent JSONL rooms,
  10 MCP tools, anti-loop guards, consensus (propose/vote), and stdio + HTTP
  transports.

[Unreleased]: https://github.com/kolotovalexander/mcp-huddle/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/kolotovalexander/mcp-huddle/compare/v0.5.0...v0.6.0
[0.3.0]: https://github.com/kolotovalexander/mcp-huddle/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/kolotovalexander/mcp-huddle/releases/tag/v0.2.0
[0.1.2]: https://github.com/kolotovalexander/mcp-huddle/releases/tag/v0.1.2
