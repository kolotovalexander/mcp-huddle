// ── Helpers ───────────────────────────────────────────────
const AGENT_CLS = {
  Claude:'agent-claude', Codex:'agent-codex', Gemini:'agent-gemini',
  Human:'agent-human',   System:'agent-system',
};
const AVATAR_CLS = {
  Claude:'avatar-claude', Codex:'avatar-codex', Gemini:'avatar-gemini',
  Human:'avatar-human',   System:'avatar-system',
};
const AGENT_LETTER = {Claude:'C', Codex:'X', Gemini:'G', Human:'H', System:'S'};

function agentCls(a)  { return AGENT_CLS[a]  || 'agent-other'; }
function avatarCls(a) { return AVATAR_CLS[a] || 'avatar-other'; }
function agentLetter(a) { return AGENT_LETTER[a] || (String(a||'?')[0] || '?').toUpperCase(); }

function el(tag, attrs, children) {
  const node = document.createElement(tag);
  if (attrs) for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') node.className = v;
    else if (k === 'dataset') for (const [dk, dv] of Object.entries(v)) node.dataset[dk] = dv;
    else if (k === 'text') node.textContent = v;
    else node.setAttribute(k, v);
  }
  if (children) for (const c of children) {
    if (c == null) continue;
    node.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
  }
  return node;
}

function avatar(agent, sizeCls) {
  return el('div', {class: `avatar ${sizeCls || 'avatar-md'} ${avatarCls(agent)}`, text: agentLetter(agent)});
}

function fmtTime(ts) {
  return new Date((ts || 0) * 1000).toLocaleTimeString('ru', {hour:'2-digit', minute:'2-digit'});
}

// ── State ─────────────────────────────────────────────────
let currentRoom = null, currentOwner = null, lastId = 0;
let rooms = [], msgMap = {};

// ── Theme toggle ──────────────────────────────────────────
function initTheme() {
  const saved = localStorage.getItem('agentbus-theme') || 'dark';
  document.documentElement.setAttribute('data-theme', saved);
  document.getElementById('theme-toggle').onclick = () => {
    const next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem('agentbus-theme', next);
  };
}

// ── Rooms ─────────────────────────────────────────────────
async function loadRooms() {
  try {
    const r = await fetch('/api/rooms');
    rooms = await r.json();
    document.getElementById('room-count').textContent = rooms.length;
    renderRooms();
  } catch(e) {}
}

function renderRooms() {
  const sidebar = document.getElementById('room-list');
  sidebar.innerHTML = '';
  if (!rooms.length) {
    sidebar.appendChild(el('div', {class: 'empty-sidebar', text: 'No rooms yet. Call room_create() from an agent.'}));
    return;
  }

  // Hierarchy: project basename → session_id → rooms
  const projects = new Map();
  for (const r of rooms) {
    const parts = (r.cwd || '').replace(/[/]+$/, '').split('/').filter(Boolean);
    const proj = parts.length ? parts[parts.length - 1] : '—';
    const sess = r.session_id || 'default';
    if (!projects.has(proj)) projects.set(proj, new Map());
    const sessions = projects.get(proj);
    if (!sessions.has(sess)) sessions.set(sess, []);
    sessions.get(sess).push(r);
  }

  for (const [proj, sessions] of projects) {
    const block = el('div', {class: 'proj-block'});
    const total = [...sessions.values()].reduce((sum, rs) => sum + rs.length, 0);
    block.appendChild(el('div', {class: 'proj-header'}, [
      el('span', {text: proj}),
      el('span', {class: 'count', text: String(total)}),
    ]));

    for (const [sess, rs] of sessions) {
      const sessLabel = sess.length > 14 ? sess.slice(0, 14) + '…' : sess;
      block.appendChild(el('div', {class: 'sess-header'}, [
        el('span', {class: 'arrow', text: '›'}),
        el('span', {text: sessLabel}),
      ]));

      for (const r of rs) {
        const active = r.id === currentRoom;
        const item = el('div', {
          class: 'room-item' + (active ? ' active' : ''),
          dataset: {id: r.id, owner: r.owner},
        }, [
          el('div', {class: 'room-name'}, [
            el('span', {class: `dot dot-${r.status}` + (r.status === 'open' ? ' pulse' : '')}),
            el('span', {text: r.name}),
          ]),
          el('div', {class: 'room-meta', text: `${(r.participants || []).length}·${fmtTime(r.last_activity || r.created_at)}`}),
        ]);
        block.appendChild(item);
      }
    }
    sidebar.appendChild(block);
  }
}

