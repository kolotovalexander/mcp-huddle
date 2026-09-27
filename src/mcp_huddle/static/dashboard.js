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
// Identity colour as a CSS value: the skin's --c-<agent> for known agents, a
// stable hashed --c-dyn-N slot for dynamic names (e.g. OpenCode-nvidia).
const AGENT_DYN_SLOTS = 6;
function agentColor(a) {
  const cls = AGENT_CLS[a];
  if (cls) return `var(--c-${cls.slice('agent-'.length)})`;
  let h = 0;
  for (const ch of String(a || '')) h = (h * 31 + ch.codePointAt(0)) >>> 0;
  return `var(--c-dyn-${h % AGENT_DYN_SLOTS}, var(--text-muted))`;
}
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

// ── Same-origin HTTP + dashboard authentication ──────────
// The raw token is submitted once to /api/auth and is never placed in a URL,
// cookie, log, or browser storage. Only the derived process-local credential is
// retained in this page's JavaScript memory and attached to same-origin fetches.
class HuddleHTTPError extends Error {
  constructor(status, message) {
    super(message || `HTTP ${status}`);
    this.name = 'HuddleHTTPError';
    this.status = status;
  }
}

let authPromise = null;
let dashboardCredential = null;
let authNeedsUserAction = false;

async function responseError(resp) {
  const data = await resp.json().catch(() => ({}));
  return new HuddleHTTPError(resp.status, data.error || `${resp.status} ${resp.statusText}`);
}

function hideAuthRequired() {
  const banner = document.getElementById('auth-required');
  if (banner) banner.remove();
}

function showDashboardNotice(message, buttonText, retryAction) {
  let banner = document.getElementById('auth-required');
  if (!banner) {
    banner = el('div', {id: 'auth-required', class: 'lq-panel'});
    Object.assign(banner.style, {
      position: 'fixed', zIndex: '10000', top: '12px', left: '50%',
      transform: 'translateX(-50%)', padding: '12px 16px', display: 'flex',
      alignItems: 'center', gap: '12px', maxWidth: 'min(680px, calc(100vw - 24px))',
      boxShadow: '0 8px 30px rgba(0,0,0,.35)',
    });
    const label = el('span', {id: 'auth-required-message'});
    const retry = el('button', {id: 'auth-required-retry', class: 'lq-btn'});
    banner.append(label, retry);
    document.body.appendChild(banner);
  }
  const label = document.getElementById('auth-required-message');
  if (label) label.textContent = message;
  const retry = document.getElementById('auth-required-retry');
  if (retry) {
    retry.textContent = buttonText;
    retry.onclick = retryAction;
  }
}

function showAuthRequired(message = 'Authentication is required.') {
  showDashboardNotice(message, 'Enter token', async () => {
    authNeedsUserAction = false;
    try {
      await authenticateDashboard(true);
      await loadRooms();
      if (currentRoom) {
        await fetchMessages(false);
        closeAgentStreams();
        await attachAgentPanels(currentRoom);
      }
    } catch(e) {
      showAuthRequired(e && e.message ? e.message : 'Authentication failed.');
    }
  });
}

function showLoadError(error) {
  const detail = error && error.message ? error.message : 'request failed';
  showDashboardNotice(`Could not load rooms: ${detail}`, 'Retry', async () => {
    hideAuthRequired();
    await loadRooms();
  });
}

async function authenticateDashboard(forcePrompt = false) {
  if (authPromise) return authPromise;
  authPromise = (async () => {
    const stateResp = await fetch('/api/auth', {
      credentials: 'omit', cache: 'no-store', headers: {'Accept': 'application/json'},
    });
    if (!stateResp.ok) throw await responseError(stateResp);
    const state = await stateResp.json();
    if (!state.required) {
      dashboardCredential = null;
      authNeedsUserAction = false;
      hideAuthRequired();
      return;
    }
    if (dashboardCredential && !forcePrompt) return;
    if (authNeedsUserAction && !forcePrompt) {
      throw new HuddleHTTPError(401, 'Authentication required — use Enter token.');
    }
    let tokenInput = window.prompt('MCP_HUDDLE_TOKEN is required for this dashboard:');
    if (tokenInput === null) {
      authNeedsUserAction = true;
      showAuthRequired('Authentication cancelled. The dashboard has not loaded protected data.');
      throw new HuddleHTTPError(401, 'Authentication cancelled — use Enter token to retry.');
    }
    try {
      const authResp = await fetch('/api/auth', {
        method: 'POST',
        credentials: 'omit',
        cache: 'no-store',
        headers: {'Accept': 'application/json', 'Content-Type': 'application/json'},
        body: JSON.stringify({token: tokenInput}),
      });
      if (!authResp.ok) {
        authNeedsUserAction = true;
        const error = await responseError(authResp);
        showAuthRequired('Token rejected. Protected data was not loaded.');
        throw error;
      }
      const data = await authResp.json();
      if (typeof data.credential !== 'string' || !data.credential || data.credential.length > 256) {
        authNeedsUserAction = true;
        showAuthRequired('Authentication returned an invalid credential.');
        throw new HuddleHTTPError(500, 'Invalid authentication response');
      }
      dashboardCredential = data.credential;
      authNeedsUserAction = false;
      hideAuthRequired();
    } finally {
      tokenInput = '';
    }
  })();
  try {
    return await authPromise;
  } finally {
    authPromise = null;
  }
}

async function apiFetch(url, options = {}, retryAuth = true) {
  const requestOptions = {...options, credentials: 'omit'};
  requestOptions.headers = new Headers(options.headers || {});
  if (!requestOptions.headers.has('Accept')) requestOptions.headers.set('Accept', 'application/json');
  if (dashboardCredential) requestOptions.headers.set('X-Huddle-Credential', dashboardCredential);
  let resp = await fetch(url, requestOptions);
  if (resp.status === 401 && retryAuth) {
    dashboardCredential = null;
    await authenticateDashboard(false);
    if (dashboardCredential) requestOptions.headers.set('X-Huddle-Credential', dashboardCredential);
    else requestOptions.headers.delete('X-Huddle-Credential');
    resp = await fetch(url, requestOptions);
  }
  if (!resp.ok) throw await responseError(resp);
  return resp;
}

function showRequestError(action, error) {
  const detail = error && error.message ? error.message : 'request failed';
  alert(`${action}: ${detail}`);
}

