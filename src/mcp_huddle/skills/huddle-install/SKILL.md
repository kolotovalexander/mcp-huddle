---
name: huddle-install
description: Install or update Huddle on this device and configure detected local agent clients and Huddle Skills. Use when asked to install Huddle from its repository; not for cloud or multi-device deployment.
---

# Install Huddle

Use the user's requested repository/revision. Read its README and `docs/ONBOARDING.md` before executing code. The supported server platforms are macOS, Linux and WSL with Python 3.11+. Do not claim native Windows server support.

1. Clone https://github.com/kolotovalexander/mcp-huddle into a user-owned directory, or reuse its clean checkout. Preserve local edits on updates.
2. Run `python3 install.py` to inspect detected clients. Then run `python3 install.py --apply --start` within the checkout when the user requested installation. This installs an isolated runtime, registers supported clients and copies this Skill plus `huddle` to detected clients. It does not install the agents themselves or log into their accounts.
3. Read `~/.mcp-huddle/setup-report.json`. Resolve `needs attention` using that client's current native MCP help. Existing conflicting configurations are retained. Do not guess config keys, disable safeguards, copy credentials or add blanket tool approvals.
4. Verify one client can list Huddle tools and create/read a room with `auto_spawn=False`. Do not spend model usage just to verify installation. Refresh clients if new Skills/MCP tools are not discovered until restart.
5. Check native login status without reading credentials or running inference. Enable only selected authenticated managed-spawn profiles in `~/.mcp-huddle/registry.json`; retain other entries and enforced read-only behavior. Antigravity cannot enforce read-only. Missing login or unknown availability remains a reported prerequisite, not an invitation to install or authorize unrelated agents.
6. Report installed/configured/verified separately, including unsupported clients, authentication requirements and whether the server survives reboot. Installation alone does not prove agent spawning or delivery.

The dashboard is http://127.0.0.1:8014/dashboard. See `docs/ONBOARDING.md` for maintenance, backups and limitations. No prerequisite AgentSync, Jev, 9router or other personal infrastructure is required.
