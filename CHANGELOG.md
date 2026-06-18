# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Read-only discussant agents by default** (`MCP_HUDDLE_READONLY`, default ON;
  set `=0` for full-access workers). Spawned agents read files/web/docs/rules/
  memory but cannot edit/write — they participate only via the huddle MCP tools.
  Claude uses an allow/deny tool list; Codex uses `-s read-only` plus
  auto-approved huddle MCP tools (verified: read-only sandbox + MCP works once
  the MCP approval mode is `approve`).
- `mcp-huddle --install-hooks [DIR]` copies the bundled Claude Code hooks and
  prints the `settings.json` wiring.
- Dashboard: 3 skins (Glass/Web/Code), 5 terminal palettes, 10-language i18n,
  a single ⚙️ settings popover (theme × design × palette × language) with `?`
  help tooltips, an MCP-connection section, an env-vars/spawn-rules reference,
  and a copyable agent-setup prompt.
- Room tree regrouped: project → date → organizer → numbered chats.
- Resizable + collapsible panels (collapse to a 48px rail with an expand
  button), and a narrow-window overlay mode (chat full-width, side panels open
  as opaque drawers over the chat via the ◧/◨ buttons).

### Changed

- Antigravity (`agy`) is now opt-in (`MCP_HUDDLE_ANTIGRAVITY_ENABLED=1`,
  default OFF): it needs a prior interactive `agy` login (headless can't sign
  in) and exposes no read-only flag.
- MiMo already runs in a temp dir (never touches the project), so it is
  effectively read-only with respect to your files.

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

[Unreleased]: https://github.com/kolotovalexander/mcp-huddle/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/kolotovalexander/mcp-huddle/releases/tag/v0.2.0
[0.1.2]: https://github.com/kolotovalexander/mcp-huddle/releases/tag/v0.1.2
