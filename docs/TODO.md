# mcp-huddle — TODO / roadmap

Open items, roughly by priority.

## Distribution and multi-device follow-up

- [ ] **Cloud coordinator + per-device executors.** Keep room history and SQLite on one server; authenticated local executors own native sessions and project folders. Support local and cloud clients, offline status, durable job claims and deduplicated delivery. Start with private networking; external HTTPS requires an explicit authentication design. Preserve native-first routing. This is future work, not implemented cloud support.
- [ ] Persistent dashboard service installation for macOS/Linux and an explicit uninstall/restore command. Current setup starts a detached server only; reboot requires another start.
- [ ] Verified MCP adapters for additional harness versions; report unsupported integrations rather than inventing keys. Native Windows requires replacement of POSIX file locking.

## Spawn / agents
- [ ] **Interactive agent sessions (option).** Today every turn is a fresh
  one-shot subprocess (`cd <project> && codex exec / claude -p "<brief>"`),
  re-spawned per addressed message; Codex keeps continuity via `codex resume`.
  Explore an opt-in *interactive* mode: keep a long-lived agent process in a PTY
  and feed it messages, instead of respawning. Trade-offs: live cross-turn
  memory vs. PTY lifecycle management, output capture, process-leak/crash
  isolation. Should be opt-in, not the default (one-shot is simpler + crash-safe).
- [ ] **Read-only for `agy` / MiMo.** Read-only is enforced for Claude + Codex.
  `agy` has no read-only flag (now opt-in, logged-in + huddle-MCP-wired here).
  MiMo runs in a temp dir (no project writes).
- [ ] **MiMo: give it project read access?** Tested 2026-06-19 on MiMo
  `0.1.1-preview.1`: running `mimo run` in the project dir did NOT hang (the old
  0.1.x project-scan hang did not reproduce), so the temp-cwd workaround may be
  removable — BUT MiMo's free provider currently returns `403 Illegal access`
  (same in temp and project cwd), so MiMo is non-functional regardless right
  now. Revisit (and consider letting MiMo read the project read-only) once the
  free provider works; consider defaulting MiMo OFF until then.

## Reliability / recovery
- [ ] **Detect arbitrary in-place rewrites of agent-event logs.** SSE resume
  currently validates bounded sentinels at the file start and immediately
  before the acknowledged cursor. That covers normal append, rotation and
  truncate/regrow recovery, but a deliberate same-inode rewrite confined to
  the untouched middle can evade the bounded check. Keep append-only writers
  as the supported contract or add a per-record integrity chain.
- [ ] **Explicit recovery for an unowned wake lease.** A persisted lease whose
  owning Huddle server crashed is deliberately fail-closed: another process
  cannot safely infer ownership from a PID or signal it. Add a human-confirmed
  recovery operation that verifies room state and clears the lease without
  reintroducing PID-reuse kills or duplicate spawns.
- [ ] **Recover rooms stranded in `closing`.** The close transition is
  fail-closed and idempotent, but a process crash between the `closing` claim
  and terminal marker can leave the room unavailable. Add an explicit,
  auditable takeover/recovery path.

## From the release audit
- [x] Rate-limit `message_post` beyond the existing circuit breaker.
- [x] Authenticate dashboard fetch/SSE and all HTTP/MCP data endpoints when
  `MCP_HUDDLE_TOKEN` is set.
- [x] Scrub spawned-agent environments and require explicit `pass_env` names
  (the runner's `--api-key-env` opts in its named key automatically).
- [ ] De-duplicate the runner logic (`openai_compatible_runner` / `mimo_runner`).

## Nice-to-have
- [ ] Translate the dashboard env-var/spawn-rules strings into the 8 non-en/ru
  locales (currently fall back to English).
- [x] Make `test_spawn_agent_verify_alive_rejects_confirmed_exit` deterministic
  (the exit state and health-check boundary are synchronized with fakes; real
  spawn/log/reaper coverage remains in separate tests).
