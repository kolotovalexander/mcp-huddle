"""Configurable agent-spawn registry for auto_spawn rooms.

Default registry uses Codex, Antigravity, MiMo, Qwen, and DeepSeek when available.
Claude is present but OFF by default (opt-in via MCP_HUDDLE_CLAUDE_ENABLED=1)
because since 2026-06-15 headless `claude -p` is metered against a separate
Agent SDK credit pool, not the subscription.

Adding / overriding agents (precedence high → low):
  1. MCP_HUDDLE_SPAWN_REGISTRY env var → JSON file, a FULL replacement of the
     registry (highest precedence; malformed → hard error).
  2. ~/.mcp-huddle/registry.json (huddle home; honours $MCP_HUDDLE_HOME) →
     JSON array of SpawnSpec dicts MERGED onto DEFAULT_REGISTRY by "name": an
     existing name is overridden in place, a new name is appended. Drop/edit
     one file to add a model — no env var, no code change. Malformed → stderr
     warning + ignored (never crashes).
  3. DEFAULT_REGISTRY (below).

Each registry entry is a SpawnSpec:
  {
    "name": "Codex",                              # display name in the room
    "cmd":  ["codex", "...", "{brief}"],          # argv; "{brief}" is replaced
    "enabled": true                               # set False to skip
  }

A missing binary is auto-disabled at module-load time so room_create with
auto_spawn never crashes — it just spawns whatever is available.

Phase 1 changes (2026-04-30):
  * Codex spawned with `--json` and `--output-last-message <file>` for
    structured event capture and last-message extraction.
  * Google-model slot spawned via Antigravity CLI (`agy -p`, plain text). The
    legacy Gemini CLI was removed 2026-06-11 (EOL 2026-06-18); Antigravity is
    now the only Google-model runner.
  * stdout+stderr redirected to per-room per-agent JSONL log file
    (~/.mcp-huddle/rooms/<id>/agents/<name>.events.jsonl) instead of DEVNULL.
    This is what feeds the dashboard SSE pane.
  * spawn_all accepts an optional briefs dict {AgentName: brief} for
    per-agent custom briefs (Phase 1: Claude can task each spawned agent
    differently).
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import NotRequired, TypedDict
from urllib import error as urlerror
from urllib import request as urlrequest

from . import bus  # reuse HUDDLE_HOME so the on-disk registry lives next to rooms


class SpawnSpec(TypedDict):
    name: str
    cmd: list[str]
    enabled: bool
    probe_url: NotRequired[str]
    requires_model: NotRequired[str]
    probe_chat_url: NotRequired[str]
    probe_chat_model: NotRequired[str]
    probe_timeout_sec: NotRequired[float]


class AgentSpawnError(RuntimeError):
    """Raised when a process starts but fails the optional health check."""


def _first_existing_binary(candidates: list[str]) -> str | None:
    """Return the first executable found by PATH lookup or absolute fallback.

    Launchd/daemon environments often have a reduced PATH. Using absolute
    fallbacks keeps auto_spawn stable even when an interactive shell can see a
    binary that the MCP daemon cannot.
    """
    for candidate in candidates:
        if "/" in candidate:
            if Path(candidate).exists():
                return candidate
            continue
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return None


_CODEX_BIN = _first_existing_binary([
    "codex",
    "/opt/homebrew/bin/codex",
    "/Applications/Codex.app/Contents/Resources/codex",
])
# Gemini CLI removed 2026-06-11 (EOL 2026-06-18). The Google-model advisor slot
# now runs exclusively on Antigravity (`agy`). GEMINI.md / ~/.gemini stay — they
# are Antigravity's config home, not the dead CLI.
_ANTIGRAVITY_BIN = _first_existing_binary([
    "agy",
    "/opt/homebrew/bin/agy",
])
_CLAUDE_BIN = _first_existing_binary([
    "claude",
    "/Applications/cmux.app/Contents/Resources/bin/claude",
    "/opt/homebrew/bin/claude",
    str(Path.home() / ".claude/local/claude"),
])
_MIMO_BIN = _first_existing_binary([
    "mimo",
    "/opt/homebrew/bin/mimo",
])

# Codex sandbox for huddle participation. Codex talks to the room via the
# huddle MCP server. Under a RESTRICTED sandbox (read-only / workspace-write)
# Codex treats every MCP tool call as approval-requiring; with `-a never` that
# approval is auto-denied → "user cancelled MCP tool call" (verified 2026-06-14
# — even messages_read, a pure read, is cancelled). Only danger-full-access
# lets MCP calls through without approval. This matches the user's global
# ~/.codex/config.toml default (approval_policy=never + danger-full-access);
# the previous read-only pin was the anomaly that silently muted Codex.
_CODEX_SANDBOX = "danger-full-access"


def _google_advisor_spec() -> SpawnSpec:
    """Build the Google-model advisor slot for the spawn registry.

    Runs on Antigravity CLI (`agy`) only — the Gemini CLI was removed (EOL
    2026-06-18). Opt-in (default OFF) for two reasons: (1) `agy` needs an
    interactive Google OAuth login that headless spawns can't complete — you
    must run `agy` once and sign in first; (2) `agy` exposes no read-only flag,
    so even under MCP_HUDDLE_READONLY it would run with full access in the
    project dir (we can't constrain it like Claude/Codex). Enable deliberately
    via MCP_HUDDLE_ANTIGRAVITY_ENABLED=1 once you've logged in and accept that.
    """
    enabled = (
        _ANTIGRAVITY_BIN is not None
        and os.environ.get("MCP_HUDDLE_ANTIGRAVITY_ENABLED", "0") != "0"
    )
    if _ANTIGRAVITY_BIN:
        return {
            "name": "Antigravity",
            "cmd": [
                _ANTIGRAVITY_BIN,
                "--dangerously-skip-permissions",
                "--print-timeout", "15m",
                "-p", "{brief}",
            ],
            "enabled": enabled,
        }
    return {"name": "Antigravity", "cmd": ["agy", "-p", "{brief}"], "enabled": False}


def _mimo_advisor_spec() -> SpawnSpec:
    """Build the MiMo Code advisor slot.

    MiMo Code (Xiaomi, OpenCode fork) ships a built-in free "MiMo Auto"
    provider, so headless `mimo run` works without API keys. Upstream bug in
    0.1.x: `mimo run` hangs forever before the session starts when ANY MCP
    server is configured, so MiMo cannot call huddle MCP tools itself. Like
    Qwen/DeepSeek it goes through a runner (mimo_runner) that reads the room
    from disk, generates via `mimo run` with MCP hard-disabled, and posts the
    result through the bus. Disabled if the `mimo` binary is not installed.
    """
    if _MIMO_BIN:
        return {
            "name": "MiMo",
            "cmd": [
                sys.executable,
                "-m", "mcp_huddle.mimo_runner",
                "--agent", "MiMo",
                "--mimo-bin", _MIMO_BIN,
                "--brief", "{brief}",
            ],
            "enabled": os.environ.get("MCP_HUDDLE_MIMO_ENABLED", "1") != "0",
        }
    return {
        "name": "MiMo",
        "cmd": [sys.executable, "-m", "mcp_huddle.mimo_runner", "--brief", "{brief}"],
        "enabled": False,
    }


# NOTE: the local Qwen (:3264) and DeepSeek (:9655) advisor slots were removed
# 2026-06-18 together with the reverse-API browser-session bridges they fronted.
# Those bridges (Qwen/GLM/Kimi/DeepSeek + the :3274 failover proxy) were retired
# as legacy once agentmemory and remember moved to the Mac LiteLLM router (:4000).


def _models_payload_has_model(payload: object, model: str) -> bool:
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return any(isinstance(item, dict) and item.get("id") == model for item in data)
        models = payload.get("models")
        if isinstance(models, list):
            return model in models or any(
                isinstance(item, dict) and item.get("id") == model for item in models
            )
    if isinstance(payload, list):
        return model in payload or any(
            isinstance(item, dict) and item.get("id") == model for item in payload
        )
    return False


_PROBE_CACHE: dict[tuple[str, ...], tuple[float, bool]] = {}


def _cached_probe(key: tuple[str, ...], ttl_sec: float, check) -> bool:
    now = time.time()
    cached = _PROBE_CACHE.get(key)
    if cached and now - cached[0] < ttl_sec:
        return cached[1]
    ok = bool(check())
    _PROBE_CACHE[key] = (now, ok)
    return ok


def _chat_probe_available(url: str, model: str, timeout: float) -> bool:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Return only OK."}],
        "temperature": 0,
        "max_tokens": 8,
    }
    req = urlrequest.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "authorization": "Bearer dummy-key",
        },
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urlerror.URLError):
        return False
    return bool(data.get("choices"))


def _spawn_spec_available(spec: SpawnSpec) -> bool:
    if not spec.get("enabled"):
        return False
    probe_url = spec.get("probe_url")
    required_model = spec.get("requires_model")
    chat_url = spec.get("probe_chat_url")
    chat_model = spec.get("probe_chat_model") or required_model
    if not probe_url or not required_model:
        return True
    timeout = float(spec.get("probe_timeout_sec", 0.8))
    ttl_sec = float(os.environ.get("MCP_HUDDLE_PROBE_CACHE_TTL_SEC", "300"))

    def check_models() -> bool:
        try:
            with urlrequest.urlopen(probe_url, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError, urlerror.URLError):
            return False
        return _models_payload_has_model(payload, required_model)

    if not _cached_probe(("models", probe_url, required_model), ttl_sec, check_models):
        return False
    if chat_url and chat_model:
        return _cached_probe(
            ("chat", chat_url, chat_model),
            ttl_sec,
            lambda: _chat_probe_available(chat_url, chat_model, max(timeout, 10.0)),
        )
    return True


def _is_ascii(text: str) -> bool:
    return all(ord(ch) < 128 for ch in text)


# Codex CLI crashes when cwd contains non-ASCII characters: it copies the path
# into the `x-codex-turn-metadata` HTTP header and the UTF-8 bytes break the
# request (upstream issue #17468). When a project lives under such a path we
# run Codex from an ASCII fallback cwd and hand it the real project path as an
# absolute path inside the brief instead.
_CODEX_NONASCII_CWD_NOTE = (
    "\n\n[ВАЖНО — рабочее окружение]\n"
    "Codex CLI падает на не-ASCII cwd, поэтому этот процесс запущен из "
    "служебного ASCII-каталога, НЕ из каталога проекта. Файлы проекта — по "
    "абсолютному пути:\n  {real}\n"
    "Обращайся к ним только по абсолютным путям; cwd использовать нельзя.\n"
)


def _codex_safe_cwd_and_brief(cwd: str, brief: str) -> tuple[str, str]:
    """Return (cwd, brief) safe for a Codex spawn. If cwd is non-ASCII, swap to
    an ASCII fallback cwd and append the real project path to the brief."""
    if cwd and not _is_ascii(cwd):
        return str(Path.home()), brief + _CODEX_NONASCII_CWD_NOTE.format(real=cwd)
    return cwd, brief


# Default registry: enabled=False if the binary is missing.
# Codex emits `--json` structured events to stdout for the dashboard SSE
# endpoint; Antigravity (`agy -p`) emits plain text. `{last_message}` is
# replaced with a per-agent file path so the agent's last reply is captured
# for downstream tools.
DEFAULT_REGISTRY: list[SpawnSpec] = [
    {
        "name": "Codex",
        "cmd": [
            _CODEX_BIN or "codex", "-a", "never", "exec",
            "--disable", "guardian_approval",
            "--json",
            "--output-last-message", "{last_message}",
            # Model is NOT pinned — it comes from ~/.codex/config.toml (SoT).
            # Hardcoding it here drifts the moment the config default changes.
            "-c", 'model_reasoning_effort="medium"',
            # Full access so Codex's huddle MCP tool calls aren't auto-cancelled
            # under `-a never` (a restricted sandbox makes MCP calls need
            # approval, which `never` denies). See _CODEX_SANDBOX note.
            "-s", _CODEX_SANDBOX,
            "{brief}",
        ],
        "enabled": _CODEX_BIN is not None,
    },
    _google_advisor_spec(),
    _mimo_advisor_spec(),
    {
        "name": "Claude",
        "cmd": [
            _CLAUDE_BIN or "claude",
            "--dangerously-skip-permissions",
            "--model", "sonnet",
            "-p", "{brief}",
        ],
        # Opt-in (default OFF). Since 2026-06-15 Anthropic moved `claude -p`
        # (headless) off the subscription onto a separate, metered Agent SDK
        # credit pool (Pro $20 / Max5x $100 / Max20x $200, no rollover, full
        # API rates). Each invited-Claude turn here is a fresh `claude -p`
        # spawn, so auto-spawning it bleeds that credit pool for the least
        # differentiated voice in a multi-model room — the organizer's own
        # interactive Claude session is already present for free. Enable
        # deliberately via MCP_HUDDLE_CLAUDE_ENABLED=1 when you specifically
        # want a second Claude perspective and accept the metered cost.
        # Ref: support.claude.com article 15036540.
        "enabled": (
            _CLAUDE_BIN is not None
            and os.environ.get("MCP_HUDDLE_CLAUDE_ENABLED", "0") != "0"
        ),
    },
]


def _resolve_spawn_args(
    spec: SpawnSpec,
    brief: str,
    log_dir: Path,
) -> tuple[list[str], str | None]:
    name = spec["name"]
    last_msg_path: str | None = None
    argv = []
    for arg in spec["cmd"]:
        if "{brief}" in arg:
            arg = arg.replace("{brief}", brief)
        if "{last_message}" in arg:
            last_msg_path = str(log_dir / f"{name.lower()}.last_message.txt")
            arg = arg.replace("{last_message}", last_msg_path)
        argv.append(arg)
    return argv, last_msg_path


def log_spawn_failure(
    spec: SpawnSpec,
    brief: str,
    cwd: str,
    log_dir: Path,
    exc: BaseException,
) -> None:
    """Write spawn failures to stderr so daemon logs keep the real cause."""
    try:
        argv, _ = _resolve_spawn_args(spec, brief, log_dir)
    except Exception:  # pragma: no cover - defensive logging only
        argv = list(spec.get("cmd", []))
    log_path = log_dir / f"{spec['name'].lower()}.events.jsonl"
    print(
        "[mcp-huddle] failed to spawn "
        f"{spec['name']}: {type(exc).__name__}: {exc}; "
        f"cwd={cwd!r}; argv={argv!r}; log_path={log_path}",
        file=sys.stderr,
        flush=True,
    )
    traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)


def _reap_in_background(proc: subprocess.Popen, name: str,
                        on_exit=None) -> None:
    """Ensure short-lived spawned agents do not remain as defunct children.

    on_exit: optional callable(returncode) invoked once the process exits.
    Used by the wake machinery to clear the busy lease and drain queued
    requests the moment an agent turn ends (event-driven, no polling)."""
    def wait_for_exit() -> None:
        returncode = None
        try:
            returncode = proc.wait()
        except Exception:
            pass
        if on_exit is not None:
            try:
                on_exit(returncode)
            except Exception:
                pass

    # daemon=True so these reaper threads never block interpreter exit. There is
    # no process-wide shutdown/cleanup hook to join them against; each thread
    # only blocks on proc.wait() and is harmless to abandon at exit (any pending
    # on_exit cleanup is best-effort and bounded by the child's lifetime).
    thread = threading.Thread(
        target=wait_for_exit,
        name=f"mcp-huddle-reap-{name}-{proc.pid}",
        daemon=True,
    )
    thread.start()


def _registry_file_path() -> Path:
    """Location of the optional on-disk registry file.

    Lives in the huddle home (``~/.mcp-huddle`` by default, or
    ``$MCP_HUDDLE_HOME``) — the same dir that holds rooms — so a user can add a
    model by dropping/editing a single JSON file with no env var and no code
    change. Resolved at call time (via ``bus.HUDDLE_HOME``) so it follows the
    same home the rest of the server uses, and so tests can repoint the home.
    """
    return bus.HUDDLE_HOME / "registry.json"


def _load_env_registry() -> list[SpawnSpec] | None:
    """Parse the MCP_HUDDLE_SPAWN_REGISTRY env override, or None if unset.

    Behaviour preserved verbatim: a set-but-malformed file raises ValueError
    (callers surface the cause); a JSON array is a FULL replacement of the
    default registry (highest precedence).
    """
    path = os.environ.get("MCP_HUDDLE_SPAWN_REGISTRY")
    if not (path and Path(path).is_file()):
        return None
    with open(path) as f:
        raw = f.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"MCP_HUDDLE_SPAWN_REGISTRY points to malformed JSON "
            f"({path}): {exc}. Expected a JSON array of SpawnSpec objects."
        ) from exc
    if not isinstance(data, list):
        raise ValueError(
            f"MCP_HUDDLE_SPAWN_REGISTRY ({path}): expected a JSON array "
            f"of SpawnSpec objects, got {type(data).__name__}."
        )
    return data


def _load_registry_file() -> list[SpawnSpec] | None:
    """Load the optional ~/.mcp-huddle/registry.json, or None if absent/invalid.

    Unlike the env override (a full replacement that fails loudly), the file is
    a best-effort convenience layer: anything malformed prints a clear stderr
    warning and is ignored so the server never crashes on a bad hand-edit.
    Returns a list of SpawnSpec dicts (each must carry a "name").
    """
    path = _registry_file_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"[mcp-huddle] WARNING: ignoring registry file {path}: {exc}. "
            f"Expected a JSON array of SpawnSpec objects.",
            file=sys.stderr,
            flush=True,
        )
        return None
    if not isinstance(data, list) or not all(
        isinstance(spec, dict) and isinstance(spec.get("name"), str) for spec in data
    ):
        print(
            f"[mcp-huddle] WARNING: ignoring registry file {path}: expected a "
            f"JSON array of SpawnSpec objects, each with a string \"name\".",
            file=sys.stderr,
            flush=True,
        )
        return None
    return data


def _merge_registry(
    base: list[SpawnSpec], overrides: list[SpawnSpec]
) -> list[SpawnSpec]:
    """Merge `overrides` onto `base` keyed by "name".

    An override whose name matches an existing entry replaces it in place
    (preserving order); a new name is appended. Inputs are not mutated.
    """
    merged: list[SpawnSpec] = [dict(spec) for spec in base]  # type: ignore[misc]
    index = {spec.get("name"): i for i, spec in enumerate(merged)}
    for spec in overrides:
        name = spec.get("name")
        if name in index:
            merged[index[name]] = spec
        else:
            index[name] = len(merged)
            merged.append(spec)
    return merged


# Read-only discussant mode (MCP_HUDDLE_READONLY): spawned agents may READ
# (files, web, docs, rules, memory) but must not EDIT/WRITE anything — they
# participate only through the huddle MCP tools (message_post / messages_read),
# never by changing files. Agents communicate via the bus, not file edits, so
# this does not hamper participation.
_CLAUDE_RO_FLAGS = [
    "--allowedTools", "Read,Glob,WebFetch,WebSearch,mcp__huddle__*",
    "--disallowedTools", "Edit,Write,NotebookEdit,MultiEdit,Bash",
    "--permission-mode", "default",
]


def _readonly_enabled() -> bool:
    # Default ON: huddle agents are read-only discussants, not workers. They
    # read freely and talk via the bus, but never edit files. Set
    # MCP_HUDDLE_READONLY=0 to spawn full-access agents instead.
    return os.environ.get("MCP_HUDDLE_READONLY", "1").lower() not in ("0", "false", "no")


def _apply_readonly(spec: SpawnSpec) -> SpawnSpec:
    """Rewrite a spec so the agent reads freely but cannot edit/write files.

    - Claude: swap `--dangerously-skip-permissions` for an allow/deny tool list
      (read + web + huddle MCP only; Edit/Write/Bash denied). The allowlist
      auto-denies the rest in headless `-p`, so it never hangs on a prompt.
    - Codex: switch the sandbox to `read-only` and auto-approve the huddle MCP
      tools (a restricted sandbox otherwise routes MCP calls through approval,
      which `-a never` would cancel). Cross-model council, 2026-06-19.
    Other agents are returned unchanged (no confirmed read-only flag yet).
    """
    name = spec.get("name")
    cmd = list(spec.get("cmd") or [])
    if name == "Claude":
        cmd = [c for c in cmd if c != "--dangerously-skip-permissions"]
        if cmd:
            cmd = [cmd[0], *_CLAUDE_RO_FLAGS, *cmd[1:]]
    elif name == "Codex":
        out: list[str] = []
        i = 0
        while i < len(cmd):
            out.append(cmd[i])
            if cmd[i] == "-s" and i + 1 < len(cmd):
                out.append("read-only")  # replace the sandbox value
                i += 2
                continue
            i += 1
        # Auto-approve huddle MCP tools so read-only doesn't cancel them;
        # insert before the trailing positional ({brief}).
        approve = ["-c", 'mcp_servers.huddle.default_tools_approval_mode="approve"']
        out = [*out[:-1], *approve, out[-1]] if out else out
        cmd = out
    return {**spec, "cmd": cmd}


def _raw_registry() -> list[SpawnSpec]:
    """Merged registry BEFORE availability filtering.

    Precedence: MCP_HUDDLE_SPAWN_REGISTRY env (full replacement) >
    ~/.mcp-huddle/registry.json (merged onto defaults) > DEFAULT_REGISTRY.
    When MCP_HUDDLE_READONLY is set, every spec is rewritten to read-only.
    """
    env_registry = _load_env_registry()
    if env_registry is not None:
        reg = env_registry
    else:
        file_overrides = _load_registry_file()
        reg = (_merge_registry(DEFAULT_REGISTRY, file_overrides)
               if file_overrides is not None else list(DEFAULT_REGISTRY))
    if _readonly_enabled():
        reg = [_apply_readonly(s) for s in reg]
    return reg


def load_registry() -> list[SpawnSpec]:
    """Return the enabled+available registry.

    Sources, in precedence order: MCP_HUDDLE_SPAWN_REGISTRY env >
    ~/.mcp-huddle/registry.json > DEFAULT_REGISTRY. See _raw_registry.
    """
    return [spec for spec in _raw_registry() if _spawn_spec_available(spec)]


def _agent_status(spec: SpawnSpec) -> tuple[bool, str]:
    """Best-effort (available?, reason) for one registry spec.

    Side-effect-light: the only probe it triggers is the same cached
    `_spawn_spec_available` check load_registry already uses, and only for
    specs the author left enabled (DEFAULT_REGISTRY specs carry no probe_url
    after the Qwen/DeepSeek removal, so this stays cheap).
    """
    if not spec.get("enabled", False):
        cmd = spec.get("cmd") or []
        binary = cmd[0] if cmd else ""
        if binary and _first_existing_binary([binary]) is None:
            return False, "binary not found"
        return False, "off by env flag"
    if not _spawn_spec_available(spec):
        return False, "model probe failed"
    return True, "ready"


def discovery_summary() -> list[str]:
    """One concise line per registry agent: "Name -> enabled" / "... disabled (reason)".

    Reads the merged registry (env/file/defaults) so it reflects exactly what
    auto_spawn would consider. Reasons are best-effort: "binary not found",
    "model probe failed", "off by env flag".
    """
    lines: list[str] = []
    for spec in _raw_registry():
        name = spec.get("name", "?")
        ok, reason = _agent_status(spec)
        lines.append(f"{name} -> enabled" if ok else f"{name} -> disabled ({reason})")
    return lines


def log_discovery_summary(file=sys.stderr) -> None:
    """Print the agent discovery summary once (server/__main__ may call this)."""
    print("[mcp-huddle] agent discovery:", file=file, flush=True)
    for line in discovery_summary():
        print(f"[mcp-huddle]   {line}", file=file, flush=True)


def get_enabled_spec(agent_name: str) -> SpawnSpec | None:
    """Return an enabled registry spec by display name."""
    for spec in load_registry():
        if spec.get("name") == agent_name and spec.get("enabled"):
            return spec
    return None


def spawn_agent(
    spec: SpawnSpec,
    brief: str,
    cwd: str,
    log_dir: Path,
    verify_alive_sec: float = 0.0,
    on_exit=None,
) -> tuple[int, str, str | None]:
    """Spawn one agent.

    Returns (pid, log_path, last_message_path).
    last_message_path is None for agents whose argv doesn't reference {last_message}.

    on_exit: optional callable(returncode) fired when the process exits.

    Side effects: creates log_dir, opens log file, redirects stdout+stderr to it.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    name = spec["name"]
    log_path = log_dir / f"{name.lower()}.events.jsonl"
    if name == "Codex":
        cwd, brief = _codex_safe_cwd_and_brief(cwd, brief)
    argv, last_msg_path = _resolve_spawn_args(spec, brief, log_dir)

    log_file = open(log_path, "ab", buffering=0)
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd or None,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
    finally:
        # Popen dups the fd into the child; we can close ours so the parent
        # process doesn't keep the log file held open after the child exits.
        log_file.close()
    _reap_in_background(proc, name, on_exit=on_exit)
    if verify_alive_sec > 0:
        time.sleep(verify_alive_sec)
        returncode = proc.poll()
        if returncode is not None:
            exc = AgentSpawnError(
                f"{name} exited within {verify_alive_sec:.3g}s with status {returncode}"
            )
            log_spawn_failure(spec, brief, cwd, log_dir, exc)
            raise exc
    return proc.pid, str(log_path), last_msg_path


def spawn_all(
    brief: str,
    cwd: str,
    log_dir: Path,
    briefs: dict[str, str] | None = None,
    verify_alive_sec: float = 0.0,
    on_exit_factory=None,
    skip_names: set[str] | None = None,
) -> tuple[list[str], list[int], dict[str, dict[str, str | None]]]:
    """Spawn every enabled agent in the registry.

    Args:
      brief: default brief used when `briefs` doesn't have a per-agent entry.
      cwd: working directory for spawned processes.
      log_dir: where each agent's <name>.events.jsonl is written.
      briefs: optional {AgentName: brief} for per-agent customization.
      skip_names: registry names to NOT spawn (e.g. the room owner — already
        present as the calling session, would otherwise spawn a duplicate).

    Returns:
      (names, pids, agent_meta) where agent_meta is
      {name: {"log_path": "...", "last_message_path": "..." or None}}.
    """
    names: list[str] = []
    pids: list[int] = []
    agent_meta: dict[str, dict[str, str | None]] = {}
    briefs = briefs or {}
    skip_names = skip_names or set()
    for spec in load_registry():
        if not spec.get("enabled"):
            continue
        if spec["name"] in skip_names:
            continue
        agent_brief = briefs.get(spec["name"], brief)
        try:
            pid, log_path, last_msg = spawn_agent(
                spec, agent_brief, cwd, log_dir,
                verify_alive_sec=verify_alive_sec,
                on_exit=on_exit_factory(spec["name"]) if on_exit_factory else None,
            )
            pids.append(pid)
            names.append(spec["name"])
            agent_meta[spec["name"]] = {
                "log_path": log_path,
                "last_message_path": last_msg,
            }
        except (FileNotFoundError, PermissionError) as exc:
            # Tolerate races (binary disappears between check and spawn).
            log_spawn_failure(spec, agent_brief, cwd, log_dir, exc)
        except AgentSpawnError:
            # spawn_agent already logged the concrete early-exit status.
            pass
        except OSError as exc:
            log_spawn_failure(spec, agent_brief, cwd, log_dir, exc)
            raise
    return names, pids, agent_meta


# ── Phase 2: thread_id capture + Codex resume ────────────────────────────────

def parse_codex_thread_id(log_path: str, timeout: float = 10.0) -> str | None:
    """Tail the agent log file until we see a {"type":"thread.started",...} event.
    Returns thread_id (UUID string) or None on timeout / non-Codex agents.

    Codex --json emits this as the very first line of stdout, so this typically
    completes in <1s after spawn.
    """
    import time
    deadline = time.time() + timeout
    p = Path(log_path)
    while time.time() < deadline:
        if p.exists() and p.stat().st_size > 0:
            try:
                with open(p, "rb") as f:
                    for raw in f:
                        line = raw.decode("utf-8", errors="replace").strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        # A changed/non-Codex log format may yield a non-dict
                        # (list, str, number) — skip it rather than crash.
                        if not isinstance(obj, dict):
                            continue
                        if obj.get("type") == "thread.started":
                            thread_id = obj.get("thread_id")
                            if thread_id is not None:
                                return thread_id
            except OSError:
                pass
        time.sleep(0.1)
    return None


def codex_log_has_completed_turn(log_path: str) -> bool:
    """Return True once Codex has persisted at least one completed turn."""
    p = Path(log_path)
    if not p.exists():
        return False
    try:
        with open(p, "rb") as f:
            for raw in f:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "turn.completed":
                    return True
    except OSError:
        return False
    return False


# Substrings that mark a provider usage/rate-limit refusal. Matched
# case-insensitively. Kept narrow on purpose: a generic word like "limit"
# alone would false-positive on agents that merely *discuss* rate limits in
# their reply body.
_RATE_LIMIT_MARKERS = (
    "usage limit",
    "rate limit",
    "rate_limit",
    "too many requests",
    "quota exceeded",
    "insufficient_quota",
    "error code: 429",
)
# Extra hints required for a PLAIN-TEXT line to count as a limit (structured
# JSON error events are trusted on the marker alone). Avoids flagging an
# agent's prose that happens to mention "rate limit".
_RATE_LIMIT_PLAINTEXT_HINTS = (
    "try again",
    "upgrade",
    "retry after",
    "retry-after",
    "resets at",
    "reset at",
    "429",
)


def _looks_like_rate_limit(text: str, *, require_hint: bool) -> bool:
    low = text.lower()
    if not any(marker in low for marker in _RATE_LIMIT_MARKERS):
        return False
    if not require_hint:
        return True
    return any(hint in low for hint in _RATE_LIMIT_PLAINTEXT_HINTS)


def detect_rate_limit(log_path: str) -> str | None:
    """Scan an agent log for a provider usage/rate-limit refusal.

    Returns a short reason string (the offending message) or None.

    Detection is conservative to avoid false positives from message bodies:
      * Codex `--json` logs — trust only `error` / `turn.failed` event types
        (an agent that *posts about* rate limits does so via `item.*` events,
        which are ignored here).
      * Plain-text logs (e.g. Antigravity `agy -p`) — a line must contain both
        a limit marker AND a recovery hint ("try again", "upgrade", ...).
    """
    p = Path(log_path)
    if not p.exists():
        return None
    try:
        with open(p, "rb") as f:
            text = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None

    reason: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("{"):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = obj.get("type")
            if etype == "error":
                candidate = obj.get("message", "") or ""
            elif etype == "turn.failed":
                candidate = (obj.get("error") or {}).get("message", "") or ""
            else:
                continue
            if candidate and _looks_like_rate_limit(candidate, require_hint=False):
                reason = candidate.strip()
        else:
            if _looks_like_rate_limit(line, require_hint=True):
                reason = line
    return reason


def codex_resume(thread_id: str, prompt: str, cwd: str, log_path: str,
                 last_msg_path: str | None = None, on_exit=None) -> int:
    """Resume a Codex thread with a new prompt. Cheaper than fresh spawn —
    Codex remembers prior conversation via its rollout file.

    Appends events to the same log_path so the dashboard SSE keeps streaming.
    Returns PID of the spawned codex exec resume process.

    `codex exec resume` has no `-s/--sandbox` flag — sandbox must be pinned
    via `-c sandbox_mode=...`, else it falls back to ~/.codex/config.toml.
    We pin danger-full-access so Codex's huddle MCP tool calls aren't auto-
    cancelled: under a restricted sandbox + `-a never`, MCP calls need approval
    that `never` denies ("user cancelled MCP tool call"). `-a` is a top-level
    flag (before `exec`). The model is left to ~/.codex/config.toml (SoT).
    """
    cwd, prompt = _codex_safe_cwd_and_brief(cwd, prompt)
    argv = [
        _CODEX_BIN or "codex", "-a", "never",            # top-level: never auto-approve tool calls
        "exec", "resume", thread_id,                     # subcommand
        "--json",                                        # JSONL events to stdout
        "-c", 'model_reasoning_effort="medium"',
        # Full access (resume has no -s flag, so pin via -c). A restricted
        # sandbox makes Codex's huddle MCP calls approval-requiring, which
        # `-a never` auto-cancels — so the resumed turn would post nothing.
        "-c", f'sandbox_mode="{_CODEX_SANDBOX}"',
        "-c", "features.guardian_approval=false",
    ]
    if last_msg_path:
        argv += ["-o", last_msg_path]                    # short form of --output-last-message
    argv.append(prompt)

    log_file = open(log_path, "ab", buffering=0)
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd or None,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
    finally:
        log_file.close()
    _reap_in_background(proc, "Codex", on_exit=on_exit)
    return proc.pid