// ── i18n: UI-chrome localisation across 10 languages ───────────────
// Only the static chrome is translated (labels, buttons, hints, tooltips);
// room names / agent messages are live data and stay as authored. Keys are
// looked up for the current LANG, then fall back to English, then the key.
const I18N = {
  en: {
    'app.subtitle': 'rooms · multi-agent discussion',
    'btn.closeAll': 'Close all', 'btn.deleteClosed': 'Delete closed', 'btn.nukeAll': 'Nuke all',
    'btn.view': 'View', 'btn.live': 'live', 'btn.copy': 'Copy', 'btn.send': 'Send',
    'btn.search': 'Search',
    'room.owner': 'Owner', 'room.round': 'Round', 'room.noRound': 'No recorded round', 'room.messages': 'messages',
    'room.created': 'Created', 'room.updated': 'Updated',
    'room.copyName': 'Copy name', 'room.copyId': 'Copy room ID',
    'room.copied': 'Copied', 'room.copyFailed': 'Copy failed',
    'swarm.title': 'Team brief', 'swarm.goal': 'Goal', 'swarm.mode': 'Mode',
    'swarm.phase': 'Phase', 'swarm.round': 'Round', 'swarm.responsibilities': 'Responsibilities',
    'swarm.tasks': 'Tasks', 'swarm.decisions': 'Decisions', 'swarm.facts': 'Facts',
    'swarm.final': 'Final result', 'swarm.working': 'In progress', 'swarm.completed': 'Completed',
    'swarm.empty': 'Nothing recorded yet', 'swarm.owner': 'Owner', 'swarm.reporter': 'Reporter',
    'swarm.members': 'Members', 'swarm.memberDone': 'done', 'swarm.memberActive': 'working',
    'swarm.memberWaiting': 'waiting', 'swarm.noRole': 'no role yet',
    'swarm.childAgents': 'Child agents', 'swarm.childQuota': 'Limit',
    'swarm.childProfiles': 'Allowed profiles', 'swarm.childParent': 'parent',
    'swarm.childRunning': 'running', 'swarm.childStarting': 'starting',
    'swarm.childExited': 'finished', 'swarm.childFailed': 'failed', 'swarm.childUnknown': 'status unavailable',
    'swarm.childOpenRoom': 'Open child room', 'swarm.childHistoryNone': 'Context: task only',
    'swarm.childHistoryRecent': 'Context: last 10 room messages',
    'swarm.childContextNote': 'Context selection does not restrict file or tool access.',
    'swarm.childRelaySent': 'Result sent to parent room', 'swarm.childRelayFailed': 'Result could not be sent',
    'swarm.childRelayOff': 'Result forwarding is off', 'swarm.childRelayPending': 'Result forwarding pending',
    'lanes.title': 'Agents', 'lanes.collapse': 'Collapse', 'lanes.expand': 'Expand',
    'lanes.waiting': 'Waiting', 'lanes.working': 'Working', 'lanes.done': 'Finished',
    'lanes.offline': 'Offline', 'lanes.online': 'Online', 'lanes.unknown': 'No status',
    'lanes.thinking': 'Thinking', 'lanes.responding': 'Writing an answer',
    'lanes.starting': 'Starting', 'lanes.queued': 'Queued',
    'lanes.limited': 'Rate limited', 'lanes.stuck': 'Stalled',
    'lanes.messages': 'messages', 'round.discussion': 'Discussion', 'round.label': 'Round',
    'lanes.legend': '□ request  ■ answer  • comment',
    'lanes.now': 'now', 'lanes.nowFar': 'now — no messages since', 'lanes.noPosts': 'has not posted yet',
    'lanes.pending': 'request', 'lanes.unanswered': 'no reply to', 'lanes.from': 'from',
    'lanes.last': 'last', 'lanes.until': 'until', 'lanes.active': 'active',
    'kind.request': 'request', 'kind.comment': 'comment', 'kind.ack': 'acknowledged',
    'kind.busy': 'busy', 'kind.result': 'answer', 'kind.final': 'final',
    'kind.system': 'system', 'kind.close': 'closed', 'kind.other': 'message',
    'compose.as': 'From Human', 'compose.request': 'Request', 'compose.comment': 'Comment',
    'compose.system': 'Important', 'compose.to': 'To', 'compose.all': 'Everyone',
    'compose.hint': 'A request wakes its recipients. A comment does not.',
    'compose.reply': 'Reply', 'compose.replyTo': 'Replying to {agent} · #{id}', 'compose.cancelReply': 'Cancel reply',
    'compose.placeholder': 'Write to the room… Ctrl+Enter to send',
    'footer.noRoom': 'Choose a room to view the discussion', 'footer.search': 'search',
    'footer.theme': 'Theme', 'footer.text': 'Text', 'footer.rows': 'Rows',
    'footer.paper': 'paper', 'footer.ink': 'ink',
    'footer.spacious': 'Spacious', 'footer.dense': 'Compact',
    'search.placeholder': 'Search room titles and messages',
    'search.hint': 'Type a room name or words from a conversation',
    'search.pending': 'Searching…', 'search.empty': 'No matching rooms or messages',
    'chat.selectRoom': 'Select a room',
    'chat.selectHint': 'Use room_create() from an agent to start a discussion',
    'chat.closed': 'Room closed — read-only',
    'chat.resolved': 'Room resolved — read-only',
    'chat.placeholder': 'Message as Human — system priority, bypasses anti-loop…',
    'chat.closedTitle': 'Room closed',
    'chat.pickAnother': 'Pick another room from the sidebar',
    'activity.title': 'Agent activity',
    'activity.hint': 'Opens when you select a room with spawned agents',
    'activity.liveTitle': 'Agent activity · live',
    'activity.noStream': 'no live stream',
    'activity.pending': 'waiting for events',
    'activity.detail': 'Technical details',
    'activity.started': 'Started', 'activity.completed': 'Completed',
    'activity.failed': 'Failed', 'activity.error': 'Error',
    'activity.cancelled': 'Stopped', 'activity.retrying': 'Retrying',
    'activity.liveStatus': 'Live', 'activity.resetStatus': 'Stream reset',
    'activity.errorStatus': 'Stream error', 'activity.authStatus': 'Authentication required',
    'activity.answer': 'Answer', 'activity.fragment': 'Answer fragment',
    'activity.step': 'Step completed', 'activity.tool': 'Tool call',
    'activity.output': 'Process output', 'activity.generic': 'Agent event',
    'activity.transcript': 'Room conversation', 'activity.noParticipants': 'No participants in this room',
    'activity.loadAgentsFailed': 'Could not load participants',
    'activity.ownerHint': 'Room organizer. Their messages appear in the room conversation.',
    'activity.staticHint': 'Participant has no live event stream.',
    'activity.emptyLog': 'Empty log entry',
    'activity.agentStarted': 'Agent started', 'activity.roomWork': 'Working with room',
    'activity.limitReached': 'Provider limit reached',
    'activity.setupIssue': 'Agent setup needs attention',
    'activity.unownedLease': 'Agent state is unclear',
    'activity.staleLease': 'Agent stopped responding',
    'activity.wakeFailed': 'Agent could not resume',
    'activity.wakeFailures': 'Resume failures',
    'status.open': 'Open', 'status.idle': 'Idle',
    'status.busy': 'Working', 'status.online': 'Online',
    'status.offline': 'Offline', 'status.closed': 'Closed',
    'status.resolved': 'Resolved', 'status.closing': 'Closing',
    'status.closing_requested': 'Closing requested',
    'activity.internal': 'Internal step',
    'roomMode.council': 'Council', 'roomMode.relay': 'Relay',
    'roomMode.team': 'Team', 'roomMode.swarm': 'Swarm',
    'roomMode.ordinary': 'Ordinary room',
    'roomMode.title': 'Room mode',
    'sidebar.empty': 'No rooms yet. Call room_create() from an agent.',
    'set.appearance': 'Appearance', 'set.theme': 'Theme', 'set.skin': 'Design',
    'set.palette': 'Palette', 'set.lang': 'Language', 'set.mcp': 'MCP connection',
    'set.roomView': 'Room list', 'roomView.latest': 'Latest activity', 'roomView.projects': 'By project',
    'theme.auto': 'Auto', 'theme.dark': 'Dark', 'theme.light': 'Light',
    'mcp.endpoint': 'HTTP endpoint', 'mcp.claude': 'Claude Code', 'mcp.codex': 'Codex (config.toml)',
    'mcp.stdio': 'stdio (any client)',
    'mcp.hint': 'Dashboard and MCP share one port. Attach agents over HTTP, or run the mcp-huddle binary as a stdio server.',
    'tip.closeAll': 'Close every open room (kills live spawned agent processes; owners are left alone).',
    'tip.deleteClosed': 'Permanently delete all closed rooms from disk.',
    'tip.nukeAll': 'Close AND delete every room. Owner processes are not touched.',
    'tip.view': 'Appearance (theme, design, palette, language) and MCP connection info.',
    'tip.theme': 'Light/dark mode. Auto follows your OS setting.',
    'tip.skin': 'Overall look: Glass (frosted), Web (flat), or Code (IDE).',
    'tip.palette': 'Colour scheme — popular terminal palettes (Dracula, Nord, …).',
    'tip.lang': 'Interface language.',
    'tip.collapse': 'Collapse this panel. Drag the edge to resize; double-click the edge to collapse.',
    'tip.restoreSidebar': 'Show the rooms list', 'tip.restoreActivity': 'Show the activity panel',
    'set.spawn': 'Agents & spawn (env)', 'set.agentPrompt': 'Agent setup prompt',
    'tip.spawn': 'Environment variables that control the server and which agents spawn. Click a name to copy.',
    'tip.agentPrompt': 'Paste this to an AI agent so it can connect to and use huddle.',
    'var.readonly': 'Read-only agents are the DEFAULT (read files/web/docs, edit nothing, talk only via MCP). Set =0 for full-access worker agents.',
    'var.registryFile': 'Drop-in JSON to add/override agents (merged with defaults).',
    'var.registryEnv': 'Path to a registry JSON (highest precedence, overrides the file).',
    'var.claude': 'Enable the Claude spawn slot (off by default; metered).',
    'var.antigravity': 'Enable the Antigravity (agy) slot (off by default; needs a prior interactive `agy` login; not read-only-enforced).',
    'var.mimo': 'Disable the MiMo spawn slot (on by default; runs in a temp dir, never touches the project).',
    'var.token': 'If set, protect MCP plus all room data/actions. The dashboard keeps only a derived credential in page memory.',
    'var.home': 'Data directory (rooms, logs). Default ~/.mcp-huddle.',
    'var.port': 'HTTP port for the dashboard/MCP (default 8014).',
    'agentPrompt.text': 'Connect to the huddle MCP server at {origin}/mcp — e.g. run: claude mcp add --transport http huddle {origin}/mcp\nThen use: room_list (see rooms), room_create (start one), messages_read (catch up), message_post (reply). Reuse an existing room or create a new one.\nOnly answer kind=request addressed to you (to=YourName or to=all); never reply to comment/ack/result/final (anti-loop).',
  },
  ru: {
    'app.subtitle': 'комнат · мультиагентное обсуждение',
    'btn.closeAll': 'Закрыть все', 'btn.deleteClosed': 'Удалить закрытые', 'btn.nukeAll': 'Снести всё',
    'btn.view': 'Вид', 'btn.live': 'онлайн', 'btn.copy': 'Копировать', 'btn.send': 'Отправить',
    'btn.search': 'Поиск', 'search.placeholder': 'Название комнаты или слова из переписки',
    'room.owner': 'Владелец', 'room.round': 'Раунд', 'room.noRound': 'Раунд не задан', 'room.messages': 'сообщений',
    'room.created': 'Создана', 'room.updated': 'Обновлена',
    'room.copyName': 'Копировать имя', 'room.copyId': 'Копировать ID комнаты',
    'room.copied': 'Скопировано', 'room.copyFailed': 'Не удалось скопировать',
    'swarm.title': 'План команды', 'swarm.goal': 'Цель', 'swarm.mode': 'Режим',
    'swarm.phase': 'Этап', 'swarm.round': 'Раунд', 'swarm.responsibilities': 'Обязанности',
    'swarm.tasks': 'Задачи', 'swarm.decisions': 'Решения', 'swarm.facts': 'Факты',
    'swarm.final': 'Итог', 'swarm.working': 'В работе', 'swarm.completed': 'Завершён',
    'swarm.empty': 'Пока ничего не записано', 'swarm.owner': 'Ответственный', 'swarm.reporter': 'Сводит результат',
    'swarm.members': 'Участники', 'swarm.memberDone': 'готово', 'swarm.memberActive': 'в работе',
    'swarm.memberWaiting': 'ждёт', 'swarm.noRole': 'роли нет',
    'swarm.childAgents': 'Дочерние агенты', 'swarm.childQuota': 'Лимит',
    'swarm.childProfiles': 'Разрешённые профили', 'swarm.childParent': 'родитель',
    'swarm.childRunning': 'работает', 'swarm.childStarting': 'запускается',
    'swarm.childExited': 'завершил', 'swarm.childFailed': 'ошибка', 'swarm.childUnknown': 'статус неизвестен',
    'swarm.childOpenRoom': 'Открыть дочернюю комнату', 'swarm.childHistoryNone': 'Контекст: только задание',
    'swarm.childHistoryRecent': 'Контекст: последние 10 сообщений комнаты',
    'swarm.childContextNote': 'Выбор контекста не ограничивает доступ к файлам и инструментам.',
    'swarm.childRelaySent': 'Результат передан в родительскую комнату', 'swarm.childRelayFailed': 'Не удалось передать результат',
    'swarm.childRelayOff': 'Передача результата выключена', 'swarm.childRelayPending': 'Передача результата ожидает завершения',
    'lanes.title': 'Агенты', 'lanes.collapse': 'Свернуть', 'lanes.expand': 'Развернуть',
    'lanes.waiting': 'Ждёт', 'lanes.working': 'Работает', 'lanes.done': 'Закончил',
    'lanes.offline': 'Не в сети', 'lanes.online': 'На связи', 'lanes.unknown': 'Нет статуса',
    'lanes.thinking': 'Думает', 'lanes.responding': 'Пишет ответ',
    'lanes.starting': 'Запускается', 'lanes.queued': 'В очереди',
    'lanes.limited': 'Уперся в лимит', 'lanes.stuck': 'Завис',
    'lanes.messages': 'сообщений', 'round.discussion': 'Обсуждение', 'round.label': 'Раунд',
    'lanes.legend': '□ запрос  ■ ответ  • реплика',
    'lanes.now': 'сейчас', 'lanes.nowFar': 'сейчас — сообщений не было с', 'lanes.noPosts': 'ещё не писал',
    'lanes.pending': 'запрос', 'lanes.unanswered': 'без ответа на', 'lanes.from': 'от',
    'lanes.last': 'последнее', 'lanes.until': 'до', 'lanes.active': 'в работе',
    'kind.request': 'запрос', 'kind.comment': 'комментарий', 'kind.ack': 'принял',
    'kind.busy': 'занят', 'kind.result': 'ответ', 'kind.final': 'итог',
    'kind.system': 'системное', 'kind.close': 'закрытие', 'kind.other': 'сообщение',
    'compose.as': 'От имени Human', 'compose.request': 'Запрос', 'compose.comment': 'Комментарий',
    'compose.system': 'Важное', 'compose.to': 'Кому', 'compose.all': 'Всем',
    'compose.hint': 'Запрос разбудит адресатов. Комментарий — нет.',
    'compose.reply': 'Ответить', 'compose.replyTo': 'Ответ на сообщение {agent} · №{id}', 'compose.cancelReply': 'Отменить ответ',
    'compose.placeholder': 'Написать в комнату… Ctrl+Enter — отправить',
    'footer.noRoom': 'Выберите комнату, чтобы читать обсуждение', 'footer.search': 'поиск',
    'footer.theme': 'Тема', 'footer.text': 'Текст', 'footer.rows': 'Строки',
    'footer.paper': 'бумага', 'footer.ink': 'чернила',
    'footer.spacious': 'Просторно', 'footer.dense': 'Плотно',
    'search.hint': 'Введите название комнаты или слова из переписки',
    'search.pending': 'Ищу…', 'search.empty': 'Совпадений нет',
    'chat.selectRoom': 'Выберите комнату',
    'chat.selectHint': 'Вызовите room_create() из агента, чтобы начать обсуждение',
    'chat.closed': 'Комната закрыта — только чтение',
    'chat.resolved': 'Комната решена — только чтение',
    'chat.placeholder': 'Сообщение от имени Human — system-приоритет, обходит anti-loop…',
    'chat.closedTitle': 'Комната закрыта',
    'chat.pickAnother': 'Выберите другую комнату слева',
    'activity.title': 'Активность агентов',
    'activity.hint': 'Откроется при выборе комнаты со spawned-агентами',
    'activity.liveTitle': 'Активность агентов · онлайн',
    'activity.noStream': 'нет live-потока',
    'activity.pending': 'ждём события',
    'activity.detail': 'Технические подробности',
    'activity.started': 'Запущено', 'activity.completed': 'Завершено',
    'activity.failed': 'Не выполнено', 'activity.error': 'Ошибка',
    'activity.cancelled': 'Остановлено', 'activity.retrying': 'Повторная попытка',
    'activity.liveStatus': 'Онлайн', 'activity.resetStatus': 'Поток сброшен',
    'activity.errorStatus': 'Ошибка потока', 'activity.authStatus': 'Нужна авторизация',
    'activity.answer': 'Ответ', 'activity.fragment': 'Фрагмент ответа',
    'activity.step': 'Шаг завершён', 'activity.tool': 'Вызов инструмента',
    'activity.output': 'Вывод процесса', 'activity.generic': 'Событие агента',
    'activity.transcript': 'Переписка комнаты', 'activity.noParticipants': 'В комнате нет участников',
    'activity.loadAgentsFailed': 'Не удалось загрузить участников',
    'activity.ownerHint': 'Организатор комнаты. Его сообщения видны в переписке комнаты.',
    'activity.staticHint': 'У участника нет live-потока событий.',
    'activity.emptyLog': 'Пустая запись лога',
    'activity.agentStarted': 'Агент запущен', 'activity.roomWork': 'Работа с комнатой',
    'activity.limitReached': 'Достигнут лимит провайдера',
    'activity.setupIssue': 'Проблема с настройкой агента',
    'activity.unownedLease': 'Состояние агента неясно',
    'activity.staleLease': 'Агент перестал отвечать',
    'activity.wakeFailed': 'Не удалось возобновить агента',
    'activity.wakeFailures': 'Ошибок возобновления',
    'status.open': 'Открыта', 'status.idle': 'Без активности',
    'status.busy': 'Работает', 'status.online': 'Онлайн',
    'status.offline': 'Не в сети', 'status.closed': 'Закрыта',
    'status.resolved': 'Решена', 'status.closing': 'Закрывается',
    'status.closing_requested': 'Запрошено закрытие',
    'activity.internal': 'Внутренний шаг',
    'roomMode.council': 'Совет', 'roomMode.relay': 'Эстафета',
    'roomMode.team': 'Команда', 'roomMode.swarm': 'Рой',
    'roomMode.ordinary': 'Обычная комната',
    'roomMode.title': 'Режим комнаты',
    'sidebar.empty': 'Пока нет комнат. Вызовите room_create() из агента.',
    'set.appearance': 'Оформление', 'set.theme': 'Тема', 'set.skin': 'Дизайн',
    'set.palette': 'Палитра', 'set.lang': 'Язык', 'set.mcp': 'MCP-подключение',
    'set.roomView': 'Список комнат', 'roomView.latest': 'По последней активности', 'roomView.projects': 'По проектам',
    'theme.auto': 'Авто', 'theme.dark': 'Тёмная', 'theme.light': 'Светлая',
    'mcp.endpoint': 'HTTP endpoint', 'mcp.claude': 'Claude Code', 'mcp.codex': 'Codex (config.toml)',
    'mcp.stdio': 'stdio (любой клиент)',
    'mcp.hint': 'Дашборд и MCP на одном порту. Подключайте агентов по HTTP, либо запустите бинарь mcp-huddle как stdio-сервер.',
    'tip.closeAll': 'Закрыть все открытые комнаты (убивает живые процессы агентов; owner-ов не трогает).',
    'tip.deleteClosed': 'Навсегда удалить с диска все закрытые комнаты.',
    'tip.nukeAll': 'Закрыть И удалить все комнаты. Процессы owner-ов не трогаются.',
    'tip.view': 'Оформление (тема, дизайн, палитра, язык) и данные MCP-подключения.',
    'tip.theme': 'Светлый/тёмный режим. «Авто» следует за настройкой ОС.',
    'tip.skin': 'Общий вид: Glass (стекло), Web (плоский) или Code (IDE).',
    'tip.palette': 'Цветовая схема — популярные терминальные палитры (Dracula, Nord, …).',
    'tip.lang': 'Язык интерфейса.',
    'tip.collapse': 'Свернуть панель. Тяните за край для изменения ширины; двойной клик по краю — свернуть.',
    'tip.restoreSidebar': 'Показать список комнат', 'tip.restoreActivity': 'Показать панель активности',
    'set.spawn': 'Агенты и спавн (env)', 'set.agentPrompt': 'Промпт настройки агента',
    'tip.spawn': 'Переменные окружения, управляющие сервером и тем, какие агенты спавнятся. Клик по имени — скопировать.',
    'tip.agentPrompt': 'Вставьте это AI-агенту, чтобы он подключился к huddle и начал им пользоваться.',
    'var.readonly': 'Read-only агенты — ПО УМОЛЧАНИЮ (читают файлы/веб/доки, ничего не редактируют, общаются только через MCP). =0 — полнодоступные агенты-работники.',
    'var.registryFile': 'Drop-in JSON для добавления/переопределения агентов (мержится с дефолтами).',
    'var.registryEnv': 'Путь к registry JSON (высший приоритет, перекрывает файл).',
    'var.claude': 'Включить слот Claude (по умолчанию выкл.; тарифицируется).',
    'var.antigravity': 'Включить слот Antigravity (agy) (по умолчанию выкл.; нужен предварительный интерактивный вход `agy`; read-only не гарантируется).',
    'var.mimo': 'Выключить слот MiMo (по умолчанию вкл.; работает в temp-папке, проект не трогает).',
    'var.token': 'Если задан — защищает MCP, чтение комнат и все действия. Дашборд хранит только производный credential в памяти страницы.',
    'var.home': 'Каталог данных (комнаты, логи). По умолчанию ~/.mcp-huddle.',
    'var.port': 'HTTP-порт дашборда/MCP (по умолчанию 8014).',
    'agentPrompt.text': 'Подключись к huddle MCP по адресу {origin}/mcp — например: claude mcp add --transport http huddle {origin}/mcp\nДалее: room_list (список комнат), room_create (создать), messages_read (прочитать), message_post (ответить). Переиспользуй существующую комнату или создай новую.\nОтвечай только на kind=request, адресованные тебе (to=ТвоёИмя или to=all); никогда не отвечай на comment/ack/result/final (анти-луп).',
  },
  es: {"app.subtitle": "salas · debate multiagente", "btn.closeAll": "Cerrar todo", "btn.deleteClosed": "Eliminar cerradas", "btn.nukeAll": "Borrar todo", "btn.view": "Ver", "btn.live": "en vivo", "btn.copy": "Copiar", "btn.send": "Enviar", "chat.selectRoom": "Selecciona una sala", "chat.selectHint": "Usa room_create() desde un agente para iniciar un debate", "chat.closed": "Sala cerrada — solo lectura", "chat.resolved": "Sala resuelta — solo lectura", "chat.placeholder": "Mensaje como Human — prioridad de sistema, omite anti-loop…", "chat.closedTitle": "Sala cerrada", "chat.pickAnother": "Elige otra sala en la barra lateral", "activity.title": "Actividad de agentes", "activity.hint": "Se abre al seleccionar una sala con agentes generados", "sidebar.empty": "Aún no hay salas. Llama a room_create() desde un agente.", "set.appearance": "Apariencia", "set.theme": "Tema", "set.skin": "Diseño", "set.palette": "Paleta", "set.lang": "Idioma", "set.mcp": "Conexión MCP", "theme.auto": "Auto", "theme.dark": "Oscuro", "theme.light": "Claro", "mcp.endpoint": "HTTP endpoint", "mcp.claude": "Claude Code", "mcp.codex": "Codex (config.toml)", "mcp.stdio": "stdio (cualquier cliente)", "mcp.hint": "El panel y MCP comparten un mismo puerto. Conecta agentes por HTTP, o ejecuta el binario mcp-huddle como servidor stdio.", "tip.closeAll": "Cierra todas las salas abiertas (detiene los procesos de agentes generados en vivo; los owners no se tocan).", "tip.deleteClosed": "Elimina permanentemente del disco todas las salas cerradas.", "tip.nukeAll": "Cierra Y elimina todas las salas. Los procesos owner no se tocan.", "tip.view": "Apariencia (tema, diseño, paleta, idioma) e info de conexión MCP.", "tip.theme": "Modo claro/oscuro. Auto sigue la configuración de tu OS.", "tip.skin": "Aspecto general: Glass (esmerilado), Web (plano) o Code (IDE).", "tip.palette": "Esquema de color — paletas de terminal populares (Dracula, Nord, …).", "tip.lang": "Idioma de la interfaz.", "tip.collapse": "Contrae este panel. Arrastra el borde para redimensionar; haz doble clic en el borde para contraer.", "tip.restoreSidebar": "Mostrar la lista de salas", "tip.restoreActivity": "Mostrar el panel de actividad"},
  de: {"app.subtitle": "Räume · Multi-Agenten-Diskussion", "btn.closeAll": "Alle schließen", "btn.deleteClosed": "Geschlossene löschen", "btn.nukeAll": "Alles löschen", "btn.view": "Ansicht", "btn.live": "live", "btn.copy": "Kopieren", "btn.send": "Senden", "chat.selectRoom": "Raum auswählen", "chat.selectHint": "Nutze room_create() aus einem Agenten, um eine Diskussion zu starten", "chat.closed": "Raum geschlossen — schreibgeschützt", "chat.resolved": "Raum aufgelöst — schreibgeschützt", "chat.placeholder": "Nachricht als Human — Systempriorität, umgeht anti-loop…", "chat.closedTitle": "Raum geschlossen", "chat.pickAnother": "Wähle einen anderen Raum aus der Seitenleiste", "activity.title": "Agenten-Aktivität", "activity.hint": "Öffnet sich, wenn du einen Raum mit gestarteten Agenten auswählst", "sidebar.empty": "Noch keine Räume. Rufe room_create() aus einem Agenten auf.", "set.appearance": "Darstellung", "set.theme": "Theme", "set.skin": "Design", "set.palette": "Palette", "set.lang": "Sprache", "set.mcp": "MCP-Verbindung", "theme.auto": "Auto", "theme.dark": "Dunkel", "theme.light": "Hell", "mcp.endpoint": "HTTP endpoint", "mcp.claude": "Claude Code", "mcp.codex": "Codex (config.toml)", "mcp.stdio": "stdio (beliebiger Client)", "mcp.hint": "Dashboard und MCP teilen sich einen Port. Verbinde Agenten über HTTP oder starte das mcp-huddle-Binary als stdio-Server.", "tip.closeAll": "Jeden offenen Raum schließen (beendet laufende, gestartete Agentenprozesse; Owner bleiben unberührt).", "tip.deleteClosed": "Alle geschlossenen Räume dauerhaft von der Festplatte löschen.", "tip.nukeAll": "Jeden Raum schließen UND löschen. Owner-Prozesse bleiben unberührt.", "tip.view": "Darstellung (Theme, Design, Palette, Sprache) und Infos zur MCP-Verbindung.", "tip.theme": "Heller/dunkler Modus. Auto folgt deiner OS-Einstellung.", "tip.skin": "Gesamtlook: Glass (matt), Web (flach) oder Code (IDE).", "tip.palette": "Farbschema — beliebte Terminal-Paletten (Dracula, Nord, …).", "tip.lang": "Sprache der Oberfläche.", "tip.collapse": "Dieses Panel einklappen. Ziehe am Rand zum Anpassen; Doppelklick auf den Rand zum Einklappen.", "tip.restoreSidebar": "Raumliste anzeigen", "tip.restoreActivity": "Aktivitäts-Panel anzeigen"},
  fr: {"app.subtitle": "salons · discussion multi-agents", "btn.closeAll": "Tout fermer", "btn.deleteClosed": "Supprimer les fermés", "btn.nukeAll": "Tout effacer", "btn.view": "Affichage", "btn.live": "en direct", "btn.copy": "Copier", "btn.send": "Envoyer", "chat.selectRoom": "Sélectionnez un salon", "chat.selectHint": "Utilisez room_create() depuis un agent pour lancer une discussion", "chat.closed": "Salon fermé — lecture seule", "chat.resolved": "Salon résolu — lecture seule", "chat.placeholder": "Écrire en tant que Humain — priorité système, contourne l'anti-loop…", "chat.closedTitle": "Salon fermé", "chat.pickAnother": "Choisissez un autre salon dans la barre latérale", "activity.title": "Activité des agents", "activity.hint": "S'ouvre quand vous sélectionnez un salon avec des agents lancés", "sidebar.empty": "Aucun salon pour l'instant. Appelez room_create() depuis un agent.", "set.appearance": "Apparence", "set.theme": "Thème", "set.skin": "Design", "set.palette": "Palette", "set.lang": "Langue", "set.mcp": "Connexion MCP", "theme.auto": "Auto", "theme.dark": "Sombre", "theme.light": "Clair", "mcp.endpoint": "HTTP endpoint", "mcp.claude": "Claude Code", "mcp.codex": "Codex (config.toml)", "mcp.stdio": "stdio (tout client)", "mcp.hint": "Le tableau de bord et MCP partagent un seul port. Connectez les agents en HTTP, ou lancez le binaire mcp-huddle comme serveur stdio.", "tip.closeAll": "Fermer tous les salons ouverts (arrête les processus d'agents lancés ; les propriétaires ne sont pas touchés).", "tip.deleteClosed": "Supprimer définitivement du disque tous les salons fermés.", "tip.nukeAll": "Fermer ET supprimer tous les salons. Les processus propriétaires ne sont pas touchés.", "tip.view": "Apparence (thème, design, palette, langue) et infos de connexion MCP.", "tip.theme": "Mode clair/sombre. Auto suit le réglage de votre OS.", "tip.skin": "Aspect général : Glass (givré), Web (plat) ou Code (IDE).", "tip.palette": "Jeu de couleurs — palettes de terminal populaires (Dracula, Nord, …).", "tip.lang": "Langue de l'interface.", "tip.collapse": "Réduire ce panneau. Faites glisser le bord pour redimensionner ; double-cliquez le bord pour réduire.", "tip.restoreSidebar": "Afficher la liste des salons", "tip.restoreActivity": "Afficher le panneau d'activité"},
  pt: {"app.subtitle": "salas · discussão multiagente", "btn.closeAll": "Fechar todas", "btn.deleteClosed": "Excluir fechadas", "btn.nukeAll": "Apagar tudo", "btn.view": "Exibir", "btn.live": "ao vivo", "btn.copy": "Copiar", "btn.send": "Enviar", "chat.selectRoom": "Selecione uma sala", "chat.selectHint": "Use room_create() de um agente para iniciar uma discussão", "chat.closed": "Sala fechada — somente leitura", "chat.resolved": "Sala resolvida — somente leitura", "chat.placeholder": "Mensagem como Humano — prioridade do sistema, ignora o anti-loop…", "chat.closedTitle": "Sala fechada", "chat.pickAnother": "Escolha outra sala na barra lateral", "activity.title": "Atividade dos agentes", "activity.hint": "Abre ao selecionar uma sala com agentes iniciados", "sidebar.empty": "Nenhuma sala ainda. Chame room_create() de um agente.", "set.appearance": "Aparência", "set.theme": "Tema", "set.skin": "Design", "set.palette": "Paleta", "set.lang": "Idioma", "set.mcp": "Conexão MCP", "theme.auto": "Automático", "theme.dark": "Escuro", "theme.light": "Claro", "mcp.endpoint": "HTTP endpoint", "mcp.claude": "Claude Code", "mcp.codex": "Codex (config.toml)", "mcp.stdio": "stdio (qualquer cliente)", "mcp.hint": "O dashboard e o MCP compartilham uma porta. Conecte agentes via HTTP ou execute o binário mcp-huddle como servidor stdio.", "tip.closeAll": "Fecha todas as salas abertas (encerra os processos de agentes iniciados; os owners não são afetados).", "tip.deleteClosed": "Exclui permanentemente do disco todas as salas fechadas.", "tip.nukeAll": "Fecha E exclui todas as salas. Os processos owner não são tocados.", "tip.view": "Aparência (tema, design, paleta, idioma) e informações de conexão MCP.", "tip.theme": "Modo claro/escuro. Automático segue a configuração do seu OS.", "tip.skin": "Aparência geral: Glass (fosco), Web (plano) ou Code (IDE).", "tip.palette": "Esquema de cores — paletas populares de terminal (Dracula, Nord, …).", "tip.lang": "Idioma da interface.", "tip.collapse": "Recolhe este painel. Arraste a borda para redimensionar; clique duas vezes na borda para recolher.", "tip.restoreSidebar": "Mostrar a lista de salas", "tip.restoreActivity": "Mostrar o painel de atividade"},
  zh: {"app.subtitle": "房间 · 多智能体讨论", "btn.closeAll": "全部关闭", "btn.deleteClosed": "删除已关闭", "btn.nukeAll": "全部清除", "btn.view": "查看", "btn.live": "实时", "btn.copy": "复制", "btn.send": "发送", "chat.selectRoom": "选择一个房间", "chat.selectHint": "从智能体调用 room_create() 即可开始讨论", "chat.closed": "房间已关闭 — 只读", "chat.resolved": "房间已结案 — 只读", "chat.placeholder": "以 Human 身份发言 — 系统优先级，绕过 anti-loop…", "chat.closedTitle": "房间已关闭", "chat.pickAnother": "从侧边栏挑选另一个房间", "activity.title": "智能体活动", "activity.hint": "选择含已生成智能体的房间时打开", "sidebar.empty": "暂无房间。从智能体调用 room_create()。", "set.appearance": "外观", "set.theme": "主题", "set.skin": "设计", "set.palette": "配色", "set.lang": "语言", "set.mcp": "MCP 连接", "theme.auto": "自动", "theme.dark": "深色", "theme.light": "浅色", "mcp.endpoint": "HTTP endpoint", "mcp.claude": "Claude Code", "mcp.codex": "Codex (config.toml)", "mcp.stdio": "stdio（任意客户端）", "mcp.hint": "仪表盘和 MCP 共用一个端口。通过 HTTP 接入智能体，或将 mcp-huddle 可执行文件作为 stdio 服务器运行。", "tip.closeAll": "关闭所有打开的房间（结束实时生成的智能体进程；不影响 owner）。", "tip.deleteClosed": "从磁盘永久删除所有已关闭的房间。", "tip.nukeAll": "关闭并删除每个房间。不影响 owner 进程。", "tip.view": "外观（主题、设计、配色、语言）及 MCP 连接信息。", "tip.theme": "浅色/深色模式。自动会跟随你的 OS 设置。", "tip.skin": "整体外观：Glass（毛玻璃）、Web（扁平）或 Code（IDE）。", "tip.palette": "配色方案 — 流行的终端配色（Dracula、Nord…）。", "tip.lang": "界面语言。", "tip.collapse": "折叠此面板。拖动边缘可调整大小；双击边缘可折叠。", "tip.restoreSidebar": "显示房间列表", "tip.restoreActivity": "显示活动面板"},
  ja: {"app.subtitle": "ルーム · マルチエージェント討議", "btn.closeAll": "すべて閉じる", "btn.deleteClosed": "閉じたルームを削除", "btn.nukeAll": "全削除", "btn.view": "表示", "btn.live": "ライブ", "btn.copy": "コピー", "btn.send": "送信", "chat.selectRoom": "ルームを選択", "chat.selectHint": "エージェントから room_create() を呼び出して討議を開始", "chat.closed": "ルームは閉じています — 読み取り専用", "chat.resolved": "ルームは解決済み — 読み取り専用", "chat.placeholder": "Human として送信 — システム優先、anti-loop を回避…", "chat.closedTitle": "ルームは閉じています", "chat.pickAnother": "サイドバーから別のルームを選択", "activity.title": "エージェントの活動", "activity.hint": "起動済みエージェントのあるルームを選択すると開きます", "sidebar.empty": "ルームがまだありません。エージェントから room_create() を呼び出してください。", "set.appearance": "外観", "set.theme": "テーマ", "set.skin": "デザイン", "set.palette": "パレット", "set.lang": "言語", "set.mcp": "MCP 接続", "theme.auto": "自動", "theme.dark": "ダーク", "theme.light": "ライト", "mcp.endpoint": "HTTP endpoint", "mcp.claude": "Claude Code", "mcp.codex": "Codex (config.toml)", "mcp.stdio": "stdio（任意のクライアント）", "mcp.hint": "ダッシュボードと MCP は同じポートを共有します。HTTP でエージェントを接続するか、mcp-huddle バイナリを stdio サーバーとして実行してください。", "tip.closeAll": "開いているすべてのルームを閉じる（起動中のエージェントプロセスを終了。オーナーには触れません）。", "tip.deleteClosed": "閉じたすべてのルームをディスクから完全に削除します。", "tip.nukeAll": "すべてのルームを閉じて削除します。オーナープロセスには触れません。", "tip.view": "外観（テーマ、デザイン、パレット、言語）と MCP 接続情報。", "tip.theme": "ライト/ダークモード。自動は OS の設定に従います。", "tip.skin": "全体の見た目：Glass（すりガラス）、Web（フラット）、Code（IDE）。", "tip.palette": "配色 — 人気のターミナルパレット（Dracula、Nord、…）。", "tip.lang": "インターフェースの言語。", "tip.collapse": "このパネルを折りたたみます。端をドラッグでサイズ変更、端をダブルクリックで折りたたみ。", "tip.restoreSidebar": "ルーム一覧を表示", "tip.restoreActivity": "活動パネルを表示"},
  ar: {"app.subtitle": "غرف · نقاش متعدد الوكلاء", "btn.closeAll": "إغلاق الكل", "btn.deleteClosed": "حذف المغلقة", "btn.nukeAll": "مسح الكل", "btn.view": "عرض", "btn.live": "مباشر", "btn.copy": "نسخ", "btn.send": "إرسال", "chat.selectRoom": "اختر غرفة", "chat.selectHint": "استخدم room_create() من وكيل لبدء نقاش", "chat.closed": "الغرفة مغلقة — للقراءة فقط", "chat.resolved": "الغرفة محسومة — للقراءة فقط", "chat.placeholder": "راسل كإنسان — أولوية النظام، يتجاوز anti-loop…", "chat.closedTitle": "الغرفة مغلقة", "chat.pickAnother": "اختر غرفة أخرى من الشريط الجانبي", "activity.title": "نشاط الوكلاء", "activity.hint": "يُفتح عند اختيار غرفة بها وكلاء مُشغَّلون", "sidebar.empty": "لا توجد غرف بعد. استدعِ room_create() من وكيل.", "set.appearance": "المظهر", "set.theme": "السمة", "set.skin": "التصميم", "set.palette": "لوحة الألوان", "set.lang": "اللغة", "set.mcp": "اتصال MCP", "theme.auto": "تلقائي", "theme.dark": "داكن", "theme.light": "فاتح", "mcp.endpoint": "HTTP endpoint", "mcp.claude": "Claude Code", "mcp.codex": "Codex (config.toml)", "mcp.stdio": "stdio (أي عميل)", "mcp.hint": "تشترك لوحة التحكم وMCP في منفذ واحد. اربط الوكلاء عبر HTTP، أو شغّل ثنائي mcp-huddle كخادم stdio.", "tip.closeAll": "إغلاق كل غرفة مفتوحة (يُنهي عمليات الوكلاء المُشغَّلة مباشرةً؛ ولا يمسّ المالكين).", "tip.deleteClosed": "حذف جميع الغرف المغلقة نهائيًا من القرص.", "tip.nukeAll": "إغلاق وحذف كل غرفة. لا تُمسّ عمليات المالكين.", "tip.view": "المظهر (السمة، التصميم، لوحة الألوان، اللغة) ومعلومات اتصال MCP.", "tip.theme": "الوضع الفاتح/الداكن. يتبع الوضع التلقائي إعداد OS لديك.", "tip.skin": "المظهر العام: Glass (زجاجي)، أو Web (مسطّح)، أو Code (IDE).", "tip.palette": "نظام الألوان — لوحات طرفية شائعة (Dracula، Nord، …).", "tip.lang": "لغة الواجهة.", "tip.collapse": "طيّ هذه اللوحة. اسحب الحافة لتغيير الحجم؛ انقر مزدوجًا على الحافة للطيّ.", "tip.restoreSidebar": "إظهار قائمة الغرف", "tip.restoreActivity": "إظهار لوحة النشاط"},
  hi: {"app.subtitle": "rooms · multi-agent चर्चा", "btn.closeAll": "सभी बंद करें", "btn.deleteClosed": "बंद हटाएँ", "btn.nukeAll": "सब मिटाएँ", "btn.view": "व्यू", "btn.live": "लाइव", "btn.copy": "कॉपी", "btn.send": "भेजें", "chat.selectRoom": "एक room चुनें", "chat.selectHint": "चर्चा शुरू करने के लिए किसी agent से room_create() चलाएँ", "chat.closed": "Room बंद — केवल पढ़ने योग्य", "chat.resolved": "Room हल हुआ — केवल पढ़ने योग्य", "chat.placeholder": "Human के रूप में संदेश — सिस्टम प्राथमिकता, anti-loop को बायपास करता है…", "chat.closedTitle": "Room बंद", "chat.pickAnother": "साइडबार से कोई दूसरा room चुनें", "activity.title": "Agent गतिविधि", "activity.hint": "जब आप spawn किए गए agents वाला room चुनते हैं तब खुलता है", "sidebar.empty": "अभी कोई room नहीं। किसी agent से room_create() चलाएँ।", "set.appearance": "रूप-रंग", "set.theme": "थीम", "set.skin": "डिज़ाइन", "set.palette": "पैलेट", "set.lang": "भाषा", "set.mcp": "MCP कनेक्शन", "theme.auto": "स्वचालित", "theme.dark": "डार्क", "theme.light": "लाइट", "mcp.endpoint": "HTTP endpoint", "mcp.claude": "Claude Code", "mcp.codex": "Codex (config.toml)", "mcp.stdio": "stdio (कोई भी क्लाइंट)", "mcp.hint": "Dashboard और MCP एक ही पोर्ट साझा करते हैं। Agents को HTTP पर जोड़ें, या mcp-huddle बाइनरी को stdio सर्वर के रूप में चलाएँ।", "tip.closeAll": "हर खुले room को बंद करें (लाइव spawn किए गए agent प्रोसेस बंद होते हैं; owners को नहीं छेड़ा जाता)।", "tip.deleteClosed": "सभी बंद rooms को डिस्क से स्थायी रूप से हटाएँ।", "tip.nukeAll": "हर room को बंद करें और हटाएँ। Owner प्रोसेस अछूते रहते हैं।", "tip.view": "रूप-रंग (थीम, डिज़ाइन, पैलेट, भाषा) और MCP कनेक्शन जानकारी।", "tip.theme": "लाइट/डार्क मोड। स्वचालित आपके OS सेटिंग का अनुसरण करता है।", "tip.skin": "समग्र रूप: Glass (फ्रॉस्टेड), Web (फ्लैट), या Code (IDE)।", "tip.palette": "रंग योजना — लोकप्रिय टर्मिनल पैलेट (Dracula, Nord, …)।", "tip.lang": "इंटरफ़ेस भाषा।", "tip.collapse": "इस पैनल को समेटें। आकार बदलने के लिए किनारा खींचें; समेटने के लिए किनारे पर डबल-क्लिक करें।", "tip.restoreSidebar": "rooms सूची दिखाएँ", "tip.restoreActivity": "गतिविधि पैनल दिखाएँ"},
};
const I18N_LANGS = [
  {v:'en', label:'English'}, {v:'ru', label:'Русский'}, {v:'es', label:'Español'},
  {v:'de', label:'Deutsch'}, {v:'fr', label:'Français'}, {v:'pt', label:'Português'},
  {v:'zh', label:'中文'}, {v:'ja', label:'日本語'}, {v:'ar', label:'العربية'}, {v:'hi', label:'हिन्दी'},
];
let LANG = (function () {
  try {
    const s = localStorage.getItem('agentbus-lang');
    if (s && I18N[s]) return s;
    const n = (navigator.language || 'en').slice(0, 2).toLowerCase();
    return I18N[n] ? n : 'en';
  } catch (e) { return 'en'; }
})();
function t(key) {
  const d = I18N[LANG] || {};
  if (key in d && d[key]) return d[key];
  return (I18N.en[key] != null) ? I18N.en[key] : key;
}
function applyI18n() {
  document.documentElement.setAttribute('lang', LANG);
  document.documentElement.setAttribute('dir', LANG === 'ar' ? 'rtl' : 'ltr');
  document.querySelectorAll('[data-i18n]').forEach(e => { e.textContent = t(e.getAttribute('data-i18n')); });
  document.querySelectorAll('[data-i18n-title]').forEach(e => { e.title = t(e.getAttribute('data-i18n-title')); });
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

function fmtDateTime(ts) {
  return new Date(ts * 1000).toLocaleString(LANG || 'en', {
    day: '2-digit', month: '2-digit', year: 'numeric',
    hour: '2-digit', minute: '2-digit',
  });
}

function messageCount(n) {
  if (LANG === 'ru') {
    const word = n % 10 === 1 && n % 100 !== 11 ? 'сообщение'
      : n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 12 || n % 100 > 14)
        ? 'сообщения' : 'сообщений';
    return `${n} ${word}`;
  }
  return `${n} ${n === 1 ? 'message' : t('room.messages')}`;
}

