"""Configurable agent-spawn registry for auto_spawn rooms.

The default registry provides Codex, Antigravity, MiMo, OpenCode, Claude and
two fixed Opus review profiles. Only explicitly enabled/available slots run;
Claude-family, Antigravity, MiMo and OpenCode automation is opt-in because it
may consume account usage or lacks a fully enforced read-only mode.
Anthropic paused the announced separate SDK billing change on 2026-06-15;
`claude -p` still supports subscription usage. Verify native authentication
rather than inferring billing or availability from an absent API key.

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
    "enabled": true,                              # set False to skip
    "auto": true,                                 # optional, default true; set
                                                    # false to exclude from
                                                    # auto_spawn=True while still
                                                    # reachable via an explicit
                                                    # dict auto_spawn, room_invite,
                                                    # or a wake-path request
    "pass_env": ["PROVIDER_API_KEY"]              # optional explicit names;
                                                    # values are copied at spawn
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
import _thread
import hashlib
import json
import secrets
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import BinaryIO, Callable, Literal, NamedTuple, NotRequired, TypedDict
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlparse

from . import bus  # reuse HUDDLE_HOME so the on-disk registry lives next to rooms
from . import child_processes


class SpawnSpec(TypedDict):
    name: str
    cmd: list[str]
    enabled: bool
    probe_url: NotRequired[str]
    requires_model: NotRequired[str]
    probe_chat_url: NotRequired[str]
    probe_chat_model: NotRequired[str]
    probe_timeout_sec: NotRequired[float]
    # Curated auto_spawn=True roster flag. Defaults to True when absent — set
    # False on a spec to exclude it from `auto_spawn=True` while still
    # allowing it to be woken by an explicit dict auto_spawn={name: brief},
    # room_invite, or a wake-path request (those always ignore this flag; a
    # deliberately named agent always works). See _filter_for_auto_spawn_true.
    auto: NotRequired[bool]
    # A named profile is a fixed, reviewed runner contract. It is deliberately
    # narrower than a generic environment/configuration API for registry JSON.
    profile: NotRequired[str]
    # Local Huddle MCP endpoint (loopback, path /mcp). Used by the
    # owner-enabled subscription reviewer, Claude write rooms, and — when set
    # on a Codex profile — to pin that child's `huddle` MCP URL. Other
    # configured MCP servers are not removed. Credentials and remote URLs are
    # never accepted here.
    mcp_url: NotRequired[str]
    # Additional parent environment variable names that this child explicitly
    # needs. Values stay out of registry JSON and logs; only the named values
    # are copied at spawn time. Provider runners also opt in their
    # ``--api-key-env`` variable automatically.
    pass_env: NotRequired[list[str]]
    model: NotRequired[str]
    effort: NotRequired[str]
    variant: NotRequired[str]


class AgentSpawnError(RuntimeError):
    """Raised when a process starts but fails the optional health check."""


class CliLoginProbe(TypedDict):
    status: Literal["authenticated", "unauthenticated", "unknown"]
    reason: Literal[
        "cli_login_present", "cli_logged_out", "unsupported_harness",
        "non_native_profile", "probe_unavailable", "probe_timeout",
        "unrecognized_output",
    ]


class _RetainedChild(NamedTuple):
    """Fallback lifecycle outcome used by post-spawn health checks."""

    handle: str
    registered: bool


_DIRECT_OPUS_REVIEW_PROFILE = "claude-opus-direct-review"
_SUBSCRIPTION_OPUS_REVIEW_PROFILE = "claude-opus-subscription-review"
_DIRECT_OPUS_REVIEW_CWD_PREFIX = "mcp-huddle-opus-review-"
_DIRECT_OPUS_REMOVED_ENV = frozenset({
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_MODEL",
    "CLAUDE_CODE_MODEL",
    "CLAUDE_CODE_FALLBACK_MODEL",
})
_DIRECT_OPUS_ENDPOINT_ENV = "MCP_HUDDLE_DIRECT_REVIEW_MCP_URL"
_DIRECT_OPUS_WORKSPACE_ENV = "MCP_HUDDLE_CLAUDE_OPUS_WORKSPACE_HEADER"
_DIRECT_OPUS_REVIEW_FLAGS = [
    "--tools", "Read,Glob,Grep,WebSearch,ToolSearch",
    "--allowedTools", (
        "mcp__huddle__messages_read,mcp__huddle__message_post,"
        "mcp__huddle__status_set,mcp__huddle__room_info,"
        "mcp__huddle__room_status,mcp__huddle__room_summarize"
    ),
    "--disallowedTools", (
        "Edit,Write,NotebookEdit,MultiEdit,Bash,Agent,Task,"
        "mcp__huddle__room_create,mcp__huddle__room_invite,"
        "mcp__huddle__notify_register,mcp__huddle__room_reclaim,"
        "mcp__huddle__room_round_advance,mcp__huddle__propose_resolution,"
        "mcp__huddle__resolution_vote"
    ),
    "--permission-mode", "dontAsk",
]


# Child processes must not inherit the server's whole environment: a Huddle
# reviewer has no reason to receive unrelated GitHub/AWS/etc. credentials.
# Keep only process-discovery, locale, user config paths and the room store.
# Variables that can carry provider credentials are deliberately absent and
# must be opted in by a typed profile, ``--api-key-env`` or SpawnSpec.pass_env.
_SAFE_CHILD_ENV = frozenset({
    "PATH", "HOME", "TMPDIR", "TMP", "TEMP", "USER", "LOGNAME", "SHELL",
    "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "TERM", "COLORTERM", "NO_COLOR",
    "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
    "PYTHONPATH", "VIRTUAL_ENV", "MCP_HUDDLE_HOME", "CODEX_HOME",
    "CLAUDE_CONFIG_DIR",
})
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_BACKGROUND_LOCK = threading.Lock()
_REAPER_THREADS: set[object] = set()
_SPAWN_TIMERS: set[threading.Timer] = set()


class _EmergencyReaper:
    """Join-compatible low-level fallback when ``Thread.start`` is unavailable."""

    def __init__(self, target, name: str):
        self._target = target
        self.name = name
        self.ident: int | None = None
        self._done = threading.Event()

    def _run(self) -> None:
        try:
            self._target()
        finally:
            self._done.set()
            with _BACKGROUND_LOCK:
                _REAPER_THREADS.discard(self)

    def start(self) -> None:
        self.ident = _thread.start_new_thread(self._run, ())

    def join(self, timeout: float | None = None) -> None:
        self._done.wait(timeout)

    def is_alive(self) -> bool:
        return not self._done.is_set()


def _reset_background_after_fork() -> None:
    """A fork child owns none of the parent's reaper threads or timers."""
    global _BACKGROUND_LOCK, _REAPER_THREADS, _SPAWN_TIMERS
    _BACKGROUND_LOCK = threading.Lock()
    _REAPER_THREADS = set()
    _SPAWN_TIMERS = set()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_background_after_fork)


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
    str(Path.home() / ".local/bin/claude"),
    "/Applications/cmux.app/Contents/Resources/bin/claude",
    "/opt/homebrew/bin/claude",
    str(Path.home() / ".claude/local/claude"),
])
_MIMO_BIN = _first_existing_binary([
    "mimo",
    "/opt/homebrew/bin/mimo",
])
_OPENCODE_BIN = _first_existing_binary([
    "opencode",
    "/opt/homebrew/bin/opencode",
])
_OPENCODE_TIMEOUT_BIN = _first_existing_binary([
    "timeout",
    "gtimeout",
    "/opt/homebrew/bin/timeout",
])
try:
    _OPENCODE_TIMEOUT_SEC = int(os.environ.get("MCP_HUDDLE_OPENCODE_TIMEOUT_SEC", "1200"))
except ValueError:
    _OPENCODE_TIMEOUT_SEC = 1200
if _OPENCODE_TIMEOUT_SEC <= 0:
    _OPENCODE_TIMEOUT_SEC = 1200

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

    Default OFF: headless `mimo run` proved unreliable in multi-round sessions
    (empty output / silent wake-fails), so the advisor slot is opt-in. Set
    MCP_HUDDLE_MIMO_ENABLED=1 to re-enable once upstream is fixed.
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
            "enabled": os.environ.get("MCP_HUDDLE_MIMO_ENABLED", "0") == "1",
        }
    return {
        "name": "MiMo",
        "cmd": [sys.executable, "-m", "mcp_huddle.mimo_runner", "--brief", "{brief}"],
        "enabled": False,
    }


def _opencode_spec() -> SpawnSpec:
    """Build the optional OpenCode slot without assuming a provider route.

    OpenCode's model/provider names are local configuration, not a stable
    huddle contract. Do not pin a route that may not exist on this machine;
    ``opencode`` resolves its configured default model after explicit opt-in.
    A timeout wrapper is required because initial auto-spawn has no wake lease
    for the existing stuck-wake watchdog to release.
    """
    enabled = (
        _OPENCODE_BIN is not None
        and _OPENCODE_TIMEOUT_BIN is not None
        and os.environ.get("MCP_HUDDLE_OPENCODE_ENABLED", "0") == "1"
    )
    timeout_bin = _OPENCODE_TIMEOUT_BIN or "timeout"
    opencode_bin = _OPENCODE_BIN or "opencode"
    return {
        "name": "OpenCode",
        "cmd": [
            timeout_bin, str(_OPENCODE_TIMEOUT_SEC),
            opencode_bin, "run", "{brief}",
        ],
        "enabled": enabled,
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
    _opencode_spec(),
    {
        "name": "Claude",
        "cmd": [
            _CLAUDE_BIN or "claude",
            "--dangerously-skip-permissions",
            "--model", "sonnet",
            "-p", "{brief}",
        ],
        # Explicit opt-in avoids spending usage on an unsolicited second
        # reviewer. SDK/print usage is still supported by subscriptions:
        # support.claude.com article 15036540, update 2026-06-15 (checked
        # 2026-08-31). Authentication and current plan limits decide the route.
        "enabled": (
            _CLAUDE_BIN is not None
            and os.environ.get("MCP_HUDDLE_CLAUDE_ENABLED", "0") != "0"
        ),
    },
    # Explicit, direct Anthropic reviewer. Deliberately disabled and excluded
    # from auto-spawn: an owner must supply API credentials and a workspace
    # header through its service/runtime environment before inviting it.
    # Do not place key or workspace values in this registry.
    {
        "name": "Claude Opus 5 (direct review)",
        # The typed profile below ignores registry argv and builds its fixed,
        # restricted invocation. Keep a binary here for discovery only.
        "cmd": [_CLAUDE_BIN or "claude"],
        "enabled": False,
        "auto": False,
        "profile": _DIRECT_OPUS_REVIEW_PROFILE,
    },
    {
        "name": "Claude Opus 5 (subscription review)",
        "cmd": [_CLAUDE_BIN or "claude"],
        "enabled": False,
        "auto": False,
        "profile": _SUBSCRIPTION_OPUS_REVIEW_PROFILE,
    },
]


def _claude_opus_review_spec() -> SpawnSpec:
    """Return the built-in manual direct-Anthropic Opus review profile."""
    return next(
        spec for spec in DEFAULT_REGISTRY
        if spec["name"] == "Claude Opus 5 (direct review)"
    )


def _protected_opus_profiles() -> dict[str, SpawnSpec]:
    return {
        spec["name"]: spec for spec in DEFAULT_REGISTRY
        if spec.get("profile") in {
            _DIRECT_OPUS_REVIEW_PROFILE, _SUBSCRIPTION_OPUS_REVIEW_PROFILE,
        }
    }


def _opus_profile_override(current: SpawnSpec, override: SpawnSpec) -> SpawnSpec:
    """Only enablement and the subscription's local endpoint are configurable."""
    result = dict(current)
    if isinstance(override.get("enabled"), bool):
        result["enabled"] = override["enabled"]
    if current.get("profile") == _SUBSCRIPTION_OPUS_REVIEW_PROFILE:
        if "mcp_url" in override:
            result["mcp_url"] = override["mcp_url"]
    result["auto"] = False
    return result


def _validate_protected_profile_names(registry: list[SpawnSpec]) -> None:
    """A typed profile cannot be aliased to bypass its explicit-selection rule."""
    canonical = {spec["profile"]: name for name, spec in _protected_opus_profiles().items()}
    for spec in registry:
        name = canonical.get(spec.get("profile"))
        if name is not None and spec.get("name") != name:
            raise AgentSpawnError("protected Opus profile must use its canonical registry name")


_VALID_CODEX_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
_VALID_CLAUDE_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
_VALID_AGY_EFFORTS = {"low", "medium", "high"}

# Exact-model preflights are intentionally short and process-local. The key
# contains only non-secret routing inputs; native CLI auth remains in its own
# config/keychain and candidate registry `pass_env` values are never copied.
_MODEL_PREFLIGHT_CACHE: dict[tuple[str, str, str, str, str], tuple[float, dict[str, str]]] = {}
_MODEL_PREFLIGHT_LOCK = threading.Lock()
_MODEL_PREFLIGHT_TTL_SEC = 60.0
_MODEL_PREFLIGHT_TIMEOUT_SEC = 45
_MODEL_PREFLIGHT_SENTINEL = "Reply with exactly this text and nothing else: HUDDLE PREFLIGHT OK"
_MODEL_PREFLIGHT_EXPECTED = "HUDDLE PREFLIGHT OK"


