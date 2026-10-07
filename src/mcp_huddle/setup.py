"""Portable, explicit setup of Huddle clients and bundled Skills."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.request

URL = 'http://127.0.0.1:8014/mcp'
# Detection is not authentication. No model is invoked during installation.
CLIENTS = {
    'claude': ('.claude', '.claude/skills'),
    'codex': ('.codex', '.codex/skills'),
    'gemini': ('.gemini', '.gemini/skills'),
    'agy': ('.gemini/config', '.gemini/config/skills'),
    'opencode': ('.config/opencode', '.config/opencode/skills'),
    'hermes': ('.hermes', '.hermes/skills'),
}


def write_file(path, content, home, changes):
    """Back up originals once per content hash; same content is a no-op."""
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != home and home in parent.parents):
        raise ValueError(f'Refusing to replace symlink: {path}')
    if path.exists():
        old = path.read_bytes()
        if old == content:
            return
        digest = hashlib.sha256(old).hexdigest()[:16]
        backup = home / '.mcp-huddle/setup-backups' / digest / path.relative_to(home)
        backup.parent.mkdir(parents=True, exist_ok=True)
        if not backup.exists():
            backup.write_bytes(old)
            backup.chmod(0o600)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.huddle-new')
    with temp.open('xb') as stream:
        stream.write(content)
    temp.chmod(0o600)
    os.replace(temp, path)
    changes.append(str(path))


def merge_json(path, key, entry, home, changes):
    data = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(data, dict) or not isinstance(data.get(key, {}), dict):
        raise ValueError(f'Unsupported configuration structure: {path}')
    section = data.setdefault(key, {})
    old = section.get('huddle')
    if old is not None and old != entry:
        raise ValueError(f'Existing huddle configuration differs; retained: {path}')
    section['huddle'] = entry
    write_file(path, (json.dumps(data, indent=2) + '\n').encode(), home, changes)


def configure(client, home, changes):
    if client == 'claude':
        subprocess.run(['claude', 'mcp', 'add', '--scope', 'user', '--transport', 'http', 'huddle', URL], check=True, capture_output=True)
    elif client == 'codex':
        subprocess.run(['codex', 'mcp', 'add', 'huddle', '--url', URL], check=True, capture_output=True)
    elif client == 'gemini':
        merge_json(home / '.gemini/settings.json', 'mcpServers', {'httpUrl': URL}, home, changes)
    elif client == 'agy':
        subprocess.run(['agy', 'mcp', 'add', '--type', 'http', 'huddle', URL], check=True, capture_output=True)
    elif client == 'opencode':
        path = home / '.config/opencode/opencode.json'
        if path.with_suffix('.jsonc').exists():
            raise ValueError('OpenCode JSONC configuration exists; manual merge required')
        merge_json(path, 'mcp', {'type': 'remote', 'url': URL, 'enabled': True}, home, changes)
    else:
        subprocess.run(['hermes', 'config', 'set', 'mcp_servers.huddle', json.dumps({'url': URL})], check=True, capture_output=True)


def install_skills(client, home, changes):
    source = Path(__file__).parent / 'skills'
    root = home / CLIENTS[client][1]
    for skill in source.iterdir():
        if not skill.is_dir():
            continue
        for file in skill.rglob('*'):
            if file.is_file():
                write_file(root / skill.name / file.relative_to(skill), file.read_bytes(), home, changes)


def verify_mcp():
    """Read-only handshake: distinguish an arbitrary web server from Huddle."""
    payload = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
        'protocolVersion': '2025-03-26', 'capabilities': {},
        'clientInfo': {'name': 'huddle-setup', 'version': '1'}}}
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'}

    def request(value):
        req = urllib.request.Request(URL, data=json.dumps(value).encode(), headers=headers)
        with urllib.request.urlopen(req, timeout=5) as response:
            raw = response.read(2 * 1024 * 1024).decode()
            session = response.headers.get('Mcp-Session-Id')
            if session:
                headers['Mcp-Session-Id'] = session
            if raw.startswith('event:') or raw.startswith('data:'):
                raw = next(line[5:].strip() for line in raw.splitlines() if line.startswith('data:'))
            return json.loads(raw) if raw else {}

    try:
        result = request(payload)
        if 'result' not in result:
            raise ValueError('MCP initialization failed')
        request({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        result = request({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}})
        names = {tool['name'] for tool in result.get('result', {}).get('tools', [])}
        if not {'room_create', 'message_post', 'swarm_pilot_create'} <= names:
            raise ValueError('Listener does not expose the required Huddle tools')
        return 'Huddle MCP initialize/tools-list verified; no agent or room launched'
    finally:
        if 'Mcp-Session-Id' in headers:
            try:
                urllib.request.urlopen(urllib.request.Request(URL, method='DELETE', headers=headers), timeout=2).close()
            except Exception:
                pass


def start_server(home):
    """Start once. Do not kill or replace an existing listener."""
    existing = False
    try:
        with urllib.request.urlopen('http://127.0.0.1:8014/dashboard', timeout=2) as response:
            existing = response.status == 200
    except Exception:
        pass
    if existing:
        return 'Existing server retained; ' + verify_mcp()
    logs = home / '.mcp-huddle/logs'
    logs.mkdir(parents=True, exist_ok=True)
    with (logs / 'setup-server.log').open('ab') as log:
        proc = subprocess.Popen([sys.executable, '-m', 'mcp_huddle', '--http', '--port', '8014'], stdout=log, stderr=log,
                                start_new_session=(os.name != 'nt'))
    for _ in range(40):
        if proc.poll() is not None:
            raise RuntimeError('Huddle exited at startup; inspect ~/.mcp-huddle/logs/setup-server.log')
        ready = False
        try:
            with urllib.request.urlopen('http://127.0.0.1:8014/dashboard', timeout=1) as response:
                ready = response.status == 200
        except Exception:
            time.sleep(.25)
        if ready:
            try:
                verified = verify_mcp()
            except Exception:
                proc.terminate()
                proc.wait(timeout=5)
                raise
            return f'Dashboard started, PID {proc.pid}; ' + verified + '; restart after reboot is not installed'
    proc.terminate()
    proc.wait(timeout=5)
    raise RuntimeError('Dashboard readiness timed out; owned process stopped, startup log retained')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--start', action='store_true')
    parser.add_argument('--home', type=Path, default=Path.home(), help='isolated fixture home or user home')
    args = parser.parse_args(argv)
    home = args.home.expanduser().resolve()
    if args.apply and os.name != 'posix':
        parser.error('Huddle server requires macOS/Linux/WSL (POSIX file locking)')
    detected = [c for c, (config, _) in CLIENTS.items() if shutil.which(c) or (home / config).is_dir()]
    overrides = [name for name in ('CLAUDE_CONFIG_DIR', 'CODEX_HOME', 'HERMES_HOME', 'XDG_CONFIG_HOME', 'OPENCODE_CONFIG', 'MCP_HUDDLE_HOME') if os.environ.get(name)]
    report = {'profile_overrides': overrides, 'apply': args.apply, 'endpoint': URL, 'clients': {}, 'changed': []}
    if args.apply and os.environ.get('MCP_HUDDLE_TOKEN'):
        parser.error('Token-protected deployment detected; configure client authentication through native tools first. No token is copied by this installer')
    if args.apply and 'MCP_HUDDLE_HOME' in overrides:
        parser.error('Custom MCP_HUDDLE_HOME detected; use that existing deployment instead of configuring default storage')
    for client in detected:
        status = {'skills': 'planned', 'mcp': 'planned', 'authentication': 'not checked', 'binary_available': bool(shutil.which(client))}
        report['clients'][client] = status
        if not args.apply:
            continue
        try:
            if (client == 'claude' and 'CLAUDE_CONFIG_DIR' in overrides) or (client == 'codex' and 'CODEX_HOME' in overrides) or (client == 'hermes' and 'HERMES_HOME' in overrides) or (client == 'opencode' and any(x in overrides for x in ('XDG_CONFIG_HOME', 'OPENCODE_CONFIG', 'MCP_HUDDLE_HOME'))):
                raise ValueError('Custom profile path detected; retained. Use native client setup for that profile')
            install_skills(client, home, report['changed'])
            status['skills'] = 'installed'
            # Config-only discovery must not call a CLI that is absent.
            if client in {'claude', 'codex', 'agy', 'hermes'} and not shutil.which(client):
                raise ValueError('CLI absent; MCP registration deferred')
            if client in {'claude', 'codex', 'agy', 'hermes'}:
                if home != Path.home().resolve():
                    raise ValueError('Native CLI config mutation disabled for fixture home')
                config = home / {'claude': '.claude.json', 'codex': '.codex/config.toml', 'agy': '.gemini/config/mcp_config.json', 'hermes': '.hermes/config.yaml'}[client]
                if client == 'hermes' and config.exists():
                    # The client owns YAML parsing. Never dump the complete config.
                    value = subprocess.run(['hermes', 'config', 'get', 'mcp_servers.huddle'], capture_output=True, text=True)
                    if value.returncode == 0 and value.stdout.strip() not in {'', 'None', 'null'}:
                        raise ValueError('Existing Hermes huddle registration retained; review with native config command')
                if config.is_symlink():
                    raise ValueError('Native client configuration is a symlink; retained for manual review')
                if config.exists():
                    # Native command owns parsing; preserve the original first.
                    old = config.read_bytes()
                    digest = hashlib.sha256(old).hexdigest()[:16]
                    backup = home / '.mcp-huddle/setup-backups' / digest / config.relative_to(home)
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    if not backup.exists():
                        backup.write_bytes(old)
                        backup.chmod(0o600)
                    # Do not overwrite an existing registration through native add.
                    if client == 'codex':
                        import tomllib
                        existing = tomllib.loads(old.decode()).get('mcp_servers', {}).get('huddle')
                    elif client in {'claude', 'agy'}:
                        existing = json.loads(old).get('mcpServers', {}).get('huddle')
                    else:
                        existing = None
                    if existing is not None:
                        if not isinstance(existing, dict):
                            raise ValueError('Unsupported existing Huddle entry; retained')
                        if existing.get('enabled') is False or existing.get('disabled') is True:
                            raise ValueError('Existing Huddle registration is disabled; preserved')
                        if (existing.get('url') or existing.get('httpUrl') or existing.get('serverUrl')) == URL:
                            status['mcp'] = 'existing'
                            continue
                        raise ValueError('Existing huddle registration retained; manual review required')
            configure(client, home, report['changed'])
            if client in {'claude', 'codex', 'agy'}:
                if not config.exists():
                    raise ValueError('Native registration could not be read back at the supported path')
                if client == 'codex':
                    import tomllib
                    entry = tomllib.loads(config.read_text()).get('mcp_servers', {}).get('huddle', {})
                else:
                    entry = json.loads(config.read_text()).get('mcpServers', {}).get('huddle', {})
                if (entry.get('url') or entry.get('httpUrl') or entry.get('serverUrl')) != URL:
                    raise ValueError('Native registration URL did not match; manual review needed')
            if client == 'hermes':
                value = subprocess.run(['hermes', 'config', 'get', 'mcp_servers.huddle'], capture_output=True, text=True, check=True)
                if URL not in value.stdout:
                    raise ValueError('Hermes registration was not read back; managed configuration may reject edits')
            status['mcp'] = 'configured'
        except (ValueError, OSError, AttributeError, subprocess.CalledProcessError) as exc:
            status['detail'] = str(exc) if not isinstance(exc, subprocess.CalledProcessError) else 'Native registration command failed; original backed up, inspect current configuration before retry'
            status['mcp'] = 'needs attention'
    if args.apply:
        from .spawn import DEFAULT_REGISTRY
        path = home / '.mcp-huddle/registry.json'
        if not path.exists():
            ready = {c for c, item in report['clients'].items() if item['mcp'] in {'configured', 'existing'}}
            names = {'Codex': 'codex', 'Claude': 'claude', 'Antigravity': 'agy', 'OpenCode': 'opencode'}
            profiles = []
            for spec in DEFAULT_REGISTRY:
                entry = dict(spec)
                entry['enabled'] = False
                entry['setup_ready'] = names.get(entry['name']) in ready
                if entry['setup_ready']:
                    entry['mcp_url'] = URL
                profiles.append(entry)
            write_file(path, (json.dumps(profiles, indent=2) + '\n').encode(), home, report['changed'])
            report['registry'] = 'Created disabled supported profiles. Select and enable them after checking native login and model availability. Gemini/Hermes can participate through MCP; managed spawn profiles are not bundled.'
        else:
            report['registry'] = 'Existing registry retained'
        if args.start:
            if home != Path.home().resolve():
                raise ValueError('--start is disabled for a fixture home')
            try:
                report['server'] = start_server(home)
            except Exception as exc:
                report['server'] = 'needs attention'
                report['server_detail'] = str(exc)
        out = home / '.mcp-huddle/setup-report.json'
        write_file(out, (json.dumps(report, indent=2) + '\n').encode(), home, [])
    print(json.dumps(report, indent=2))
    return report


def cli():
    report = main()
    return 2 if report['apply'] and (report.get('server') == 'needs attention' or any(c['mcp'] == 'needs attention' for c in report['clients'].values())) else 0


if __name__ == '__main__':
    raise SystemExit(cli())
