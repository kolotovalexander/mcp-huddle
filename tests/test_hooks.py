import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from mcp_huddle import __main__ as cli
from mcp_huddle import bus as hook_bus


ROOT = Path(__file__).resolve().parents[1]
SESSION_HOOKS = [
    ROOT / "src/mcp_huddle/hooks/session-end.sh",
    ROOT / "examples/hooks/session-end.sh",
]
NOTIFICATION_HOOKS = [
    ROOT / "src/mcp_huddle/hooks/claude-check.sh",
    ROOT / "src/mcp_huddle/hooks/gemini-check.sh",
    ROOT / "examples/hooks/claude-check.sh",
    ROOT / "examples/hooks/gemini-check.sh",
]
HOOK_PAIRS = [
    (
        ROOT / "src/mcp_huddle/hooks/claude-check.sh",
        ROOT / "examples/hooks/claude-check.sh",
    ),
    (
        ROOT / "src/mcp_huddle/hooks/gemini-check.sh",
        ROOT / "examples/hooks/gemini-check.sh",
    ),
    (SESSION_HOOKS[0], SESSION_HOOKS[1]),
]


@pytest.mark.parametrize(("packaged", "example"), HOOK_PAIRS)
def test_packaged_and_example_hooks_are_byte_identical(
    packaged: Path, example: Path,
) -> None:
    assert packaged.read_bytes() == example.read_bytes()