def probe_cli_model_response(spec: SpawnSpec, cwd: str) -> dict[str, str]:
    """Make one bounded sentinel request using an explicitly pinned model/effort.

    Only native Claude and Codex CLIs are supported. The prompt is a fixed
    public sentinel, and the process runs outside the project in an isolated
    no-tools/read-only invocation. Raw CLI output is never returned or logged.
    Missing explicit model or effort is reported as unsupported because a
    harness default cannot prove which exact route answered.
    """
    command = spec.get("cmd", [])
    if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
        return {"status": "unsupported", "reason": "unsupported_harness"}
    binary_index = _effective_binary_index(command)
    if binary_index is None:
        return {"status": "unsupported", "reason": "unsupported_harness"}
    binary = command[binary_index]
    harness = Path(binary).name
    if harness not in {"claude", "codex"}:
        return {"status": "unsupported", "reason": "unsupported_harness"}
    try:
        settings = model_settings_for_spec(spec)
    except Exception:
        return {"status": "unsupported", "reason": "invalid_model_settings"}
    model, effort = settings.get("model"), settings.get("effort")
    if not model or not effort:
        return {"status": "unsupported", "reason": "model_effort_not_explicit"}

    cache_key = (
        str(Path(binary).expanduser()), model, effort,
        os.environ.get("CODEX_HOME", ""), os.environ.get("CLAUDE_CONFIG_DIR", ""),
    )
    now = time.monotonic()
    with _MODEL_PREFLIGHT_LOCK:
        cached = _MODEL_PREFLIGHT_CACHE.get(cache_key)
        if cached and now - cached[0] < _MODEL_PREFLIGHT_TTL_SEC:
            return dict(cached[1])
        if cached:
            _MODEL_PREFLIGHT_CACHE.pop(cache_key, None)

    if harness == "claude":
        argv = [
            binary, "--restricted", "--strict-mcp-config", "--setting-sources", "",
            "--tools", "", "--permission-prompts", "none",
            "--no-session-persistence", "--model", model, "--effort", effort,
            "-p", _MODEL_PREFLIGHT_SENTINEL,
        ]
    else:
        argv = [
            binary, "exec", "--ephemeral",
            "--skip-git-repo-check", "--sandbox", "read-only",
            "-c", "mcp_servers={}",
            "--model", model, "-c", f'model_reasoning_effort="{effort}"',
            _MODEL_PREFLIGHT_SENTINEL,
        ]

    try:
        result = subprocess.run(
            argv, cwd=cwd, env=build_sanitized_environment(),
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=_MODEL_PREFLIGHT_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        probe = {"status": "failed", "reason": "response_timeout"}
    except (OSError, ValueError):
        probe = {"status": "unknown", "reason": "probe_unavailable"}
    else:
        stdout = result.stdout if isinstance(result.stdout, str) else ""
        if result.returncode != 0:
            probe = {"status": "failed", "reason": "provider_request_failed"}
        elif stdout.strip() == _MODEL_PREFLIGHT_EXPECTED:
            probe = {"status": "passed", "reason": "sentinel_response_received"}
        else:
            probe = {"status": "failed", "reason": "sentinel_response_not_received"}

    with _MODEL_PREFLIGHT_LOCK:
        if len(_MODEL_PREFLIGHT_CACHE) >= 128:
            _MODEL_PREFLIGHT_CACHE.clear()
        _MODEL_PREFLIGHT_CACHE[cache_key] = (time.monotonic(), dict(probe))
    return probe


def _validated_model_overrides(spec: SpawnSpec, binary: str) -> dict[str, str]:
    """Validate registry model controls and return only explicitly supplied ones."""
    values: dict[str, str] = {}
    for key in ("model", "effort", "variant"):
        if key not in spec:
            continue
        value = spec[key]
        if not isinstance(value, str) or not value.strip():
            raise AgentSpawnError(
                f"{spec.get('name', binary)} {key} must be a non-empty string"
            )
        values[key] = value.strip()

    if binary == "codex":
        if "variant" in values:
            raise AgentSpawnError("Codex does not support 'variant'; use 'effort'")
        if values.get("effort") and values["effort"] not in _VALID_CODEX_EFFORTS:
            raise AgentSpawnError(f"Codex unsupported effort '{values['effort']}'")
    elif binary == "claude":
        if "variant" in values:
            raise AgentSpawnError("Claude does not support 'variant'; use 'effort'")
        if values.get("effort") and values["effort"] not in _VALID_CLAUDE_EFFORTS:
            raise AgentSpawnError(f"Claude unsupported effort '{values['effort']}'")
    elif binary == "agy":
        if "variant" in values:
            raise AgentSpawnError("Antigravity does not support 'variant'; use 'effort'")
        if values.get("effort") and values["effort"] not in _VALID_AGY_EFFORTS:
            raise AgentSpawnError(f"Antigravity unsupported effort '{values['effort']}'")
    elif binary == "opencode":
        if "effort" in values:
            raise AgentSpawnError("OpenCode does not support 'effort'; use 'variant'")
    elif values:
        raise AgentSpawnError(
            f"Agent {spec.get('name', binary)} ({binary}) does not support model/effort/variant overrides"
        )
    return values


def _remove_option_values(
    argv: list[str], options: tuple[str, ...], should_remove,
) -> list[str]:
    """Remove selected option/value pairs, including --option=value forms."""
    result: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        option = next((name for name in options if arg == name), None)
        if option is not None and i + 1 < len(argv):
            value = argv[i + 1]
            if should_remove(value):
                i += 2
                continue
        else:
            option = next((name for name in options if arg.startswith(name + "=")), None)
            if option is not None and should_remove(arg[len(option) + 1:]):
                i += 1
                continue
        result.append(arg)
        i += 1
    return result


def _setting_from_argv(argv: list[str], options: tuple[str, ...]) -> str | None:
    """Read the last effective spelling of an option from a CLI template."""
    result = None
    for i, arg in enumerate(argv):
        option = next((name for name in options if arg == name), None)
        if option is not None and i + 1 < len(argv):
            result = argv[i + 1]
            continue
        option = next((name for name in options if arg.startswith(name + "=")), None)
        if option is not None:
            result = arg[len(option) + 1:]
    return result


def _codex_effort_from_argv(argv: list[str]) -> str | None:
    value = None
    for i, arg in enumerate(argv):
        config = None
        if arg in ("-c", "--config") and i + 1 < len(argv):
            config = argv[i + 1]
        elif arg.startswith("-c="):
            config = arg[3:]
        elif arg.startswith("--config="):
            config = arg[len("--config="):]
        if config:
            key, separator, configured_value = config.partition("=")
            if separator and key.strip() == "model_reasoning_effort":
                value = configured_value.strip().strip("\"'")
    return value


def model_settings_for_spec(spec: SpawnSpec) -> dict[str, str]:
    """Return effective explicit CLI model settings to pin to a room session."""
    argv = _apply_model_effort_variant(spec, list(spec.get("cmd") or []))
    binary = _effective_binary(spec.get("cmd") or [])
    settings: dict[str, str] = {}
    model = _setting_from_argv(argv, ("--model", "-m"))
    if model:
        settings["model"] = model
    if binary == "codex":
        effort = _codex_effort_from_argv(argv)
        if effort:
            settings["effort"] = effort
    else:
        effort = _setting_from_argv(argv, ("--effort",))
        variant = _setting_from_argv(argv, ("--variant",))
        if effort:
            settings["effort"] = effort
        if variant:
            settings["variant"] = variant
    return settings


def _apply_model_effort_variant(spec: SpawnSpec, argv: list[str]) -> list[str]:
    binary = _effective_binary(spec.get("cmd") or [])
    overrides = _validated_model_overrides(spec, binary)
    model = overrides.get("model")
    effort = overrides.get("effort")
    variant = overrides.get("variant")

    if not overrides:
        return argv

    out = list(argv)
    if model is not None:
        out = _remove_option_values(out, ("--model", "-m"), lambda _value: True)
    if effort is not None and binary in ("claude", "agy"):
        out = _remove_option_values(out, ("--effort",), lambda _value: True)
    if variant is not None and binary == "opencode":
        out = _remove_option_values(out, ("--variant",), lambda _value: True)
    if effort is not None and binary == "codex":
        def is_effort_setting(value: str) -> bool:
            key, separator, _configured_value = value.partition("=")
            return bool(separator and key.strip() == "model_reasoning_effort")
        out = _remove_option_values(out, ("-c", "--config"), is_effort_setting)

    # Inject before -p or {brief} or at the end
    inject_idx = len(out)
    for idx, arg in enumerate(out):
        if arg == "-p" or "{brief}" in arg:
            inject_idx = idx
            break

    injects = []
    if binary in ("claude", "agy"):
        if model: injects.extend(["--model", model])
        if effort: injects.extend(["--effort", effort])
    elif binary == "opencode":
        if model: injects.extend(["-m", model])
        if variant: injects.extend(["--variant", variant])
    elif binary == "codex":
        if model: injects.extend(["-m", model])
        if effort: injects.extend(["-c", f'model_reasoning_effort="{effort}"'])

    return out[:inject_idx] + injects + out[inject_idx:]


_FINGERPRINT_PERMISSION_KEY = re.compile(
    r"(?:permission|sandbox|approval|allowed.?tools|deny.?tools|read.?only|readonly|"
    r"writ(?:e|able)|access|trust|guard)", re.IGNORECASE,
)
_FINGERPRINT_SECRET_ARG = re.compile(
    r"(?i)^--?(?:api[_-]?(?:key|token)|access[_-]?token|refresh[_-]?token|token|secret|"
    r"password|credential|authorization|auth(?:orization)?-header)$"
)
_FINGERPRINT_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|"
    r"password|credential|authorization)\b\s*[=:]\s*)([^\s,;]+)"
)
_FINGERPRINT_AUTH_SCHEME = re.compile(
    r"(?i)(\bauthorization\s*:\s*(?:bearer|basic)\s+)[^\s,;]+"
)
_FINGERPRINT_SECRET_QUERY = re.compile(
    r"(?i)([?&](?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|"
    r"password|credential)=)[^&#\s]+"
)
_FINGERPRINT_URL_CREDENTIALS = re.compile(r"(://)[^/@\s]+@")