// ── Chat ──────────────────────────────────────────────────
function buildChatShell(room) {
  const chat = document.getElementById('chat-area');
  chat.innerHTML = '';

  const isClosed = room.status === 'closed';
  const isReadOnly = isClosed || room.status === 'resolved';

  const titleChildren = [
    el('span', {class: 'hash', text: '#'}),
    el('span', {text: room.name || room.id || ''}),
  ];
  if (isClosed) {
    titleChildren.push(el('span', {class: 'kind kind-close', text: 'closed'}));
  } else if (room.status === 'resolved') {
    titleChildren.push(el('span', {class: 'kind kind-final', text: 'resolved'}));
  } else if (room.status === 'closing_requested') {
    titleChildren.push(el('span', {class: 'kind kind-system', text: 'closing requested'}));
  }

  const closeBtnAttrs = {class: 'lq-btn danger', id: 'btn-close', text: 'Close'};
  if (isClosed) {
    closeBtnAttrs.disabled = '';
    closeBtnAttrs.title = 'Room is already closed';
  }

  const header = el('div', {class: 'chat-header'}, [
    el('div', {}, [
      el('div', {class: 'chat-title'}, titleChildren),
      el('div', {class: 'chat-meta', id: 'chat-meta', text: 'Loading…'}),
    ]),
    el('div', {class: 'topbar-actions'}, [
      el('div', {class: 'avatar-stack', id: 'avatar-stack'}),
      el('button', closeBtnAttrs),
    ]),
  ]);

  const messages = el('div', {class: 'messages', id: 'messages'});

  const inputAttrs = {
    id: 'human-inp',
    type: 'text',
    placeholder: isReadOnly
      ? (isClosed ? 'Room closed — read-only' : 'Room resolved — read-only')
      : 'Send as Human — system priority bypasses anti-loop rules…',
  };
  if (isReadOnly) inputAttrs.disabled = '';
  const input = el('input', inputAttrs);

  const sendAttrs = {class: 'send-btn', id: 'btn-send', text: 'Send ↵'};
  if (isReadOnly) sendAttrs.disabled = '';
  const send = el('button', sendAttrs);

  const inputWrap = el('div', {class: 'input-wrap'}, [
    el('div', {class: 'input-row'}, [
      avatar('Human', 'avatar-sm'),
      input, send,
    ]),
  ]);

  chat.appendChild(header);
  chat.appendChild(messages);
  chat.appendChild(inputWrap);

  if (!isClosed) {
    document.getElementById('btn-close').onclick = closeRoom;
  }
  if (!isReadOnly) {
    send.onclick = sendMsg;
    input.onkeydown = e => { if (e.key === 'Enter') sendMsg(); };
  }
}

async function openRoom(id, owner) {
  currentRoom = id;
  currentOwner = owner;
  lastId = 0;
  msgMap = {};
  closeAgentStreams();  // tear down EventSources from previous room
  renderRooms();
  buildChatShell(rooms.find(x => x.id === id) || {id});
  await fetchMessages(true);
  await attachAgentPanels(id);  // Phase 1: live Codex/Gemini event stream
}

// ── Phase 1: agent live event panels ─────────────────────────────────────────

let agentStreams = {};  // {agentName: EventSource}

function closeAgentStreams() {
  for (const k in agentStreams) {
    try { agentStreams[k].close(); } catch(e) {}
  }
  agentStreams = {};
}