function messageKindLabel(kind) {
  return t(`kind.${kind}`) === `kind.${kind}` ? t('kind.other') : t(`kind.${kind}`);
}

function messageRecipientLabel(to) {
  return to === 'all' ? t('compose.all').toLowerCase() : to;
}

// ── State ─────────────────────────────────────────────────
let currentRoom = null, currentOwner = null, lastId = 0;
let rooms = [], msgMap = {};
let searchResults = null, searchPending = false, searchTimer = null, searchSequence = 0;
let agentMetaTotals = {}; // {agentName: {tokens_total, tokens_in, tokens_out, msgs, models:Set, last_reasoning}}
let lastStatuses = {};    // {agentName: 'online'|'busy'|...} — latest room status snapshot
let lastPhases = {};      // lifecycle phase explicitly reported by agent/server
let lastHealth = {};      // wake-health from /api/room_agents (rate-limit window, last wake)
let roomMessages = [], laneCollapsed = false, composerKind = 'request', lastRenderedRound = null;
let selectedReplyTarget = null;
let roomData = null;

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
  {v: 'opus', label: '✦ Editorial'},
  {v: 'glass', label: '🪟 Glass'}, {v: 'web', label: '💬 Web'}, {v: 'code', label: '⌨️ Code'},
];
const PALETTE_OPTS = [
  {v: 'default', label: 'Default'}, {v: 'dracula', label: 'Dracula'}, {v: 'nord', label: 'Nord'},
  {v: 'tokyonight', label: 'Tokyo Night'}, {v: 'catppuccin', label: 'Catppuccin'}, {v: 'gruvbox', label: 'Gruvbox'},
];

