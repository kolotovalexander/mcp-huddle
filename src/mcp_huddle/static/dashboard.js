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
let agentMetaTotals = {}; // {agentName: {tokens_total, tokens_in, tokens_out, msgs, models:Set, last_reasoning}}

function metaBadge(meta) {
  if (!meta) return null;
  const parts = [];
  if (meta.model) parts.push(meta.model);
  if (meta.reasoning) parts.push('r=' + meta.reasoning);
  const tokTotal = (meta.tokens_total ?? ((meta.tokens_in || 0) + (meta.tokens_out || 0))) || null;
  if (tokTotal) parts.push(tokTotal.toLocaleString() + ' tok');
  if (!parts.length) return null;
  const span = el('span', {class: 'msg-meta', text: parts.join(' · ')});
  if (meta.tokens_in || meta.tokens_out) {
    span.title = `in=${meta.tokens_in||0} out=${meta.tokens_out||0}` +
                 (meta.duration_ms != null ? ` · ${meta.duration_ms}ms` : '');
  }
  return span;
}

function bumpAgentTotals(agent, meta) {
  if (!agent || !meta) return;
  const t = agentMetaTotals[agent] = agentMetaTotals[agent] || {
    tokens_total: 0, tokens_in: 0, tokens_out: 0, msgs: 0, models: new Set(), last_reasoning: null,
  };
  t.msgs += 1;
  if (meta.tokens_total) t.tokens_total += meta.tokens_total;
  if (meta.tokens_in)    t.tokens_in    += meta.tokens_in;
  if (meta.tokens_out)   t.tokens_out   += meta.tokens_out;
  if (!meta.tokens_total && (meta.tokens_in || meta.tokens_out)) {
    t.tokens_total += (meta.tokens_in || 0) + (meta.tokens_out || 0);
  }
  if (meta.model) t.models.add(meta.model);
  if (meta.reasoning) t.last_reasoning = meta.reasoning;
}

function renderAgentTotalsBadge(agent) {
  const t = agentMetaTotals[agent];
  if (!t) return;
  const tag = document.getElementById(`agent-totals-${agent}`);
  if (!tag) return;
  const models = [...t.models].join(',');
  const parts = [];
  if (models) parts.push(models);
  if (t.last_reasoning) parts.push('r=' + t.last_reasoning);
  if (t.tokens_total) parts.push(t.tokens_total.toLocaleString() + ' tok');
  if (t.msgs) parts.push(`${t.msgs} msg`);
  tag.textContent = parts.join(' · ') || '·';
}

// ── Theme: auto (follows OS) | dark | light ────────────────
// Cycle order: auto → dark → light → auto. Click the moon icon to advance.
// 'auto' listens to prefers-color-scheme so theme switches when OS does.
const THEME_CYCLE = ['auto', 'dark', 'light'];
const THEME_GLYPH = {auto: '🌓', dark: '🌙', light: '☀️'};
const THEME_LABEL = {auto: 'Auto', dark: 'Dark', light: 'Light'};

function applyTheme(mode) {
  const html = document.documentElement;
  if (mode === 'auto') {
    const dark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    html.setAttribute('data-theme', dark ? 'dark' : 'light');
  } else {
    html.setAttribute('data-theme', mode);
  }
  const btn = document.getElementById('theme-toggle');
  if (btn) {
    btn.textContent = THEME_GLYPH[mode];
    btn.title = `Theme: ${THEME_LABEL[mode]} (click to cycle)`;
  }
}