async function attachAgentPanels(roomId) {
  let resp;
  try {
    resp = await fetch('/api/room_agents?room_id=' + encodeURIComponent(roomId));
  } catch(e) { return; }
  if (!resp.ok) return;
  const {agents} = await resp.json();
  if (!agents || Object.keys(agents).length === 0) return;

  const chat = document.getElementById('chat-area');
  if (!chat) return;

  const wrap = el('div', {class: 'agent-panels', id: 'agent-panels'});
  const header = el('div', {class: 'agent-panels-header', text: 'Agent activity (live)'});
  wrap.appendChild(header);

  for (const name of Object.keys(agents)) {
    const panel = el('details', {class: 'agent-panel', dataset: {agent: name}, open: ''}, [
      el('summary', {class: 'agent-panel-summary'}, [
        avatar(name, 'avatar-sm'),
        el('span', {class: 'agent-panel-name', text: name}),
        el('span', {class: 'agent-panel-status', id: `agent-status-${name}`, text: '·'}),
      ]),
      el('div', {class: 'agent-events', id: `agent-events-${name}`}),
    ]);
    wrap.appendChild(panel);

    const url = `/agents/${encodeURIComponent(roomId)}/${encodeURIComponent(name)}/events`;
    const es = new EventSource(url);
    es.addEventListener('open', () => {
      const s = document.getElementById(`agent-status-${name}`);
      if (s) s.textContent = '● live';
    });
    es.addEventListener('error', () => {
      const s = document.getElementById(`agent-status-${name}`);
      if (s) s.textContent = '× closed';
    });
    es.onmessage = (ev) => appendAgentEvent(name, ev.data);
    agentStreams[name] = es;
  }

  chat.appendChild(wrap);
}

function appendAgentEvent(agentName, raw) {
  const list = document.getElementById(`agent-events-${agentName}`);
  if (!list) return;
  let summary = raw;
  let detail = null;
  try {
    const obj = JSON.parse(raw);
    // Codex --json events: {type, agent_message?, delta?, ...}
    // Gemini stream-json: {type, content?, ...}
    if (obj.type) {
      summary = obj.type;
      if (obj.agent_message) summary += ': ' + String(obj.agent_message).slice(0, 120);
      else if (obj.delta) summary += ': ' + String(obj.delta).slice(0, 120);
      else if (obj.content) summary += ': ' + String(obj.content).slice(0, 120);
      detail = JSON.stringify(obj, null, 2);
    }
  } catch(e) {
    // Not JSON — show as plain text (e.g. stderr lines).
  }
  const line = el('div', {class: 'agent-event'}, [
    el('span', {class: 'agent-event-summary', text: summary}),
  ]);
  if (detail) {
    line.title = detail;
  }
  list.appendChild(line);
  // Auto-scroll: keep last 200 events to avoid runaway DOM.
  while (list.children.length > 200) list.removeChild(list.firstChild);
  list.scrollTop = list.scrollHeight;
}

function renderOne(m) {
  const list = document.getElementById('messages');
  if (!list) return;
  msgMap[m.id] = {body: m.body, agent: m.agent};

  const isSystem = (m.agent === 'System' || m.kind === 'system' || m.kind === 'close');

  if (isSystem) {
    const div = el('div', {class: 'msg is-system', dataset: {id: String(m.id)}}, [
      el('div', {class: 'msg-body', text: m.body}),
    ]);
    list.appendChild(div);
    list.scrollTop = list.scrollHeight;
    return;
  }

  const line = el('div', {class: 'msg-line'}, [
    el('span', {class: 'msg-name', text: m.agent}),
    m.to ? el('span', {class: 'msg-to', text: '→ ' + m.to}) : null,
    el('span', {class: `kind kind-${m.kind}`, text: m.kind}),
    el('span', {class: 'msg-time', text: fmtTime(m.timestamp)}),
  ]);

  const bubble = el('div', {class: 'msg-bubble'});
  if (m.reply_to != null) {
    const q = msgMap[m.reply_to];
    const replyName = q ? q.agent : `#${m.reply_to}`;
    const replyAgentColor = q ? `var(--c-${(q.agent||'').toLowerCase()}, var(--text-muted))` : 'var(--text-muted)';
    const preview = q
      ? (q.body.length > 90 ? q.body.slice(0,90) + '…' : q.body)
      : `(message #${m.reply_to})`;

    const replyEl = el('div', {class: 'reply'}, [
      el('div', {class: 'reply-bar'}),
      el('div', {class: 'reply-content'}, [
        el('div', {class: 'reply-name', text: '↳ ' + replyName}),
        el('div', {class: 'reply-text', text: preview}),
      ]),
    ]);
    replyEl.querySelector('.reply-bar').style.background = replyAgentColor;
    replyEl.querySelector('.reply-name').style.color = replyAgentColor;
    bubble.appendChild(replyEl);
  }
  bubble.appendChild(el('div', {class: 'msg-body', text: m.body}));

  const div = el('div', {
    class: `msg ${agentCls(m.agent)} kind-${m.kind}`,
    dataset: {id: String(m.id)},
  }, [
    avatar(m.agent),
    el('div', {class: 'msg-content'}, [line, bubble]),
  ]);

  list.appendChild(div);
  list.scrollTop = list.scrollHeight;
}

