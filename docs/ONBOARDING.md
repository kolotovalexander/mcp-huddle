# Install Huddle by sending a repository link

Send this message to your coding agent:

> Install https://github.com/kolotovalexander/mcp-huddle on this device. Read and follow the bundled huddle-install Skill at src/mcp_huddle/skills/huddle-install/SKILL.md. Discover available agent clients, configure Huddle MCP and install its Skills for them. Preserve my existing settings. Complete setup and report anything that needs my login or approval.

No AgentSync, Jev, 9router or personal configuration is required. Use the source checkout for the current features; an older PyPI release can contain different functionality.

## One command after cloning

Requirements: macOS/Linux (or WSL), Python 3.11+, network access for Python package installation. The server uses POSIX file locking; native Windows is not supported yet. Your agents must already be installed. Login remains with their native tools.

```sh
git clone https://github.com/kolotovalexander/mcp-huddle.git
cd mcp-huddle
python3 install.py --apply --start
```

Without `--apply`, `python3 install.py` only previews detected clients. Installation creates an isolated environment in `~/.mcp-huddle/venv`; it does not modify your system Python. Existing custom home overrides or a token-protected Huddle deployment require native setup and are preserved. Client settings are merged or registered through native commands. Conflicting Huddle entries and unsupported formats are retained and reported. Original changed settings are backed up under `~/.mcp-huddle/setup-backups/`; keep these private because your original configuration can contain credentials.

The setup report is `~/.mcp-huddle/setup-report.json`. It distinguishes installed Skills, configured MCP clients and deferred work. Binary/config discovery does not prove login, remaining quota, supported model or successful agent execution. No model prompt is sent by the installer.

## Supported clients

Claude Code and Codex use native registration commands. Antigravity and Hermes also use native configuration commands. Gemini CLI and OpenCode use their supported local JSON formats. Custom profile paths and incompatible configurations are reported for native setup. Disabled managed-spawn profiles are created for Codex, Claude, Antigravity and OpenCode; enable selected profiles in ~/.mcp-huddle/registry.json after checking native login/model availability. Antigravity has no enforced read-only transform; Gemini/Hermes may connect as participants, but managed-spawn profiles are not bundled. Unsupported or unavailable environments are not silently considered installed. Review the report after every setup.

Huddle provides two bundled Skills: `huddle-install` for setup and `huddle` for native-first routing, room modes, permissions and delivery. Newly installed Skills may require a new agent session. Agents communicate in English; human summaries use the user's language.

## Start, update and verify

The dashboard is http://127.0.0.1:8014/dashboard; MCP clients connect to http://127.0.0.1:8014/mcp. `--start` starts a detached process with logs in `~/.mcp-huddle/logs/setup-server.log`. It does not install automatic startup after reboot. An existing listener is retained. Setup checks MCP initialization and the required Huddle tool names without launching agents or creating rooms. Do not terminate an unknown process to free the port.

After reboot, run the installed environment's `mcp-huddle --http`, or rerun setup with `--start`. To update, preserve checkout changes, fetch the chosen revision, and rerun installation. A running server still uses its loaded code: arrange a controlled restart of the owned process to activate Python changes. Installation does not restart existing services.

Verify from one configured client: list MCP tools, create a room with `auto_spawn=False`, post a comment, read it back and close the room. This checks connectivity without invoking a paid model. Before using managed modes, verify login and choose an available profile/model. See [README](../README.md), [mode and delivery Skill](../src/mcp_huddle/skills/huddle/SKILL.md), and [backups](OPS_BACKUPS.md).

Keep Huddle local. External network access and multiple devices are future work in [TODO](TODO.md). Configuration does not disable client safeguards, grant write access to projects or install agent applications.