function applyTheme(mode) {
  if (!['auto', 'dark', 'light'].includes(mode)) mode = 'auto';
  let resolved = mode === 'auto'
    ? (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')
    : mode;
  // The 5 palettes are dark terminal schemes; a non-default palette forces dark
  // STRUCTURE so the base light-theme overrides (light buttons/insets/scrollbars)
  // don't clash with the dark palette tokens. The user's mode choice is kept in
  // data-theme-mode and restored when the palette returns to "default".
  const pal = document.documentElement.getAttribute('data-palette')
    || localStorage.getItem('agentbus-palette') || 'default';
  if (pal !== 'default') resolved = 'dark';
  document.documentElement.setAttribute('data-theme', resolved);
  document.documentElement.setAttribute('data-theme-mode', mode);
}
function applySkin(skin) {
  if (!SKIN_OPTS.some(o => o.v === skin)) skin = 'opus';
  document.documentElement.setAttribute('data-skin', skin);
}
function applyPalette(pal) {
  if (!PALETTE_OPTS.some(o => o.v === pal)) pal = 'default';
  document.documentElement.setAttribute('data-palette', pal);
  // Re-resolve the theme: palettes force dark structure; "default" restores the
  // user's light/dark/auto choice.
  applyTheme(localStorage.getItem('agentbus-theme') || 'auto');
}

function helpIcon(tipKey) {
  return el('span', {class: 'help-icon', 'data-i18n-title': tipKey, title: t(tipKey), text: '?'});
}

function buildSettingsRow(titleKey, opts, storeKey, getCur, apply, tipKey) {
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
  const label = el('div', {class: 'set-label'}, [el('span', {text: t(titleKey)}), tipKey ? helpIcon(tipKey) : null]);
  return el('div', {class: 'set-row'}, [label, seg]);
}

function buildCopyRow(label, value) {
  const inp = el('input', {class: 'set-copy-input', value: value, readonly: 'readonly', title: value});
  const btn = el('button', {class: 'set-opt set-copy-btn', text: t('btn.copy')});
  btn.onclick = () => {
    try { navigator.clipboard && navigator.clipboard.writeText(value); } catch (_) {}
    inp.focus(); inp.select && inp.select();
    btn.textContent = '✓';
    setTimeout(() => { btn.textContent = t('btn.copy'); }, 1200);
  };
  return el('div', {class: 'set-row'}, [
    el('div', {class: 'set-label', text: label}),
    el('div', {class: 'set-copy'}, [inp, btn]),
  ]);
}

// A reference row: env-var / file name (click to copy) + a short description.
function buildVarRow(name, desc) {
  const code = el('code', {class: 'set-var-name', title: t('btn.copy'), text: name});
  code.onclick = () => {
    try { navigator.clipboard && navigator.clipboard.writeText(name); } catch (_) {}
    const old = code.textContent; code.textContent = '✓ ' + name;
    setTimeout(() => { code.textContent = old; }, 900);
  };
  return el('div', {class: 'set-var'}, [code, el('div', {class: 'set-var-desc', text: desc})]);
}

// A copyable multi-line block (read-only textarea + copy button).
function buildPromptRow(value) {
  const ta = el('textarea', {class: 'set-prompt', readonly: 'readonly', rows: '5'});
  ta.value = value;
  const btn = el('button', {class: 'set-opt set-copy-btn', text: t('btn.copy')});
  btn.onclick = () => {
    try { navigator.clipboard && navigator.clipboard.writeText(value); } catch (_) {}
    ta.focus(); ta.select && ta.select();
    btn.textContent = '✓';
    setTimeout(() => { btn.textContent = t('btn.copy'); }, 1200);
  };
  return el('div', {class: 'set-prompt-wrap'}, [ta, btn]);
}

// Keep the everyday view controls visible and put connection/spawn reference
// material behind explicit disclosures. This keeps the popover useful on a
// laptop without removing any existing configuration or copy actions.
function buildSettingsSection(title, children, open = false) {
  const section = el('details', {class: 'set-section'});
  if (open) section.open = true;
  section.appendChild(el('summary', {class: 'set-section-summary'}, [
    el('span', {text: title}),
    el('span', {class: 'set-section-chevron', text: '›', 'aria-hidden': 'true'}),
  ]));
  section.appendChild(el('div', {class: 'set-section-body'}, children));
  return section;
}

function buildServiceAction(label, action, danger = false) {
  const button = el('button', {type: 'button', class: `set-service-action${danger ? ' danger' : ''}`, text: label});
  button.addEventListener('click', () => {
    document.getElementById('settings-popover').hidden = true;
    action();
  });
  return button;
}

function getRoomView() {
  return localStorage.getItem('agentbus-room-view') === 'projects' ? 'projects' : 'latest';
}

function applyRoomView(view) {
  if (view !== 'projects') view = 'latest';
  renderRooms();
}

// Theme labels are localised (emoji + word); skin/palette/lang labels are proper nouns.
function themeOpts() {
  return [
    {v: 'auto',  label: '🌓 ' + t('theme.auto')},
    {v: 'dark',  label: '🌙 ' + t('theme.dark')},
    {v: 'light', label: '☀️ ' + t('theme.light')},
  ];
}

function buildSettingsPopover(pop) {
  pop.innerHTML = '';
  const origin = location.origin;
  pop.appendChild(el('div', {class: 'set-title'}, [el('span', {text: t('set.appearance')}), helpIcon('tip.view')]));
  pop.appendChild(buildSettingsRow('set.theme', themeOpts(), 'agentbus-theme',
    () => localStorage.getItem('agentbus-theme') || 'auto', applyTheme, 'tip.theme'));
  pop.appendChild(buildSettingsRow('set.roomView', [
    {v: 'latest', label: t('roomView.latest')},
    {v: 'projects', label: t('roomView.projects')},
  ], 'agentbus-room-view', getRoomView, applyRoomView));
  pop.appendChild(buildSettingsSection(`${t('set.skin')} · ${t('set.palette')} · ${t('set.lang')}`, [
    buildSettingsRow('set.lang', I18N_LANGS, 'agentbus-lang', () => LANG, setLang, 'tip.lang'),
    buildSettingsRow('set.skin', SKIN_OPTS, 'agentbus-skin',
      () => localStorage.getItem('agentbus-skin') || 'opus', applySkin, 'tip.skin'),
    buildSettingsRow('set.palette', PALETTE_OPTS, 'agentbus-palette',
      () => localStorage.getItem('agentbus-palette') || 'default', applyPalette, 'tip.palette'),
  ]));
  pop.appendChild(el('div', {class: 'set-sep'}));
  pop.appendChild(buildSettingsSection(t('set.mcp'), [
    buildCopyRow(t('mcp.endpoint'), origin + '/mcp'),
    buildCopyRow(t('mcp.claude'), `claude mcp add --transport http huddle ${origin}/mcp`),
    buildCopyRow(t('mcp.codex'), `[mcp_servers.huddle]\nurl = "${origin}/mcp"`),
    buildCopyRow(t('mcp.stdio'), 'mcp-huddle'),
    el('div', {class: 'set-hint', text: t('mcp.hint')}),
  ]));

  // ── Environment variables / spawn rules (reference; click a name to copy) ──
  pop.appendChild(el('div', {class: 'set-sep'}));
  pop.appendChild(buildSettingsSection(t('set.spawn'), [
    buildVarRow('MCP_HUDDLE_READONLY=0', t('var.readonly')),
    buildVarRow('~/.mcp-huddle/registry.json', t('var.registryFile')),
    buildVarRow('MCP_HUDDLE_SPAWN_REGISTRY', t('var.registryEnv')),
    buildVarRow('MCP_HUDDLE_CLAUDE_ENABLED=1', t('var.claude')),
    buildVarRow('MCP_HUDDLE_ANTIGRAVITY_ENABLED=1', t('var.antigravity')),
    buildVarRow('MCP_HUDDLE_MIMO_ENABLED=0', t('var.mimo')),
    buildVarRow('MCP_HUDDLE_TOKEN', t('var.token')),
    buildVarRow('MCP_HUDDLE_HOME', t('var.home')),
    buildVarRow('PORT', t('var.port')),
  ]));

  // ── Copy-paste prompt to onboard an agent into huddle ──
  pop.appendChild(el('div', {class: 'set-sep'}));
  pop.appendChild(buildSettingsSection(t('set.agentPrompt'), [
    helpIcon('tip.agentPrompt'),
    buildPromptRow(t('agentPrompt.text').split('{origin}').join(location.origin)),
  ]));
  pop.appendChild(buildSettingsSection(LANG === 'ru' ? 'Обслуживание' : 'Maintenance', [
    buildServiceAction(t('btn.closeAll'), bulkCloseAll),
    buildServiceAction(t('btn.deleteClosed'), bulkDeleteClosed, true),
    buildServiceAction(t('btn.nukeAll'), bulkNuke, true),
  ]));
}

function setLang(lang) {
  if (!I18N[lang]) lang = 'en';
  LANG = lang;
  try { localStorage.setItem('agentbus-lang', lang); } catch (_) {}
  applyI18n();
  if (roomData) renderSwarmPilot(roomData);
  updateActivityStatuses(lastStatuses);
  document.querySelectorAll('.agent-transcript').forEach(transcript => {
    const content = transcript.querySelector('.agent-transcript-scroll');
    const summary = transcript.querySelector('summary');
    if (content && summary) summary.textContent = `${t('activity.transcript')} · ${content.querySelectorAll('.agent-transcript-message').length}`;
  });
  const pop = document.getElementById('settings-popover');
  if (pop) buildSettingsPopover(pop);  // rebuild so the popover's own labels update
}

function initSettings() {
  // Apply saved values (head script already set them pre-paint; re-assert).
  applyTheme(localStorage.getItem('agentbus-theme') || 'auto');
  applySkin(localStorage.getItem('agentbus-skin') || 'opus');
  applyPalette(localStorage.getItem('agentbus-palette') || 'default');
  applyI18n();

  // Re-apply on OS theme change while in 'auto' mode.
  const mq = window.matchMedia('(prefers-color-scheme: dark)');
  const onSys = () => { if ((localStorage.getItem('agentbus-theme') || 'auto') === 'auto') applyTheme('auto'); };
  if (mq.addEventListener) mq.addEventListener('change', onSys);
  else if (mq.addListener) mq.addListener(onSys);

  const pop = document.getElementById('settings-popover');
  const btn = document.getElementById('settings-btn');
  if (!pop || !btn) return;
  buildSettingsPopover(pop);

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
    const r = await apiFetch('/api/rooms');
    const loaded = await r.json();
    if (!Array.isArray(loaded)) throw new Error('invalid rooms response');
    rooms = loaded;
    document.getElementById('room-count').textContent = rooms.length;
    renderRooms();
    const roomFromLink = new URLSearchParams(location.hash.slice(1)).get('room');
    if (!currentRoom && roomFromLink) {
      const target = rooms.find(room => room.id === roomFromLink);
      if (target) openRoom(target.id, target.owner);
    }
    hideAuthRequired();
  } catch(e) {
    // Preserve the last successfully rendered room list during auth/network errors.
    if (e && e.status === 401) showAuthRequired(e.message);
    else showLoadError(e);
  }
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

const ROOM_MODES = new Set(['council', 'relay', 'team', 'swarm']);
function roomModeBadge(room, showOrdinary = false) {
  const mode = room && room.swarm_pilot && room.swarm_pilot.mode;
  if (!ROOM_MODES.has(mode)) {
    return showOrdinary ? el('span', {
      class: 'room-mode room-mode-ordinary',
      text: t('roomMode.ordinary'),
      title: t('roomMode.title'),
    }) : null;
  }
  return el('span', {
    class: `room-mode room-mode-${mode}`,
    text: t(`roomMode.${mode}`),
    title: t('roomMode.title'),
  });
}

function roomItem(r, label, indent = 46) {
  const active = r.id === currentRoom;
  // Search results are intentionally compact and may omit swarm_pilot. Reuse
  // the full /api/rooms record when it is already loaded in memory.
  const fullRoom = rooms.find(room => room.id === r.id) || r;
  const modeBadge = roomModeBadge(fullRoom, true);
  return el('div', {
    class: 'room-item' + (active ? ' active' : ''),
    dataset: {id: r.id, owner: r.owner},
    style: `padding-left:${indent}px`,
    title: r.created_at ? `${t('room.created')} ${fmtDateTime(r.created_at)}` : '',
  }, [
    el('div', {class: 'room-name'}, [
      el('span', {class: `dot dot-${r.status}` + (r.status === 'open' ? ' pulse' : '')}),
      el('span', {text: label}),
      modeBadge,
    ]),
    el('div', {class: 'room-meta', text: `${(r.participants || []).length}·${fmtTime(r.last_activity || r.created_at)}`}),
  ]);
}

function renderRooms() {
  const sidebar = document.getElementById('room-list');
  sidebar.innerHTML = '';
  if (!document.getElementById('room-search').hidden) {
    if (searchPending) {
      sidebar.appendChild(el('div', {class: 'empty-sidebar', text: t('search.pending')}));
    } else if (searchResults) {
      if (!searchResults.length) sidebar.appendChild(el('div', {class: 'empty-sidebar', text: t('search.empty')}));
      for (const hit of searchResults) {
        const item = roomItem(hit, hit.name, 18);
        item.classList.add('search-hit');
        if (hit.snippet) item.appendChild(el('div', {class: 'search-snippet', text: hit.snippet}));
        if (hit.message_id != null) item.dataset.messageId = String(hit.message_id);
        sidebar.appendChild(item);
      }
    } else {
      sidebar.appendChild(el('div', {class: 'empty-sidebar', text: t('search.hint')}));
    }
    return;
  }
  if (!rooms.length) {
    sidebar.appendChild(el('div', {class: 'empty-sidebar', text: t('sidebar.empty')}));
    return;
  }

  if (getRoomView() === 'latest') {
    const sorted = rooms.slice().sort((a, b) => {
      const activity = (b.last_activity || b.created_at || 0) - (a.last_activity || a.created_at || 0);
      return activity || String(a.name || a.id).localeCompare(String(b.name || b.id));
    });
    sorted.forEach(r => sidebar.appendChild(roomItem(r, r.name || r.id, 18)));
    return;
  }

  // Hierarchy: project → date → organizer (owner) → chats.
  // Chats keep the agent-chosen name; if an organizer has more than one on a
  // given day they are numbered (number first), e.g. "1. design review".
  const projects = new Map();  // proj → date → org → [rooms]
  for (const r of rooms) {
    const parts = (r.cwd || '').replace(/[/]+$/, '').split('/').filter(Boolean);
    const proj = parts.length ? parts[parts.length - 1] : '—';
    const d = new Date((r.created_at || 0) * 1000);
    const dateKey = isFinite(d.getTime()) ? d.toISOString().slice(0, 10) : '0000-00-00';
    const org = r.owner || '—';
    if (!projects.has(proj)) projects.set(proj, new Map());
    const dates = projects.get(proj);
    if (!dates.has(dateKey)) dates.set(dateKey, new Map());
    const orgs = dates.get(dateKey);
    if (!orgs.has(org)) orgs.set(org, []);
    orgs.get(org).push(r);
  }

  const dateLabel = (key) => {
    const d = new Date(key + 'T00:00:00');
    return isFinite(d.getTime())
      ? d.toLocaleDateString(LANG, {day: '2-digit', month: 'short', year: 'numeric'})
      : key;
  };
  const sortDesc = (a, b) => (a < b ? 1 : a > b ? -1 : 0);  // newest dates first
  const countRooms = (orgs) => [...orgs.values()].reduce((s, rs) => s + rs.length, 0);

  // group(key, label, count, depth) → {group, body}; clicking the header folds it.
  const group = (key, label, count, depth) => {
    const folded = treeFolded(key);
    const g = el('div', {class: 'tree-group' + (folded ? ' collapsed' : '')});
    const head = el('div', {class: 'tree-group-header', style: `padding-left:${10 + depth * 12}px`}, [
      el('span', {class: 'tree-arrow', text: '▾'}),
      el('span', {class: 'tree-label', text: label}),
      el('span', {class: 'count', text: String(count)}),
    ]);
    head.onclick = () => toggleTree(key);
    const body = el('div', {class: 'tree-group-body'});
    g.appendChild(head); g.appendChild(body);
    return {g, body};
  };

  for (const proj of [...projects.keys()].sort()) {
    const dates = projects.get(proj);
    const pTotal = [...dates.values()].reduce((s, orgs) => s + countRooms(orgs), 0);
    const {g: pg, body: pb} = group('proj:' + proj, proj, pTotal, 0);
    sidebar.appendChild(pg);

    for (const dateKey of [...dates.keys()].sort(sortDesc)) {
      const orgs = dates.get(dateKey);
      const {g: dg, body: db} = group('date:' + proj + '/' + dateKey, dateLabel(dateKey), countRooms(orgs), 1);
      pb.appendChild(dg);

      for (const org of [...orgs.keys()].sort()) {
        const rs = orgs.get(org).slice().sort((a, b) => (a.created_at || 0) - (b.created_at || 0));
        const {g: og, body: ob} = group('org:' + proj + '/' + dateKey + '/' + org, org, rs.length, 2);
        db.appendChild(og);

        rs.forEach((r, i) => {
          const label = rs.length > 1 ? `${i + 1}. ${r.name}` : r.name;
          ob.appendChild(roomItem(r, label));
        });
      }
    }
  }
}

// ── Chat ──────────────────────────────────────────────────
async function copyRoomValue(btn, text, defaultLabelKey) {
  if (!btn) return;
  const originalText = t(defaultLabelKey);
  clearTimeout(btn._copyTimer);
  btn.classList.remove('is-copied', 'is-failed');

  let ok = false;
  if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
    try {
      await navigator.clipboard.writeText(text);
      ok = true;
    } catch (_) {}
  }
  if (!ok) {
    let ta;
    try {
      ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.style.position = 'fixed';
      ta.style.top = '-9999px';
      ta.style.left = '-9999px';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.focus();
      ta.select();
      ok = Boolean(document.execCommand && document.execCommand('copy'));
    } catch (_) {
      ok = false;
    } finally {
      if (ta && ta.parentNode) ta.parentNode.removeChild(ta);
    }
  }

  if (ok) {
    btn.textContent = t('room.copied');
    btn.classList.add('is-copied');
    btn.title = '';
    btn._copyTimer = setTimeout(() => {
      btn.textContent = originalText;
      btn.classList.remove('is-copied');
    }, 1500);
  } else {
    btn.textContent = t('room.copyFailed');
    btn.classList.add('is-failed');
    btn.title = text;
    btn._copyTimer = setTimeout(() => {
      btn.textContent = originalText;
      btn.classList.remove('is-failed');
    }, 2500);
  }
}