@pytest.fixture
def http_capture():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - stdlib handler API
            length = int(self.headers.get("Content-Length", "0"))
            requests.append({
                "path": self.path,
                "headers": dict(self.headers),
                "body": self.rfile.read(length),
            })
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"closed": []}')

        def log_message(self, _format, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def redirect_capture():
    origin_requests = []
    destination_requests = []

    class DestinationHandler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - stdlib handler API
            destination_requests.append(dict(self.headers))
            self.send_response(200)
            self.end_headers()

        def log_message(self, _format, *_args):
            pass

    destination = ThreadingHTTPServer(("127.0.0.1", 0), DestinationHandler)
    destination_thread = threading.Thread(target=destination.serve_forever, daemon=True)
    destination_thread.start()

    class OriginHandler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - stdlib handler API
            origin_requests.append(dict(self.headers))
            self.send_response(307)
            self.send_header(
                "Location",
                f"http://127.0.0.1:{destination.server_port}/redirect-target",
            )
            self.end_headers()

        def log_message(self, _format, *_args):
            pass

    origin = ThreadingHTTPServer(("127.0.0.1", 0), OriginHandler)
    origin_thread = threading.Thread(target=origin.serve_forever, daemon=True)
    origin_thread.start()
    try:
        yield (
            f"http://127.0.0.1:{origin.server_port}",
            origin_requests,
            destination_requests,
        )
    finally:
        origin.shutdown()
        destination.shutdown()
        origin.server_close()
        destination.server_close()
        origin_thread.join(timeout=2)
        destination_thread.join(timeout=2)


@pytest.fixture
def slow_header_capture():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - stdlib handler API
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            requests.append(self.path)
            try:
                self.connection.sendall(b"HTTP/1.1 200 OK\r\n")
                # Never terminate the headers naturally within the hook budget.
                for index in range(12):
                    time.sleep(0.2)
                    self.connection.sendall(f"X-Slow-{index}: 1\r\n".encode())
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

        def log_message(self, _format, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    "script",
    NOTIFICATION_HOOKS,
)
def test_hook_claims_managed_notification_without_filename_injection(
    script: Path, tmp_path: Path
) -> None:
    notification_dir = tmp_path / "notifications"
    notification_dir.mkdir()
    target = notification_dir / (
        'agent-bus-x\');__import__("pathlib").Path("PWNED").touch();#-notify.json'
    )
    target.write_text(json.dumps({
        "room_id": "room_safe",
        "from_agent": "Reviewer\nforged-line",
        "msg_id": 7,
    }))
    env = dict(os.environ, MCP_HUDDLE_HOME=str(tmp_path))

    completed = subprocess.run(
        ["bash", str(script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    assert "Huddle [room_safe]" in completed.stdout
    assert "Reviewer forged-line" in completed.stdout
    assert not target.exists()
    assert not (tmp_path / "PWNED").exists()
    assert list(notification_dir.glob("*.claim.*")) == []


@pytest.mark.parametrize(
    "script",
    NOTIFICATION_HOOKS,
)
def test_hook_refuses_symlink_notification_without_reading_target(
    script: Path, tmp_path: Path
) -> None:
    notification_dir = tmp_path / "notifications"
    notification_dir.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({
        "room_id": "SECRET_OUTSIDE_NOTIFICATION_ROOT",
        "from_agent": "attacker",
        "msg_id": 99,
    }))
    target = notification_dir / "agent-bus-test-notify.json"
    target.symlink_to(outside)

    completed = subprocess.run(
        ["bash", str(script)],
        cwd=tmp_path,
        env=dict(os.environ, MCP_HUDDLE_HOME=str(tmp_path)),
        capture_output=True,
        text=True,
        check=True,
    )

    assert completed.stdout == ""
    assert outside.exists()
    assert "SECRET_OUTSIDE_NOTIFICATION_ROOT" in outside.read_text()
    assert not target.exists()
    assert list(notification_dir.glob("*.claim.*")) == []


@pytest.mark.parametrize("script", SESSION_HOOKS)
def test_session_end_uses_hook_input_configured_loopback_and_token(
    script: Path, tmp_path: Path, http_capture
) -> None:
    base_url, requests = http_capture
    token = "secret-not-for-url-or-body"
    env = dict(
        os.environ,
        MCP_HUDDLE_HTTP_BASE_URL=f"{base_url}/huddle-root/",
        MCP_HUDDLE_TOKEN=token,
    )

    completed = subprocess.run(
        ["bash", str(script)],
        input=json.dumps({
            "hook_event_name": "SessionEnd",
            "session_id": "session-from-stdin",
        }),
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    assert completed.stdout == ""
    assert completed.stderr == ""
    assert len(requests) == 1
    request = requests[0]
    assert request["path"] == "/huddle-root/api/rooms_close_session"
    assert request["headers"]["X-Huddle-Token"] == token
    assert json.loads(request["body"]) == {"session_id": "session-from-stdin"}
    assert token not in request["path"]
    assert token.encode() not in request["body"]
    source = script.read_text()
    assert "/tmp/claude-session-id" not in source
    assert "curl " not in source


@pytest.mark.parametrize("script", SESSION_HOOKS)
def test_session_end_waits_briefly_for_delayed_hook_input(
    script: Path, tmp_path: Path, http_capture
) -> None:
    base_url, requests = http_capture
    process = subprocess.Popen(
        ["bash", str(script)],
        cwd=tmp_path,
        env=dict(os.environ, MCP_HUDDLE_HTTP_BASE_URL=base_url),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    time.sleep(0.05)
    process.stdin.write(json.dumps({
        "hook_event_name": "SessionEnd",
        "session_id": "delayed-session",
    }))
    process.stdin.close()
    assert process.wait(timeout=2) == 0
    assert process.stdout.read() == ""
    assert process.stderr.read() == ""
    assert len(requests) == 1
    assert json.loads(requests[0]["body"]) == {"session_id": "delayed-session"}


@pytest.mark.parametrize("script", SESSION_HOOKS)
def test_session_end_partial_hook_input_is_bounded_and_suppresses_fallback(
    script: Path, tmp_path: Path, http_capture
) -> None:
    base_url, requests = http_capture
    process = subprocess.Popen(
        ["bash", str(script)],
        cwd=tmp_path,
        env=dict(
            os.environ,
            MCP_HUDDLE_HTTP_BASE_URL=base_url,
            MCP_HUDDLE_SESSION_ID="fallback-must-not-run",
        ),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdin is not None
    started = time.monotonic()
    process.stdin.write('{"hook_event_name":"SessionEnd",')
    process.stdin.flush()
    assert process.wait(timeout=1.2) == 0
    elapsed = time.monotonic() - started
    process.stdin.close()

    assert elapsed < 1.0
    assert requests == []


@pytest.mark.parametrize("script", SESSION_HOOKS)
def test_session_end_has_hard_wall_budget_for_trickled_http_headers(
    script: Path, tmp_path: Path, slow_header_capture
) -> None:
    base_url, requests = slow_header_capture
    started = time.monotonic()

    completed = subprocess.run(
        ["bash", str(script)],
        input=json.dumps({
            "hook_event_name": "SessionEnd",
            "session_id": "bounded-http-session",
        }),
        cwd=tmp_path,
        env=dict(os.environ, MCP_HUDDLE_HTTP_BASE_URL=base_url),
        capture_output=True,
        text=True,
        check=True,
        timeout=2,
    )
    elapsed = time.monotonic() - started

    assert completed.stdout == ""
    assert completed.stderr == ""
    assert requests == ["/api/rooms_close_session"]
    assert elapsed < 1.5


@pytest.mark.parametrize("script", SESSION_HOOKS)
@pytest.mark.parametrize("payload", [
    {"hook_event_name": "Stop", "session_id": "stop-session"},
    {"session_id": "missing-event"},
])
def test_session_end_ignores_non_session_end_payload_even_with_fallback(
    script: Path, payload: dict, tmp_path: Path, http_capture
) -> None:
    base_url, requests = http_capture
    env = dict(
        os.environ,
        MCP_HUDDLE_HTTP_BASE_URL=base_url,
        MCP_HUDDLE_SESSION_ID="fallback-must-not-run",
    )

    subprocess.run(
        ["bash", str(script)], input=json.dumps(payload), cwd=tmp_path, env=env,
        capture_output=True, text=True, check=True,
    )

    assert requests == []


@pytest.mark.parametrize("script", SESSION_HOOKS)
def test_session_end_does_not_follow_redirect_or_forward_token(
    script: Path, tmp_path: Path, redirect_capture
) -> None:
    base_url, origin_requests, destination_requests = redirect_capture
    token = "redirect-secret"
    env = dict(
        os.environ,
        MCP_HUDDLE_HTTP_BASE_URL=base_url,
        MCP_HUDDLE_TOKEN=token,
    )

    subprocess.run(
        ["bash", str(script)],
        input=json.dumps({"hook_event_name": "SessionEnd", "session_id": "session"}),
        cwd=tmp_path, env=env, capture_output=True, text=True, check=True,
    )

    assert len(origin_requests) == 1
    assert origin_requests[0]["X-Huddle-Token"] == token
    assert destination_requests == []


def test_hook_installer_wires_session_end_not_stop(tmp_path: Path, capsys) -> None:
    destination = tmp_path / "installed-hooks"

    cli._install_hooks(str(destination))

    output = capsys.readouterr().out
    assert '"SessionEnd"' in output
    assert '"Stop"' not in output
    assert (destination / "session-end.sh").exists()


def test_session_end_secure_file_fallback_refuses_symlink(
    tmp_path: Path, http_capture
) -> None:
    base_url, requests = http_capture
    script = SESSION_HOOKS[0]
    session_file = tmp_path / "session-id"
    session_file.write_text("session-from-file\n")
    session_file.chmod(0o600)
    env = dict(
        os.environ,
        MCP_HUDDLE_HTTP_BASE_URL=base_url,
        MCP_HUDDLE_SESSION_FILE=str(session_file),
    )

    subprocess.run(
        ["bash", str(script)], input="", cwd=tmp_path, env=env,
        capture_output=True, text=True, check=True,
    )
    assert json.loads(requests.pop()["body"]) == {"session_id": "session-from-file"}

    link = tmp_path / "linked-session-id"
    link.symlink_to(session_file)
    env["MCP_HUDDLE_SESSION_FILE"] = str(link)
    subprocess.run(
        ["bash", str(script)], input="", cwd=tmp_path, env=env,
        capture_output=True, text=True, check=True,
    )
    assert requests == []


def test_session_file_fifo_is_rejected_without_blocking(
    tmp_path: Path, http_capture
) -> None:
    base_url, requests = http_capture
    fifo = tmp_path / "session-fifo"
    os.mkfifo(fifo)
    env = dict(
        os.environ,
        MCP_HUDDLE_HTTP_BASE_URL=base_url,
        MCP_HUDDLE_SESSION_FILE=str(fifo),
    )

    subprocess.run(
        ["bash", str(SESSION_HOOKS[0])], input="", cwd=tmp_path, env=env,
        capture_output=True, text=True, check=True, timeout=2,
    )

    assert requests == []


@pytest.mark.parametrize("script", NOTIFICATION_HOOKS)
def test_hook_recovers_dead_claim_without_clobbering_new_notice(
    script: Path, tmp_path: Path
) -> None:
    notification_dir = tmp_path / "notifications"
    notification_dir.mkdir()
    base = notification_dir / "agent-bus-recovery-notify.json"
    orphan = notification_dir / (
        "agent-bus-recovery-notify.json.claim.v2.999999."
        "0123456789abcdef0123456789abcdef"
    )
    orphan.write_text(json.dumps({
        "room_id": "room_old", "from_agent": "old-agent", "msg_id": 1,
    }))
    old = time.time() - 10
    os.utime(orphan, (old, old))
    base.write_text(json.dumps({
        "room_id": "room_new", "from_agent": "new-agent", "msg_id": 2,
    }))

    completed = subprocess.run(
        ["bash", str(script)], cwd=tmp_path,
        env=dict(os.environ, MCP_HUDDLE_HOME=str(tmp_path)),
        capture_output=True, text=True, check=True,
    )

    assert "Huddle [room_old]" in completed.stdout
    assert "Huddle [room_new]" in completed.stdout
    assert not orphan.exists()
    assert not base.exists()
    assert list(notification_dir.glob("*.claim.*")) == []


def test_hook_keeps_live_claim_and_bounds_dead_claim_recovery(tmp_path: Path) -> None:
    notification_dir = tmp_path / "notifications"
    notification_dir.mkdir()
    current_pid = os.getpid()
    live = notification_dir / (
        f"agent-bus-live-notify.json.claim.v2.{current_pid}."
        "0123456789abcdef0123456789abcdef"
    )
    live.write_text(json.dumps({
        "room_id": "room_live", "from_agent": "live-agent", "msg_id": 1,
    }))
    old = time.time() - 10
    os.utime(live, (old, old))
    for index in range(40):
        claim = notification_dir / (
            f"agent-bus-{index:02d}-notify.json.claim.v2.999999."
            f"{index:032x}"
        )
        claim.write_text(json.dumps({
            "room_id": f"room_{index}", "from_agent": "dead-agent", "msg_id": index,
        }))
        os.utime(claim, (old, old))

    completed = subprocess.run(
        ["bash", str(NOTIFICATION_HOOKS[0])], cwd=tmp_path,
        env=dict(os.environ, MCP_HUDDLE_HOME=str(tmp_path)),
        capture_output=True, text=True, check=True,
    )

    assert live.exists()
    assert "room_live" not in completed.stdout
    assert completed.stdout.count("dead-agent sent a request") == 32
    remaining_dead = [
        path for path in notification_dir.glob("*.claim.v2.999999.*")
    ]
    assert len(remaining_dead) == 8


def test_hook_recovers_legacy_claim_name(tmp_path: Path) -> None:
    notification_dir = tmp_path / "notifications"
    notification_dir.mkdir()
    legacy = notification_dir / "agent-bus-old-notify.json.claim.12345.999999"
    legacy.write_text(json.dumps({
        "room_id": "room_legacy", "from_agent": "legacy-agent", "msg_id": 3,
    }))
    old = time.time() - 10
    os.utime(legacy, (old, old))

    completed = subprocess.run(
        ["bash", str(NOTIFICATION_HOOKS[0])], cwd=tmp_path,
        env=dict(os.environ, MCP_HUDDLE_HOME=str(tmp_path)),
        capture_output=True, text=True, check=True,
    )

    assert "Huddle [room_legacy]" in completed.stdout
    assert not legacy.exists()


def test_hook_refuses_symlink_notification_directory(tmp_path: Path) -> None:
    huddle_home = tmp_path / "huddle-home"
    huddle_home.mkdir()
    outside = tmp_path / "outside-notifications"
    outside.mkdir()
    notice = outside / "agent-bus-outside-notify.json"
    notice.write_text(json.dumps({
        "room_id": "SECRET_OUTSIDE_ROOT", "from_agent": "attacker", "msg_id": 4,
    }))
    (huddle_home / "notifications").symlink_to(outside, target_is_directory=True)

    completed = subprocess.run(
        ["bash", str(NOTIFICATION_HOOKS[0])], cwd=tmp_path,
        env=dict(os.environ, MCP_HUDDLE_HOME=str(huddle_home)),
        capture_output=True, text=True, check=True,
    )

    assert completed.stdout == ""
    assert notice.exists()
    assert "SECRET_OUTSIDE_ROOT" in notice.read_text()


def test_notification_fifo_is_rejected_without_blocking(tmp_path: Path) -> None:
    notification_dir = tmp_path / "notifications"
    notification_dir.mkdir()
    fifo = notification_dir / "agent-bus-fifo-notify.json"
    os.mkfifo(fifo)

    subprocess.run(
        ["bash", str(NOTIFICATION_HOOKS[0])], cwd=tmp_path,
        env=dict(os.environ, MCP_HUDDLE_HOME=str(tmp_path)),
        capture_output=True, text=True, check=True, timeout=2,
    )

    assert not fifo.exists()


def test_hook_and_publisher_share_target_lock_without_losing_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    room_id = hook_bus.create_room(
        "Publisher race", "Codex", 0, str(tmp_path), "session",
    )
    target = hook_bus.NOTIFICATIONS_DIR / "agent-bus-race-notify.json"
    hook_bus.register_notify(room_id, "Watcher", str(target))
    target.write_text(json.dumps({
        "room_id": room_id, "from_agent": "older", "kind": "request", "msg_id": 0,
    }))
    publisher_holds_lock = threading.Event()
    allow_publish = threading.Event()
    errors = []
    real_write = hook_bus._write_json

    def paused_write(path: Path, data: dict) -> None:
        if path == target:
            publisher_holds_lock.set()
            assert allow_publish.wait(timeout=5)
        real_write(path, data)

    monkeypatch.setattr(hook_bus, "_write_json", paused_write)

    def publish() -> None:
        try:
            hook_bus.post_message(
                room_id, "Codex", "replacement", "request", to="Watcher",
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    publisher = threading.Thread(target=publish)
    publisher.start()
    assert publisher_holds_lock.wait(timeout=5)
    hook = subprocess.Popen(
        ["bash", str(NOTIFICATION_HOOKS[0])], cwd=tmp_path,
        env=dict(os.environ, MCP_HUDDLE_HOME=str(hook_bus.HUDDLE_HOME)),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    time.sleep(0.1)
    assert hook.poll() is None

    allow_publish.set()
    publisher.join(timeout=5)
    stdout, stderr = hook.communicate(timeout=5)

    assert not publisher.is_alive()
    assert errors == []
    assert hook.returncode == 0
    assert stderr == ""
    assert "Huddle" in stdout
    assert "msg #1" in stdout
    assert not target.exists()
