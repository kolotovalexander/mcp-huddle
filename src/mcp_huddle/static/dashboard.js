// ── Helpers ───────────────────────────────────────────────
// Active roster: Antigravity, Codex, Claude, MiMo, DeepSeek, Qwen.
// Gemini is legacy (CLI EOL 2026-06-18) — kept only so old room history renders.
const AGENT_CLS = {
  Claude:'agent-claude', Codex:'agent-codex', Antigravity:'agent-antigravity',
  Qwen:'agent-qwen', MiMo:'agent-mimo', DeepSeek:'agent-deepseek',
  Gemini:'agent-gemini',
  Human:'agent-human',   System:'agent-system',
};
const AVATAR_CLS = {
  Claude:'avatar-claude', Codex:'avatar-codex', Antigravity:'avatar-antigravity',
  Qwen:'avatar-qwen', MiMo:'avatar-mimo', DeepSeek:'avatar-deepseek',
  Gemini:'avatar-gemini',
  Human:'avatar-human',   System:'avatar-system',
};
const AGENT_LETTER = {Claude:'C', Codex:'X', Antigravity:'A', Qwen:'Q', MiMo:'M',
  DeepSeek:'D', Gemini:'G', Human:'H', System:'S'};

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

// ── Markdown rendering (Telegram-style, dependency-free & XSS-safe) ──────────
// Agents post Markdown (## headers, **bold**, `code`, ```fences```, - lists).
// We escape ALL message text first, then re-introduce a fixed whitelist of
// tags — message content can never inject HTML.
function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function renderMarkdown(src) {
  src = String(src == null ? '' : src);

  // 1. Pull out fenced code blocks + inline code so their content is never
  //    treated as Markdown. Placeholders are wrapped in a Private-Use-Area
  //    char (U+E000) — it never appears in real chat text.
  const blocks = [];
  src = src.replace(/```[^\n`]*\n?([\s\S]*?)```/g, (_, code) => {
    blocks.push(code.replace(/\n+$/, ''));
    return 'CB' + (blocks.length - 1) + '';
  });
  const inlines = [];
  src = src.replace(/`([^`\n]+)`/g, (_, code) => {
    inlines.push(code);
    return 'IC' + (inlines.length - 1) + '';
  });

  const restoreInline = t => t
    .replace(/IC(\d+)/g, (_, i) => `<code>${escapeHtml(inlines[+i])}</code>`)
    .replace(/CB(\d+)/g, (_, i) => `<pre class="md-pre"><code>${escapeHtml(blocks[+i])}</code></pre>`);

  function inline(text) {
    let t = escapeHtml(text);
    // links [label](url) — only http(s)/mailto survive, else neutralised
    t = t.replace(/\[([^\]\n]+)\]\(([^)\s]+)\)/g, (_, label, url) => {
      const safe = /^(https?:\/\/|mailto:)/i.test(url) ? url : '#';
      return `<a href="${safe}" target="_blank" rel="noopener noreferrer">${label}</a>`;
    });
    t = t.replace(/\*\*([^\n]+?)\*\*/g, '<strong>$1</strong>');
    t = t.replace(/__([^\n]+?)__/g, '<strong>$1</strong>');
    t = t.replace(/~~([^\n]+?)~~/g, '<del>$1</del>');
    t = t.replace(/(^|[^*\w])\*([^*\n]+?)\*(?!\*)/g, '$1<em>$2</em>');
    t = t.replace(/(^|[^_\w])_([^_\n]+?)_(?![_\w])/g, '$1<em>$2</em>');
    return restoreInline(t);
  }

  const lines = src.split('\n');
  let html = '', listType = null;
  const closeList = () => { if (listType) { html += `</${listType}>`; listType = null; } };

  for (const line of lines) {
    const cb = line.match(/^\s*CB(\d+)\s*$/);
    if (cb) { closeList(); html += restoreInline('CB' + cb[1] + ''); continue; }
    if (/^\s*$/.test(line)) { closeList(); continue; }

    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) { closeList(); html += `<div class="md-h md-h${h[1].length}">${inline(h[2])}</div>`; continue; }
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { closeList(); html += '<hr class="md-hr">'; continue; }

    const q = line.match(/^\s*>\s?(.*)$/);
    if (q) { closeList(); html += `<blockquote class="md-quote">${inline(q[1])}</blockquote>`; continue; }

    const ol = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (ol) {
      if (listType !== 'ol') { closeList(); html += '<ol class="md-list">'; listType = 'ol'; }
      html += `<li>${inline(ol[1])}</li>`; continue;
    }
    const ul = line.match(/^\s*[-*+]\s+(.*)$/);
    if (ul) {
      if (listType !== 'ul') { closeList(); html += '<ul class="md-list">'; listType = 'ul'; }
      html += `<li>${inline(ul[1])}</li>`; continue;
    }

    closeList();
    html += `<div class="md-line">${inline(line)}</div>`;
  }
  closeList();
  return html;
}