function buildChatShell(room) {
  const chat = document.getElementById('chat-area');
  chat.innerHTML = '';

  const isClosed = room.status === 'closed';
  const isReadOnly = isClosed || room.status === 'resolved';

  const titleChildren = [
    el('span', {class: 'hash', text: '#'}),
    el('span', {text: room.name || room.id || ''}),
  ];
  const modeBadge = roomModeBadge(room, true);
  if (modeBadge) titleChildren.push(modeBadge);
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
    el('div', {class: 'room-heading'}, [
      el('div', {class: 'room-crumb', id: 'room-crumb', text: room.cwd || room.project || ''}),
      el('div', {class: 'chat-title'}, titleChildren),
      el('div', {class: 'chat-meta', id: 'chat-meta', text: 'Loading…'}),
    ]),
    el('div', {class: 'room-actions'}, [
      el('button', {class: 'room-text-action', id: 'copy-room-name', text: t('room.copyName')}),
      el('button', {class: 'room-text-action', id: 'copy-room-id', text: t('room.copyId')}),
      actionBtn,
    ]),
  ]);

  const lanes = el('section', {class: 'room-lanes', id: 'room-lanes', 'aria-label': t('lanes.title')});
  const swarmPanel = el('section', {class: 'swarm-pilot', id: 'swarm-pilot', hidden: '', 'aria-label': t('swarm.title')});
  const messages = el('div', {class: 'messages', id: 'messages'});

  const inputAttrs = {
    id: 'human-inp',
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
    placeholder: isReadOnly ? (isClosed ? t('chat.closed') : t('chat.resolved')) : t('compose.placeholder'),
  };
  if (isReadOnly) inputAttrs.disabled = '';
  const input = el('textarea', inputAttrs);

  const sendAttrs = {class: 'send-btn', id: 'btn-send', text: t('btn.send')};
  if (isReadOnly) sendAttrs.disabled = '';
  const send = el('button', sendAttrs);

  const participants = (room.participants || []).filter(p => p !== 'Human' && p !== 'System');
  const recipient = el('select', {id: 'human-to', 'aria-label': t('compose.to')}, [
    el('option', {value: 'all', text: t('compose.all')}),
    ...participants.map(p => el('option', {value: p, text: p})),
  ]);
  const kindButtons = ['request', 'comment', 'system'].map(kind => {
    const b = el('button', {type: 'button', class: 'composer-kind', dataset: {kind},
      'aria-pressed': String(composerKind === kind), text: t(`compose.${kind}`)});
    b.onclick = () => {
      composerKind = kind;
      if (kind !== 'comment' && selectedReplyTarget) {
        selectedReplyTarget = null;
        renderComposerReplyTarget();
      }
      document.querySelectorAll('.composer-kind').forEach(x => x.setAttribute('aria-pressed', String(x.dataset.kind === kind)));
      const hint = document.getElementById('composer-hint');
      if (hint) hint.textContent = kind === 'request' ? t('compose.hint') : '';
    };
    return b;
  });
  const inputWrap = el('div', {class: 'input-wrap room-composer'}, [
    el('div', {class: 'composer-controls'}, [
      el('span', {text: t('compose.as')}),
      el('span', {class: 'composer-kinds'}, kindButtons),
      el('label', {}, [t('compose.to') + ' ', recipient]),
      el('span', {class: 'composer-hint', id: 'composer-hint', text: composerKind === 'request' ? t('compose.hint') : ''}),
    ]),
    el('div', {id: 'composer-reply-slot', class: 'composer-reply-slot'}),
    el('div', {class: 'input-row'}, [
      input, send,
    ]),
  ]);
  renderComposerReplyTarget();

  chat.appendChild(header);
  chat.appendChild(lanes);
  chat.appendChild(swarmPanel);
  chat.appendChild(messages);
  chat.appendChild(inputWrap);

  if (isClosed) {
    document.getElementById('btn-delete').onclick = deleteRoom;
  } else {
    document.getElementById('btn-close').onclick = closeRoom;
  }
  if (!isReadOnly) {
    send.onclick = sendMsg;
    input.onkeydown = e => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); sendMsg(); } };
  }
  const btnCopyName = document.getElementById('copy-room-name');
  if (btnCopyName) {
    btnCopyName.onclick = () => copyRoomValue(btnCopyName, room.name || room.id || '', 'room.copyName');
  }
  const btnCopyId = document.getElementById('copy-room-id');
  if (btnCopyId) {
    btnCopyId.onclick = () => copyRoomValue(btnCopyId, room.id, 'room.copyId');
  }
  renderSwarmPilot(room);
}

const SWARM_BUCKETS = [
  ['responsibilities', 'swarm.responsibilities'], ['tasks', 'swarm.tasks'],
  ['decisions', 'swarm.decisions'], ['facts', 'swarm.facts'],
];
function swarmEntryText(entry) {
  if (typeof entry === 'string') return entry;
  return entry && typeof entry.value === 'string' ? entry.value : '';
}
// Pure: one row per member with their claimed roles and this round's state.
function swarmMemberRows(state) {
  const members = Array.isArray(state && state.members) ? state.members.filter(m => typeof m === 'string') : [];
  const asObj = v => (v && typeof v === 'object' ? v : {});
  const done = asObj(state.done), dispatched = asObj(state.dispatched), resp = asObj(state.responsibilities);
  return members.map(name => ({
    name,
    roles: Object.keys(resp).filter(k => asObj(resp[k]).member === name),
    status: name in done ? 'done' : name in dispatched ? 'active' : 'waiting',
  }));
}
function renderSwarmPilot(room) {
  const panel = document.getElementById('swarm-pilot');
  const state = room && room.swarm_pilot;
  if (!panel) return;
  if (!state || typeof state !== 'object') { panel.hidden = true; panel.replaceChildren(); return; }
  panel.hidden = false;
  const wasOpen = panel.querySelector('details')?.open;
  const storageKey = `agentbus-swarm-panel:${room.id || currentRoom}`;
  let savedOpen = null;
  try { savedOpen = localStorage.getItem(storageKey); } catch (_) {}
  const open = savedOpen === null ? (wasOpen ?? true) : savedOpen === '1';
  panel.replaceChildren();

  const mode = ROOM_MODES.has(state.mode) ? t(`roomMode.${state.mode}`) : String(state.mode || '—');
  const phase = state.phase === 'completed' ? t('swarm.completed')
    : state.phase === 'working' ? t('swarm.working') : String(state.phase || '—');
  const memberRows = swarmMemberRows(state);
  const doneCount = memberRows.filter(m => m.status === 'done').length;
  const progress = memberRows.length ? ` · ${doneCount}/${memberRows.length}` : '';
  const head = el('summary', {class: 'swarm-summary'}, [
    el('span', {class: 'swarm-marker', 'aria-hidden': 'true'}),
    el('span', {class: 'swarm-title', text: t('swarm.title')}),
    el('span', {class: 'swarm-meta', text: `${t('swarm.mode')}: ${mode} · ${t('swarm.phase')}: ${phase} · ${t('swarm.round')} ${state.round || 1}${progress}`}),
  ]);
  const details = el('details', {class: 'swarm-details'});
  details.open = open;
  details.addEventListener('toggle', () => {
    try { localStorage.setItem(storageKey, details.open ? '1' : '0'); } catch (_) {}
  });
  details.appendChild(head);
  const body = el('div', {class: 'swarm-body'});
  if (state.goal) body.appendChild(el('div', {class: 'swarm-goal'}, [
    el('span', {class: 'swarm-label', text: `${t('swarm.goal')} · `}),
    el('span', {text: String(state.goal)}),
  ]));

  if (memberRows.length) {
    const strip = el('ul', {class: 'swarm-members', 'aria-label': t('swarm.members')});
    for (const m of memberRows) {
      const statusKey = {done: 'swarm.memberDone', active: 'swarm.memberActive', waiting: 'swarm.memberWaiting'}[m.status];
      strip.appendChild(el('li', {class: `swarm-member swarm-member-${m.status}`}, [
        el('span', {class: 'swarm-member-name', text: m.name}),
        el('span', {class: 'swarm-member-role', text: m.roles.length ? m.roles.join(', ') : t('swarm.noRole')}),
        el('span', {class: 'swarm-member-status', text: t(statusKey)}),
      ]));
    }
    body.appendChild(strip);
  }

  const childPolicy = state.child_agents && typeof state.child_agents === 'object' ? state.child_agents : null;
  const childRecords = state.children && typeof state.children === 'object' ? Object.entries(state.children)
    .filter(([, child]) => child && typeof child === 'object') : [];
  // Keep the child section out of ordinary pilots; once children exist, show
  // both the room's configured allowance and the readable child roster.
  if (childRecords.length) {
    const childSection = el('section', {class: 'swarm-children'});
    childSection.appendChild(el('h3', {text: t('swarm.childAgents')}));
    if (childPolicy) {
      const policyParts = [];
      if (Number.isInteger(childPolicy.max_children)) {
        policyParts.push(`${t('swarm.childQuota')}: ${childPolicy.max_children}`);
      }
      if (Array.isArray(childPolicy.profiles) && childPolicy.profiles.length) {
        policyParts.push(`${t('swarm.childProfiles')}: ${childPolicy.profiles.filter(x => typeof x === 'string').join(', ')}`);
      }
      if (policyParts.length) childSection.appendChild(el('p', {class: 'swarm-child-policy', text: policyParts.join(' · ')}));
    }
    const childList = el('ul', {class: 'swarm-child-list'});
    for (const [name, child] of childRecords) {
      const statusKey = child.status === 'failed' ? 'swarm.childFailed'
        : child.status === 'exited' ? 'swarm.childExited'
          : child.status === 'reserved' ? 'swarm.childStarting'
            : child.status === 'running' ? 'swarm.childRunning' : 'swarm.childUnknown';
      const statusClass = child.status === 'failed' ? 'failed'
        : child.status === 'exited' ? 'exited'
          : child.status === 'reserved' ? 'starting'
            : child.status === 'running' ? 'running' : 'unknown';
      const identity = [child.parent ? `${t('swarm.childParent')}: ${child.parent}` : '',
        typeof child.profile === 'string' ? child.profile : ''].filter(Boolean).join(' · ');
      const childItem = el('li', {class: `swarm-child swarm-child-${statusClass}`}, [
        el('span', {class: 'swarm-child-name', text: name}),
        identity ? el('span', {class: 'swarm-child-meta', text: identity}) : null,
        el('span', {class: 'swarm-child-status', text: t(statusKey)}),
      ]);
      if (child.invite === 'child_room') {
        const childRoom = typeof child.child_room === 'string' ? child.child_room : '';
        if (childRoom) {
          const roomLink = el('button', {type: 'button', class: 'swarm-child-room-link',
            text: `${t('swarm.childOpenRoom')} · ${childRoom}`});
          roomLink.addEventListener('click', () => {
            const linkedRoom = rooms.find(r => r.id === childRoom);
            openRoom(childRoom, linkedRoom ? linkedRoom.owner : 'System');
          });
          childItem.appendChild(roomLink);
        }
        const historyLabel = child.history === 'recent' ? 'swarm.childHistoryRecent' : 'swarm.childHistoryNone';
        childItem.appendChild(el('span', {class: 'swarm-child-meta', text: t(historyLabel)}));
        childItem.appendChild(el('span', {class: 'swarm-child-context-note', text: t('swarm.childContextNote')}));
        const relayLabel = child.relay === 'none' ? 'swarm.childRelayOff'
          : child.relay_status === 'sent' ? 'swarm.childRelaySent'
            : child.relay_status === 'failed' || child.delivery === 'relay_failed' ? 'swarm.childRelayFailed'
              : 'swarm.childRelayPending';
        childItem.appendChild(el('span', {class: `swarm-child-relay${relayLabel === 'swarm.childRelayFailed' ? ' failed' : ''}`,
          text: t(relayLabel)}));
      }
      childList.appendChild(childItem);
    }
    childSection.appendChild(childList);
    body.appendChild(childSection);
  }

  const grid = el('div', {class: 'swarm-grid'});
  for (const [bucketName, labelKey] of SWARM_BUCKETS) {
    const bucket = state[bucketName] && typeof state[bucketName] === 'object' ? state[bucketName] : {};
    const entries = Object.entries(bucket);
    const group = el('section', {class: `swarm-group swarm-${bucketName}`});
    group.appendChild(el('h3', {text: `${t(labelKey)} · ${entries.length}`}));
    if (!entries.length) {
      group.appendChild(el('p', {class: 'swarm-empty', text: t('swarm.empty')}));
    } else {
      const list = el('ul', {class: 'swarm-list'});
      for (const [key, entry] of entries) {
        const value = swarmEntryText(entry);
        const member = entry && typeof entry === 'object' ? entry.member : '';
        const item = el('li', {}, [
          el('span', {class: 'swarm-key', text: key}),
          el('span', {class: 'swarm-value', text: value || '—'}),
        ]);
        if (member) item.appendChild(el('span', {class: 'swarm-owner', text: `${t(bucketName === 'responsibilities' && key === 'reporter' ? 'swarm.reporter' : 'swarm.owner')}: ${member}`}));
        list.appendChild(item);
      }
      group.appendChild(list);
    }
    grid.appendChild(group);
  }
  body.appendChild(grid);
  if (state.final && typeof state.final === 'object' && state.final.result) {
    body.appendChild(el('section', {class: 'swarm-final'}, [
      el('h3', {text: t('swarm.final')}),
      el('p', {text: String(state.final.result)}),
      state.final.member ? el('span', {class: 'swarm-owner', text: `${t('swarm.owner')}: ${state.final.member}`}) : null,
    ]));
  }
  details.appendChild(body);
  panel.appendChild(details);
}

async function openRoom(id, owner) {
  currentRoom = id;
  currentOwner = owner;
  selectedReplyTarget = null;
  history.replaceState(null, '', `${location.pathname}${location.search}#room=${encodeURIComponent(id)}`);
  lastId = 0;
  msgMap = {};
  roomMessages = [];
  roomData = null;
  lastPhases = {};
  lastHealth = {};
  lastRenderedRound = null;
  agentMetaTotals = {};
  closeAgentStreams();  // abort authenticated fetch streams from previous room
  renderRooms();
  buildChatShell(rooms.find(x => x.id === id) || {id});
  await fetchMessages(true);
  await attachAgentPanels(id);  // Phase 1: live agent event stream (Codex / runner agents)
  if (roomData && currentRoom === id) { renderRoomLanes(roomData, lastStatuses); updateFooterStatus(); }
  // Re-paint totals badges after panels rebuilt
  Object.keys(agentMetaTotals).forEach(renderAgentTotalsBadge);
  relayout();  // reveal the activity panel now that a room is open
}

function initRoomSearch() {
  const panel = document.getElementById('room-search');
  const input = document.getElementById('room-search-input');
  const button = document.getElementById('search-toggle');
  input.placeholder = t('search.placeholder');
  input.setAttribute('aria-label', t('search.placeholder'));
  const close = () => {
    panel.hidden = true;
    input.value = '';
    searchResults = null;
    searchPending = false;
    clearTimeout(searchTimer);
    searchSequence++;
    button.setAttribute('aria-expanded', 'false');
    renderRooms();
  };
  button.onclick = () => {
    if (!panel.hidden) return close();
    panel.hidden = false;
    button.setAttribute('aria-expanded', 'true');
    if (isOverlay()) { layout.drawer = 'sidebar'; relayout(); }
    else if (layout.sidebarCollapsed) { layout.sidebarCollapsed = false; persistLayout(); relayout(); }
    input.focus();
    renderRooms();
  };
  document.getElementById('room-search-close').onclick = close;
  input.oninput = () => {
    clearTimeout(searchTimer);
    const query = input.value.trim();
    const sequence = ++searchSequence;
    if (!query) { searchResults = null; searchPending = false; renderRooms(); return; }
    searchPending = true;
    renderRooms();
    searchTimer = setTimeout(async () => {
      try {
        let results;
        try {
          const response = await apiFetch(`/api/rooms_search?q=${encodeURIComponent(query)}`);
          results = (await response.json()).results;
        } catch (error) {
          // An older running server may still serve the fresh dashboard files.
          // Keep search usable until its Python process is restarted.
          if (error.status !== 404) throw error;
          results = await searchOnOlderServer(query, sequence);
        }
        if (sequence !== searchSequence) return;
        searchResults = results;
        searchPending = false;
        renderRooms();
      } catch (error) {
        if (sequence !== searchSequence) return;
        searchPending = false;
        searchResults = [];
        renderRooms();
        showDashboardNotice(`Search failed: ${error.message}`, 'Retry', () => input.dispatchEvent(new Event('input')));
      }
    }, 250);
  };
  input.onkeydown = event => { if (event.key === 'Escape') close(); };
}

async function searchOnOlderServer(query, sequence) {
  const needle = query.toLocaleLowerCase();
  const snapshot = rooms.slice();
  const found = [];
  let next = 0;
  async function worker() {
    while (next < snapshot.length && sequence === searchSequence) {
      const room = snapshot[next++];
      const name = String(room.name || room.id);
      const titleMatch = name.toLocaleLowerCase().includes(needle);
      let match = null;
      try {
        const response = await apiFetch(`/api/messages_json?room_id=${encodeURIComponent(room.id)}`);
        const data = await response.json();
        match = (data.messages || []).find(msg =>
          typeof msg.body === 'string' && msg.body.toLocaleLowerCase().includes(needle));
      } catch (_) { /* A title hit still remains searchable if its log is unavailable. */ }
      if (!titleMatch && !match) continue;
      const body = match ? match.body.replace(/\s+/g, ' ') : '';
      const at = body.toLocaleLowerCase().indexOf(needle);
      const start = Math.max(0, at - 72);
      found.push({id: room.id, name, owner: room.owner, status: room.status,
        title_match: titleMatch, last_activity: room.last_activity || room.created_at,
        message_id: match ? match.id : null,
        snippet: body ? (start ? '…' : '') + body.slice(start, start + 190) +
          (start + 190 < body.length ? '…' : '') : ''});
    }
  }
  await Promise.all(Array.from({length: Math.min(4, snapshot.length)}, worker));
  found.sort((a, b) => Number(b.title_match) - Number(a.title_match) ||
    (b.last_activity || 0) - (a.last_activity || 0));
  return found.slice(0, 100);
}

// ── Phase 1: agent live event panels ─────────────────────────────────────────

let agentStreams = {};  // {agentName: reconnect state}
let activityStreamGeneration = 0;

function closeAgentStreams() {
  activityStreamGeneration += 1;
  for (const k in agentStreams) {
    const stream = agentStreams[k];
    stream.cancelled = true;
    try { stream.controller?.abort(); } catch(e) {}
    if (stream.retryTimer) clearTimeout(stream.retryTimer);
    if (stream.retryResolve) stream.retryResolve();
  }
  agentStreams = {};
  resetActivityPanel(t('activity.hint'));
}

