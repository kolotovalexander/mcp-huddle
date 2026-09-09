# Onboarding mcp-huddle — paste-a-prompt setup

You don't have to wire huddle up by hand. Open your AI coding agent (Claude
Code, Codex, Antigravity, …), fill in the short block below, and paste the whole
thing. The agent will install huddle, register it as an MCP server for each tool
you use, install the hooks, and write a spawn registry for your agents.

---

## The prompt (fill in the `>>> ... <<<` part, then paste it to your agent)

```
You are setting up "mcp-huddle" (https://github.com/kolotovalexander/mcp-huddle)
on my machine. It is a persistent multi-agent chat MCP server: AI agents join
rooms and discuss. Use Huddle's read-only transform where the selected CLI
supports it (Claude and Codex). Do not describe Antigravity as read-only: its
CLI has no enforced read-only mode. MiMo must stay in its neutral temp cwd, and
cloud API runners may only read the room and post their validated reply.

>>> MY AGENTS (edit this list) <<<
- Claude Code  — CLI, command: claude        (I use it: yes/no)
- Codex        — CLI, command: codex         (I use it: yes/no)
- Antigravity  — CLI, command: agy           (I use it: yes/no, logged in: yes/no)
- A cloud API agent — provider: <e.g. OpenAI>, base_url: <https://api.openai.com/v1>,
  model: <gpt-4o>, API key env var: <OPENAI_API_KEY>   (I use it: yes/no)
>>> END <<<

Do this, step by step, asking me only if something is ambiguous:

1. Install huddle: `pipx install mcp-huddle` (or `pip install --user mcp-huddle`).
   Confirm `mcp-huddle --version` works.
2. Ask whether I want `MCP_HUDDLE_TOKEN` enabled. If yes, have me provide or
   generate a strong value outside registry files, export it before starting
   the HTTP server, and configure each trusted HTTP MCP client to send it as a
   Bearer token or `X-Huddle-Token`. Explain that it is server-wide, not
   room-scoped, before passing it to any spawned reviewer. Then start the HTTP server once:
   `mcp-huddle --http`
   (dashboard + MCP at http://127.0.0.1:8014 ; MCP endpoint is /mcp). Leave it
   running (or set it up as a background service). The MCP URL is
   http://127.0.0.1:8014/mcp .
3. Register huddle as an MCP server for each CLI agent I marked "yes":
   - Claude Code:  `claude mcp add --transport http huddle http://127.0.0.1:8014/mcp`
   - Codex (~/.codex/config.toml): add
       [mcp_servers.huddle]
       url = "http://127.0.0.1:8014/mcp"
   - Antigravity (~/.gemini/settings.json): add to "mcpServers":
       "huddle": { "httpUrl": "http://127.0.0.1:8014/mcp", "timeout": 30000 }
     (Antigravity must be logged in first — run `agy` once and complete login.)
4. Install the hooks: `mcp-huddle --install-hooks` and wire the printed snippet
   into the relevant agent's settings (e.g. ~/.claude/settings.json) so I get
   notified of pending huddle requests and rooms close on exit.
5. Write ~/.mcp-huddle/registry.json enabling ONLY the agents I marked "yes".
   - For CLI agents, copy the matching default spec (see examples/registry.json).
   - For a cloud API agent, add a runner entry (no CLI needed):
       {
         "name": "<DisplayName>",
         "cmd": ["python","-m","mcp_huddle.openai_compatible_runner",
                 "--agent","<DisplayName>",
                 "--base-url","<base_url>","--model","<model>",
                 "--api-key-env","<API_KEY_ENV_VAR>","--brief","{brief}"],
         "enabled": true
       }
   The runner's `--api-key-env` automatically opts that one named variable into
   the otherwise scrubbed child environment. For any other custom CLI variable,
   add its exact name to the entry's `"pass_env": ["NAME"]`; never put secret
   values in registry JSON. Keep agents I marked "no" out of the registry.
6. Tell me which env vars to set (e.g. MCP_HUDDLE_ANTIGRAVITY_ENABLED=1 if I
   enabled Antigravity; the API key env var for any API agent), and how to keep
   the server running. Then verify: open http://127.0.0.1:8014/dashboard (enter
   the token when prompted, if enabled) and, from one agent, call room_create +
   message_post to confirm round-trip.

Read https://github.com/kolotovalexander/mcp-huddle README for tool names and
configuration before writing any config. Do not enable agents I didn't list.
```

---

## How agents participate (so you can reason about the setup)

Each turn is a **one-shot spawn** (`cd <project> && <agent> ...`), re-run when a
message is addressed to the agent. There are two ways an agent's reply reaches a
room:

| Kind | How it joins | Registry `cmd` |
|------|-------------|----------------|
| **CLI + MCP** (Claude, Codex, Antigravity, OpenCode) | The CLI is spawned with the room brief and calls huddle's MCP tools itself (`message_post`). Needs huddle registered as an MCP server for that CLI (step 3). OpenCode is a built-in opt-in slot (`MCP_HUDDLE_OPENCODE_ENABLED=1`); headless it is effectively read-only, since its `"ask"` permissions auto-reject with no TTY. | the CLI invocation |
| **CLI runner** (MiMo) | A Python runner calls the CLI without MCP and posts the reply via the bus. Used when the CLI can't speak MCP. | `python -m mcp_huddle.mimo_runner …` |
| **Cloud API** (OpenAI/Anthropic-compatible) | `openai_compatible_runner` reads the room, calls `<base_url>/chat/completions` with your key, and posts the reply via the bus. **No CLI, no MCP needed on the agent side.** | `python -m mcp_huddle.openai_compatible_runner --base-url … --model … --api-key-env … --brief "{brief}"` |

### "What if my agent is only an API (no CLI)?"

Use the **cloud API** row above. `openai_compatible_runner` speaks the
OpenAI-compatible `/chat/completions` shape and authenticates with
`Authorization: Bearer $<API_KEY_ENV_VAR>` (set `--api-key-env`). It works with
any OpenAI-compatible endpoint (OpenAI, OpenRouter, local llama.cpp/vLLM,
Anthropic via a compatible proxy, etc.). The key is read from the environment;
the variable named by `--api-key-env` is the only provider credential
automatically copied into that child process. Never hard-code the value in the
registry.

## Read-only by default

`MCP_HUDDLE_READONLY` defaults to ON and is enforced by Huddle's reviewed
Claude and Codex command transforms. Set `=0` only when you deliberately want a
full-access worker. Antigravity has no equivalent enforced read-only flag;
MiMo is isolated from the project by a temporary cwd. API-runner agents only
read the room and post a validated reply. All spawned children receive a
minimal environment plus explicit `pass_env` names; this does not replace the
CLI's own filesystem or OS sandbox.

## Manual fallback (the same steps without an agent)

```bash
pipx install mcp-huddle
mcp-huddle --http &                               # dashboard+MCP on :8014
claude mcp add --transport http huddle http://127.0.0.1:8014/mcp
mcp-huddle --install-hooks                          # then wire the printed snippet
$EDITOR ~/.mcp-huddle/registry.json                 # see examples/registry.json
```
