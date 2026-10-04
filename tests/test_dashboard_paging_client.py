"""Focused Node check for bounded dashboard history-window behavior."""

from pathlib import Path
import shutil
import subprocess

import pytest


def test_dashboard_history_window_stays_bounded_and_navigable():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the dashboard paging regression")
    dashboard = Path(__file__).parents[1] / "src/mcp_huddle/static/dashboard.js"
    script = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const start = src.indexOf('function normalizeMessagePage');
const end = src.indexOf('\nfunction renderMessageWindow', start);
if (start < 0 || end < 0) throw new Error('paging helpers not found');
const MESSAGE_PAGE_SIZE = 100;
const MESSAGE_WINDOW_LIMIT = 300;
eval(src.slice(start, end));

const existing = Array.from({length: 300}, (_, i) => ({id: i + 301}));
const older = Array.from({length: 100}, (_, i) => ({id: i + 201}));
const historic = mergeMessageWindow(existing, older, 'older');
if (historic.length !== 300 || historic[0].id !== 201 || historic.at(-1).id !== 500) {
  throw new Error(`older navigation window is wrong: ${historic.length}/${historic[0]?.id}/${historic.at(-1)?.id}`);
}
const newer = Array.from({length: 100}, (_, i) => ({id: i + 501}));
const advanced = mergeMessageWindow(historic, newer, 'newer');
if (advanced.length !== 300 || advanced[0].id !== 301 || advanced.at(-1).id !== 600) {
  throw new Error(`newer navigation window is wrong: ${advanced.length}/${advanced[0]?.id}/${advanced.at(-1)?.id}`);
}
const page = normalizeMessagePage({messages: [{id: 602}, {id: 601}], latest_id: 602}, 'updates', 600);
if (page.messages.map(m => m.id).join(',') !== '601,602') throw new Error('updates were not sorted and filtered');
const focus = normalizeMessagePage({messages: Array.from({length: 500}, (_, i) => ({id: i + 1}))}, 'focus', 402);
if (focus.messages.length !== 100 || focus.messages.at(-1).id !== 401) throw new Error('message focus did not return the page before the target');
"""
    completed = subprocess.run(
        [node, "-e", script, str(dashboard)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
