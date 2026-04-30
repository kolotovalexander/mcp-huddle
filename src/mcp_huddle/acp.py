"""Phase 2.5 stub: Gemini ACP (Agent Client Protocol) daemon integration.

ACP is JSON-RPC 2.0 over stdio. Spec: https://geminicli.com/docs/cli/acp-mode/

INTENT (when implemented):
  * Spawn `gemini --acp` once as a long-running daemon (one process per Gemini
    identity, multiplexed across many rooms)
  * Manage a session per room via `newSession` / `loadSession`
  * On respond_via_agent for Gemini: send `prompt` JSON-RPC to existing daemon
    instead of forking a new `gemini -p` process
  * Stream events from daemon stdout back to per-room agent log files (the
    existing SSE endpoint will pick them up unchanged)

EXPECTED BENEFITS:
  * 0ms cold start vs ~3-5s for fresh `gemini -p`
  * Persistent per-room context without prepending huge digests
  * Native streaming events (typing indicators, tool calls, partial responses)

WHY THIS IS A STUB:
  * ACP requires async lifecycle (start daemon, monitor health, restart on
    crash, correlate JSON-RPC ids ↔ pending awaits)
  * Spec details (auth flow, capabilities negotiation, session mode/model
    overrides) need careful implementation + integration testing
  * Risk of half-broken implementation is high; Phase 1 + Phase 2 (Codex
    resume) already deliver the main value.

NEXT STEPS for whoever picks this up:
  1. Implement `class AcpClient` with async send/recv JSON-RPC over stdio
  2. Add `initialize`, `newSession`, `prompt`, `cancel` method wrappers
  3. Module-level singleton: `get_gemini_daemon()` that lazy-spawns the daemon
  4. In server.respond_via_agent for Gemini: route to AcpClient.prompt()
  5. Wire daemon stdout into agent log file (so SSE picks it up)
  6. Lifecycle: tear down daemon on huddle server shutdown (lifespan context)
  7. Tests: mock `gemini --acp` with a fake JSON-RPC responder

Reference implementations:
  * Zed: https://zed.dev/acp/agent/gemini-cli
  * IntelliJ: https://glaforge.dev/posts/2026/02/01/...
  * Spec: https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/acp-mode.md
"""

class AcpNotImplemented(NotImplementedError):
    """Raised when Phase 2.5 ACP integration is not yet wired up."""
    pass


def gemini_acp_prompt(session_id: str | None, prompt: str) -> str:
    """Stub for Phase 2.5: send a prompt to Gemini --acp daemon.

    Currently raises AcpNotImplemented. Use respond_via_agent for Codex
    (Phase 2 Codex resume works) or a fresh `gemini -p` spawn.
    """
    raise AcpNotImplemented(
        "Gemini --acp daemon integration is Phase 2.5 (not yet implemented). "
        "Use respond_via_agent with agent_name='Codex' for resume support, "
        "or auto_spawn={'Gemini': '<fresh brief>'} for a new process."
    )