function fmtTime(ts) {
  return new Date((ts || 0) * 1000).toLocaleTimeString('ru', {hour:'2-digit', minute:'2-digit'});
}

// ── State ─────────────────────────────────────────────────
let currentRoom = null, currentOwner = null, lastId = 0;
let rooms = [], msgMap = {};
let agentMetaTotals = {}; // {agentName: {tokens_total, tokens_in, tokens_out, msgs, models:Set, last_reasoning}}
let lastStatuses = {};    // {agentName: 'online'|'busy'|...} — latest room status snapshot

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

// ── Appearance settings: Theme × Skin × Palette in one popover ──
// Three orthogonal axes, each persisted in localStorage and reflected on
// <html> as data-theme / data-skin / data-palette. The ⚙️ topbar button
// opens a popover with a segmented control per axis (no more cycle buttons).
const THEME_OPTS = [
  {v: 'auto', label: '🌓 Auto'}, {v: 'dark', label: '🌙 Dark'}, {v: 'light', label: '☀️ Light'},
];
const SKIN_OPTS = [
  {v: 'glass', label: '🪟 Glass'}, {v: 'web', label: '💬 Web'}, {v: 'code', label: '⌨️ Code'},
];
const PALETTE_OPTS = [
  {v: 'default', label: 'Default'}, {v: 'dracula', label: 'Dracula'}, {v: 'nord', label: 'Nord'},
  {v: 'tokyonight', label: 'Tokyo Night'}, {v: 'catppuccin', label: 'Catppuccin'}, {v: 'gruvbox', label: 'Gruvbox'},
];