function parseSSEText(state, text, finish = false) {
  state.buffer += text;
  const events = [];
  const consumeLine = line => {
    if (line.endsWith('\r')) line = line.slice(0, -1);
    if (line === '') {
      if (state.data.length) {
        events.push({
          event: state.event || 'message',
          data: state.data.join('\n'),
          id: state.lastEventId,
          generation: state.fileGeneration,
          cursor: state.fileCursor,
        });
      }
      state.event = '';
      state.data = [];
      state.fileGeneration = '';
      state.fileCursor = '';
      return;
    }
    if (line.startsWith(':')) return;
    const colon = line.indexOf(':');
    const field = colon < 0 ? line : line.slice(0, colon);
    let value = colon < 0 ? '' : line.slice(colon + 1);
    if (value.startsWith(' ')) value = value.slice(1);
    if (field === 'event') state.event = value;
    else if (field === 'data') state.data.push(value);
    else if (field === 'id' && !value.includes('\0')) state.lastEventId = value;
    else if (field === 'generation') state.fileGeneration = value;
    else if (field === 'cursor') state.fileCursor = value;
  };
  while (state.buffer) {
    const lf = state.buffer.indexOf('\n');
    const cr = state.buffer.indexOf('\r');
    let newline = lf < 0 ? cr : cr < 0 ? lf : Math.min(lf, cr);
    if (newline < 0) break;
    // A CR at a chunk boundary may be the first half of CRLF.
    if (!finish && state.buffer[newline] === '\r' && newline === state.buffer.length - 1) break;
    consumeLine(state.buffer.slice(0, newline));
    const width = state.buffer[newline] === '\r' && state.buffer[newline + 1] === '\n' ? 2 : 1;
    state.buffer = state.buffer.slice(newline + width);
  }
  if (finish) {
    // A transport EOF is not an SSE frame delimiter. Discard any pending
    // partial event so its id/offset is not committed; reconnect resumes from
    // the last event that ended with an actual blank line.
    state.buffer = '';
    state.event = '';
    state.data = [];
    state.lastEventId = '';
    state.fileGeneration = '';
    state.fileCursor = '';
  }
  return events;
}

function streamIsCurrent(name, stream) {
  return !stream.cancelled && stream.generation === activityStreamGeneration
    && stream.roomId === currentRoom && agentStreams[name] === stream;
}

function reconnectDelay(stream, delayMs) {
  return new Promise(resolve => {
    stream.retryResolve = resolve;
    stream.retryTimer = setTimeout(resolve, delayMs);
  }).finally(() => {
    stream.retryTimer = null;
    stream.retryResolve = null;
  });
}

async function streamAgentEvents(baseUrl, name, stream) {
  const status = () => document.getElementById(`agent-status-${name}`);
  try {
    while (streamIsCurrent(name, stream)) {
      const controller = new AbortController();
      stream.controller = controller;
      try {
      const separator = baseUrl.includes('?') ? '&' : '?';
      let url = `${baseUrl}${separator}offset=${encodeURIComponent(stream.offset)}`;
      if (stream.fileGeneration) {
        url += `&generation=${encodeURIComponent(stream.fileGeneration)}`;
      }
      if (stream.fileCursor) {
        url += `&cursor=${encodeURIComponent(stream.fileCursor)}`;
      }
        const resp = await apiFetch(url, {
          headers: {'Accept': 'text/event-stream'},
          cache: 'no-store',
          signal: controller.signal,
        });
        if (!resp.body) throw new HuddleHTTPError(502, 'Streaming response has no body');
        const reader = resp.body.getReader();
        const decoder = new TextDecoder('utf-8');
        const parser = {
          buffer: '', event: '', data: [], lastEventId: '',
          fileGeneration: '', fileCursor: '',
        };
        while (streamIsCurrent(name, stream)) {
          const {value, done} = await reader.read();
          const events = parseSSEText(
            parser, decoder.decode(value || new Uint8Array(), {stream: !done}), done,
          );
          for (const event of events) {
            if (!streamIsCurrent(name, stream)) return;
            const parsedOffset = /^\d+$/.test(event.id || '') ? Number(event.id) : null;
            if (event.event === 'open' || event.event === 'reset') {
              if (/^[0-9a-f]{64}$/.test(event.generation || '')) {
                stream.fileGeneration = event.generation;
              }
              if (/^[0-9a-f]{64}$/.test(event.cursor || '')) {
                stream.fileCursor = event.cursor;
              }
              if (Number.isSafeInteger(parsedOffset)) stream.offset = parsedOffset;
              const node = status();
              if (node) {
                const outcome = swarmAgentOutcome(roomData, name);
                if (outcome) {
                  node.textContent = swarmOutcomeText(outcome);
                } else {
                  node.textContent = event.event === 'open'
                    ? `● ${t('activity.liveStatus')}`
                    : `↻ ${t('activity.resetStatus')}`;
                }
              }
            } else if (event.event === 'error') {
              const node = status();
              if (node) node.textContent = `× ${t('activity.errorStatus')}`;
              if (event.data) {
                appendAgentEvent(name, JSON.stringify({type: 'error', error: event.data}));
              }
            } else {
              if (Number.isSafeInteger(parsedOffset)) {
                if (parsedOffset <= stream.offset) continue;
                stream.offset = parsedOffset;
              }
              if (/^[0-9a-f]{64}$/.test(event.cursor || '')) {
                stream.fileCursor = event.cursor;
              }
              appendAgentEvent(name, event.data);
              stream.attempt = 0;
            }
          }
          if (done) break;
        }
      } catch(e) {
        if (!streamIsCurrent(name, stream) || controller.signal.aborted
            || (e && e.name === 'AbortError')) return;
        if (e && e.status === 401) {
          const node = status();
          if (node) node.textContent = `× ${t('activity.authStatus')}`;
          showAuthRequired('Authentication expired. Enter the token to reconnect.');
          return;
        }
      }
      if (!streamIsCurrent(name, stream)) return;
      stream.attempt = Math.min((stream.attempt || 0) + 1, 5);
      const delayMs = Math.min(1000 * (2 ** (stream.attempt - 1)), 10000);
      const node = status();
      if (node) {
        const outcome = swarmAgentOutcome(roomData, name);
        if (outcome) {
          node.textContent = swarmOutcomeText(outcome);
        } else {
          node.textContent = `↻ ${t('activity.retrying')} · ${delayMs / 1000}s`;
        }
      }
      await reconnectDelay(stream, delayMs);
    }
  } finally {
    if (agentStreams[name] === stream) delete agentStreams[name];
  }
}

function resetActivityPanel(emptyHint) {
  const panel = document.getElementById('activity-panel');
  if (!panel) return null;
  panel.innerHTML = '';
  if (emptyHint) {
    panel.appendChild(el('div', {class: 'activity-empty'}, [
      el('div', {class: 'activity-empty-title', text: t('activity.title')}),
      el('div', {class: 'activity-empty-hint', text: emptyHint}),
    ]));
  }
  return panel;
}

async function attachAgentPanels(roomId) {
  const attachGeneration = activityStreamGeneration;
  let resp;
  try {
    resp = await apiFetch('/api/room_agents?room_id=' + encodeURIComponent(roomId));
  } catch(e) {
    if (roomId === currentRoom && attachGeneration === activityStreamGeneration) {
      resetActivityPanel(t('activity.loadAgentsFailed'));
    }
    return;
  }
  if (roomId !== currentRoom || attachGeneration !== activityStreamGeneration) return;
  const {agents, health} = await resp.json();
  if (roomId !== currentRoom || attachGeneration !== activityStreamGeneration) return;
  const spawned = agents || {};
  const healthMap = health || {};
  if (roomId === currentRoom) lastHealth = healthMap;
  const panel = resetActivityPanel(null);
  if (!panel) return;

  // Show EVERY participant — not just huddle-spawned ones. The room owner
  // (Claude) has no spawned process / event log, but the user still wants to
  // see that it is in the room and its online/busy status.
  const room = (roomData && roomData.id === roomId) ? roomData : (rooms.find(x => x.id === roomId) || roomData || {});
  const seen = new Set();
  const participants = [];
  for (const p of (room.participants || [])) { if (!seen.has(p)) { seen.add(p); participants.push(p); } }
  for (const p of Object.keys(spawned)) { if (!seen.has(p)) { seen.add(p); participants.push(p); } }

  if (!participants.length) {
    resetActivityPanel(t('activity.noParticipants'));
    return;
  }

  const wrap = el('div', {class: 'agent-panels', id: 'agent-panels'});
  wrap.appendChild(el('div', {class: 'agent-panels-header', text: t('activity.liveTitle')}));
  const scroll = el('div', {class: 'agent-panels-scroll'});
  wrap.appendChild(scroll);

  for (const name of participants) {
    const isSpawned = !!spawned[name];
    const outcome = swarmAgentOutcome(room, name);
    const isFailed = outcome === 'failed';
    const healthSpan = el('span', {class: 'agent-panel-health', id: `agent-health-${name}`});

    const summary = el('summary', {class: 'agent-panel-summary'}, [
      avatar(name, 'avatar-sm'),
      el('span', {class: 'agent-panel-name', text: name}),
      el('span', {class: `agent-status-dot ${isFailed ? 'failed' : 'offline'}`, id: `agent-sdot-${name}`,
                  title: outcome ? `${name}: ${swarmOutcomeText(outcome)}` : `${name}: offline`}),
      el('span', {class: 'agent-panel-totals', id: `agent-totals-${name}`, text: ''}),
      el('span', {class: `agent-panel-status${isFailed ? ' failed' : ''}`, id: `agent-status-${name}`,
                  text: outcome ? swarmOutcomeText(outcome) : (isSpawned ? t('activity.pending') : t('activity.noStream'))}),
      healthSpan,
    ]);

    const transcript = buildAgentTranscript(name, roomMessages);
    const body = isSpawned
      ? el('div', {class: 'agent-events', id: `agent-events-${name}`})
      : el('div', {class: 'agent-panel-hint', text: name === room.owner
          ? t('activity.ownerHint') : t('activity.staticHint')});

    const detailsEl = el('details', {
      class: 'agent-panel' + (isSpawned ? '' : ' static'),
      dataset: {agent: name}, open: '', style: `--agent-c:${agentColor(name)}`,
    }, [summary, transcript, body]);
    scroll.appendChild(detailsEl);

    if (isSpawned) {
      const hLabel = activityHealthLabel(healthMap[name]);
      if (hLabel) { healthSpan.textContent = hLabel; healthSpan.classList.add('warn'); }
      const url = `/agents/${encodeURIComponent(roomId)}/${encodeURIComponent(name)}/events`;
      const stream = {
        roomId, generation: activityStreamGeneration, offset: 0, attempt: 0,
        fileGeneration: '',
        fileCursor: '',
        controller: null, retryTimer: null, retryResolve: null, cancelled: false,
      };
      agentStreams[name] = stream;
      streamAgentEvents(url, name, stream);
    }
  }

  panel.appendChild(wrap);
  updateActivityStatuses(lastStatuses);
}

// Check whether an agent has finished their part in a Swarm room.
function isSwarmMemberDone(room, name) {
  const current = room || roomData || (currentRoom ? rooms.find(x => x.id === currentRoom) : null);
  const pilot = current && current.swarm_pilot;
  if (!pilot || typeof pilot !== 'object') return false;
  // A child has its own lifecycle; it is not a pilot member and must not be
  // inferred complete from the team's final phase.
  const children = pilot.children && typeof pilot.children === 'object' ? pilot.children : {};
  if (Object.prototype.hasOwnProperty.call(children, name)) {
    const child = children[name];
    return !!child && child.status === 'exited' && child.returncode === 0;
  }
  const done = pilot.done && typeof pilot.done === 'object' ? pilot.done : {};
  if (name in done) return true;
  if (pilot.phase === 'completed') {
    const members = Array.isArray(pilot.members) ? pilot.members : [];
    if (!members.length || members.includes(name)) return true;
  }
  return false;
}

function isSwarmChildFailed(room, name) {
  const current = room || roomData || (currentRoom ? rooms.find(x => x.id === currentRoom) : null);
  const pilot = current && current.swarm_pilot;
  const children = pilot && pilot.children && typeof pilot.children === 'object' ? pilot.children : {};
  if (!Object.prototype.hasOwnProperty.call(children, name)) return false;
  const child = children[name];
  return !!child && (child.status === 'failed'
    || (child.status === 'exited' && child.returncode !== undefined
      && child.returncode !== null && Number(child.returncode) !== 0));
}

function swarmAgentOutcome(room, name) {
  if (isSwarmChildFailed(room, name)) return 'failed';
  if (isSwarmMemberDone(room, name)) return 'done';
  const current = room || roomData || {};
  if (current.status === 'closed' || current.status === 'resolved') {
    const health = lastHealth[name];
    // A closed room alone is not proof the agent finished. Require the server's
    // process check; retain an explicit failed-exit signal as an error.
    if (health && health.process_state === 'exited') {
      return health.last_wake_failed ? 'failed' : 'done';
    }
  }
  return '';
}

function swarmOutcomeText(outcome) {
  return outcome === 'failed' ? `✗ ${t('activity.failed')}` : t('lanes.done');
}

// Wake-health label for an agent panel (from /api/room_agents `health`).
function activityHealthLabel(h) {
  if (!h) return '';
  if (h.unowned_lease) return `⚠ ${t('activity.unownedLease')}`;
  if (h.stale_lease) return `⚠ ${t('activity.staleLease')}`;
  if (h.last_wake_failed) return `✗ ${t('activity.wakeFailed')}`;
  if (h.wake_fail_count > 0) return `⚠ ${t('activity.wakeFailures')}: ${h.wake_fail_count}`;
  return '';
}

// Refresh the online/busy dot on each agent panel from a status snapshot.
function updateActivityStatuses(statuses) {
  statuses = statuses || {};
  document.querySelectorAll('[id^="agent-sdot-"]').forEach(dot => {
    const name = dot.id.slice('agent-sdot-'.length);
    const outcome = swarmAgentOutcome(roomData, name);
    const isDone = outcome === 'done';
    const isFailed = outcome === 'failed';
    const st = statuses[name] || 'offline';
    const cls = isDone ? 'offline' : isFailed ? 'failed'
      : (st === 'busy' ? 'busy' : st === 'online' ? 'online' : 'offline');
    dot.className = 'agent-status-dot ' + cls;
    dot.title = `${name}: ${outcome ? swarmOutcomeText(outcome) : t(`status.${st}`)}`;

    const statusNode = document.getElementById(`agent-status-${name}`);
    if (statusNode) {
      statusNode.classList.toggle('failed', isFailed);
      if (outcome) {
        statusNode.textContent = swarmOutcomeText(outcome);
      } else if (statusNode.textContent === t('lanes.done')
          || statusNode.textContent === `✗ ${t('activity.failed')}`) {
        statusNode.textContent = agentStreams[name] ? `● ${t('activity.liveStatus')}` : t('activity.pending');
      }
    }
  });
}

// Terminal runners emit a mixture of JSONL, plain stderr and ANSI control
// sequences. Keep the raw record behind an explicit disclosure, while the
// everyday panel shows a short human-readable status or result.
function stripAnsi(value) {
  return String(value == null ? '' : value)
    .replace(/\x1B\][^\x07]*(?:\x07|\x1B\\)/g, '')
    .replace(/\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])/g, '')
    .replace(/\x9B[0-?]*[ -/]*[@-~]/g, '')
    .replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]/g, ' ');
}

function printableDiagnostic(value) {
  return String(value == null ? '' : value)
    .replace(/\x1B/g, '\\x1b')
    .replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]/g, c => `\\x${c.charCodeAt(0).toString(16).padStart(2, '0')}`);
}

function activityTypeLabel(type, object) {
  const key = String(type || '').toLowerCase().replace(/\s+/g, '_');
  if (object && object.agent_message != null) return t('activity.answer');
  if (object && object.item && object.item.type === 'agent_message') return t('activity.answer');
  if (object && object.item && object.item.type === 'mcp_tool_call') return t('activity.tool');
  if (object && object.item && object.item.type === 'command_execution') return t('activity.internal');
  if (/agent[_ .-]?message|message/.test(key)) return t('activity.answer');
  if (/delta|chunk|stream/.test(key)) return t('activity.fragment');
  if (/tool[_ .-]?call|tool|function[_ .-]?call/.test(key)) return t('activity.tool');
  if (/huddle[_ .-]|room[_ .-]|messages?[_ .-]?read|message[_ .-]?post/.test(key)) return t('activity.roomWork');
  if (/thread[._-]?started|turn[._-]?started|spawn|^start|^started/.test(key)) return t('activity.started');
  if (/thread[._-]?completed|turn[._-]?completed|^complete|^completed|finished|success/.test(key)) return t('activity.completed');
  if (/failed|failure/.test(key)) return t('activity.failed');
  if (/error|exception/.test(key)) return t('activity.error');
  if (/rate.?limit|quota/.test(key)) return t('activity.limitReached');
  if (/cancel|abort|stopped/.test(key)) return t('activity.cancelled');
  if (/churn|retry|reconnect/.test(key)) return t('activity.retrying');
  if (/output|stdout|stderr/.test(key)) return t('activity.output');
  if (/item[._-]?completed|step[._-]?completed/.test(key)) return t('activity.step');
  return t('activity.generic');
}

function activityPayload(object) {
  if (!object || typeof object !== 'object') return '';
  const item = object.item && typeof object.item === 'object' ? object.item : null;
  const value = object.agent_message ?? object.message ?? object.delta ?? object.content
    ?? object.text ?? object.error ?? object.reason ?? (item && (item.text || item.message));
  if (value == null || typeof value === 'object') return '';
  return stripAnsi(value).replace(/\s+/g, ' ').trim();
}

