"""Installation acceptance without touching real client settings or models."""
import json
import pytest
from pathlib import Path
from mcp_huddle import setup


@pytest.fixture(autouse=True)
def clean_profile_environment(monkeypatch):
    for name in ('CLAUDE_CONFIG_DIR', 'CODEX_HOME', 'HERMES_HOME', 'XDG_CONFIG_HOME', 'OPENCODE_CONFIG', 'MCP_HUDDLE_HOME', 'MCP_HUDDLE_TOKEN'):
        monkeypatch.delenv(name, raising=False)


def test_repeated_setup_preserves_other_servers_and_backs_up(tmp_path, monkeypatch):
    monkeypatch.setattr(setup.shutil, 'which', lambda _: None)
    path = tmp_path / '.config/opencode/opencode.json'
    path.parent.mkdir(parents=True)
    original = {'mcp': {'other': {'type': 'local', 'command': ['safe']}}, 'theme': 'mine'}
    path.write_text(json.dumps(original))
    first = setup.main(['--apply', '--home', str(tmp_path)])
    assert first['clients']['opencode']['mcp'] == 'configured'
    assert json.loads(path.read_text())['mcp']['other'] == original['mcp']['other']
    assert json.loads(path.read_text())['theme'] == 'mine'
    before = path.read_bytes()
    second = setup.main(['--apply', '--home', str(tmp_path)])
    assert second['clients']['opencode']['mcp'] == 'configured'
    assert path.read_bytes() == before
    assert list((tmp_path / '.mcp-huddle/setup-backups').rglob('opencode.json'))
    assert (tmp_path / '.config/opencode/skills/huddle/SKILL.md').exists()


def test_conflict_and_jsonc_are_reported_without_overwrite(tmp_path, monkeypatch):
    monkeypatch.setattr(setup.shutil, 'which', lambda _: None)
    path = tmp_path / '.gemini/settings.json'
    path.parent.mkdir()
    content = '{"mcpServers":{"huddle":{"httpUrl":"http://localhost:9999/mcp"}}}'
    path.write_text(content)
    opencode = tmp_path / '.config/opencode/opencode.jsonc'
    opencode.parent.mkdir(parents=True)
    opencode.write_text('// user configuration\n{}')
    result = setup.main(['--apply', '--home', str(tmp_path)])
    assert result['clients']['gemini']['mcp'] == 'needs attention'
    assert result['clients']['opencode']['mcp'] == 'needs attention'
    assert path.read_text() == content
    assert not opencode.with_suffix('.json').exists()


def test_preview_and_symlink_protection(tmp_path, monkeypatch):
    monkeypatch.setattr(setup.shutil, 'which', lambda _: None)
    config = tmp_path / '.gemini'
    config.mkdir()
    setup.main(['--home', str(tmp_path)])
    assert list(tmp_path.iterdir()) == [config]
    outside = tmp_path / 'user-source'
    outside.mkdir()
    (config / 'skills').symlink_to(outside, target_is_directory=True)
    result = setup.main(['--apply', '--home', str(tmp_path)])
    assert result['clients']['gemini']['mcp'] == 'needs attention'
    assert not list(outside.iterdir())


def test_protected_deployment_refuses_before_changes(tmp_path, monkeypatch):
    monkeypatch.setenv('MCP_HUDDLE_TOKEN', 'fixture-not-a-secret')
    with pytest.raises(SystemExit):
        setup.main(['--apply', '--home', str(tmp_path)])
    assert not list(tmp_path.iterdir())


def test_startup_handshake_failure_is_not_retried(tmp_path, monkeypatch):
    class Response:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    calls = {'dashboard': 0, 'mcp': 0, 'terminated': False}
    def urlopen(*args, **kwargs):
        calls['dashboard'] += 1
        if calls['dashboard'] == 1:
            raise OSError('No existing listener')
        return Response()
    def handshake():
        calls['mcp'] += 1
        raise ValueError('MCP identity mismatch')
    class Process:
        pid = 123
        def poll(self):
            return None
        def terminate(self):
            calls['terminated'] = True
        def wait(self, timeout):
            return 0
    monkeypatch.setattr(setup.urllib.request, 'urlopen', urlopen)
    monkeypatch.setattr(setup, 'verify_mcp', handshake)
    monkeypatch.setattr(setup.subprocess, 'Popen', lambda *a, **kw: Process())
    with pytest.raises(ValueError, match='MCP identity mismatch'):
        setup.start_server(tmp_path)
    assert calls['mcp'] == 1
    assert calls['terminated']