function initTheme() {
  // Migrate legacy values: only 'dark'/'light' were stored before; treat them
  // as explicit choices. Anything else (or unset) defaults to 'auto'.
  const raw = localStorage.getItem('agentbus-theme');
  let mode = THEME_CYCLE.includes(raw) ? raw : 'auto';
  applyTheme(mode);

  // Re-apply on OS theme change while in 'auto' mode.
  const mq = window.matchMedia('(prefers-color-scheme: dark)');
  const onSysChange = () => {
    if ((localStorage.getItem('agentbus-theme') || 'auto') === 'auto') {
      applyTheme('auto');
    }
  };
  if (mq.addEventListener) mq.addEventListener('change', onSysChange);
  else if (mq.addListener) mq.addListener(onSysChange);  // legacy Safari

  document.getElementById('theme-toggle').onclick = () => {
    const cur = localStorage.getItem('agentbus-theme') || 'auto';
    const idx = THEME_CYCLE.indexOf(cur);
    const next = THEME_CYCLE[(idx + 1) % THEME_CYCLE.length];
    localStorage.setItem('agentbus-theme', next);
    applyTheme(next);
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

  // Closed rooms: show Delete button instead of Close (Close is irrelevant; user
  // wants either to keep history read-only or wipe the room from disk).
  const actionBtn = isClosed
    ? el('button', {class: 'lq-btn danger', id: 'btn-delete', text: 'Delete', title: 'Permanently remove this room from disk'})
    : el('button', {class: 'lq-btn danger', id: 'btn-close', text: 'Close'});

  const header = el('div', {class: 'chat-header'}, [
    el('div', {}, [
      el('div', {class: 'chat-title'}, titleChildren),
      el('div', {class: 'chat-meta', id: 'chat-meta', text: 'Loading…'}),
    ]),
    el('div', {class: 'topbar-actions'}, [
      el('div', {class: 'avatar-stack', id: 'avatar-stack'}),
      actionBtn,
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

  if (isClosed) {
    document.getElementById('btn-delete').onclick = deleteRoom;
  } else {
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
  agentMetaTotals = {};
  closeAgentStreams();  // tear down EventSources from previous room
  renderRooms();
  buildChatShell(rooms.find(x => x.id === id) || {id});
  await fetchMessages(true);
  await attachAgentPanels(id);  // Phase 1: live Codex/Gemini event stream
  // Re-paint totals badges after panels rebuilt
  Object.keys(agentMetaTotals).forEach(renderAgentTotalsBadge);
}

// ── Phase 1: agent live event panels ─────────────────────────────────────────

let agentStreams = {};  // {agentName: EventSource}

function closeAgentStreams() {
  for (const k in agentStreams) {
    try { agentStreams[k].close(); } catch(e) {}
  }
  agentStreams = {};
  resetActivityPanel('Откроется при выборе комнаты со spawned-агентами');
}

function resetActivityPanel(emptyHint) {
  const panel = document.getElementById('activity-panel');
  if (!panel) return null;
  panel.innerHTML = '';
  if (emptyHint) {
    panel.appendChild(el('div', {class: 'activity-empty'}, [
      el('div', {class: 'activity-empty-title', text: 'Agent activity'}),
      el('div', {class: 'activity-empty-hint', text: emptyHint}),
    ]));
  }
  return panel;
}

async function attachAgentPanels(roomId) {
  let resp;
  try {
    resp = await fetch('/api/room_agents?room_id=' + encodeURIComponent(roomId));
  } catch(e) {
    resetActivityPanel('Не удалось загрузить агентов');
    return;
  }
  if (!resp.ok) {
    resetActivityPanel('Нет данных об агентах');
    return;
  }
  const {agents} = await resp.json();
  const panel = resetActivityPanel(null);
  if (!panel) return;

  if (!agents || Object.keys(agents).length === 0) {
    resetActivityPanel('В этой комнате нет spawned-агентов');
    return;
  }

  const wrap = el('div', {class: 'agent-panels', id: 'agent-panels'});
  const header = el('div', {class: 'agent-panels-header', text: 'Agent activity (live)'});
  wrap.appendChild(header);

  for (const name of Object.keys(agents)) {
    const detailsEl = el('details', {class: 'agent-panel', dataset: {agent: name}, open: ''}, [
      el('summary', {class: 'agent-panel-summary'}, [
        avatar(name, 'avatar-sm'),
        el('span', {class: 'agent-panel-name', text: name}),
        el('span', {class: 'agent-panel-totals', id: `agent-totals-${name}`, text: '·'}),
        el('span', {class: 'agent-panel-status', id: `agent-status-${name}`, text: '·'}),
      ]),
      el('div', {class: 'agent-events', id: `agent-events-${name}`}),
    ]);
    wrap.appendChild(detailsEl);

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

  panel.appendChild(wrap);
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

  const badge = metaBadge(m.meta);
  if (m.meta) {
    bumpAgentTotals(m.agent, m.meta);
    renderAgentTotalsBadge(m.agent);
  }
  const line = el('div', {class: 'msg-line'}, [
    el('span', {class: 'msg-name', text: m.agent}),
    m.to ? el('span', {class: 'msg-to', text: '→ ' + m.to}) : null,
    el('span', {class: `kind kind-${m.kind}`, text: m.kind}),
    el('span', {class: 'msg-time', text: fmtTime(m.timestamp)}),
    badge,
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
  if (room.session_id) {
    meta.appendChild(document.createTextNode(' · '));
    meta.appendChild(el('span', {class: 'msg-meta', text: 'sid: ' + room.session_id, title: 'session_id'}));
  }
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

async function deleteRoom() {
  if (!currentRoom) return;
  if (!confirm('Permanently delete this room from disk?\n\nAll messages, agent logs and metadata will be lost. This cannot be undone.')) return;
  closeAgentStreams();
  const resp = await fetch('/api/room_delete', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({room_id: currentRoom, owner: currentOwner}),
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({error: 'unknown error'}));
    alert(`Failed to delete: ${err.error || 'unknown error'}`);
    return;
  }
  currentRoom = null;
  const chat = document.getElementById('chat-area');
  chat.innerHTML = '';
  chat.appendChild(el('div', {class: 'empty'}, [
    el('div', {class: 'empty-title', text: 'Room deleted'}),
    el('div', {class: 'empty-hint', text: 'Pick another room from the sidebar'}),
  ]));
  await loadRooms();
}

// ── Bulk room actions ─────────────────────────────────────
function fmtBulkSummary(label, r) {
  const parts = [];
  if (r.closed) parts.push(`closed: ${r.closed.length}`);
  if (r.already_closed?.length) parts.push(`already closed: ${r.already_closed.length}`);
  if (r.deleted) parts.push(`deleted: ${r.deleted.length}`);
  if (r.skipped_open?.length) parts.push(`skipped open: ${r.skipped_open.length}`);
  if (r.killed != null) parts.push(`killed: ${r.killed}`);
  if (r.skipped_dead != null) parts.push(`dead skipped: ${r.skipped_dead}`);
  if (r.skipped_owner != null) parts.push(`owners spared: ${r.skipped_owner}`);
  if (r.errors?.length) parts.push(`errors: ${r.errors.length}`);
  return `${label}\n${parts.join(' · ')}`;
}

async function bulkAction(endpoint, confirmMsg, label) {
  if (!confirm(confirmMsg)) return;
  closeAgentStreams();
  const resp = await fetch(endpoint, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
  const data = await resp.json().catch(() => ({error: 'invalid response'}));
  if (!resp.ok) {
    alert(`${label} failed: ${data.error || resp.status}`);
    return;
  }
  if (currentRoom) {
    currentRoom = null;
    const chat = document.getElementById('chat-area');
    chat.innerHTML = '';
    chat.appendChild(el('div', {class: 'empty'}, [
      el('div', {class: 'empty-title', text: label}),
      el('div', {class: 'empty-hint', text: 'Pick another room from the sidebar'}),
    ]));
  }
  await loadRooms();
  alert(fmtBulkSummary(label, data));
}

async function bulkCloseAll() {
  await bulkAction(
    '/api/rooms_close_all',
    'Закрыть ВСЕ открытые комнаты?\n\nЖивые spawned агенты (кроме owner-ов) получат SIGTERM. Owner PIDs не трогаются. Мёртвые PIDs пропускаются.',
    'Bulk close',
  );
}

async function bulkDeleteClosed() {
  await bulkAction(
    '/api/rooms_delete_closed',
    'Удалить с диска ВСЕ закрытые комнаты?\n\nИстория, логи агентов, метаданные потеряны навсегда. Открытые комнаты не трогаются.',
    'Bulk delete',
  );
}

async function bulkNuke() {
  await bulkAction(
    '/api/rooms_nuke',
    '🔥 NUKE ALL ROOMS?\n\n1. Kill живых spawned PIDs (кроме owner-ов всех комнат).\n2. Закрыть все open комнаты.\n3. Wipe всех комнат с диска.\n\nOwner PIDs не трогаются. Действие необратимо.',
    'Nuke all',
  );
}

// ── Event delegation ──────────────────────────────────────
document.addEventListener('click', e => {
  const item = e.target.closest('.room-item');
  if (item) { openRoom(item.dataset.id, item.dataset.owner); return; }
  if (e.target.closest('#bulk-close-all'))      { bulkCloseAll(); return; }
  if (e.target.closest('#bulk-delete-closed'))  { bulkDeleteClosed(); return; }
  if (e.target.closest('#bulk-nuke'))           { bulkNuke(); return; }
});

// ── Polling ───────────────────────────────────────────────
async function tick() {
  await loadRooms();
  if (currentRoom) await fetchMessages(false);
}

// ── Activity panel resize ─────────────────────────────────
const ACTIVITY_MIN = 220, ACTIVITY_MAX_FRAC = 0.6;

function applyActivityWidth(px) {
  const minPx = ACTIVITY_MIN;
  const maxPx = Math.max(minPx + 40, Math.floor(window.innerWidth * ACTIVITY_MAX_FRAC));
  const clamped = Math.max(minPx, Math.min(maxPx, px | 0));
  document.documentElement.style.setProperty('--activity-w', clamped + 'px');
  return clamped;
}

function initActivityResizer() {
  const saved = parseInt(localStorage.getItem('agentbus-activity-w') || '380', 10);
  applyActivityWidth(isFinite(saved) ? saved : 380);

  const resizer = document.getElementById('activity-resizer');
  if (!resizer) return;
  let dragging = false;

  resizer.addEventListener('pointerdown', e => {
    dragging = true;
    resizer.classList.add('dragging');
    resizer.setPointerCapture?.(e.pointerId);
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
  });
  resizer.addEventListener('pointermove', e => {
    if (!dragging) return;
    const w = window.innerWidth - e.clientX - 14; // 14px = .app padding right
    applyActivityWidth(w);
  });
  const stop = e => {
    if (!dragging) return;
    dragging = false;
    resizer.classList.remove('dragging');
    try { resizer.releasePointerCapture?.(e.pointerId); } catch(_){}
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
    const cur = getComputedStyle(document.documentElement).getPropertyValue('--activity-w').trim();
    if (cur) localStorage.setItem('agentbus-activity-w', parseInt(cur, 10));
  };
  resizer.addEventListener('pointerup', stop);
  resizer.addEventListener('pointercancel', stop);
}

initTheme();
initActivityResizer();
loadRooms();
setInterval(tick, 3000);
