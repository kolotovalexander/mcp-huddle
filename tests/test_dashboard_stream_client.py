"""Browser-stream reconnect regression executed with the installed Node runtime."""

from pathlib import Path
import shutil
import subprocess

import pytest


def test_disconnect_reconnect_resumes_offset_without_duplicate_replay():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the dashboard stream regression")
    dashboard = Path(__file__).parents[1] / "src/mcp_huddle/static/dashboard.js"
    script = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const start = src.indexOf('function parseSSEText');
const end = src.indexOf('\nfunction resetActivityPanel', start);
if (start < 0 || end < 0) throw new Error('stream helpers not found');

let currentRoom = 'room_test';
let activityStreamGeneration = 7;
let agentStreams = {};
const rendered = [];
const statusNode = {textContent: ''};
global.document = {getElementById: () => statusNode};
// streamAgentEvents uses the dashboard's global translator in production;
// provide that same dependency in this isolated browser-stream harness.
const labels = {
  'activity.liveStatus': 'Live',
  'activity.resetStatus': 'Stream reset',
  'activity.errorStatus': 'Stream error',
  'activity.authStatus': 'Authentication required',
  'activity.retrying': 'Retrying',
};
global.t = key => labels[key] || key;
global.roomData = {};
global.swarmAgentOutcome = () => '';
global.appendAgentEvent = (_name, data) => rendered.push(data);
global.showAuthRequired = () => {};
global.HuddleHTTPError = class extends Error { constructor(status, message) { super(message); this.status = status; } };

function responseFrom(chunks, onExhausted) {
  let index = 0;
  return {body: {getReader: () => ({read: async () => {
    if (index < chunks.length) {
      return {value: new TextEncoder().encode(chunks[index++]), done: false};
    }
    if (onExhausted) onExhausted();
    return {value: undefined, done: true};
  }})}};
}

let calls = 0;
const requested = [];
global.apiFetch = async url => {
  calls += 1;
  requested.push(url);
  if (calls === 1) {
    return responseFrom([
      `event: open\ngeneration: ${'a'.repeat(64)}\ncursor: ${'0'.repeat(64)}\nid: 0\ndata: streaming\n\n`
      + `cursor: ${'c'.repeat(64)}\nid: 10\ndata: first\n\ncursor: ${'d'.repeat(64)}\nid: 20\ndata: truncated`
    ]);
  }
  if (calls === 2) {
    return responseFrom([
      `event: open\ngeneration: ${'a'.repeat(64)}\ncursor: ${'c'.repeat(64)}\nid: 10\ndata: streaming\n\n`
      + `cursor: ${'d'.repeat(64)}\nid: 20\ndata: second\n\n`
    ]);
  }
  if (calls === 3) {
    return responseFrom([
      `event: open\ngeneration: ${'b'.repeat(64)}\ncursor: ${'e'.repeat(64)}\nid: 0\ndata: streaming\n\n`
      + `cursor: ${'f'.repeat(64)}\nid: 20\ndata: NEW_LOG_FIRST_EVENT\n\n`
    ], () => { currentRoom = 'different_room'; });
  }
  throw new Error(`unexpected reconnect ${calls}`);
};

const realSetTimeout = global.setTimeout;
global.setTimeout = (fn, _delay) => realSetTimeout(fn, 0);
eval(src.slice(start, end));

const stream = {
  roomId: 'room_test', generation: 7, offset: 0, attempt: 0,
  fileGeneration: '', fileCursor: '',
  controller: null, retryTimer: null, retryResolve: null, cancelled: false,
};
agentStreams.Codex = stream;
streamAgentEvents('/agents/room_test/Codex/events', 'Codex', stream).then(() => {
  if (calls !== 3) throw new Error(`expected three connections, got ${calls}`);
  if (!requested[1].includes(`offset=10&generation=${'a'.repeat(64)}&cursor=${'c'.repeat(64)}`)) {
    throw new Error(`did not resume from committed frame: ${requested[1]}`);
  }
  if (!requested[2].includes(`offset=20&generation=${'a'.repeat(64)}&cursor=${'d'.repeat(64)}`)) {
    throw new Error(`did not carry old generation into rotation: ${requested[2]}`);
  }
  if (JSON.stringify(rendered) !== JSON.stringify(['first', 'second', 'NEW_LOG_FIRST_EVENT'])) {
    throw new Error(`duplicate or missing replay: ${JSON.stringify(rendered)}`);
  }
  if (stream.offset !== 20) throw new Error(`wrong resume offset ${stream.offset}`);
  if (stream.fileGeneration !== 'b'.repeat(64)) throw new Error('new generation not committed');
  if (stream.fileCursor !== 'f'.repeat(64)) throw new Error('new cursor not committed');
  if (statusNode.textContent !== '● Live') throw new Error(`wrong translated stream status: ${statusNode.textContent}`);
}).catch(error => {
  console.error(error);
  process.exitCode = 1;
});
"""
    completed = subprocess.run(
        [node, "-e", script, str(dashboard)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
