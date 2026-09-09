#!/usr/bin/env bash
# Huddle — SessionEnd hook for Claude Code.
# Claude supplies the event and session id in hook JSON on stdin. Explicit env/file
# fallbacks support other hook runners without a shared predictable /tmp file.
python3 -c '
import ipaddress
import json
import os
import select
import stat
import sys
import urllib.parse
import urllib.request

MAX_HOOK_INPUT = 65536
MAX_SESSION_FILE = 4096


def hook_session_id():
    try:
        ready, _, _ = select.select([sys.stdin.buffer], [], [], 0)
        if ready:
            raw = sys.stdin.buffer.read(MAX_HOOK_INPUT + 1)
            if raw.strip():
                # Any supplied hook payload suppresses compatibility fallbacks.
                # A missing/wrong event must never turn Stop into SessionEnd.
                if len(raw) > MAX_HOOK_INPUT:
                    return ""
                payload = json.loads(raw)
                if (not isinstance(payload, dict)
                        or payload.get("hook_event_name") != "SessionEnd"):
                    return ""
                value = payload.get("session_id")
                return value.strip() if isinstance(value, str) else ""
    except Exception:
        return ""

    value = os.environ.get("MCP_HUDDLE_SESSION_ID", "").strip()
    if value:
        return value

    path = os.environ.get("MCP_HUDDLE_SESSION_FILE", "")
    if not path:
        return ""
    fd = None
    try:
        flags = (os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
                 | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
        fd = os.open(path, flags)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid() or info.st_size > MAX_SESSION_FILE
                or info.st_mode & 0o022):
            return ""
        with os.fdopen(fd, encoding="utf-8") as stream:
            fd = None
            return stream.read(MAX_SESSION_FILE + 1).strip()
    except (OSError, UnicodeError):
        return ""
    finally:
        if fd is not None:
            os.close(fd)


def endpoint_url():
    base = os.environ.get("MCP_HUDDLE_HTTP_BASE_URL", "").strip()
    if not base:
        try:
            port = int(os.environ.get("PORT", "8014"))
        except ValueError:
            return ""
        if not 1 <= port <= 65535:
            return ""
        base = f"http://127.0.0.1:{port}"
    try:
        parsed = urllib.parse.urlsplit(base)
        host = parsed.hostname
        if (parsed.scheme not in {"http", "https"} or not host
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            return ""
        try:
            if not ipaddress.ip_address(host).is_loopback:
                return ""
        except ValueError:
            return ""
        parsed.port  # validate malformed or out-of-range ports
        if ("\\" in parsed.path or "//" in parsed.path
                or any(part in {".", ".."} for part in parsed.path.split("/"))):
            return ""
        path = parsed.path.rstrip("/") + "/api/rooms_close_session"
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
    except (TypeError, ValueError):
        return ""


try:
    session_id = hook_session_id()
    endpoint = endpoint_url()
    if session_id and endpoint and len(session_id) <= 1024 and "\x00" not in session_id:
        body = json.dumps({"session_id": session_id}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        token = os.environ.get("MCP_HUDDLE_TOKEN")
        if token:
            # Kept in process environment/request headers: never URL, body or argv.
            headers["X-Huddle-Token"] = token
        request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=3) as response:
            response.read(1024)
except Exception:
    # SessionEnd hooks are best effort and must never block session exit.
    pass
'
exit 0