function renderAvatarStack(participants) {
  const stack = document.getElementById('avatar-stack');
  if (!stack) return;
  stack.innerHTML = '';
  for (const p of (participants || []).slice(0, 5)) {
    stack.appendChild(avatar(p, 'avatar-sm'));
  }
}

function renderChatMeta(room, statuses) {
  const meta = document.getElementById('chat-meta');
  if (!meta) return;
  meta.innerHTML = '';
  const parts = (room.participants || []);
  meta.appendChild(document.createTextNode(parts.join(' · ') + ' · '));
  meta.appendChild(el('span', {class: 'msg-time', text: room.status}));
  for (const [agent, st] of Object.entries(statuses || {})) {
    if (!st || st === 'online') continue;
    meta.appendChild(document.createTextNode(' '));
    meta.appendChild(el('span', {class: `kind kind-${st === 'busy' ? 'busy' : 'comment'}`, text: `${agent}: ${st}`}));
  }
}

async function fetchMessages(initial) {
  if (!currentRoom) return;
  try {
    const url = `/api/messages_json?room_id=${encodeURIComponent(currentRoom)}&since_id=${lastId}`;
    const resp = await fetch(url);
    const data = await resp.json();
    if (data.error) return;

    if (data.room) {
      renderChatMeta(data.room, data.statuses);
      renderAvatarStack(data.room.participants);
    }

    if (initial) {
      const list = document.getElementById('messages');
      if (list) list.innerHTML = '';
      msgMap = {};
    }

    const msgs = data.messages || [];
    for (const m of msgs) renderOne(m);
    if (msgs.length) lastId = msgs[msgs.length-1].id;
  } catch(e) {}
}

async function sendMsg() {
  if (!currentRoom) return;
  const inp = document.getElementById('human-inp');
  const btn = document.getElementById('btn-send');
  const body = inp && inp.value ? inp.value.trim() : '';
  if (!body) return;
  inp.disabled = true; btn.disabled = true;
  try {
    await fetch('/api/message_post', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({room_id: currentRoom, agent: 'Human', body, kind: 'system', to: 'all'}),
    });
    inp.value = '';
    await fetchMessages(false);
  } finally {
    inp.disabled = false; btn.disabled = false;
    inp.focus();
  }
}

async function closeRoom() {
  if (!currentRoom || !confirm('Close this room?')) return;
  await fetch('/api/room_close', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({room_id: currentRoom, owner: currentOwner}),
  });
  currentRoom = null;
  const chat = document.getElementById('chat-area');
  chat.innerHTML = '';
  chat.appendChild(el('div', {class: 'empty'}, [
    el('div', {class: 'empty-title', text: 'Room closed'}),
    el('div', {class: 'empty-hint', text: 'Pick another room from the sidebar'}),
  ]));
  await loadRooms();
}

// ── Event delegation ──────────────────────────────────────
document.addEventListener('click', e => {
  const item = e.target.closest('.room-item');
  if (item) openRoom(item.dataset.id, item.dataset.owner);
});

// ── Polling ───────────────────────────────────────────────
async function tick() {
  await loadRooms();
  if (currentRoom) await fetchMessages(false);
}

initTheme();
loadRooms();
setInterval(tick, 3000);