def _fingerprint_safe_value(value):
    """Drop credential values recursively while keeping non-secret settings."""
    if isinstance(value, dict):
        return {
            str(key): (
                "<redacted>" if _fingerprint_secret_name(str(key))
                else _fingerprint_safe_value(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_fingerprint_safe_value(item) for item in value]
    if isinstance(value, str):
        safe = _FINGERPRINT_AUTH_SCHEME.sub(r"\1<redacted>", value)
        safe = _FINGERPRINT_SECRET_ASSIGNMENT.sub(r"\1<redacted>", safe)
        safe = _FINGERPRINT_SECRET_QUERY.sub(r"\1<redacted>", safe)
        return _FINGERPRINT_URL_CREDENTIALS.sub(r"\1<redacted>@", safe)
    return value


def _fingerprint_secret_name(value: str) -> bool:
    """Match credential fields without treating limits as credentials."""
    normalized = re.sub(r"[^a-z0-9]", "", value.lower())
    return (
        normalized in {"token", "authorization", "auth"}
        or normalized.endswith((
            "apikey", "apitoken", "accesstoken", "refreshtoken", "secret", "password",
            "credential", "authorization", "authheader",
        ))
        or _FINGERPRINT_SECRET_ARG.fullmatch(value) is not None
    )


def _fingerprint_safe_argv(argv: list[str]) -> list[str]:
    """Keep effective argv shape while removing values passed as credentials."""
    result: list[str] = []
    redact_next = False
    keep_env_name_next = False
    for arg in argv:
        if keep_env_name_next:
            result.append(arg)
            keep_env_name_next = False
            continue
        if redact_next:
            result.append("<redacted>")
            redact_next = False
            continue
        if re.match(r"^--[\w-]+-env=", arg):
            result.append(arg)
            continue
        if re.match(r"^--[\w-]+-env$", arg):
            result.append(arg)
            keep_env_name_next = True
            continue
        if _FINGERPRINT_AUTH_SCHEME.search(arg):
            result.append(_FINGERPRINT_AUTH_SCHEME.sub(r"\1<redacted>", arg))
            continue
        if _fingerprint_secret_name(arg) and "=" not in arg and not arg.endswith("-env"):
            result.append(arg)
            redact_next = True
            continue
        safe = _FINGERPRINT_AUTH_SCHEME.sub(r"\1<redacted>", arg)
        safe = _FINGERPRINT_SECRET_ASSIGNMENT.sub(r"\1<redacted>", safe)
        safe = _FINGERPRINT_SECRET_QUERY.sub(r"\1<redacted>", safe)
        safe = _FINGERPRINT_URL_CREDENTIALS.sub(r"\1<redacted>@", safe)
        result.append(safe)
    return result


def spec_fingerprint(spec: SpawnSpec) -> str:
    """Return a stable SHA-256 fingerprint of an effective registry profile.

    Pass a spec from :func:`_raw_registry`; its ``cmd`` already contains the
    read-only transform when enabled. This function applies model controls to
    that command once and deliberately does not reapply read-only rewriting.
    Environment values and probe fields are never read or included; only
    explicitly passed environment variable names are part of the digest. A
    profile ``mcp_url`` enters only as its own SHA-256 digest, so a changed
    endpoint is spec drift while the result carries no URL text.
    """
    cmd = list(spec.get("cmd") or [])
    effective_argv = _apply_model_effort_variant(spec, cmd)
    canonical: dict[str, object] = {
        "name": spec.get("name", ""),
        "enabled": spec.get("enabled", True),
        "auto": spec.get("auto", True),
        "argv": _fingerprint_safe_argv(effective_argv),
        "pass_env": sorted(
            name for name in spec.get("pass_env", []) if isinstance(name, str)
        ),
    }
    for key in (
        "model", "effort", "variant", "profile", "max_tokens", "max_output_tokens",
        "token_limit", "max_completion_tokens",
    ):
        if key in spec:
            canonical[key] = _fingerprint_safe_value(spec[key])
    if "mcp_url" in spec:
        # Absent for most profiles, so their existing fingerprints are unchanged.
        canonical["mcp_url_sha256"] = hashlib.sha256(
            str(spec["mcp_url"]).encode("utf-8")
        ).hexdigest()
    for key, value in spec.items():
        if _FINGERPRINT_PERMISSION_KEY.search(str(key)):
            canonical[str(key)] = _fingerprint_safe_value(value)

    payload = json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _resolve_spawn_args(
    spec: SpawnSpec,
    brief: str,
    log_dir: Path,
    log_name: str | None = None,
    member_header: bool = False,
) -> tuple[list[str], str | None]:
    name = bus._safe_path_component(log_name or spec["name"], "agent_name")
    last_msg_path: str | None = None

    template = _apply_model_effort_variant(spec, list(spec.get("cmd") or []))
    template = _apply_codex_mcp_route(spec, template, member_header)

    argv = []
    for arg in template:
        if "{brief}" in arg:
            arg = arg.replace("{brief}", brief)
        if "{last_message}" in arg:
            last_msg_path = str(log_dir / f"{name.lower()}.last_message.txt")
            arg = arg.replace("{last_message}", last_msg_path)
        argv.append(arg)
    return argv, last_msg_path


def _codex_loopback_mcp_url(raw_url: object) -> str:
    """Rebuild a validated loopback ``/mcp`` URL from its parsed parts only."""
    if not isinstance(raw_url, str):
        raise AgentSpawnError("Codex mcp_url must be a loopback http(s) URL ending in /mcp")
    try:
        _direct_opus_review_endpoint_config(raw_url)
    except AgentSpawnError:
        raise AgentSpawnError(
            "Codex mcp_url must be a loopback http(s) URL ending in /mcp"
        ) from None
    parsed = urlparse(raw_url)
    host = f"[{parsed.hostname}]" if parsed.hostname == "::1" else parsed.hostname
    return f"{parsed.scheme}://{host}:{parsed.port}/mcp"


# Per-wake member identity for Codex Swarm members on an HTTP Huddle route.
# The raw secret lives only in the child's environment; Codex reads it into
# this header via ``env_http_headers``. It is never placed in argv or logs and
# is separate from the global MCP_HUDDLE_TOKEN guard.
MEMBER_TOKEN_ENV = "HUDDLE_MEMBER_TOKEN"
MEMBER_TOKEN_HEADER = "X-Huddle-Member"


def new_member_token() -> tuple[str, str]:
    """Return a fresh (secret, sha256 hex digest); store only the digest."""
    secret = secrets.token_urlsafe(32)
    return secret, member_token_digest(secret)


def member_token_digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def member_identity_supported(spec: SpawnSpec) -> bool:
    """Only a Codex profile with an explicit loopback HTTP ``mcp_url`` qualifies.

    Without ``mcp_url`` the child's ``huddle`` server may be stdio, which has no
    HTTP headers; such launches keep the legacy unauthenticated behavior.
    """
    return "mcp_url" in spec and _effective_binary(list(spec.get("cmd") or [])) == "codex"


def _codex_huddle_server_config(url: str, member_header: bool) -> str:
    """One inline ``mcp_servers`` override for the pinned Huddle endpoint."""
    table = ("{huddle={url=" + json.dumps(url)
             + ',default_tools_approval_mode="approve"')
    if member_header:
        table += (',env_http_headers={' + json.dumps(MEMBER_TOKEN_HEADER)
                  + "=" + json.dumps(MEMBER_TOKEN_ENV) + "}")
    return "mcp_servers=" + table + "}}"


def _apply_codex_mcp_route(
    spec: SpawnSpec, template: list[str], member_header: bool = False,
) -> list[str]:
    """Pin a Codex child's ``huddle`` MCP URL to the profile's ``mcp_url``.

    Opt-in: without ``mcp_url`` the argv is unchanged. With it, one ``-c``
    sets the ``huddle`` server to that URL with Huddle tool approval. Observed
    with ``codex mcp list``: the ``huddle`` entry shows this URL, and the other
    globally configured MCP servers (for example aggregators) remain, so this
    does not stop an agent from reaching another Huddle through them. It adds
    no server and leaves sandbox, approval, model and effort arguments from
    the enforced transforms as they are. Runs at launch, after the read-only
    and room write transforms.
    """
    if "mcp_url" not in spec or _effective_binary(template) != "codex":
        return template
    url = _codex_loopback_mcp_url(spec.get("mcp_url"))
    route = ["-c", _codex_huddle_server_config(url, member_header)]
    # Keep the prompt positional last, like the read-only transform does.
    for index in range(len(template) - 1, -1, -1):
        if "{brief}" in template[index]:
            return [*template[:index], *route, *template[index:]]
    return [*template, *route]


def _open_standalone_log(path: Path, *, create_parent: bool) -> BinaryIO:
    """Open an explicitly supplied non-room log without following symlinks.

    Room-owned paths use :func:`bus._safe_open_fd`. Standalone callers retain
    the existing arbitrary-directory API, but the final directory and file
    are opened through stable dirfds with ``O_NOFOLLOW`` and the target must
    be a regular file. The path itself is never derived from room metadata.
    """
    path = Path(path)
    if not path.name or path.name in {".", ".."}:
        raise ValueError("Invalid standalone log path")
    if create_parent:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    dir_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    parent_fd = os.open(path.parent, dir_flags)
    fd: int | None = None
    try:
        safety_flags = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        # O_NONBLOCK prevents an attacker-prepared FIFO from hanging the
        # server before fstat can reject the non-regular target. It has no
        # behavioral effect on the regular files accepted below.
        flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_NONBLOCK", 0)
        try:
            fd = os.open(
                path.name, flags | os.O_CREAT | os.O_EXCL | safety_flags,
                0o600, dir_fd=parent_fd,
            )
        except FileExistsError:
            fd = os.open(path.name, flags | safety_flags, dir_fd=parent_fd)
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise ValueError("Standalone agent log must be a regular file")
        return os.fdopen(fd, "ab", buffering=0)
    except Exception:
        if fd is not None:
            os.close(fd)
        raise
    finally:
        os.close(parent_fd)


def _close_parent_log_safely(log_file: BinaryIO) -> None:
    """Close the parent's duplicate without abandoning a spawned child.

    Once ``Popen`` succeeds, a close error must not skip child registration:
    callers would interpret that exception as a failed spawn and roll back its
    persisted lease while the child is still alive. The file object remains
    the sole descriptor owner: manually closing a number returned by
    ``fileno()`` could let its later finalizer close an unrelated reused fd.
    """
    try:
        log_file.close()
    except BaseException:
        return


def _direct_opus_review_endpoint_config(raw_url: str | None) -> str:
    """Return strict MCP JSON for a runtime-provided loopback Huddle endpoint.

    A room server chooses its own local port, so this profile must not embed a
    stale endpoint in code or registry. The URL is deliberately not repeated in
    errors or logs because a malformed runtime value could contain sensitive
    data.
    """
    if not raw_url:
        raise AgentSpawnError(f"missing required environment: {_DIRECT_OPUS_ENDPOINT_ENV}")
    try:
        parsed = urlparse(raw_url)
        valid_port = parsed.port is not None
    except ValueError:
        valid_port = False
        parsed = None
    if (
        parsed is None
        or parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or not valid_port
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != "/mcp"
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise AgentSpawnError(
            f"{_DIRECT_OPUS_ENDPOINT_ENV} must be a loopback http(s) URL ending in /mcp"
        )
    return json.dumps({"mcpServers": {"huddle": {"type": "http", "url": raw_url}}})


def _direct_opus_review_read_root(cwd: str) -> str:
    """Validate the one project directory Claude may read under --restricted."""
    candidate = Path(cwd)
    if not candidate.is_absolute() or candidate.is_symlink() or not candidate.is_dir():
        raise AgentSpawnError(
            "direct review requires an absolute, existing non-symlink approved read root"
        )
    resolved = candidate.resolve()
    home = Path.home().resolve()
    if (
        resolved == Path(resolved.anchor)
        or resolved == home
        or home.is_relative_to(resolved)
        or resolved in {
            Path("/System"), Path("/usr"), Path("/var"), Path("/private"), Path("/Volumes"),
        }
    ):
        raise AgentSpawnError("direct review requires a project-scoped approved read root")
    return str(resolved)


def _direct_opus_review_argv(brief: str, read_root: str, mcp_config: str) -> list[str]:
    """Build the non-overridable native Claude invocation for this profile."""
    return [
        _CLAUDE_BIN or "claude",
        *_DIRECT_OPUS_REVIEW_FLAGS,
        "--bare",
        "--restricted",
        "--strict-mcp-config",
        "--mcp-config", mcp_config,
        "--add-dir", read_root,
        "--model", "claude-opus-5-5",
        "-p", (
            f"{brief}\n\n"
            "For this direct-review turn, use Huddle only to return one `result` "
            "to the request that invoked this review. Do not post Huddle `request` "
            "messages or initiate additional work."
        ),
    ]


def _subscription_opus_review_argv(brief: str, read_root: str, mcp_config: str) -> list[str]:
    """Use native account login with the same fixed read-only review boundary.

    --bare is intentionally absent: it disables OAuth/Keychain authentication.
    --restricted ignores user/project settings; only the selected room MCP and
    approved read root are exposed. No provider, model, or permission fallback.
    """
    argv = _direct_opus_review_argv(brief, read_root, mcp_config)
    argv.remove("--bare")
    argv[argv.index("--tools") + 1] = "Read,Glob,Grep,ToolSearch"
    argv[argv.index("--allowedTools") + 1] = (
        "Read,Glob,Grep,ToolSearch," + argv[argv.index("--allowedTools") + 1]
    )
    argv[argv.index("-p"):argv.index("-p")] = [
        "--setting-sources", "", "--disable-slash-commands", "--no-chrome",
        "--output-format", "stream-json", "--verbose",
    ]
    argv[-1] = argv[-1].replace("direct-review turn", "subscription-review turn")
    return argv


def _verify_subscription_auth(env: dict[str, str], cwd: str) -> None:
    """Native CLI owns credentials. Inspect only non-secret status fields."""
    try:
        probe = subprocess.run(
            [_CLAUDE_BIN or "claude", "--restricted", "--setting-sources", "",
             "auth", "status", "--json"],
            cwd=cwd, env=env, capture_output=True, text=True, timeout=20,
        )
        auth = json.loads(probe.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise AgentSpawnError("subscription review: native auth status unavailable") from None
    if not (
        probe.returncode == 0 and isinstance(auth, dict)
        and auth.get("loggedIn") is True
        and auth.get("authMethod") == "claude.ai"
        and auth.get("apiProvider") == "firstParty"
        and auth.get("subscriptionType")
    ):
        raise AgentSpawnError("subscription review requires an existing claude.ai subscription login")


def probe_cli_login(spec: SpawnSpec, cwd: str) -> CliLoginProbe:
    """Check a known CLI's local login without issuing a model request.

    The status proves only that the CLI recognizes a local login. It does not
    prove that the requested model, provider route, or current quota works.
    Output is deliberately limited to closed status/reason codes because CLI
    stdout and stderr can contain account identities or credential material.
    Native config paths and Keychain access remain available through the same
    minimal HOME/CODEX_HOME/CLAUDE_CONFIG_DIR child environment as a spawn.
    Provider credential variables and registry pass_env values are not copied.
    """
    if spec.get("profile") == _DIRECT_OPUS_REVIEW_PROFILE:
        return {"status": "unknown", "reason": "non_native_profile"}
    command = spec.get("cmd", [])
    if not isinstance(command, list) or not all(isinstance(x, str) for x in command):
        return {"status": "unknown", "reason": "unsupported_harness"}
    binary_index = _effective_binary_index(command)
    if binary_index is None:
        return {"status": "unknown", "reason": "unsupported_harness"}
    binary = command[binary_index]
    harness = Path(binary).name
    if harness == "claude":
        argv = [binary, "auth", "status", "--json"]
    elif harness == "codex":
        argv = [binary, "login", "status"]
    else:
        return {"status": "unknown", "reason": "unsupported_harness"}

    try:
        result = subprocess.run(
            argv, cwd=cwd, env=build_sanitized_environment(),
            capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        return {"status": "unknown", "reason": "probe_timeout"}
    except (OSError, ValueError):
        return {"status": "unknown", "reason": "probe_unavailable"}

    if harness == "claude":
        try:
            payload = json.loads(result.stdout)
        except (TypeError, ValueError):
            return {"status": "unknown", "reason": "unrecognized_output"}
        if isinstance(payload, dict) and payload.get("loggedIn") is False:
            return {"status": "unauthenticated", "reason": "cli_logged_out"}
        if result.returncode == 0 and isinstance(payload, dict) and payload.get("loggedIn") is True:
            return {"status": "authenticated", "reason": "cli_login_present"}
        return {"status": "unknown", "reason": "unrecognized_output"}

    # Codex versions may write login status to stdout or stderr. Inspect only
    # recognized whole lines, never return or log either raw stream. Conflicting
    # signals fail closed even when the process exits successfully.
    lines = result.stdout.splitlines() + result.stderr.splitlines()
    logged_out = any(re.fullmatch(r"Not logged in\s*", line, re.IGNORECASE) for line in lines)
    logged_in = any(re.fullmatch(r"Logged in using .+", line, re.IGNORECASE) for line in lines)
    if logged_out and logged_in:
        return {"status": "unknown", "reason": "unrecognized_output"}
    if logged_out:
        return {"status": "unauthenticated", "reason": "cli_logged_out"}
    if result.returncode == 0 and logged_in:
        return {"status": "authenticated", "reason": "cli_login_present"}
    return {"status": "unknown", "reason": "unrecognized_output"}


def log_spawn_failure(
    spec: SpawnSpec,
    brief: str,
    cwd: str,
    log_dir: Path,
    exc: BaseException,
) -> None:
    """Write a value-free spawn failure summary to daemon stderr.

    Registry argv may contain an expanded room brief, provider arguments, or
    other caller-controlled values.  Neither argv nor the exception message is
    safe to copy into a shared daemon log.
    """
    log_path = log_dir / f"{spec['name'].lower()}.events.jsonl"
    print(
        "[mcp-huddle] failed to spawn "
        f"{spec['name']}: error_type={type(exc).__name__}; "
        f"arg_count={len(spec.get('cmd') or [])}; "
        f"cwd={cwd!r}; log_path={log_path}",
        file=sys.stderr,
        flush=True,
    )


def _reap_in_background(
    proc: subprocess.Popen,
    name: str,
    on_exit=None,
    cleanup_dir: str | None = None,
    owner_room_id: str = "",
    process_handle: str | None = None,
) -> str:
    """Ensure short-lived spawned agents do not remain as defunct children.

    on_exit: optional callable(returncode) invoked once the process exits.
    Used by the wake machinery to clear the busy lease and drain queued
    requests the moment an agent turn ends (event-driven, no polling)."""
    handle = child_processes.register(proc, owner_room_id, process_handle)
    if owner_room_id:
        try:
            room_status = bus.get_room_info(owner_room_id).get("status")
        except Exception:
            room_status = "missing"
        if room_status in {"missing", "closing", "closed", "resolved"}:
            # Close may win after the pre-spawn gate but before registration.
            # Now that we own the exact Popen, stop it synchronously instead of
            # waiting for the periodic cooperative reconciliation sweep.
            child_processes.close_room(owner_room_id)

    def wait_for_exit() -> None:
        returncode = None
        completed = False
        try:
            returncode = child_processes.wait(owner_room_id, handle)
            completed = returncode is not None
        except Exception:
            returncode = proc.poll()
            completed = returncode is not None
        if completed:
            if cleanup_dir is not None:
                shutil.rmtree(cleanup_dir, ignore_errors=True)
            if on_exit is not None:
                try:
                    on_exit(returncode)
                except Exception:
                    pass
        with _BACKGROUND_LOCK:
            _REAPER_THREADS.discard(threading.current_thread())

    # daemon=True so these reaper threads never block interpreter exit. Polling
    # goes through child_processes, serialized with termination; callbacks and
    # cleanup happen only after the ownership record has been removed.
    thread = threading.Thread(
        target=wait_for_exit,
        name=f"mcp-huddle-reap-{name}-{proc.pid}",
        daemon=True,
    )
    with _BACKGROUND_LOCK:
        _REAPER_THREADS.add(thread)
        # Publish and start atomically with respect to the test drain. Without
        # this ordering, teardown can snapshot the Thread after add() but
        # before start(), where join() raises RuntimeError.
        try:
            thread.start()
        except BaseException:
            _REAPER_THREADS.discard(thread)
            raise
    return handle


def _terminate_unregistered_child(
    proc: subprocess.Popen,
    owner_room_id: str,
    process_handle: str,
    timeout: float = 2.0,
) -> bool:
    """Stop and reap the exact child when ownership setup cannot complete.

    This path uses only the just-created ``Popen`` object. It never consults
    or signals a persisted PID. A stubborn child is escalated from terminate
    to kill. It returns ``False`` if either signal/wait path is unavailable so
    the caller can retain exact ownership plus a fallback reaper. An exact
    registry record is discarded only after ``poll`` confirms exit.
    """
    try:
        running = proc.poll() is None
    except Exception:
        running = True
    if running:
        try:
            proc.terminate()
        except Exception:
            pass
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            still_running = proc.poll() is None
        except Exception:
            still_running = True
        if still_running:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            proc.wait(timeout=timeout)
        except Exception:
            pass
    except Exception:
        pass

    try:
        exited = proc.poll() is not None
    except Exception:
        exited = False
    if exited:
        child_processes.discard_exited(proc, owner_room_id, process_handle)
    return exited


def _retain_failed_setup_child(
    proc: subprocess.Popen,
    name: str,
    owner_room_id: str,
    process_handle: str,
    cleanup_dir: str | None,
    on_exit=None,
) -> _RetainedChild:
    """Retain exact authority and lifecycle when normal reaper setup fails.

    The outcome is a successful active-spawn contract: the fallback retains
    the exact Popen, will clean the temporary cwd, and will invoke the original
    callback once. ``registered=False`` tells health checks to inspect that
    exact Popen rather than treating the intentionally unowned handle as dead.
    """
    handle = process_handle
    registered = False
    try:
        registered = child_processes.owns_exact(proc, owner_room_id, handle)
    except Exception:
        registered = False
    if not registered:
        try:
            child_processes.register(proc, owner_room_id, handle)
        except Exception:
            pass
        try:
            registered = child_processes.owns_exact(proc, owner_room_id, handle)
        except Exception:
            registered = False
    if not registered:
        # A caller-supplied handle may already belong to another exact child.
        # Prefer a private cleanup handle, but registry failure must not turn a
        # still-live exact Popen into a rollback-able spawn error.
        try:
            private_handle = child_processes.new_handle()
        except Exception:
            private_handle = None
        if private_handle is not None:
            try:
                child_processes.register(proc, owner_room_id, private_handle)
            except Exception:
                pass
            try:
                registered = child_processes.owns_exact(
                    proc, owner_room_id, private_handle,
                )
            except Exception:
                registered = False
            if registered:
                handle = private_handle

    lifecycle_lock = threading.Lock()
    lifecycle_finished = False

    def wait_for_failed_setup() -> None:
        nonlocal lifecycle_finished
        returncode = None
        completed = False
        if registered:
            try:
                returncode = child_processes.wait(owner_room_id, handle)
                completed = returncode is not None
            except Exception:
                try:
                    returncode = proc.poll()
                except Exception:
                    returncode = None
                completed = returncode is not None
        else:
            # The registry itself is unavailable. Retain and reap the exact
            # Popen directly; no persisted PID or foreign process is touched.
            while returncode is None:
                try:
                    returncode = proc.poll()
                except Exception:
                    returncode = None
                if returncode is None:
                    time.sleep(0.05)
            completed = True
        if completed:
            with lifecycle_lock:
                if lifecycle_finished:
                    return
                lifecycle_finished = True
            if cleanup_dir is not None:
                shutil.rmtree(cleanup_dir, ignore_errors=True)
            if on_exit is not None:
                try:
                    on_exit(returncode)
                except Exception:
                    pass

    def wait_for_failed_setup_tracked() -> None:
        try:
            wait_for_failed_setup()
        finally:
            with _BACKGROUND_LOCK:
                _REAPER_THREADS.discard(threading.current_thread())

    try:
        thread = threading.Thread(
            target=wait_for_failed_setup_tracked,
            name=f"mcp-huddle-reap-failed-setup-{name}-{proc.pid}",
            daemon=True,
        )
    except BaseException:
        thread = None
    if thread is not None:
        with _BACKGROUND_LOCK:
            _REAPER_THREADS.add(thread)
            try:
                thread.start()
            except BaseException:
                _REAPER_THREADS.discard(thread)
            else:
                return _RetainedChild(handle, registered)

    # ``threading.Thread`` can fail under process thread exhaustion. The
    # low-level primitive provides an independent reserve while retaining the
    # same join/is_alive bookkeeping used by deterministic test teardown.
    try:
        emergency = _EmergencyReaper(
            wait_for_failed_setup,
            f"mcp-huddle-emergency-reap-{name}-{proc.pid}",
        )
    except BaseException:
        emergency = None
    if emergency is not None:
        with _BACKGROUND_LOCK:
            _REAPER_THREADS.add(emergency)
            try:
                emergency.start()
            except BaseException:
                _REAPER_THREADS.discard(emergency)
            else:
                return _RetainedChild(handle, registered)

    # No asynchronous execution facility remains. Waiting synchronously is
    # deliberately fail-closed: it may delay the request, but the exact child
    # cannot coexist with a rollback claim. Normal Popen polling either
    # confirms exit (running cleanup/callback) or keeps the persisted lease.
    wait_for_failed_setup()
    return _RetainedChild(handle, registered)


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
    (preserving order), except the protected direct-review profile; a new name
    is appended. Inputs are not mutated.
    """
    merged: list[SpawnSpec] = [dict(spec) for spec in base]  # type: ignore[misc]
    index = {spec.get("name"): i for i, spec in enumerate(merged)}
    for spec in overrides:
        name = spec.get("name")
        if name in index:
            current = merged[index[name]]
            if current.get("profile") in {
                _DIRECT_OPUS_REVIEW_PROFILE, _SUBSCRIPTION_OPUS_REVIEW_PROFILE,
            }:
                # This contract is a fixed runner, not a configurable command.
                # An owner may deliberately enable it, but cannot replace its
                # profile, argv, or exclusion from auto-spawn in registry JSON.
                merged[index[name]] = _opus_profile_override(current, spec)
            else:
                merged[index[name]] = spec
        else:
            index[name] = len(merged)
            merged.append(spec)
    return merged


def _preserve_direct_opus_profile_contract(registry: list[SpawnSpec]) -> list[SpawnSpec]:
    """Keep both built-in Opus names bound to their reviewed runner contracts."""
    profiles = _protected_opus_profiles()
    preserved: list[SpawnSpec] = []
    for spec in registry:
        built_in = profiles.get(spec.get("name", ""))
        if built_in is None:
            preserved.append(spec)
            continue
        preserved.append(_opus_profile_override(built_in, spec))
    return preserved


# Read-only discussant mode (MCP_HUDDLE_READONLY): spawned agents may READ
# (files, web, docs, rules, memory) but must not EDIT/WRITE anything — they
# participate only through the huddle MCP tools (message_post / messages_read),
# never by changing files. Agents communicate via the bus, not file edits, so
# this does not hamper participation.
_CLAUDE_RO_FLAGS = [
    "--allowedTools", "Read,Glob,Grep,WebFetch,WebSearch,mcp__huddle__*",
    "--disallowedTools", "Edit,Write,NotebookEdit,MultiEdit,Bash",
    "--permission-mode", "manual", "--permission-prompts", "none",
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
    cmd = list(spec.get("cmd") or [])
    cli_index = _effective_binary_index(cmd)
    binary = _effective_binary(cmd)
    if binary == "claude":
        prefix = cmd[: (cli_index + 1)] if cli_index is not None else []
        claude_args = cmd[(cli_index + 1):] if cli_index is not None else cmd
        # A registry-supplied allowlist, denylist, or permission mode must not
        # weaken the enforced set. These list-valued switches end at the next
        # CLI option; `{brief}` is also a boundary for bare prompt templates.
        claude_args = _remove_variadic_options(
            claude_args,
            ("--allowedTools", "--allowed-tools", "--disallowedTools", "--disallowed-tools"),
        )
        claude_args = _remove_option_values(
            claude_args, ("--permission-mode", "--permission-prompts"), lambda _value: True,
        )
        claude_args = [
            arg for arg in claude_args
            if arg not in ("--dangerously-skip-permissions", "--allow-dangerously-skip-permissions")
            and not arg.startswith((
                "--dangerously-skip-permissions=", "--allow-dangerously-skip-permissions=",
            ))
        ]
        cmd = [*prefix, *_CLAUDE_RO_FLAGS, *claude_args]
    elif binary == "codex":
        # Leave timeout's own options (for example ``timeout -s TERM``)
        # untouched. Only rewrite Codex sandbox options after its executable.
        prefix = cmd[: (cli_index + 1)] if cli_index is not None else []
        codex_args = cmd[(cli_index + 1):] if cli_index is not None else cmd
        out: list[str] = list(prefix)
        i = 0
        while i < len(codex_args):
            arg = codex_args[i]
            if arg in ("--dangerously-bypass-approvals-and-sandbox", "--approve-for-me",
                       "--dangerously-bypass-hook-trust", "--full-auto", "--yolo",
                       "--ignore-rules") or arg.startswith("--dangerously-bypass-"):
                i += 1
                continue
            if arg in ("--profile", "-p", "--add-dir", "--remote", "--remote-auth-token-env"):
                i += 2
                continue
            if arg.startswith(("--profile=", "--add-dir=", "--remote=", "--remote-auth-token-env=")):
                i += 1
                continue
            if arg in ("-s", "--sandbox"):
                # Remove any registry-provided sandbox so the enforced value
                # cannot be overridden by a later duplicate flag.
                i += 2
                continue
            if arg.startswith("--sandbox=") or arg.startswith("-s="):
                i += 1
                continue
            if arg in ("-c", "--config"):
                value = codex_args[i + 1] if i + 1 < len(codex_args) else ""
                key, separator, _configured_value = value.partition("=")
                if separator and key.strip() in ("model", "model_reasoning_effort"):
                    out.extend((arg, value))
                i += 2
                continue
            if arg.startswith("--config=") or arg.startswith("-c="):
                value = arg.split("=", 1)[1]
                key, separator, _configured_value = value.partition("=")
                if separator and key.strip() in ("model", "model_reasoning_effort"):
                    out.append(arg)
                i += 1
                continue
            out.append(arg)
            i += 1
        # Auto-approve huddle MCP tools so read-only doesn't cancel them;
        # insert before the trailing positional ({brief}).
        readonly = ["-s", "read-only", "-c", 'mcp_servers.huddle.default_tools_approval_mode="approve"']
        if out and out[-1] == "{brief}":
            out = [*out[:-1], *readonly, out[-1]]
        else:
            out.extend(readonly)
        cmd = out
    return {**spec, "cmd": cmd}


def _remove_variadic_options(argv: list[str], options: tuple[str, ...]) -> list[str]:
    """Remove list-valued switches and their values until the next option."""
    result: list[str] = []
    i = 0
    removing_values = False
    while i < len(argv):
        arg = argv[i]
        option = next((name for name in options if arg == name or arg.startswith(name + "=")), None)
        if option is not None:
            removing_values = "=" not in arg
            i += 1
            continue
        if removing_values and not arg.startswith("-") and "{brief}" not in arg:
            i += 1
            continue
        removing_values = False
        result.append(arg)
        i += 1
    return result


def readonly_enforced(spec: SpawnSpec) -> bool:
    """Return whether the effective CLI has Huddle's supported read-only gate.

    This checks explicit CLI overrides as well as Huddle's injected flags. It
    does not claim OS-level sandboxing for runners without a verified gate.
    """
    if not _readonly_enabled():
        return False
    return _readonly_command_enforced(spec)


def _readonly_command_enforced(spec: SpawnSpec) -> bool:
    """Check the read-only transform of ``spec`` regardless of the env flag."""
    command = _apply_readonly(spec).get("cmd")
    if not isinstance(command, list) or not all(isinstance(arg, str) for arg in command):
        return False
    cli_index = _effective_binary_index(command)
    if cli_index is None:
        return False
    binary = _effective_binary(command)
    args = command[cli_index + 1:]
    if binary == "claude":
        unsafe_flags = {
            "--settings", "--setting-sources", "--mcp-config", "--add-dir",
            "--plugin-dir", "--agents", "--permission-mode", "--permission-prompts",
            "--allowedTools", "--allowed-tools", "--disallowedTools", "--disallowed-tools",
            "--dangerously-skip-permissions", "--allow-dangerously-skip-permissions",
        }
        # The flags injected above are the only accepted values for these
        # options; local settings/MCP extensions can carry independent tools.
        allowed = _setting_from_argv(args, ("--allowedTools", "--allowed-tools"))
        denied = _setting_from_argv(args, ("--disallowedTools", "--disallowed-tools"))
        mode = _setting_from_argv(args, ("--permission-mode",))
        prompts = _setting_from_argv(args, ("--permission-prompts",))
        expected = set(_CLAUDE_RO_FLAGS[1].split(","))
        denied_set = set((denied or "").split(","))
        if allowed != _CLAUDE_RO_FLAGS[1] or mode != "manual" or prompts != "none":
            return False
        if not {"Edit", "Write", "NotebookEdit", "MultiEdit", "Bash"}.issubset(denied_set):
            return False
        # Verify exactly one effective enforced allow/deny/mode trio.
        return not any(arg.partition("=")[0] in unsafe_flags for arg in args if arg not in {
            "--allowedTools", "--disallowedTools", "--permission-mode", "--permission-prompts",
        }) and expected == set(allowed.split(","))
    if binary == "codex":
        if any(arg in {
            "--dangerously-bypass-approvals-and-sandbox", "--approve-for-me", "--full-auto", "--yolo",
            "--profile", "--remote", "--add-dir", "--ignore-rules",
        } or arg.startswith(("--profile=", "--remote=", "--add-dir=", "--dangerously-bypass-"))
               for arg in args):
            return False
        for i, arg in enumerate(args):
            value = args[i + 1] if arg in ("-c", "--config") and i + 1 < len(args) else (
                arg.split("=", 1)[1] if arg.startswith(("-c=", "--config=")) else ""
            )
            if value:
                key, separator, _setting = value.partition("=")
                if not separator or key.strip() not in {
                    "model", "model_reasoning_effort", "mcp_servers.huddle.default_tools_approval_mode",
                }:
                    return False
        sandbox = _setting_from_argv(args, ("-s", "--sandbox"))
        approval = None
        for i, arg in enumerate(args):
            value = args[i + 1] if arg in ("-c", "--config") and i + 1 < len(args) else (
                arg.split("=", 1)[1] if arg.startswith(("-c=", "--config=")) else ""
            )
            key, separator, configured_value = value.partition("=")
            if separator and key.strip() == "mcp_servers.huddle.default_tools_approval_mode":
                approval = configured_value.strip().strip("\"'")
        return sandbox == "read-only" and approval == "approve"
    return False


# Room-scoped write mode. A room that explicitly selects a shared worktree gets
# a bounded write command built by Huddle — never the raw registry argv and
# never MCP_HUDDLE_READONLY=0.
# - Claude: a Huddle-built `--restricted` invocation (claude --help: ignores
#   user/project/local settings files, so no inherited additionalDirectories or
#   allow rules; confines file tools to cwd + --add-dir; removes code-running
#   tools; refuses bypassPermissions). `--strict-mcp-config` exposes only the
#   profile's loopback Huddle MCP. `acceptEdits` + `--permission-prompts none`
#   auto-accepts in-bounds edits and denies everything that would prompt.
#   Managed (admin policy) settings still apply by design. Because
#   --restricted drops the user's settings file, the user's own Guard is
#   re-supplied through a Huddle-generated `--settings` (which --restricted
#   still honours): only PreToolUse hooks covering file tools plus
#   permissions.deny/ask — never allow rules, additionalDirectories or mode.
#   No mandatory Edit and Write Guard hook → Claude write is rejected.
# - Codex: `workspace-write` with every inheritable workspace-write key pinned
#   on the command line: exact writable_roots, no $TMPDIR, no /tmp, no network.
_CLAUDE_WRITE_TOOLS = "Read,Glob,Grep,Edit,Write,ToolSearch"
_CLAUDE_GUARDED_TOOLS = ("Edit", "Write")
_CLAUDE_FILE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
_CLAUDE_WRITE_FLAGS = [
    "--restricted", "--setting-sources", "", "--strict-mcp-config",
    "--tools", _CLAUDE_WRITE_TOOLS,
    "--allowedTools", "Read,Glob,Grep,ToolSearch,mcp__huddle__*",
    "--disallowedTools", "Bash,WebFetch,WebSearch",
    "--permission-mode", "acceptEdits", "--permission-prompts", "none",
    "--disable-slash-commands", "--no-chrome",
]
_WRITE_ROOT_RE = re.compile(r"/[A-Za-z0-9 ._@+/-]*\Z")


class WritePolicyUnsupported(ValueError):
    """The profile cannot enforce the room's bounded write policy."""


def _validated_write_roots(roots: list[str]) -> list[str]:
    checked = []
    for root in roots:
        if (not isinstance(root, str) or not _WRITE_ROOT_RE.fullmatch(root)
                or "/../" in root + "/" or "/./" in root + "/"):
            raise WritePolicyUnsupported("write root must be a plain absolute ASCII path")
        checked.append(root)
    return checked


def _codex_workspace_write_config(extra_roots: list[str]) -> list[str]:
    """Pin every workspace-write key so ~/.codex/config.toml cannot widen it.

    ``writable_roots`` is always set (``[]`` for shared_only) because a
    command-line value replaces an inherited array instead of merging with it.
    """
    value = ", ".join(f'"{root}"' for root in _validated_write_roots(extra_roots))
    return [
        "-c", f"sandbox_workspace_write.writable_roots=[{value}]",
        "-c", "sandbox_workspace_write.exclude_tmpdir_env_var=true",
        "-c", "sandbox_workspace_write.exclude_slash_tmp=true",
        "-c", "sandbox_workspace_write.network_access=false",
    ]


def _claude_user_settings_path() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home() / ".claude") / "settings.json"


def _hook_matcher_covers(matcher: object, tool: str) -> bool:
    if matcher is None or matcher in ("", "*"):
        return True
    if not isinstance(matcher, str):
        return False
    try:
        return re.fullmatch(matcher, tool) is not None
    except re.error:
        return False


def _claude_guard_settings() -> str:
    """Return `--settings` JSON carrying only the user's file-tool Guard.

    Re-read at every check/launch, so removing the Guard disables Claude write.
    """
    required_guard = os.environ.get("MCP_HUDDLE_CLAUDE_GUARD_COMMAND", "").strip()
    if not required_guard:
        raise WritePolicyUnsupported(
            "Claude write rooms need MCP_HUDDLE_CLAUDE_GUARD_COMMAND set to the exact Guard hook command"
        )
    try:
        data = json.loads(_claude_user_settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise WritePolicyUnsupported(
            "Claude write rooms need readable user settings with the Edit/Write Guard hook"
        ) from None
    if not isinstance(data, dict) or data.get("disableAllHooks") is True:
        raise WritePolicyUnsupported("Claude user settings disable or omit hooks")
    hooks = data.get("hooks")
    pre = hooks.get("PreToolUse") if isinstance(hooks, dict) else None
    kept = [
        entry for entry in (pre if isinstance(pre, list) else [])
        if isinstance(entry, dict) and isinstance(entry.get("hooks"), list)
        and any(_hook_matcher_covers(entry.get("matcher"), tool) for tool in _CLAUDE_FILE_TOOLS)
    ]

    def guarded(tool: str) -> bool:
        return any(
            _hook_matcher_covers(entry.get("matcher"), tool)
            and any(isinstance(hook, dict) and hook.get("type") == "command"
                    and hook.get("command") == required_guard
                    for hook in entry["hooks"])
            for entry in kept
        )

    missing = [tool for tool in _CLAUDE_GUARDED_TOOLS if not guarded(tool)]
    if missing:
        raise WritePolicyUnsupported(
            f"Claude write rooms need the user's PreToolUse Guard hook for {', '.join(missing)}"
        )
    settings: dict = {"hooks": {"PreToolUse": kept}}
    permissions = data.get("permissions")
    if isinstance(permissions, dict):
        rules = {
            key: [rule for rule in permissions[key] if isinstance(rule, str)]
            for key in ("deny", "ask") if isinstance(permissions.get(key), list)
        }
        if rules:
            settings["permissions"] = rules
    return json.dumps(settings, ensure_ascii=False, separators=(",", ":"))


def _claude_write_argv(spec: SpawnSpec, cmd: list[str], cli_index: int,
                       extra: list[str]) -> list[str]:
    """Build the fixed Claude write invocation; registry flags are discarded."""
    mcp_url = spec.get("mcp_url")
    if not isinstance(mcp_url, str) or not mcp_url:
        raise WritePolicyUnsupported(
            "Claude write rooms need a loopback mcp_url in the registry profile"
        )
    try:
        mcp_config = _direct_opus_review_endpoint_config(mcp_url)
    except AgentSpawnError:
        raise WritePolicyUnsupported("Claude mcp_url must be a loopback http(s) URL ending in /mcp") from None
    args = cmd[cli_index + 1:]
    passthrough: list[str] = []
    output_format = _setting_from_argv(args, ("--output-format",))
    if output_format in ("text", "json", "stream-json"):
        passthrough += ["--output-format", output_format]
    if "--verbose" in args:
        passthrough.append("--verbose")
    settings = model_settings_for_spec(spec)
    model_args = [
        *(["--model", settings["model"]] if settings.get("model") else []),
        *(["--effort", settings["effort"]] if settings.get("effort") else []),
    ]
    return [
        *cmd[:cli_index + 1], *_CLAUDE_WRITE_FLAGS,
        "--settings", _claude_guard_settings(),
        "--mcp-config", mcp_config,
        *[arg for root in extra for arg in ("--add-dir", root)],
        *model_args, *passthrough, "-p", "{brief}",
    ]


def apply_room_write_policy(spec: SpawnSpec, extra_roots: list[str] | None = None) -> SpawnSpec:
    """Return a bounded write variant of ``spec`` for a write-enabled room.

    The launch cwd is the room's worktree (or the member's subworktree);
    ``extra_roots`` are additional writable directories. Raises
    WritePolicyUnsupported when the profile cannot enforce the policy.
    """
    extra = _validated_write_roots(list(extra_roots or []))
    if spec.get("profile") in {_DIRECT_OPUS_REVIEW_PROFILE, _SUBSCRIPTION_OPUS_REVIEW_PROFILE}:
        raise WritePolicyUnsupported("typed review profiles are read-only by contract")
    if not _readonly_command_enforced(spec):
        raise WritePolicyUnsupported(
            f"{spec.get('name', '?')} has no verified permission gate for a write room"
        )
    cmd = list(_apply_readonly(spec)["cmd"])
    cli_index = _effective_binary_index(cmd)
    binary = _effective_binary(cmd)
    if cli_index is None:
        raise WritePolicyUnsupported("profile executable is not recognized")
    start = cli_index + 1
    if binary == "claude":
        return {**spec, "cmd": _claude_write_argv(spec, cmd, cli_index, extra)}
    if binary == "codex":
        args = cmd[start:]
        if any(arg in ("-C", "--cd") or arg.startswith(("-C=", "--cd=")) for arg in args):
            raise WritePolicyUnsupported("Codex workspace root must come from the room")
        sandbox_at = [i for i in range(len(args) - 1)
                      if args[i] in ("-s", "--sandbox") and args[i + 1] == "read-only"]
        if len(sandbox_at) != 1:
            raise WritePolicyUnsupported("Codex read-only sandbox was not found")
        i = start + sandbox_at[0]
        cmd[i:i + 2] = ["-s", "workspace-write", *_codex_workspace_write_config(extra)]
        return {**spec, "cmd": cmd}
    raise WritePolicyUnsupported(f"{spec.get('name', '?')} has no supported write policy")


def _codex_resume_sandbox_args(write_roots: list[str] | None) -> list[str]:
    """Sandbox/approval args for ``codex exec resume``.

    ``write_roots=None`` keeps the process-wide read-only default; a list
    selects the room's bounded ``workspace-write`` policy (cwd + those roots).
    """
    if write_roots is not None:
        return [
            "-c", 'sandbox_mode="workspace-write"',
            "-c", "features.guardian_approval=false",
            "-c", 'mcp_servers.huddle.default_tools_approval_mode="approve"',
            *_codex_workspace_write_config(write_roots),
        ]
    readonly = _readonly_enabled()
    sandbox = "read-only" if readonly else _CODEX_SANDBOX
    args = ["-c", f'sandbox_mode="{sandbox}"', "-c", "features.guardian_approval=false"]
    if readonly:
        args += ["-c", 'mcp_servers.huddle.default_tools_approval_mode="approve"']
    return args


def _raw_registry() -> list[SpawnSpec]:
    """Merged registry BEFORE availability filtering.

    Precedence: MCP_HUDDLE_SPAWN_REGISTRY env (full replacement) >
    ~/.mcp-huddle/registry.json (merged onto defaults) > DEFAULT_REGISTRY.
    When MCP_HUDDLE_READONLY is set, every spec is rewritten to read-only.
    """
    env_registry = _load_env_registry()
    if env_registry is not None:
        reg = _preserve_direct_opus_profile_contract(env_registry)
    else:
        file_overrides = _load_registry_file()
        reg = (_merge_registry(DEFAULT_REGISTRY, file_overrides)
               if file_overrides is not None else list(DEFAULT_REGISTRY))
    _validate_protected_profile_names(reg)
    if _readonly_enabled() or any(
        spec.get("profile") == _DIRECT_OPUS_REVIEW_PROFILE and spec.get("enabled")
        for spec in reg
    ):
        reg = [
            _apply_readonly(spec)
            if _readonly_enabled() or (
                spec.get("profile") == _DIRECT_OPUS_REVIEW_PROFILE and spec.get("enabled")
            )
            else spec
            for spec in reg
        ]
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
    owner_room_id: str = "",
    process_handle: str | None = None,
    on_log_open: Callable[[int, str], None] | None = None,
    on_log_open_identity: Callable[[int, str, int, int], None] | None = None,
    log_name: str | None = None,
    member_token: str | None = None,
) -> tuple[int, str, str | None]:
    """Spawn one agent.

    member_token: optional per-wake secret. Used only when
    :func:`member_identity_supported`; it is put in the child environment and
    sent by Codex as ``X-Huddle-Member``. Otherwise it is ignored.

    Returns (pid, log_path, last_message_path).
    last_message_path is None for agents whose argv doesn't reference {last_message}.
    log_name: room participant whose log/last-message paths this turn owns.
    Defaults to the profile name; a Swarm replacement profile passes the
    original member so readers and receipts keep one room identity.

    on_exit: optional callable(returncode) fired when the process exits.
    on_log_open: optional callable(start_offset, log_path) fired after opening
    the append log and before Popen. Its offset is the open fd's byte size at
    that instant. An exception prevents launch and closes the descriptor.

    Side effects: creates log_dir, opens log file, redirects stdout+stderr to it.
    """
    _validate_protected_profile_names([spec])
    profile_name = bus._safe_path_component(spec["name"], "agent_name")
    name = bus._safe_path_component(log_name or profile_name, "agent_name")
    # Allocate the ownership generation before any resource or Popen: even
    # handle generation failure must leave no child or profile temp cwd.
    process_handle = process_handle or child_processes.new_handle()
    if owner_room_id:
        log_path, _ = bus._agent_paths(owner_room_id, name, create=True)
        log_dir = log_path.parent
    else:
        log_path = log_dir / f"{name.lower()}.events.jsonl"
    cleanup_dir: str | None = None
    if profile_name == "Codex":
        cwd, brief = _codex_safe_cwd_and_brief(cwd, brief)
    if spec.get("profile") in {_DIRECT_OPUS_REVIEW_PROFILE, _SUBSCRIPTION_OPUS_REVIEW_PROFILE}:
        # This typed profile deliberately ignores registry argv: it always has
        # a neutral cwd, a single validated project read root, direct API
        # routing, and the reviewed Claude tool allowlist.
        env = _spawn_environment(spec)
        read_root = _direct_opus_review_read_root(cwd)
        subscription = spec.get("profile") == _SUBSCRIPTION_OPUS_REVIEW_PROFILE
        endpoint = spec.get("mcp_url") if subscription else env.pop(_DIRECT_OPUS_ENDPOINT_ENV, None)
        if subscription and not isinstance(endpoint, str):
            raise AgentSpawnError("subscription review requires a local mcp_url in the registry")
        mcp_config = _direct_opus_review_endpoint_config(endpoint)
        argv_builder = _subscription_opus_review_argv if subscription else _direct_opus_review_argv
        argv = argv_builder(brief, read_root, mcp_config)
        cleanup_dir = tempfile.mkdtemp(prefix=_DIRECT_OPUS_REVIEW_CWD_PREFIX)
        cwd = cleanup_dir
        last_msg_path = None
        if subscription:
            try:
                _verify_subscription_auth(env, cwd)
            except Exception:
                shutil.rmtree(cleanup_dir, ignore_errors=True)
                raise
    else:
        member_header = bool(member_token) and member_identity_supported(spec)
        argv, last_msg_path = _resolve_spawn_args(
            spec, brief, log_dir, name, member_header=member_header,
        )
        env = _spawn_environment(spec, argv)
        env.pop(MEMBER_TOKEN_ENV, None)  # never inherit a stale parent value
        if member_header:
            env[MEMBER_TOKEN_ENV] = member_token
    # A batch stagger cannot cover separate rooms, separate Huddle processes,
    # or wake-path launches. Serialize the complete lifetime of every
    # OpenCode CLI child on the shared Huddle home instead.
    argv = _serialize_opencode_argv(argv)
    try:
        if owner_room_id:
            log_fd = bus._safe_open_fd(
                log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600,
            )
            log_file = os.fdopen(log_fd, "ab", buffering=0)
        else:
            log_file = _open_standalone_log(log_path, create_parent=True)
    except BaseException:
        if cleanup_dir is not None:
            shutil.rmtree(cleanup_dir, ignore_errors=True)
        raise
    try:
        if on_log_open is not None or on_log_open_identity is not None:
            log_stat = os.fstat(log_file.fileno())
            if on_log_open is not None:
                on_log_open(log_stat.st_size, str(log_path))
            if on_log_open_identity is not None:
                on_log_open_identity(
                    log_stat.st_size, str(log_path),
                    log_stat.st_dev, log_stat.st_ino,
                )
        proc = subprocess.Popen(
            argv,
            cwd=cwd or None,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=env,
        )
    except BaseException:
        # Preserve the callback or Popen failure even if closing its unused
        # log descriptor also fails.
        _close_parent_log_safely(log_file)
        if cleanup_dir is not None:
            shutil.rmtree(cleanup_dir, ignore_errors=True)
        raise
    # Popen duplicated the fd into the child. A parent-close failure is not a
    # spawn failure and must not bypass the ownership/reaper setup below.
    _close_parent_log_safely(log_file)
    direct_fallback = False
    try:
        handle = _reap_in_background(
            proc, name, on_exit=on_exit, cleanup_dir=cleanup_dir,
            owner_room_id=owner_room_id, process_handle=process_handle,
        )
    except BaseException:
        stopped = _terminate_unregistered_child(
            proc, owner_room_id, process_handle,
        )
        if stopped:
            if cleanup_dir is not None:
                shutil.rmtree(cleanup_dir, ignore_errors=True)
            raise
        retained = _retain_failed_setup_child(
            proc, name, owner_room_id, process_handle, cleanup_dir,
            on_exit=on_exit,
        )
        # A living exact child with a published fallback is a successful
        # spawn. Raising here would make the server roll back its persisted
        # claim while this child is still running, permitting a duplicate.
        handle = retained.handle
        direct_fallback = not retained.registered
    if verify_alive_sec > 0:
        time.sleep(verify_alive_sec)
        if direct_fallback:
            try:
                alive = proc.poll() is None
            except Exception:
                # Loss of local polling is not proof that an exact retained
                # child exited. Keep the claim fail-closed.
                alive = True
        else:
            alive = child_processes.state(owner_room_id, handle) == "alive"
        if not alive:
            returncode = proc.returncode
            exc = AgentSpawnError(
                f"{name} exited within {verify_alive_sec:.3g}s with status {returncode}"
            )
            log_spawn_failure(spec, brief, cwd, log_dir, exc)
            raise exc
    return proc.pid, str(log_path), last_msg_path


def build_sanitized_environment(extra_names: tuple[str, ...] | list[str] = ()) -> dict[str, str]:
    """Copy the minimal non-secret child environment plus explicit names.

    This function never logs names or values. Invalid explicit names fail
    closed so a malformed registry cannot accidentally widen the boundary.
    """
    names = set(_SAFE_CHILD_ENV)
    for name in extra_names:
        if not isinstance(name, str) or not _ENV_NAME_RE.fullmatch(name):
            raise AgentSpawnError("pass_env must contain valid environment variable names")
        names.add(name)
    return {name: os.environ[name] for name in names if name in os.environ}


def _api_key_env_names(argv: list[str]) -> list[str]:
    """Return explicit provider env names declared by runner argv."""
    names: list[str] = []
    for index, arg in enumerate(argv):
        if arg.startswith("--api-key-env="):
            names.append(arg.partition("=")[2])
        if arg == "--api-key-env":
            names.append(argv[index + 1] if index + 1 < len(argv) else "")
    return names


def _spawn_environment(spec: SpawnSpec, argv: list[str] | None = None) -> dict[str, str]:
    """Build a least-privilege child environment without exposing values."""
    requested = spec.get("pass_env", [])
    if not isinstance(requested, list):
        raise AgentSpawnError("pass_env must be a list of environment variable names")
    explicit_names = list(requested)
    if argv is not None:
        explicit_names.extend(_api_key_env_names(argv))

    if spec.get("profile") == _SUBSCRIPTION_OPUS_REVIEW_PROFILE:
        conflicting = sorted(name for name in (
            *_DIRECT_OPUS_REMOVED_ENV,
            "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS",
            "ANTHROPIC_DEFAULT_OPUS_MODEL", "CLAUDE_CODE_API_KEY_HELPER",
        ) if os.environ.get(name))
        if conflicting:
            raise AgentSpawnError(
                "subscription review refuses conflicting environment: " + ", ".join(conflicting)
            )
        return build_sanitized_environment()
    if spec.get("profile") != _DIRECT_OPUS_REVIEW_PROFILE:
        env = build_sanitized_environment(explicit_names)
        if argv and _effective_binary(argv) == "opencode":
            endpoint = _opencode_parent_mcp_url()
            if endpoint:
                env["OPENCODE_CONFIG_CONTENT"] = json.dumps(
                    {
                        "mcp": {
                            # Inline config merges with user config. Disable the
                            # stale global connection, then add a clean endpoint
                            # under a fresh name so old auth headers cannot be
                            # inherited or sent to this server.
                            "huddle": {"enabled": False},
                            "huddle_parent": {
                                "type": "remote",
                                "url": endpoint,
                                "enabled": True,
                                "oauth": False,
                            },
                        },
                    },
                    separators=(",", ":"),
                )
        return env
    required = (
        "ANTHROPIC_API_KEY",
        _DIRECT_OPUS_WORKSPACE_ENV,
        _DIRECT_OPUS_ENDPOINT_ENV,
    )
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise AgentSpawnError(
            f"{spec['name']} missing required environment: {', '.join(missing)}"
        )
    env = build_sanitized_environment(list(required))
    env["ANTHROPIC_BASE_URL"] = "https://api.anthropic.com"
    env["ANTHROPIC_CUSTOM_HEADERS"] = env.pop(_DIRECT_OPUS_WORKSPACE_ENV)
    return env


def _opencode_parent_mcp_url() -> str | None:
    """Resolve this Huddle HTTP process endpoint for OpenCode child MCP config.

    The CLI is the source of truth: `--port` overrides `PORT`, then Huddle's
    default port applies. Stdio Huddle has no HTTP endpoint and leaves the
    user's OpenCode MCP config untouched.
    """
    args = sys.argv[1:]
    if "--http" not in args and not os.environ.get("MCP_HUDDLE_HTTP"):
        return None

    # Keep aligned with mcp_huddle.__main__.DEFAULT_PORT.
    port = 8014
    raw_port = os.environ.get("PORT")
    if raw_port:
        try:
            candidate = int(raw_port)
            if 1 <= candidate <= 65535:
                port = candidate
        except (TypeError, ValueError):
            pass  # __main__ warns and falls back to DEFAULT_PORT.

    for index, arg in enumerate(args):
        raw = None
        if arg == "--port" and index + 1 < len(args):
            raw = args[index + 1]
        elif arg.startswith("--port="):
            raw = arg.partition("=")[2]
        if raw is not None:
            try:
                candidate = int(raw)
            except (TypeError, ValueError):
                return None
            if not 1 <= candidate <= 65535:
                return None
            port = candidate
            break

    return f"http://127.0.0.1:{port}/mcp"


# ── Same-binary spawn stagger ────────────────────────────────────────────────
#
# Two same-binary CLI processes started simultaneously in one batch can
# collide on shared local state (verified live: two `opencode run` processes
# racing on OpenCode's local SQLite → "database is locked"). Within a single
# batch spawn (room_create auto_spawn=True / dict), each spec after the first
# that resolves to the same *effective* binary is delayed by
# MCP_HUDDLE_SAME_BIN_STAGGER_SEC seconds (multiplied by its occurrence index,
# so a third same-binary spec waits 2x the stagger, a fourth 3x, ...).

def _effective_binary(cmd: list[str]) -> str:
    """Return the binary name a spec's argv actually execs, for same-binary
    collision detection — cmd[0], but skipping a leading `timeout` wrapper
    (`timeout 240 opencode ...` / `/opt/homebrew/bin/timeout 590s agy ...`)
    and its duration/flag arguments, so a timeout-wrapped invocation and a
    bare one of the same underlying binary are recognized as the same thing.
    Empty cmd → "" (never staggered against anything).
    """
    idx = _effective_binary_index(cmd)
    return Path(cmd[idx]).name if idx is not None else ""


def _effective_binary_index(cmd: list[str]) -> int | None:
    """Return the argv index of the executed program, skipping timeout args."""
    if not cmd:
        return None
    idx = 0
    if Path(cmd[0]).name == "timeout":
        idx = 1
        while idx < len(cmd):
            tok = cmd[idx]
            if tok in ("-s", "--signal", "-k", "--kill-after"):
                # These timeout options consume a value before the duration.
                idx += 2
                continue
            if tok.startswith("--signal=") or tok.startswith("--kill-after="):
                idx += 1
                continue
            if tok.startswith("-"):
                idx += 1
                continue
            if re.fullmatch(r"\d+(\.\d+)?[smhd]?", tok):
                idx += 1
                continue
            break
    return idx if idx < len(cmd) else None


def _serialize_opencode_argv(argv: list[str]) -> list[str]:
    """Wrap OpenCode so its shared local database stays locked for its lifetime.

    The wrapper replaces itself with the original argv after taking a
    process-shared advisory lock. Because the descriptor is explicitly
    inherited across exec, the lock remains held by OpenCode (or its outer
    timeout command) until the spawned process exits, including when Huddle
    terminates the exact registered Popen.
    """
    if _effective_binary(argv) != "opencode":
        return argv
    lock_path = bus.HUDDLE_HOME / "internal" / "opencode-run.lock"
    helper = Path(__file__).with_name("opencode_serial.py")
    return [sys.executable, str(helper), str(lock_path), "--", *argv]


def _same_bin_stagger_sec() -> float:
    raw = os.environ.get("MCP_HUDDLE_SAME_BIN_STAGGER_SEC", "20")
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return 20.0
    return max(0.0, val)


def compute_stagger_delays(
    specs: list[SpawnSpec], stagger_sec: float | None = None
) -> dict[str, float]:
    """Return {spec_name: delay_sec} for a batch of specs about to be spawned
    together, in the given order. The first spec seen for a given effective
    binary gets delay 0; each later spec resolving to the SAME effective
    binary is delayed by stagger_sec * occurrence_index. A spec whose
    effective binary can't be determined (empty cmd) is never delayed.
    """
    if stagger_sec is None:
        stagger_sec = _same_bin_stagger_sec()
    delays: dict[str, float] = {}
    if stagger_sec <= 0:
        return {spec["name"]: 0.0 for spec in specs}
    seen: dict[str, int] = {}
    for spec in specs:
        binary = _effective_binary(spec.get("cmd") or [])
        if not binary:
            delays[spec["name"]] = 0.0
            continue
        occurrence = seen.get(binary, 0)
        seen[binary] = occurrence + 1
        delays[spec["name"]] = stagger_sec * occurrence
    return delays


def _placeholder_agent_meta(
    spec: SpawnSpec, brief: str, log_dir: Path
) -> dict[str, object]:
    """Deterministic {log_path, last_message_path} for a spec, computable
    without actually spawning a process — lets a delayed (staggered) spawn's
    identity be registered in agent_meta immediately, before its process
    exists, so the room already knows it's coming."""
    argv, last_msg_path = _resolve_spawn_args(spec, brief, log_dir)
    log_path = log_dir / f"{spec['name'].lower()}.events.jsonl"
    result = {"log_path": str(log_path), "last_message_path": last_msg_path}
    settings = model_settings_for_spec({**spec, "cmd": argv})
    if settings:
        result["model_settings"] = settings
    if "mcp_url" in spec and _effective_binary(spec.get("cmd") or []) == "codex":
        result["mcp_url"] = _codex_loopback_mcp_url(spec["mcp_url"])
    return result


def _schedule_delayed_spawn(
    delay: float,
    spec: SpawnSpec,
    brief: str,
    cwd: str,
    log_dir: Path,
    on_exit=None,
    on_spawn_fail=None,
    should_spawn=None,
    on_spawned=None,
    owner_room_id: str = "",
    process_handle: str | None = None,
    on_log_open: Callable[[int, str], None] | None = None,
    on_log_open_identity: Callable[[int, str, int, int], None] | None = None,
) -> threading.Timer:
    """Fire spawn_agent(spec, ...) after `delay` seconds on a daemon timer
    thread, without blocking the caller. Spawn failures are logged/notified
    the same way a synchronous spawn_all failure would be, but cannot
    propagate to the caller (the caller has already returned).

    should_spawn: optional callable() -> bool re-checked WHEN THE TIMER FIRES
      (not at scheduling time) — the room's state may have changed inside the
      stagger window (closed/deleted). False → the spawn is skipped with a
      stderr log line; a check that raises is treated as False (don't spawn
      into an unknown state).
    on_spawned: optional callable(pid) invoked (best-effort) with the newly
      spawned process's pid — lets the caller record it where the synchronous
      path records diagnostic pids. Process termination still uses only the
      exact registered local Popen, never this persisted number.
    """
    def _fire() -> None:
        if should_spawn is not None:
            try:
                ok = bool(should_spawn())
            except Exception as exc:
                print(f"[mcp-huddle] delayed-spawn gate error for "
                      f"{spec['name']}: {exc}; skipping spawn",
                      file=sys.stderr, flush=True)
                return
            if not ok:
                print(f"[mcp-huddle] skipping delayed spawn of {spec['name']}: "
                      f"room no longer accepts spawns (closed/deleted during "
                      f"the stagger window)", file=sys.stderr, flush=True)
                return
        try:
            pid, _, _ = spawn_agent(
                spec, brief, cwd, log_dir, on_exit=on_exit,
                owner_room_id=owner_room_id, process_handle=process_handle,
                on_log_open=on_log_open,
                on_log_open_identity=on_log_open_identity,
            )
        except (FileNotFoundError, PermissionError, AgentSpawnError, OSError) as exc:
            log_spawn_failure(spec, brief, cwd, log_dir, exc)
            _safe_notify_spawn_fail(on_spawn_fail, spec["name"], exc)
            return
        if on_spawned is not None:
            try:
                on_spawned(pid)
            except Exception as exc:  # never let a broken callback leak upward
                print(f"[mcp-huddle] delayed-spawn on_spawned callback error "
                      f"for {spec['name']} (pid {pid}): {exc}",
                      file=sys.stderr, flush=True)

    timer: threading.Timer

    def _fire_tracked() -> None:
        try:
            _fire()
        finally:
            with _BACKGROUND_LOCK:
                _SPAWN_TIMERS.discard(timer)

    timer = threading.Timer(delay, _fire_tracked)
    timer.daemon = True
    with _BACKGROUND_LOCK:
        _SPAWN_TIMERS.add(timer)
        timer.start()
    return timer


def _drain_background_for_tests(timeout: float = 2.0) -> None:
    """Reach background quiescence before a test fixture reloads the bus.

    Callbacks may create another reaper/timer while an earlier snapshot is
    being joined, so drain repeatedly until the tracked sets are empty. A
    living remainder is a test failure rather than a hidden cross-fixture
    write into a newly selected HUDDLE_HOME.
    """
    deadline = time.time() + timeout
    while True:
        with _BACKGROUND_LOCK:
            timers = list(_SPAWN_TIMERS)
            reapers = list(_REAPER_THREADS)
        if not timers and not reapers:
            return

        for timer in timers:
            cancel = getattr(timer, "cancel", None)
            if cancel is not None:
                cancel()
        for background in [*timers, *reapers]:
            join = getattr(background, "join", None)
            if join is not None:
                try:
                    join(max(0.0, deadline - time.time()))
                except RuntimeError:
                    # A pre-existing/custom producer may still publish just
                    # before start(). Keep it tracked and retry; if it never
                    # starts, the deadline assertion below exposes the bug.
                    pass

        with _BACKGROUND_LOCK:
            for timer in timers:
                is_alive = getattr(timer, "is_alive", None)
                if is_alive is None or not is_alive():
                    _SPAWN_TIMERS.discard(timer)
            for thread in reapers:
                if thread.ident is not None and not thread.is_alive():
                    _REAPER_THREADS.discard(thread)
            remaining_timers = list(_SPAWN_TIMERS)
            remaining_reapers = list(_REAPER_THREADS)
        if not remaining_timers and not remaining_reapers:
            return
        if time.time() >= deadline:
            names = [getattr(item, "name", type(item).__name__)
                     for item in [*remaining_timers, *remaining_reapers]]
            raise AssertionError(
                "background spawn work did not quiesce before test teardown: "
                + ", ".join(names)
            )


def _safe_notify_spawn_fail(on_spawn_fail, name: str, exc: BaseException) -> None:
    """Best-effort: a broken caller callback must never break the spawn loop."""
    if on_spawn_fail is None:
        return
    try:
        on_spawn_fail(name, exc)
    except Exception:
        pass


def spawn_all(
    brief: str,
    cwd: str,
    log_dir: Path,
    briefs: dict[str, str] | None = None,
    verify_alive_sec: float = 0.0,
    on_exit_factory=None,
    skip_names: set[str] | None = None,
    on_spawn_fail=None,
    delayed_spawn_gate=None,
    on_delayed_spawn=None,
    owner_room_id: str = "",
    process_handle_factory=None,
    prepare_spawn=None,
    on_log_open_factory=None,
    on_log_open_identity_factory=None,
) -> tuple[list[str], list[int], dict[str, dict[str, object]]]:
    """Spawn every enabled agent in the registry.

    Args:
      brief: default brief used when `briefs` doesn't have a per-agent entry.
      cwd: working directory for spawned processes.
      log_dir: where each agent's <name>.events.jsonl is written.
      briefs: optional {AgentName: brief} for per-agent customization.
      skip_names: registry names to NOT spawn (e.g. the room owner — already
        present as the calling session, would otherwise spawn a duplicate).
      on_spawn_fail: optional callable(name, exc) invoked (best-effort) for
        every agent that fails to spawn, so the caller can surface it (e.g.
        post a room notice) without changing control flow here.
      delayed_spawn_gate: optional callable() -> bool re-checked when a
        staggered spawn's timer fires — False (or a raise) skips the spawn
        (e.g. the room was closed/deleted inside the stagger window).
      on_delayed_spawn: optional callable(name, pid) invoked once a staggered
        spawn actually starts, so the caller can record the pid it could not
        get from the return value (e.g. into the room's diagnostic
        spawned_pids; signalling still requires the exact local Popen).
      prepare_spawn: optional callable(spec, handle, placeholder, delay) run
        before scheduling/Popen. Returning False skips that generation. The
        server uses it to persist the initial claim before a child can exist.

    spawn_all is the auto_spawn=True path (the only caller, server._spawn_agents'
    else-branch) — it honours each spec's optional "auto" flag: a spec with
    "auto": false is skipped here even though it's enabled+available, staying
    reachable only via an explicit dict auto_spawn={name: brief}, room_invite,
    or a wake-path request (server.py talks to the registry directly for
    those, bypassing this filter). A spec with no "auto" key defaults to True.

    Same-binary stagger: within this batch, a spec resolving to the same
    effective binary (see _effective_binary) as an earlier spec in the batch
    is NOT spawned synchronously — it is scheduled on a daemon
    threading.Timer after MCP_HUDDLE_SAME_BIN_STAGGER_SEC seconds (0
    disables), so two colliding processes (e.g. two `opencode run`) never
    start at the same instant. Its name/agent_meta (log paths) are still
    registered in the return value immediately; its pid is not (unknown at
    return time) and it does not participate in verify_alive_sec.

    Returns:
      (names, pids, agent_meta) where agent_meta is
      {name: {"log_path": "...", "last_message_path": "..." or None,
              "pid": int}}. Delayed entries omit the pid until they start.
    """
    names: list[str] = []
    pids: list[int] = []
    agent_meta: dict[str, dict[str, object]] = {}
    briefs = briefs or {}
    skip_names = skip_names or set()
    specs = [
        spec for spec in load_registry()
        if spec.get("enabled")
        and spec["name"] not in skip_names
        and spec.get("auto", True) is not False
    ]
    delays = compute_stagger_delays(specs)
    for spec in specs:
        agent_brief = briefs.get(spec["name"], brief)
        process_handle = (
            process_handle_factory(spec["name"])
            if process_handle_factory is not None
            else child_processes.new_handle()
        )
        delay = delays.get(spec["name"], 0.0)
        placeholder = _placeholder_agent_meta(spec, agent_brief, log_dir)
        if prepare_spawn is not None:
            try:
                if prepare_spawn(
                    spec, process_handle, dict(placeholder), delay,
                ) is False:
                    continue
            except Exception as exc:
                _safe_notify_spawn_fail(on_spawn_fail, spec["name"], exc)
                continue
        if delay > 0:
            names.append(spec["name"])
            agent_meta[spec["name"]] = placeholder
            spawned_cb = None
            if on_delayed_spawn is not None:
                spawned_cb = (lambda name_: lambda pid: on_delayed_spawn(name_, pid))(spec["name"])
            _schedule_delayed_spawn(
                delay, spec, agent_brief, cwd, log_dir,
                on_exit=on_exit_factory(spec["name"]) if on_exit_factory else None,
                on_spawn_fail=on_spawn_fail,
                should_spawn=delayed_spawn_gate,
                on_spawned=spawned_cb,
                owner_room_id=owner_room_id,
                process_handle=process_handle,
                on_log_open=(on_log_open_factory(spec["name"])
                             if on_log_open_factory else None),
                on_log_open_identity=(on_log_open_identity_factory(spec["name"])
                                      if on_log_open_identity_factory else None),
            )
            continue
        try:
            pid, log_path, last_msg = spawn_agent(
                spec, agent_brief, cwd, log_dir,
                verify_alive_sec=verify_alive_sec,
                on_exit=on_exit_factory(spec["name"]) if on_exit_factory else None,
                owner_room_id=owner_room_id,
                process_handle=process_handle,
                on_log_open=(on_log_open_factory(spec["name"])
                             if on_log_open_factory else None),
                on_log_open_identity=(on_log_open_identity_factory(spec["name"])
                                      if on_log_open_identity_factory else None),
            )
            pids.append(pid)
            names.append(spec["name"])
            agent_meta[spec["name"]] = {
                "log_path": log_path,
                "last_message_path": last_msg,
                "pid": pid,
            }
        except (FileNotFoundError, PermissionError) as exc:
            # Tolerate races (binary disappears between check and spawn).
            log_spawn_failure(spec, agent_brief, cwd, log_dir, exc)
            _safe_notify_spawn_fail(on_spawn_fail, spec["name"], exc)
        except AgentSpawnError as exc:
            # spawn_agent already logged the concrete early-exit status.
            _safe_notify_spawn_fail(on_spawn_fail, spec["name"], exc)
        except OSError as exc:
            log_spawn_failure(spec, agent_brief, cwd, log_dir, exc)
            _safe_notify_spawn_fail(on_spawn_fail, spec["name"], exc)
            raise
    return names, pids, agent_meta


# ── Phase 2: thread_id capture + Codex resume ────────────────────────────────

_LOG_HEAD_BYTES = 256 * 1024
_LOG_TAIL_BYTES = 512 * 1024


def _read_log_bounded(log_path: str, limit: int, *, tail: bool) -> str | None:
    try:
        fd = os.open(
            log_path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        with os.fdopen(fd, "rb") as fh:
            if tail:
                fh.seek(0, os.SEEK_END)
                fh.seek(max(0, fh.tell() - limit), os.SEEK_SET)
            return fh.read(limit).decode("utf-8", errors="replace")
    except (FileNotFoundError, NotADirectoryError, OSError):
        return None


def parse_codex_thread_id_text(text: str) -> str | None:
    """Parse a Codex thread id from already-authorized log content."""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict) or obj.get("type") != "thread.started":
            continue
        thread_id = obj.get("thread_id")
        if isinstance(thread_id, str) and thread_id:
            return thread_id
    return None


def parse_codex_thread_id(log_path: str, timeout: float = 10.0) -> str | None:
    """Tail the agent log file until we see a {"type":"thread.started",...} event.
    Returns thread_id (UUID string) or None on timeout / non-Codex agents.

    Codex --json emits this as the very first line of stdout, so this typically
    completes in <1s after spawn.
    """
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        text = _read_log_bounded(log_path, _LOG_HEAD_BYTES, tail=False)
        if text:
            thread_id = parse_codex_thread_id_text(text)
            if thread_id:
                return thread_id
        time.sleep(0.1)
    return None


def codex_log_has_completed_turn_text(text: str) -> bool:
    """Inspect already-authorized Codex log content for turn completion."""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "turn.completed":
            return True
    return False


def codex_log_has_completed_turn(log_path: str) -> bool:
    """Return True once Codex has persisted at least one completed turn."""
    text = _read_log_bounded(log_path, _LOG_TAIL_BYTES, tail=True)
    return bool(text and codex_log_has_completed_turn_text(text))


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
    # OpenRouter free-tier / OpenCode phrasing, e.g. "Rate limit exceeded:
    # free-models-per-day. Add 10 credits to unlock 1000 free model requests
    # per day." Still conservative: each of these is a specific quota/credit
    # phrase, not a bare word an agent could use while merely discussing limits.
    "free-models-per-day",
    "add credits",
    "credits to unlock",
    "requests per day",
)

# ANSI/SGR escape sequences (colors, cursor moves) — OpenCode's plain-text
# `opencode run` output is styled (e.g. "\x1b[0m⚙ \x1b[0mhuddle_room_list"),
# which would otherwise hide marker/hint substrings mid-escape-code or split
# across them. Strip before matching.
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text)


def _looks_like_rate_limit(text: str, *, require_hint: bool) -> bool:
    low = text.lower()
    if not any(marker in low for marker in _RATE_LIMIT_MARKERS):
        return False
    if not require_hint:
        return True
    return any(hint in low for hint in _RATE_LIMIT_PLAINTEXT_HINTS)


def detect_rate_limit_text(text: str) -> str | None:
    """Detect a provider refusal in already-authorized log content."""
    reason: str | None = None
    for raw in text.splitlines():
        line = _strip_ansi(raw).strip()
        if not line:
            continue
        if line.startswith("{"):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            etype = obj.get("type")
            if etype == "error":
                candidate = obj.get("message") or obj.get("error") or ""
            elif etype == "turn.failed":
                err = obj.get("error")
                candidate = (err.get("message", "") if isinstance(err, dict)
                             else err or "")
            else:
                continue
            if not isinstance(candidate, str):
                continue
            if candidate and _looks_like_rate_limit(candidate, require_hint=False):
                reason = candidate.strip()
        elif _looks_like_rate_limit(line, require_hint=True):
            reason = line
    return reason


def detect_rate_limit(log_path: str) -> str | None:
    """Scan an agent log for a provider usage/rate-limit refusal.

    Returns a short reason string (the offending message) or None.

    Detection is conservative to avoid false positives from message bodies:
      * Codex `--json` logs — trust only `error` / `turn.failed` event types
        (an agent that *posts about* rate limits does so via `item.*` events,
        which are ignored here).
      * Plain-text logs (e.g. Antigravity `agy -p`, OpenCode `opencode run`)
        — a line must contain both a limit marker AND a recovery hint
        ("try again", "upgrade", "free-models-per-day", ...). ANSI/SGR escape
        codes (OpenCode styles its plain-text output) are stripped first so
        they can't split or hide a marker/hint substring.
    """
    text = _read_log_bounded(log_path, _LOG_TAIL_BYTES, tail=True)
    if text is None:
        return None

    return detect_rate_limit_text(text)


def codex_resume(thread_id: str, prompt: str, cwd: str, log_path: str,
                 last_msg_path: str | None = None, on_exit=None,
                 owner_room_id: str = "",
                 process_handle: str | None = None,
                 model_settings: dict[str, str] | None = None,
                 workspace_write_roots: list[str] | None = None,
                 mcp_url: str | None = None,
                 member_token: str | None = None) -> int:
    """Resume a Codex thread with a new prompt. Cheaper than fresh spawn —
    Codex remembers prior conversation via its rollout file.

    Appends events to the same log_path so the dashboard SSE keeps streaming.
    Returns PID of the spawned codex exec resume process.

    `codex exec resume` has no `-s/--sandbox` flag — sandbox must be pinned
    via `-c sandbox_mode=...`, else it falls back to ~/.codex/config.toml.
    We pin danger-full-access so Codex's huddle MCP tool calls aren't auto-
    cancelled: under a restricted sandbox + `-a never`, MCP calls need approval
    that `never` denies ("user cancelled MCP tool call"). `-a` is a top-level
    flag (before `exec`). A room's initial model settings are retained for
    resumed turns so a registry edit cannot switch models mid-session. A
    supplied ``mcp_url`` likewise pins the room's original Huddle endpoint.
    ``workspace_write_roots`` (a list, possibly empty) selects a write room's
    bounded workspace-write sandbox instead of the process-wide default.
    """
    if workspace_write_roots is not None and not _is_ascii(cwd or ""):
        # The ASCII fallback cwd would silently move the writable root.
        raise AgentSpawnError("write room cwd must be an ASCII path for Codex")
    cwd, prompt = _codex_safe_cwd_and_brief(cwd, prompt)
    # Read-only by default (matches the initial-spawn transform): pin
    # sandbox_mode=read-only and auto-approve the huddle MCP tools so the
    # resumed turn can still post without the restricted-sandbox approval that
    # `-a never` would otherwise cancel. MCP_HUDDLE_READONLY=0 → full access.
    sandbox_args = _codex_resume_sandbox_args(workspace_write_roots)
    if model_settings is None:
        spec = get_enabled_spec("Codex")
        settings = model_settings_for_spec(spec) if spec else {}
    else:
        # A room's initial selection is durable. Do not let a later registry
        # edit silently switch the model halfway through its native session.
        settings = _validated_model_overrides(
            {"name": "Codex", "cmd": ["codex"], **model_settings}, "codex",
        )
    model = settings.get("model")
    effort = settings.get("effort")

    argv = [
        _CODEX_BIN or "codex", "-a", "never",            # top-level: never auto-approve tool calls
        "exec", "resume", thread_id,                     # subcommand
        "--json",                                        # JSONL events to stdout
    ]
    if model:
        argv.extend(["-m", model])
    if effort:
        argv.extend(["-c", f'model_reasoning_effort="{effort}"'])

    argv.extend(sandbox_args)                            # resume has no -s flag; pin via -c
    # A member token needs the pinned HTTP route; without it the token is unused.
    member_header = bool(member_token) and mcp_url is not None
    if mcp_url is not None:
        url = _codex_loopback_mcp_url(mcp_url)
        argv.extend(["-c", _codex_huddle_server_config(url, member_header)])
    if last_msg_path:
        argv += ["-o", last_msg_path]                    # short form of --output-last-message
    argv.append(prompt)
    # Allocate exact ownership before Popen so failure cannot leave an
    # unregistered child behind.
    process_handle = process_handle or child_processes.new_handle()

    if owner_room_id:
        expected_log, expected_last = bus._agent_paths(
            owner_room_id, "Codex", create=False,
        )
        if Path(log_path).resolve(strict=False) != expected_log.resolve(strict=False):
            raise AgentSpawnError("Codex resume log path is outside its owned room")
        if (last_msg_path is not None
                and Path(last_msg_path).resolve(strict=False)
                != expected_last.resolve(strict=False)):
            raise AgentSpawnError(
                "Codex resume last-message path is outside its owned room"
            )
        if last_msg_path is not None:
            last_msg_path = str(expected_last)
            argv[argv.index("-o") + 1] = last_msg_path
        log_fd = bus._safe_open_fd(
            expected_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600,
        )
        log_file = os.fdopen(log_fd, "ab", buffering=0)
    else:
        log_file = _open_standalone_log(Path(log_path), create_parent=False)
    resume_env = build_sanitized_environment()
    resume_env.pop(MEMBER_TOKEN_ENV, None)
    if member_header:
        resume_env[MEMBER_TOKEN_ENV] = member_token
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd or None,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=resume_env,
        )
    except BaseException:
        _close_parent_log_safely(log_file)
        raise
    _close_parent_log_safely(log_file)
    try:
        _reap_in_background(
            proc, "Codex", on_exit=on_exit,
            owner_room_id=owner_room_id, process_handle=process_handle,
        )
    except BaseException:
        stopped = _terminate_unregistered_child(
            proc, owner_room_id, process_handle,
        )
        if stopped:
            raise
        _retain_failed_setup_child(
            proc, "Codex", owner_room_id, process_handle, None,
            on_exit=on_exit,
        )
        # See spawn_agent: fallback ownership is active success, not a reason
        # for the caller to roll back the wake claim.
    return proc.pid