function applyTheme(mode) {
  if (!['auto', 'dark', 'light'].includes(mode)) mode = 'auto';
  const resolved = mode === 'auto'
    ? (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')
    : mode;
  document.documentElement.setAttribute('data-theme', resolved);
  document.documentElement.setAttribute('data-theme-mode', mode);
}
function applySkin(skin) {
  if (!SKIN_OPTS.some(o => o.v === skin)) skin = 'glass';
  document.documentElement.setAttribute('data-skin', skin);
}
function applyPalette(pal) {
  if (!PALETTE_OPTS.some(o => o.v === pal)) pal = 'default';
  document.documentElement.setAttribute('data-palette', pal);
}

function buildSettingsRow(title, opts, storeKey, getCur, apply) {
  const seg = el('div', {class: 'set-seg'});
  const refresh = () => {
    const cur = getCur();
    [...seg.children].forEach(b => b.classList.toggle('active', b.dataset.v === cur));
  };
  opts.forEach(o => {
    const b = el('button', {class: 'set-opt', dataset: {v: o.v}, text: o.label});
    b.onclick = () => { localStorage.setItem(storeKey, o.v); apply(o.v); refresh(); };
    seg.appendChild(b);
  });
  refresh();
  return el('div', {class: 'set-row'}, [el('div', {class: 'set-label', text: title}), seg]);
}

function initSettings() {
  // Apply saved values (head script already set them pre-paint; re-assert).
  applyTheme(localStorage.getItem('agentbus-theme') || 'auto');
  applySkin(localStorage.getItem('agentbus-skin') || 'glass');
  applyPalette(localStorage.getItem('agentbus-palette') || 'default');

  // Re-apply on OS theme change while in 'auto' mode.
  const mq = window.matchMedia('(prefers-color-scheme: dark)');
  const onSys = () => { if ((localStorage.getItem('agentbus-theme') || 'auto') === 'auto') applyTheme('auto'); };
  if (mq.addEventListener) mq.addEventListener('change', onSys);
  else if (mq.addListener) mq.addListener(onSys);

  const pop = document.getElementById('settings-popover');
  const btn = document.getElementById('settings-btn');
  if (!pop || !btn) return;
  pop.appendChild(el('div', {class: 'set-title', text: 'Оформление'}));
  pop.appendChild(buildSettingsRow('Тема', THEME_OPTS, 'agentbus-theme',
    () => localStorage.getItem('agentbus-theme') || 'auto', applyTheme));
  pop.appendChild(buildSettingsRow('Дизайн', SKIN_OPTS, 'agentbus-skin',
    () => localStorage.getItem('agentbus-skin') || 'glass', applySkin));
  pop.appendChild(buildSettingsRow('Палитра', PALETTE_OPTS, 'agentbus-palette',
    () => localStorage.getItem('agentbus-palette') || 'default', applyPalette));

  const close = () => { pop.hidden = true; btn.setAttribute('aria-expanded', 'false'); };
  const open  = () => { pop.hidden = false; btn.setAttribute('aria-expanded', 'true'); };
  btn.onclick = (e) => { e.stopPropagation(); pop.hidden ? open() : close(); };
  document.addEventListener('click', (e) => {
    if (!pop.hidden && !pop.contains(e.target) && e.target !== btn) close();
  });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
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

// Collapsible tree state (which project/session groups are folded).
let treeCollapsed = {};
try { treeCollapsed = JSON.parse(localStorage.getItem('agentbus-tree-collapsed') || '{}') || {}; } catch (_) {}
function treeFolded(key) { return !!treeCollapsed[key]; }
function toggleTree(key) {
  if (treeCollapsed[key]) delete treeCollapsed[key];
  else treeCollapsed[key] = true;
  localStorage.setItem('agentbus-tree-collapsed', JSON.stringify(treeCollapsed));
  renderRooms();
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
    const pKey = 'proj:' + proj;
    const pFolded = treeFolded(pKey);
    const block = el('div', {class: 'proj-block' + (pFolded ? ' collapsed' : '')});
    const total = [...sessions.values()].reduce((sum, rs) => sum + rs.length, 0);
    const ph = el('div', {class: 'proj-header'}, [
      el('span', {class: 'tree-arrow', text: '▾'}),
      el('span', {class: 'tree-label', text: proj}),
      el('span', {class: 'count', text: String(total)}),
    ]);
    ph.onclick = () => toggleTree(pKey);
    block.appendChild(ph);

    const projBody = el('div', {class: 'proj-body'});
    for (const [sess, rs] of sessions) {
      const sKey = 'sess:' + proj + '/' + sess;
      const sFolded = treeFolded(sKey);
      const sessLabel = sess.length > 14 ? sess.slice(0, 14) + '…' : sess;
      const group = el('div', {class: 'sess-group' + (sFolded ? ' collapsed' : '')});
      const sh = el('div', {class: 'sess-header'}, [
        el('span', {class: 'tree-arrow', text: '▾'}),
        el('span', {class: 'tree-label', text: sessLabel}),
        el('span', {class: 'count', text: String(rs.length)}),
      ]);
      sh.onclick = () => toggleTree(sKey);
      group.appendChild(sh);

      const sessRooms = el('div', {class: 'sess-rooms'});
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
        sessRooms.appendChild(item);
      }
      group.appendChild(sessRooms);
      projBody.appendChild(group);
    }
    block.appendChild(projBody);
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
    // A bare <input type="text"> with no name makes Safari/Chrome offer
    // contact autofill (phone number etc.). Opt out explicitly: it is a
    // free-text chat field, not a contact form.
    name: 'huddle-message',
    autocomplete: 'off',
    autocorrect: 'off',
    autocapitalize: 'sentences',
    spellcheck: 'false',
    'data-1p-ignore': '',
    'data-lpignore': 'true',
    placeholder: isReadOnly
      ? (isClosed ? 'Room closed — read-only' : 'Room resolved — read-only')
      : 'Сообщение от имени Human — system-приоритет, обходит anti-loop…',
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
  await attachAgentPanels(id);  // Phase 1: live agent event stream (Codex / runner agents)
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
  const {agents, health} = await resp.json();
  const spawned = agents || {};
  const healthMap = health || {};
  const panel = resetActivityPanel(null);
  if (!panel) return;

  // Show EVERY participant — not just huddle-spawned ones. The room owner
  // (Claude) has no spawned process / event log, but the user still wants to
  // see that it is in the room and its online/busy status.
  const room = rooms.find(x => x.id === roomId) || {};
  const seen = new Set();
  const participants = [];
  for (const p of (room.participants || [])) { if (!seen.has(p)) { seen.add(p); participants.push(p); } }
  for (const p of Object.keys(spawned)) { if (!seen.has(p)) { seen.add(p); participants.push(p); } }

  if (!participants.length) {
    resetActivityPanel('В этой комнате нет участников');
    return;
  }

  const wrap = el('div', {class: 'agent-panels', id: 'agent-panels'});
  wrap.appendChild(el('div', {class: 'agent-panels-header', text: 'Agent activity (live)'}));
  const scroll = el('div', {class: 'agent-panels-scroll'});
  wrap.appendChild(scroll);

  for (const name of participants) {
    const isSpawned = !!spawned[name];
    const healthSpan = el('span', {class: 'agent-panel-health', id: `agent-health-${name}`});

    const summary = el('summary', {class: 'agent-panel-summary'}, [
      avatar(name, 'avatar-sm'),
      el('span', {class: 'agent-panel-name', text: name}),
      el('span', {class: 'agent-status-dot offline', id: `agent-sdot-${name}`,
                  title: `${name}: offline`}),
      el('span', {class: 'agent-panel-totals', id: `agent-totals-${name}`, text: ''}),
      el('span', {class: 'agent-panel-status', id: `agent-status-${name}`,
                  text: isSpawned ? '·' : 'no live stream'}),
      healthSpan,
    ]);

    const body = isSpawned
      ? el('div', {class: 'agent-events', id: `agent-events-${name}`})
      : el('div', {class: 'agent-panel-hint', text: name === room.owner
          ? 'Оркестратор комнаты. Его реплики видны в чате слева — huddle не spawn-ит owner-а, поэтому отдельного live-лога событий у него нет.'
          : 'Участник без spawned-процесса: live-потока событий нет, только статус.'});

    const detailsEl = el('details', {
      class: 'agent-panel' + (isSpawned ? '' : ' static'),
      dataset: {agent: name}, open: '',
    }, [summary, body]);
    scroll.appendChild(detailsEl);

    if (isSpawned) {
      const hLabel = activityHealthLabel(healthMap[name]);
      if (hLabel) { healthSpan.textContent = hLabel; healthSpan.classList.add('warn'); }
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
  }

  panel.appendChild(wrap);
  updateActivityStatuses(lastStatuses);
}

// Wake-health label for an agent panel (from /api/room_agents `health`).
function activityHealthLabel(h) {
  if (!h) return '';
  if (h.stale_lease) return '⚠ stale lease';
  if (h.last_wake_failed) return `✗ wake failed (rc ${h.last_wake_rc})`;
  if (h.wake_fail_count > 0) return `⚠ ${h.wake_fail_count} wake fail(s)`;
  return '';
}

// Refresh the online/busy dot on each agent panel from a status snapshot.
function updateActivityStatuses(statuses) {
  statuses = statuses || {};
  document.querySelectorAll('[id^="agent-sdot-"]').forEach(dot => {
    const name = dot.id.slice('agent-sdot-'.length);
    const st = statuses[name] || 'offline';
    const cls = st === 'busy' ? 'busy' : st === 'online' ? 'online' : 'offline';
    dot.className = 'agent-status-dot ' + cls;
    dot.title = `${name}: ${st}`;
  });
}

function appendAgentEvent(agentName, raw) {
  const list = document.getElementById(`agent-events-${agentName}`);
  if (!list) return;
  let summary = raw;
  let detail = null;
  try {
    const obj = JSON.parse(raw);
    // Codex --json events: {type, agent_message?, delta?, ...}
    // Runner events (MiMo / DeepSeek / Qwen via *_runner): {type, error?,
    //   reason?, model?, message_id?, ...}. Antigravity (agy -p) is plain text.
    if (obj.type) {
      summary = obj.type;
      // Lines word-wrap in the panel now, so we can afford a fuller preview.
      if (obj.agent_message) summary += ': ' + String(obj.agent_message).slice(0, 400);
      else if (obj.delta) summary += ': ' + String(obj.delta).slice(0, 400);
      else if (obj.content) summary += ': ' + String(obj.content).slice(0, 400);
      else if (obj.error) summary += ': ' + String(obj.error).slice(0, 400);
      else if (obj.reason) summary += ': ' + String(obj.reason).slice(0, 400);
      else if (obj.model) summary += ': ' + String(obj.model).slice(0, 120);
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
  const bodyEl = el('div', {class: 'msg-body md'});
  bodyEl.innerHTML = renderMarkdown(m.body);
  bubble.appendChild(bodyEl);

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
    lastStatuses = data.statuses || {};
    updateActivityStatuses(lastStatuses);

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

// ── Layout manager: resizable + collapsible panels + responsive ──
// Both side panels resize via their handles and collapse (button or
// double-click handle). On every resize the effective widths are
// recomputed so the chat column never drops below CHAT_MIN — when the
// window is too narrow the activity panel auto-hides first, then the
// sidebar, keeping the dashboard usable at any width.
const SIDEBAR_MIN = 170, SIDEBAR_MAX = 460, SIDEBAR_DEFAULT = 248;
const ACTIVITY_MIN = 240, ACTIVITY_DEFAULT = 380;
const CHAT_MIN = 320;
// Below this viewport width the side panels stop taking layout space and
// become overlay drawers that slide over the chat (chat = full width).
const OVERLAY_BREAKPOINT = 820;
// .app padding (14*2) + 4 grid gaps (12*4) between the 5 tracks.
const LAYOUT_GUTTER = 14 * 2 + 12 * 4;

const layout = {
  sidebarW: parseInt(localStorage.getItem('agentbus-sidebar-w') || SIDEBAR_DEFAULT, 10) || SIDEBAR_DEFAULT,
  activityW: parseInt(localStorage.getItem('agentbus-activity-w') || ACTIVITY_DEFAULT, 10) || ACTIVITY_DEFAULT,
  sidebarCollapsed: localStorage.getItem('agentbus-sidebar-collapsed') === '1',
  activityCollapsed: localStorage.getItem('agentbus-activity-collapsed') === '1',
  drawer: null, // overlay mode only: null | 'sidebar' | 'activity'
};

const clampN = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const isOverlay = () => window.innerWidth < OVERLAY_BREAKPOINT;

function relayout() {
  const main = document.querySelector('.main');
  if (!main) return;
  const backdrop = document.getElementById('drawer-backdrop');
  const sBtn = document.getElementById('sidebar-collapse');
  const aBtn = document.getElementById('activity-collapse');

  // Anchor the settings popover + drawers just under the (possibly wrapped)
  // topbar, and flag narrow mode so the topbar can shed rare controls.
  const tb = document.querySelector('.topbar');
  if (tb) document.documentElement.style.setProperty(
    '--drawer-top', Math.round(tb.getBoundingClientRect().bottom + 8) + 'px');
  document.documentElement.toggleAttribute('data-narrow', isOverlay());

  if (isOverlay()) {
    // ── Overlay/drawer mode: chat is full-width, panels float over it ──
    main.classList.add('overlay-mode');
    main.classList.remove('sidebar-off', 'activity-off');
    const sOpen = layout.drawer === 'sidebar';
    const aOpen = layout.drawer === 'activity';
    main.classList.toggle('drawer-sidebar-open', sOpen);
    main.classList.toggle('drawer-activity-open', aOpen);
    document.documentElement.style.setProperty(
      '--drawer-w', Math.min(360, Math.round(window.innerWidth * 0.86)) + 'px');
    if (backdrop) backdrop.hidden = !(sOpen || aOpen);
    if (sBtn) sBtn.classList.toggle('active', sOpen);
    if (aBtn) aBtn.classList.toggle('active', aOpen);
    return;
  }

  // ── Wide mode: resizable side-by-side panels, chat protected ──
  main.classList.remove('overlay-mode', 'drawer-sidebar-open', 'drawer-activity-open');
  layout.drawer = null;
  if (backdrop) backdrop.hidden = true;

  const avail = window.innerWidth - LAYOUT_GUTTER;
  const actMax = Math.floor(window.innerWidth * 0.6);
  let sw = layout.sidebarCollapsed ? 0 : clampN(layout.sidebarW, SIDEBAR_MIN, SIDEBAR_MAX);
  let aw = layout.activityCollapsed ? 0 : clampN(layout.activityW, ACTIVITY_MIN, actMax);

  // Protect the chat: shrink/hide activity first, then the sidebar.
  if (avail - sw - aw < CHAT_MIN && aw > 0) {
    aw = avail - sw - CHAT_MIN;
    if (aw < ACTIVITY_MIN) aw = 0;
  }
  if (avail - sw - aw < CHAT_MIN && sw > 0) {
    sw = avail - aw - CHAT_MIN;
    if (sw < SIDEBAR_MIN) sw = 0;
  }

  const sOff = layout.sidebarCollapsed || sw === 0;
  const aOff = layout.activityCollapsed || aw === 0;
  main.classList.toggle('sidebar-off', sOff);
  main.classList.toggle('activity-off', aOff);
  if (!sOff) document.documentElement.style.setProperty('--sidebar-w', Math.round(sw) + 'px');
  if (!aOff) document.documentElement.style.setProperty('--activity-w', Math.round(aw) + 'px');

  if (sBtn) sBtn.classList.toggle('active', layout.sidebarCollapsed);
  if (aBtn) aBtn.classList.toggle('active', layout.activityCollapsed);
}

function persistLayout() {
  localStorage.setItem('agentbus-sidebar-w', layout.sidebarW);
  localStorage.setItem('agentbus-activity-w', layout.activityW);
  localStorage.setItem('agentbus-sidebar-collapsed', layout.sidebarCollapsed ? '1' : '0');
  localStorage.setItem('agentbus-activity-collapsed', layout.activityCollapsed ? '1' : '0');
}

function initPanelResizer(resizerId, side) {
  const resizer = document.getElementById(resizerId);
  if (!resizer) return;
  let dragging = false;

  resizer.addEventListener('pointerdown', e => {
    if (isOverlay()) return; // no resizing in drawer mode
    e.preventDefault();
    dragging = true;
    resizer.classList.add('dragging');
    document.body.classList.add('resizing'); // global user-select:none (no text selection while dragging)
    resizer.setPointerCapture?.(e.pointerId);
  });
  resizer.addEventListener('pointermove', e => {
    if (!dragging) return;
    if (side === 'left') {
      layout.sidebarW = clampN(e.clientX - 14, SIDEBAR_MIN, SIDEBAR_MAX);
      layout.sidebarCollapsed = false;
    } else {
      layout.activityW = clampN(window.innerWidth - e.clientX - 14, ACTIVITY_MIN, Math.floor(window.innerWidth * 0.6));
      layout.activityCollapsed = false;
    }
    relayout();
  });
  const stop = e => {
    if (!dragging) return;
    dragging = false;
    resizer.classList.remove('dragging');
    document.body.classList.remove('resizing');
    try { resizer.releasePointerCapture?.(e.pointerId); } catch (_) {}
    persistLayout();
  };
  resizer.addEventListener('pointerup', stop);
  resizer.addEventListener('pointercancel', stop);
  // Double-click the handle to collapse / restore that panel.
  resizer.addEventListener('dblclick', () => {
    if (side === 'left') layout.sidebarCollapsed = !layout.sidebarCollapsed;
    else layout.activityCollapsed = !layout.activityCollapsed;
    persistLayout();
    relayout();
  });
}

// Topbar ◧/◨ buttons: collapse panels in wide mode, toggle drawers in overlay.
function togglePanel(which) {
  if (isOverlay()) {
    layout.drawer = layout.drawer === which ? null : which;
  } else if (which === 'sidebar') {
    layout.sidebarCollapsed = !layout.sidebarCollapsed;
    persistLayout();
  } else {
    layout.activityCollapsed = !layout.activityCollapsed;
    persistLayout();
  }
  relayout();
}

function initLayout() {
  initPanelResizer('sidebar-resizer', 'left');
  initPanelResizer('activity-resizer', 'right');

  const sBtn = document.getElementById('sidebar-collapse');
  if (sBtn) sBtn.onclick = () => togglePanel('sidebar');
  const aBtn = document.getElementById('activity-collapse');
  if (aBtn) aBtn.onclick = () => togglePanel('activity');

  const backdrop = document.getElementById('drawer-backdrop');
  if (backdrop) backdrop.onclick = () => { layout.drawer = null; relayout(); };

  let raf = 0;
  window.addEventListener('resize', () => {
    if (raf) return;
    raf = requestAnimationFrame(() => { raf = 0; relayout(); });
  });
  relayout();
}

initSettings();
initLayout();
loadRooms();
setInterval(tick, 3000);