function visibleActivityPayload(payload, type, object) {
  if (!payload) return '';
  if (/usage limit|rate.?limit|quota exceeded/i.test(payload)) return t('activity.limitReached');
  if (/failed to parse hooks config|unknown field.*expected/i.test(payload)) return t('activity.setupIssue');
  if (/^(?:item[._-]?completed|step[._-]?completed|thread[._-]?started|turn[._-]?started)$/i.test(type)) return '';
  const answer = (object && (object.agent_message != null
    || (object.item && object.item.type === 'agent_message')))
    || /agent[_ .-]?message|^answer$/i.test(String(type || ''));
  if (!answer) return '';
  if (/^\s*(?:\{|\[|\/Users\/|\/private\/)/.test(payload)) return '';
  return payload.slice(0, 220);
}

function plainActivityLabel(clean) {
  // OpenCode and shell bridges often print a startup banner or a tool call as
  // plain text. Do not leak their command/JSON arguments into the default UI.
  if (/reading additional input from stdin|waiting for (?:additional )?input from stdin/i.test(clean)) {
    return t('activity.agentStarted');
  }
  if (/^>\s*(build|run|start|model)\b/i.test(clean)
      || /\b(model|reasoning)\s*[·:]/i.test(clean)) {
    return t('activity.agentStarted');
  }
  if (/\b(?:huddle|room)_(?:messages?_read|message_post|room_(?:list|create|status))\b/i.test(clean)
      || /\b(?:messages?_read|message_post|room_list|room_create)\b/i.test(clean)) {
    return t('activity.roomWork');
  }
  if (/^\s*(?:tool|function|calling)\b/i.test(clean)) return t('activity.tool');
  if (/^\s*(?:error|failed|failure|exception)\b/i.test(clean)) return t('activity.error');
  // Antigravity/CLI bridges may emit a path or bare room identifier as a
  // transport line. A prose line that mentions a path remains visible.
  if (/^(?:file:\/\/\/|\/(?:Users|private|tmp|var)\/)[^\n]+$/i.test(clean)
      || /^room_[a-z0-9_-]+$/i.test(clean)
      || /^(?:room_id|session_id|request_id)\s*[:=]\s*\S+$/i.test(clean)) {
    return t('activity.internal');
  }
  return '';
}

function activityPresentation(raw) {
  const clean = stripAnsi(raw).trim();
  if (!clean) return null;
  let object = null;
  try { object = JSON.parse(clean); } catch (_) {}
  if (object && typeof object === 'object') {
    const type = object.type || object.event || object.status || '';
    const label = activityTypeLabel(type, object);
    const payload = visibleActivityPayload(activityPayload(object), type, object);
    if (payload === t('activity.limitReached') || payload === t('activity.setupIssue')) {
      return {summary: payload, raw: JSON.stringify(object, null, 2)};
    }
    const suffix = payload ? `: ${payload}` : '';
    return {
      summary: label === t('activity.generic') && !payload ? '' : `${label}${suffix}`,
      raw: JSON.stringify(object, null, 2),
    };
  }
  // Keep unclassified process output out of the readable stream. It remains
  // available in the disclosure below for diagnosis.
  if (/^[\[{]/.test(clean)) {
    return {summary: '', raw};
  }
  const label = plainActivityLabel(clean);
  if (label) return {summary: label, raw};
  return {summary: '', raw};
}

function appendAgentDiagnostic(list, raw) {
  let details = list.querySelector('.agent-diagnostics');
  if (!details) {
    details = el('details', {class: 'agent-diagnostics'}, [
      el('summary', {text: t('activity.detail')}),
      el('pre'),
    ]);
    details._records = [];
    list.appendChild(details);
  }
  details._records.push(printableDiagnostic(raw));
  while (details._records.length > 200) details._records.shift();
  details.querySelector('summary').textContent = `${t('activity.detail')} · ${details._records.length}`;
  details.querySelector('pre').textContent = details._records.join('\n\n');
}

function appendAgentEvent(agentName, raw) {
  const list = document.getElementById(`agent-events-${agentName}`);
  if (!list) return;
  const view = activityPresentation(raw);
  if (!view) return;
  const keepAtEnd = list.scrollHeight - list.scrollTop - list.clientHeight < 32;
  if (view.summary) {
    const line = el('div', {class: 'agent-event'}, [
      el('span', {class: 'agent-event-summary', text: view.summary}),
    ]);
    const diagnostics = list.querySelector('.agent-diagnostics');
    if (diagnostics) list.insertBefore(line, diagnostics);
    else list.appendChild(line);
  }
  appendAgentDiagnostic(list, raw);
  if (keepAtEnd) list.scrollTop = list.scrollHeight;
}

function transcriptMessagesFor(name, messages) {
  return messages.filter(m => m.agent === name || m.to === name || m.to === 'all');
}

function buildAgentTranscript(name, messages) {
  const relevant = transcriptMessagesFor(name, messages || []);
  const content = el('div', {class: 'agent-transcript-scroll'});
  for (const message of relevant) content.appendChild(agentTranscriptMessage(message));
  content.dataset.lastId = String((messages || []).reduce((max, m) => Math.max(max, Number(m.id) || 0), 0));
  return el('details', {class: 'agent-transcript', dataset: {agent: name}}, [
    el('summary', {text: `${t('activity.transcript')} · ${relevant.length}`}),
    content,
  ]);
}

function agentTranscriptMessage(message) {
  return el('article', {class: 'agent-transcript-message'}, [
    el('div', {class: 'agent-transcript-meta', text: `${message.agent || '—'} · ${messageKindLabel(message.kind)} · ${fmtTime(message.timestamp)}`}),
    el('div', {class: 'agent-transcript-body', text: message.body || ''}),
  ]);
}

function updateAgentTranscripts(messages) {
  const additions = (messages || []).slice();
  if (!additions.length) return;
  document.querySelectorAll('.agent-transcript').forEach(transcript => {
    const name = transcript.dataset.agent;
    const content = transcript.querySelector('.agent-transcript-scroll');
    if (!content) return;
    let lastId = Number(content.dataset.lastId) || 0;
    const keepAtEnd = content.scrollHeight - content.scrollTop - content.clientHeight < 32;
    for (const message of additions) {
      const id = Number(message.id) || 0;
      if (id <= lastId) continue;
      if (message.agent === name || message.to === name || message.to === 'all') {
        content.appendChild(agentTranscriptMessage(message));
        const summary = transcript.querySelector('summary');
        if (summary) {
          const count = content.querySelectorAll('.agent-transcript-message').length;
          summary.textContent = `${t('activity.transcript')} · ${count}`;
        }
      }
      lastId = Math.max(lastId, id);
    }
    content.dataset.lastId = String(lastId);
    if (keepAtEnd) content.scrollTop = content.scrollHeight;
  });
}

function renderOne(m) {
  const list = document.getElementById('messages');
  if (!list) return;
  msgMap[m.id] = {id: Number(m.id), body: m.body, agent: m.agent};
  const round = Number(m.round) > 0 ? Number(m.round) : 0;
  if (round !== lastRenderedRound) {
    const label = round ? `${t('round.label')} ${round}` : t('round.discussion');
    list.appendChild(el('div', {class: 'round-divider'}, [el('span', {text: label})]));
    lastRenderedRound = round;
  }

  const isSystem = (m.agent === 'System' || m.kind === 'system' || m.kind === 'close');

  if (isSystem) {
    const div = el('div', {class: 'msg is-system', dataset: {id: String(m.id)}}, [
      el('div', {class: 'msg-system-when', text: `${fmtTime(m.timestamp)} · #${m.id}`}),
      el('div', {class: 'msg-body', text: m.body}),
      replyButton(m),
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
    el('span', {class: 'msg-agent-ident'}, [avatar(m.agent, 'avatar-inline'), el('span', {class: 'msg-name', text: m.agent})]),
    el('span', {class: 'msg-time', text: `${fmtTime(m.timestamp)} · #${m.id}`}),
  ]);
  const kindLine = el('div', {class: 'msg-kind-line'}, [
    el('span', {class: `kind kind-${m.kind}`, text: messageKindLabel(m.kind)}),
    m.to ? el('span', {class: 'msg-to', text: '→ ' + messageRecipientLabel(m.to)}) : null,
    badge,
    replyButton(m),
  ]);

  const bubble = el('div', {class: 'msg-bubble'});
  if (m.reply_to != null) {
    const q = msgMap[m.reply_to];
    const replyName = q ? q.agent : `#${m.reply_to}`;
    const replyAgentColor = q ? agentColor(q.agent) : 'var(--text-muted)';
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
  // Opus skin: book-style head (large initial, name, tiny caption) floated
  // right before the text, below any reply quote, so the text wraps beside it.
  // The skin hides `line` instead; every other skin hides this head, so the
  // author is never exposed twice. "Name (note)" splits into name and caption.
  const nameParts = /^(.+?)\s+\(([^()]+)\)$/.exec(m.agent);
  const drophead = el('div', {class: 'msg-drophead'}, [
    el('span', {class: 'msg-dropcap', 'aria-hidden': 'true', text: agentLetter(m.agent)}),
    el('span', {class: 'msg-drophead-text'}, [
      el('span', {class: 'msg-name', text: nameParts ? nameParts[1] : m.agent}),
      nameParts ? el('span', {class: 'msg-model-note', text: nameParts[2]}) : null,
      el('span', {class: 'msg-time', text: `${fmtTime(m.timestamp)} · #${m.id}`}),
    ]),
  ]);
  const bodyEl = el('div', {class: 'msg-body md'});
  bodyEl.innerHTML = renderMarkdown(m.body);
  bubble.appendChild(drophead);
  bubble.appendChild(bodyEl);

  const div = el('div', {
    class: `msg ${agentCls(m.agent)} kind-${m.kind}`,
    dataset: {id: String(m.id)}, style: `--agent-c:${agentColor(m.agent)}`,
  }, [
    el('div', {class: 'msg-content'}, [line, el('div', {class: 'msg-main'}, [kindLine, bubble])]),
  ]);

  list.appendChild(div);
  list.scrollTop = list.scrollHeight;
}

// Lane state comes only from the live snapshot: explicit phase first, then the
// legacy lease status (servers without `phases`). Idle agents never blink.
const LANE_PHASES = {
  thinking: ['thinking', 'lanes.thinking'], responding: ['responding', 'lanes.responding'],
  working: ['working', 'lanes.working'], starting: ['starting', 'lanes.starting'],
  queued: ['queued', 'lanes.queued'], completed: ['done', 'lanes.done'],
  rate_limited: ['limited', 'lanes.limited'], stuck: ['stuck', 'lanes.stuck'],
  unavailable: ['offline', 'lanes.offline'], online: ['online', 'lanes.online'],
};
const LANE_ACTIVE = new Set(['thinking', 'responding', 'working', 'starting', 'queued']);

function laneState(room, name) {
  if (room.status === 'closed' || room.status === 'resolved') return ['done', t('lanes.done')];
  const st = lastStatuses && lastStatuses[name];
  const known = LANE_PHASES[lastPhases[name]];
  const h = lastHealth[name];
  // Health is fetched once per room open, so honour its end time locally.
  const limited = h && h.rate_limited && (!h.rate_limited_until || h.rate_limited_until > Date.now() / 1000);
  if (limited && (!known || !LANE_ACTIVE.has(known[0]))) return ['limited', t('lanes.limited')];
  if (known) return [known[0], t(known[1])];
  if (st === 'busy') return ['working', t('lanes.working')];
  if (st === 'online') return ['online', t('lanes.online')];
  if (st === 'offline') return ['offline', t('lanes.offline')];
  return ['unknown', t('lanes.unknown')];
}

// One short, verifiable line about what the agent is on: derived from the
// transcript (addressed requests, own posts) and wake-health, never invented.
function laneDetail(room, name, phase, authored) {
  const last = authored[authored.length - 1];
  const open = room.status !== 'closed' && room.status !== 'resolved';
  let pending = null;
  for (let i = roomMessages.length - 1; i >= 0; i--) {
    const m = roomMessages[i];
    if (last && m.id <= last.id) break;
    if (m.kind === 'request' && m.agent !== name && (m.to === name || m.to === 'all')) { pending = m; break; }
  }
  const h = lastHealth[name] || {};
  if (phase === 'limited' && h.rate_limited_until > Date.now() / 1000) {
    return `${t('lanes.until')} ${fmtTime(h.rate_limited_until)}${h.rate_limit_reason ? ' · ' + h.rate_limit_reason : ''}`;
  }
  if (LANE_ACTIVE.has(phase) && pending) return `${t('lanes.pending')} #${pending.id} ${t('lanes.from')} ${pending.agent} · ${fmtTime(pending.timestamp)}`;
  if (open && pending) return `${t('lanes.unanswered')} #${pending.id} ${t('lanes.from')} ${pending.agent}`;
  if (last) return `${t('lanes.last')}: ${messageKindLabel(last.kind)} #${last.id} · ${fmtTime(last.timestamp)}`;
  return t('lanes.noPosts');
}

function roomLanePeople(room) {
  return [...new Set((room.participants || []).filter(p => p !== 'Human' && p !== 'System'))];
}

function renderRoomLanes(room, statuses) {
  const root = document.getElementById('room-lanes');
  if (!root || !room) return;
  const isOpen = room.status !== 'closed' && room.status !== 'resolved';
  const nowSec = Date.now() / 1000;
  // Open rooms re-render once a minute so the "now" edge keeps moving.
  const signature = JSON.stringify([room.id, room.status, roomMessages.length,
    roomMessages.length && roomMessages[roomMessages.length - 1].id, statuses, lastPhases,
    lastHealth, laneCollapsed, LANG, isOpen && Math.floor(nowSec / 60)]);
  if (root.dataset.signature === signature) return;
  root.dataset.signature = signature;
  const expanded = new Set([...root.querySelectorAll('.lane-log[open]')].map(x => x.dataset.agent));
  const people = roomLanePeople(room);
  const states = Object.fromEntries(people.map(name => [name, laneState(room, name)]));
  const tally = {};
  for (const name of people) { const key = states[name][1]; tally[key] = (tally[key] || 0) + 1; }

  // Time axis: first message → now (open) or → last message (closed). A long
  // idle gap is compressed and the now edge is drawn dashed with its real time.
  let first = nowSec, lastTs = nowSec, stampCount = 0;
  for (const m of roomMessages) {
    const stamp = Number(m.timestamp);
    if (!Number.isFinite(stamp) || stamp <= 0) continue;
    if (!stampCount) first = lastTs = stamp;
    else { first = Math.min(first, stamp); lastTs = Math.max(lastTs, stamp); }
    stampCount++;
  }
  const span = Math.max(60, lastTs - first);
  const far = isOpen && nowSec - lastTs > span;
  const axisEnd = isOpen ? (far ? lastTs + span * 0.12 : Math.max(nowSec, lastTs)) : lastTs;
  const pos = ts => `${Math.max(1.5, Math.min(98.5, ((Number(ts) || first) - first) / Math.max(1, axisEnd - first) * 97 + 1.5))}%`;

  root.innerHTML = '';
  const summary = Object.entries(tally).map(([key, n]) => `${key} — ${n}`).join(' · ');
  const toggle = el('button', {class: 'lane-toggle', type: 'button', text: t(laneCollapsed ? 'lanes.expand' : 'lanes.collapse')});
  toggle.onclick = () => { laneCollapsed = !laneCollapsed; renderRoomLanes(roomData, lastStatuses); };
  const range = stampCount ? `${fmtTime(first)} → ${isOpen ? t('lanes.now') : fmtTime(lastTs)}` : '';
  root.appendChild(el('div', {class: 'lanes-head'}, [
    el('span', {text: `${t('lanes.title')}: ${summary || '—'}`}),
    range ? el('span', {class: 'lanes-range', text: range}) : null,
    el('span', {class: 'lanes-legend', text: t('lanes.legend')}),
    toggle,
  ]));
  if (laneCollapsed) return;
  for (const name of people) {
    const authored = roomMessages.filter(m => m.agent === name);
    const relevant = roomMessages.filter(m => m.agent === name || m.to === name || (m.kind === 'request' && m.to === 'all'));
    const [phase, phaseLabel] = states[name];
    const track = el('div', {class: 'lane-track'});
    for (const m of authored) {
      const tick = el('button', {class: `lane-tick kind-${m.kind}`, type: 'button',
        title: `#${m.id} · ${messageKindLabel(m.kind)} · ${fmtTime(m.timestamp)}`,
        'aria-label': `${name}: ${messageKindLabel(m.kind)} #${m.id}`});
      tick.style.left = pos(m.timestamp);
      tick.onclick = () => {
        const target = document.querySelector(`#messages .msg[data-id="${m.id}"]`);
        if (target) { target.scrollIntoView({block: 'center'}); target.classList.add('lane-flash');
          setTimeout(() => target.classList.remove('lane-flash'), 1500); }
      };
      track.appendChild(tick);
    }
    if (isOpen) {
      track.appendChild(el('span', {class: 'lane-now' + (far ? ' is-far' : ''), 'aria-hidden': 'true',
        title: far ? `${t('lanes.nowFar')} ${fmtTime(lastTs)}` : `${t('lanes.now')} ${fmtTime(nowSec)}`}));
    }
    const log = el('details', {class: 'lane-log', dataset: {agent: name}}, [
      el('summary', {text: name}),
      el('div', {class: 'lane-log-body'}, relevant.length
        ? relevant.map(m => el('button', {type: 'button'}, [
          el('span', {class: 'lane-log-meta', text: `${fmtTime(m.timestamp)} · #${m.id} · ${m.agent} · ${messageKindLabel(m.kind)}`}),
          el('span', {class: 'lane-log-message', text: m.body || ''}),
        ]))
        : [el('span', {text: '—'})]),
    ]);
    if (expanded.has(name)) log.open = true;
    log.querySelectorAll('.lane-log-body button').forEach((b, i) => {
      b.onclick = () => { const m = relevant[i]; const target = document.querySelector(`#messages .msg[data-id="${m.id}"]`);
        if (target) target.scrollIntoView({block: 'center'}); };
    });
    const detail = laneDetail(room, name, phase, authored);
    root.appendChild(el('div', {class: 'lane-row' + (LANE_ACTIVE.has(phase) ? ' is-active' : ''),
      style: `--agent-c:${agentColor(name)}`, dataset: {phase}}, [
      log,
      el('span', {class: `lane-phase phase-${phase}`, text: phaseLabel}),
      track,
      el('span', {class: 'lane-detail', text: detail, title: `${detail} · ${messageCount(authored.length)}`}),
    ]));
  }
}

function updateFooterStatus() {
  const target = document.getElementById('footer-room-status');
  if (!target) return;
  if (!roomData) { target.textContent = t('footer.noRoom'); target.classList.remove('is-active'); return; }
  const active = roomLanePeople(roomData).filter(p => LANE_ACTIVE.has(laneState(roomData, p)[0])).length;
  const parts = [roomData.name || roomData.id, t(`status.${roomData.status}`), messageCount(roomMessages.length)];
  if (active) parts.push(`${t('lanes.active')}: ${active}`);
  target.textContent = parts.join(' · ');
  target.classList.toggle('is-active', active > 0);
}

function initStatusBar() {
  const bind = (id, fn) => { const b = document.getElementById(id); if (b) b.onclick = fn; };
  const setFooterTheme = mode => {
    localStorage.setItem('agentbus-theme', mode);
    localStorage.setItem('agentbus-palette', 'default');
    applyPalette('default');
  };
  bind('footer-theme-light', () => setFooterTheme('light'));
  bind('footer-theme-dark', () => setFooterTheme('dark'));
  const saved = Number(localStorage.getItem('agentbus-reading-size') || 100);
  let size = Number.isFinite(saved) ? Math.max(85, Math.min(125, saved)) : 100;
  const applySize = () => {
    document.documentElement.style.setProperty('--reading-scale', String(size / 100));
    document.getElementById('footer-font-value').textContent = `${size}%`;
    localStorage.setItem('agentbus-reading-size', String(size));
  };
  bind('footer-font-down', () => { size = Math.max(85, size - 5); applySize(); });
  bind('footer-font-up', () => { size = Math.min(125, size + 5); applySize(); });
  applySize();
  const applyDensity = dense => {
    document.documentElement.classList.toggle('dense-rows', dense);
    localStorage.setItem('agentbus-density', dense ? 'dense' : 'normal');
  };
  bind('footer-density-normal', () => applyDensity(false));
  bind('footer-density-dense', () => applyDensity(true));
  applyDensity(localStorage.getItem('agentbus-density') === 'dense');
  updateFooterStatus();
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
  const activeRound = room.swarm_pilot && room.swarm_pilot.round || room.current_round;
  const details = [t(`status.${room.status}`)];
  details.push(activeRound ? `${t('room.round')} ${activeRound}` : t('room.noRound'));
  if (room.owner) details.push(`${t('room.owner')} ${room.owner}`);
  details.push(messageCount(roomMessages.length));
  if (room.created_at) details.push(`${t('room.created')} ${fmtDateTime(room.created_at)}`);
  if (room.last_activity) details.push(`${t('room.updated')} ${fmtDateTime(room.last_activity)}`);
  meta.textContent = details.join('  /  ');
  const crumb = document.getElementById('room-crumb');
  if (crumb) crumb.textContent = room.cwd || room.project || '';
  renderSwarmPilot(room);
}

async function fetchMessages(initial) {
  if (!currentRoom) return;
  const requestedRoom = currentRoom;
  try {
    const url = `/api/messages_json?room_id=${encodeURIComponent(requestedRoom)}&since_id=${lastId}`;
    const resp = await apiFetch(url);
    const data = await resp.json();
    // A fetch from the previous room can finish after close/delete or a room
    // switch. It must not resurrect stale room state or move the footer back.
    if (requestedRoom !== currentRoom) return;

    if (data.room) roomData = data.room;
    lastStatuses = data.statuses || {};
    lastPhases = data.phases || {};
    updateActivityStatuses(lastStatuses);

    if (initial) {
      const list = document.getElementById('messages');
      if (list) list.innerHTML = '';
      msgMap = {};
      roomMessages = [];
      lastRenderedRound = null;
    }

    const msgs = data.messages || [];
    for (const m of msgs) renderOne(m);
    roomMessages.push(...msgs);
    updateAgentTranscripts(msgs);
    if (msgs.length) lastId = msgs[msgs.length-1].id;
    if (roomData) {
      renderChatMeta(roomData, lastStatuses);
      renderRoomLanes(roomData, lastStatuses);
      updateFooterStatus();
    }
  } catch(e) {}
}

function replyButton(message) {
  const button = el('button', {
    type: 'button', class: 'message-reply-action', text: t('compose.reply'),
    title: t('compose.reply'), 'aria-label': `${t('compose.reply')} · ${message.agent} · #${message.id}`,
  });
  button.onclick = event => {
    event.preventDefault();
    event.stopPropagation();
    const id = Number(message.id);
    if (!Number.isSafeInteger(id) || id <= 0) return;
    selectedReplyTarget = {id, agent: String(message.agent || '—'), body: String(message.body || '')};
    composerKind = 'comment';
    document.querySelectorAll('.composer-kind').forEach(x => x.setAttribute('aria-pressed', String(x.dataset.kind === 'comment')));
    const hint = document.getElementById('composer-hint');
    if (hint) hint.textContent = '';
    renderComposerReplyTarget();
    const input = document.getElementById('human-inp');
    if (input) input.focus();
  };
  return button;
}

function renderComposerReplyTarget() {
  const slot = document.getElementById('composer-reply-slot');
  if (!slot) return;
  slot.replaceChildren();
  if (!selectedReplyTarget) { slot.hidden = true; return; }
  const target = selectedReplyTarget;
  const title = t('compose.replyTo').replace('{agent}', target.agent).replace('{id}', String(target.id));
  const preview = target.body.length > 120 ? target.body.slice(0, 120) + '…' : target.body;
  const clear = el('button', {
    type: 'button', class: 'composer-reply-cancel', text: t('compose.cancelReply'),
    'aria-label': t('compose.cancelReply'),
  });
  clear.onclick = () => {
    selectedReplyTarget = null;
    renderComposerReplyTarget();
    document.getElementById('human-inp')?.focus();
  };
  slot.hidden = false;
  slot.appendChild(el('div', {class: 'composer-reply-target'}, [
    el('span', {class: 'composer-reply-bar', 'aria-hidden': 'true'}),
    el('span', {class: 'composer-reply-copy'}, [
      el('span', {class: 'composer-reply-title', text: title}),
      el('span', {class: 'composer-reply-preview', text: preview}),
    ]),
    clear,
  ]));
}

async function sendMsg() {
  if (!currentRoom) return;
  const inp = document.getElementById('human-inp');
  const btn = document.getElementById('btn-send');
  const body = inp && inp.value ? inp.value.trim() : '';
  if (!body) return;
  const recipient = document.getElementById('human-to');
  const to = recipient ? recipient.value : 'all';
  inp.disabled = true; btn.disabled = true;
  try {
    await apiFetch('/api/message_post', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({room_id: currentRoom, agent: 'Human', body, kind: composerKind, to,
        ...(selectedReplyTarget ? {reply_to: Number(selectedReplyTarget.id)} : {})}),
    });
    inp.value = '';
    selectedReplyTarget = null;
    renderComposerReplyTarget();
    await fetchMessages(false);
  } catch(e) {
    showRequestError('Failed to send message', e);
  } finally {
    inp.disabled = false; btn.disabled = false;
    inp.focus();
  }
}

function clearSelectedRoom() {
  closeAgentStreams();
  currentRoom = null;
  selectedReplyTarget = null;
  currentOwner = null;
  roomData = null;
  roomMessages = [];
  lastStatuses = {};
  lastPhases = {};
  lastHealth = {};
  lastId = 0;
  agentMetaTotals = {};
  history.replaceState(null, '', `${location.pathname}${location.search}`);
  updateFooterStatus();
}

async function closeRoom() {
  if (!currentRoom || !confirm('Close this room?')) return;
  try {
    await apiFetch('/api/room_close', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({room_id: currentRoom, owner: currentOwner}),
    });
  } catch(e) {
    showRequestError('Failed to close room', e);
    return;
  }
  clearSelectedRoom();
  const chat = document.getElementById('chat-area');
  chat.innerHTML = '';
  chat.appendChild(el('div', {class: 'empty'}, [
    el('div', {class: 'empty-title', text: t('chat.closedTitle')}),
    el('div', {class: 'empty-hint', text: t('chat.pickAnother')}),
  ]));
  relayout();  // hide the activity panel now that no room is open
  await loadRooms();
}

async function deleteRoom() {
  if (!currentRoom) return;
  if (!confirm('Permanently delete this room from disk?\n\nAll messages, agent logs and metadata will be lost. This cannot be undone.')) return;
  try {
    await apiFetch('/api/room_delete', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({room_id: currentRoom, owner: currentOwner}),
    });
  } catch(e) {
    showRequestError('Failed to delete room', e);
    return;
  }
  clearSelectedRoom();
  const chat = document.getElementById('chat-area');
  chat.innerHTML = '';
  chat.appendChild(el('div', {class: 'empty'}, [
    el('div', {class: 'empty-title', text: 'Room deleted'}),
    el('div', {class: 'empty-hint', text: 'Pick another room from the sidebar'}),
  ]));
  relayout();
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
  let data;
  try {
    const resp = await apiFetch(endpoint, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
    data = await resp.json();
  } catch(e) {
    showRequestError(`${label} failed`, e);
    return;
  }
  if (currentRoom) {
    clearSelectedRoom();
    const chat = document.getElementById('chat-area');
    chat.innerHTML = '';
    chat.appendChild(el('div', {class: 'empty'}, [
      el('div', {class: 'empty-title', text: label}),
      el('div', {class: 'empty-hint', text: 'Pick another room from the sidebar'}),
    ]));
    relayout();
  }
  await loadRooms();
  alert(fmtBulkSummary(label, data));
}

async function bulkCloseAll() {
  await bulkAction(
    '/api/rooms_close_all',
    'Закрыть ВСЕ открытые комнаты?\n\nТекущий Huddle отправит SIGTERM только своим точно известным дочерним процессам. Другие живые экземпляры остановят собственные процессы при следующей проверке. Сохранённые PID и процессы владельцев напрямую не затрагиваются.',
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
    '🔥 УДАЛИТЬ ВСЕ КОМНАТЫ?\n\n1. Закрыть все комнаты.\n2. Отправить SIGTERM только точно известным дочерним процессам текущего Huddle; другие живые экземпляры остановят свои процессы при следующей проверке.\n3. Безвозвратно удалить историю и логи.\n\nСохранённые PID и процессы владельцев напрямую не затрагиваются.',
    'Nuke all',
  );
}

// ── Event delegation ──────────────────────────────────────
document.addEventListener('click', e => {
  const item = e.target.closest('.room-item');
  if (item) {
    openRoom(item.dataset.id, item.dataset.owner).then(() => {
      if (item.dataset.messageId) {
        const match = document.querySelector(`#messages .msg[data-id="${item.dataset.messageId}"]`);
        if (match) {
          document.querySelectorAll('#messages .msg.is-search-hit').forEach(x => x.classList.remove('is-search-hit'));
          match.classList.add('is-search-hit', 'lane-flash');
          match.scrollIntoView({block: 'center'});
          setTimeout(() => match.classList.remove('lane-flash'), 1500);
        }
      }
    });
    return;
  }
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
// Below this width there isn't room for sidebar+activity+chat inline, so the
// side panels become overlay drawers and the chat gets the whole width.
const OVERLAY_BREAKPOINT = 900;
// .app padding (14*2) + 4 grid gaps (12*4) between the 5 tracks.
const LAYOUT_GUTTER = 14 * 2 + 12 * 4;

const layout = {
  sidebarW: parseInt(localStorage.getItem('agentbus-sidebar-w') || SIDEBAR_DEFAULT, 10) || SIDEBAR_DEFAULT,
  activityW: parseInt(localStorage.getItem('agentbus-activity-w') || ACTIVITY_DEFAULT, 10) || ACTIVITY_DEFAULT,
  sidebarCollapsed: localStorage.getItem('agentbus-sidebar-collapsed') === '1',
  activityCollapsed: localStorage.getItem('agentbus-activity-collapsed') === '1',
  drawer: null, // overlay mode only: null | 'sidebar' | 'activity'
  // Which panel the user expanded most recently — it wins the fight for space
  // (the OTHER panel is shrunk/railed first), so an explicit expand always works.
  lastExpanded: 'sidebar',
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

  // Activity panel only exists once a room is open (nothing to show otherwise).
  const hasRoom = !!currentRoom;
  if (!hasRoom && layout.drawer === 'activity') layout.drawer = null;

  const sCol = document.querySelector('.sidebar .panel-collapse-btn');
  const aCol = document.querySelector('.activity .panel-collapse-btn');

  if (isOverlay()) {
    // ── Overlay/drawer mode: chat is full-width, panels float over it ──
    main.classList.add('overlay-mode');
    main.classList.remove('sidebar-rail', 'activity-rail', 'activity-gone');
    const sOpen = layout.drawer === 'sidebar';
    const aOpen = hasRoom && layout.drawer === 'activity';
    main.classList.toggle('drawer-sidebar-open', sOpen);
    main.classList.toggle('drawer-activity-open', aOpen);
    document.documentElement.style.setProperty(
      '--drawer-w', Math.min(360, Math.round(window.innerWidth * 0.86)) + 'px');
    if (backdrop) backdrop.hidden = !(sOpen || aOpen);
    // In-panel buttons (inside the open drawer) close it; topbar buttons summon.
    if (sCol) { sCol.textContent = '◧'; sCol.title = t('tip.collapse'); }
    if (aCol) { aCol.textContent = '◨'; aCol.title = t('tip.collapse'); }
    if (sBtn) { sBtn.style.display = ''; sBtn.classList.toggle('active', sOpen); }
    if (aBtn) { aBtn.style.display = hasRoom ? '' : 'none'; aBtn.classList.toggle('active', aOpen); }
    return;
  }

  // ── Wide mode: resizable side-by-side panels, chat protected ──
  main.classList.remove('overlay-mode', 'drawer-sidebar-open', 'drawer-activity-open');
  layout.drawer = null;
  if (backdrop) backdrop.hidden = true;

  const avail = window.innerWidth - LAYOUT_GUTTER;
  const actMax = Math.floor(window.innerWidth * 0.6);
  let sw = layout.sidebarCollapsed ? 0 : clampN(layout.sidebarW, SIDEBAR_MIN, SIDEBAR_MAX);
  let aw = (layout.activityCollapsed || !hasRoom) ? 0 : clampN(layout.activityW, ACTIVITY_MIN, actMax);

  // Protect the chat (min width) by shrinking/railing the side panels. The
  // panel the user expanded MOST RECENTLY is protected — the OTHER one is
  // shrunk first — so an explicit "expand" always succeeds (fixes: expanding
  // the activity panel while the sidebar is wide used to snap it back to rail).
  const shrinkSidebarFirst = layout.lastExpanded === 'activity';
  const shrinkSidebar = () => {
    if (avail - sw - aw < CHAT_MIN && sw > 0) {
      sw = avail - aw - CHAT_MIN;
      if (sw < SIDEBAR_MIN) sw = 0;
    }
  };
  const shrinkActivity = () => {
    if (avail - sw - aw < CHAT_MIN && aw > 0) {
      aw = avail - sw - CHAT_MIN;
      if (aw < ACTIVITY_MIN) aw = 0;
    }
  };
  if (shrinkSidebarFirst) { shrinkSidebar(); shrinkActivity(); }
  else { shrinkActivity(); shrinkSidebar(); }

  const root = document.documentElement;
  const RAIL = 48;
  // Sidebar: collapsed (by user) OR auto-shrunk to nothing → narrow rail; never gone.
  const sidebarRail = layout.sidebarCollapsed || sw === 0;
  // Activity: gone until a room is open; rail when collapsed/auto-shrunk with a room.
  const activityGone = !hasRoom;
  const activityRail = hasRoom && (layout.activityCollapsed || aw === 0);

  main.classList.toggle('sidebar-rail', sidebarRail);
  main.classList.toggle('activity-rail', activityRail);
  main.classList.toggle('activity-gone', activityGone);

  root.style.setProperty('--sb-track', sidebarRail ? RAIL + 'px' : Math.round(sw) + 'px');
  root.style.setProperty('--sb-rsz', sidebarRail ? '0px' : '6px');
  root.style.setProperty('--act-track', activityGone ? '0px' : (activityRail ? RAIL + 'px' : Math.round(aw) + 'px'));
  root.style.setProperty('--act-rsz', (activityGone || activityRail) ? '0px' : '6px');

  // Resizers only make sense when both sides of them are real panels.
  // Keep resizers IN the grid (their track just goes to 0) — display:none would
  // drop them from flow and the panels would reflow into the wrong columns
  // (the bug where a railed activity panel rendered ~2px instead of 48px).
  // Disable pointer-events instead so a collapsed panel can't be resized.
  const sRes = document.getElementById('sidebar-resizer');
  const aRes = document.getElementById('activity-resizer');
  if (sRes) sRes.style.pointerEvents = sidebarRail ? 'none' : '';
  if (aRes) aRes.style.pointerEvents = (activityGone || activityRail) ? 'none' : '';

  // In-panel button is ALWAYS visible: it collapses an open panel and, in the
  // rail state, becomes the single expand button (arrow points to where the
  // panel will grow).
  if (sCol) {
    sCol.textContent = sidebarRail ? '▸' : '◧';
    sCol.title = t(sidebarRail ? 'tip.restoreSidebar' : 'tip.collapse');
  }
  if (aCol) {
    aCol.textContent = activityRail ? '◂' : '◨';
    aCol.title = t(activityRail ? 'tip.restoreActivity' : 'tip.collapse');
  }
  // Wide mode: the rails own collapse/expand, so the topbar toggles are hidden.
  if (sBtn) sBtn.style.display = 'none';
  if (aBtn) aBtn.style.display = 'none';
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
      layout.lastExpanded = 'sidebar';
    } else {
      layout.activityW = clampN(window.innerWidth - e.clientX - 14, ACTIVITY_MIN, Math.floor(window.innerWidth * 0.6));
      layout.lastExpanded = 'activity';
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
    if (!layout.sidebarCollapsed) layout.lastExpanded = 'sidebar';  // expanding → this panel wins space
    persistLayout();
  } else {
    layout.activityCollapsed = !layout.activityCollapsed;
    if (!layout.activityCollapsed) layout.lastExpanded = 'activity';
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

  // In-panel collapse buttons (built into each panel header corner).
  document.querySelectorAll('.panel-collapse-btn').forEach(b => {
    b.onclick = () => togglePanel(b.dataset.side);
  });

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
initRoomSearch();
initStatusBar();
authenticateDashboard(true)
  .then(loadRooms)
  .catch(e => showAuthRequired(e && e.message ? e.message : 'Authentication failed.'));
setInterval(tick, 3000);
