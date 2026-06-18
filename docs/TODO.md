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
  `agy` has no read-only flag and needs interactive login (now opt-in, default
  off). MiMo already runs in a temp dir (no project writes). Revisit if `agy`
  gains a read-only/permission flag.

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
