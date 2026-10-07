#!/usr/bin/env python3
"""Install this checkout into a private venv, then configure detected clients."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import venv


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='install and configure; otherwise preview')
    parser.add_argument('--start', action='store_true', help='start the dashboard after setup')
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        parser.error('Python 3.11 or newer is required')
    if os.name != 'posix':
        parser.error('Use macOS, Linux or WSL: Huddle requires POSIX file locking')
    source = Path(__file__).resolve().parent
    if not args.apply:
        sys.path.insert(0, str(source / 'src'))
        from mcp_huddle.setup import main as setup
        setup([])
        return
    if os.environ.get('MCP_HUDDLE_TOKEN') or os.environ.get('MCP_HUDDLE_HOME'):
        parser.error('Existing custom/protected Huddle deployment detected; configure it through native setup rather than replace its runtime')
    target = Path.home() / '.mcp-huddle' / 'venv'
    venv.EnvBuilder(with_pip=True).create(target)
    python = target / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    subprocess.run([str(python), '-m', 'pip', 'install', str(source)], check=True)
    subprocess.run([str(python), '-m', 'mcp_huddle.setup', '--apply'] + (['--start'] if args.start else []), check=True)


if __name__ == '__main__':
    main()
