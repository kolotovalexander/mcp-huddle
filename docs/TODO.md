# mcp-huddle — TODO / roadmap

Open items, roughly by priority. See `docs/RELEASE_AUDIT.md` for the full audit.

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

## From the release audit (selected, not yet done)
- [ ] Rate-limit `message_post` beyond the existing circuit breaker.
- [ ] Dashboard fetches should send `MCP_HUDDLE_TOKEN` when it is set.
- [ ] Scrub sensitive env vars from spawned agent processes.
- [ ] De-duplicate the runner logic (`openai_compatible_runner` / `mimo_runner`).

## Nice-to-have
- [ ] Translate the dashboard env-var/spawn-rules strings into the 8 non-en/ru
  locales (currently fall back to English).
- [ ] Make `test_spawn_agent_verify_alive_rejects_fast_exit` deterministic
  (timing-sensitive; can flake under heavy CI load).
