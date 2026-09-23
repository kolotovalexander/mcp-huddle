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

// ── State ─────────────────────────────────────────────────
let currentRoom = null, currentOwner = null, lastId = 0;
let rooms = [], msgMap = {};
let searchResults = null, searchPending = false, searchTimer = null, searchSequence = 0;
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
  if (!SKIN_OPTS.some(o => o.v === skin)) skin = 'glass';
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
  pop.appendChild(buildSettingsRow('set.lang', I18N_LANGS, 'agentbus-lang', () => LANG, setLang, 'tip.lang'));
  pop.appendChild(buildSettingsRow('set.theme', themeOpts(), 'agentbus-theme',
    () => localStorage.getItem('agentbus-theme') || 'auto', applyTheme, 'tip.theme'));
  pop.appendChild(buildSettingsRow('set.skin', SKIN_OPTS, 'agentbus-skin',
    () => localStorage.getItem('agentbus-skin') || 'glass', applySkin, 'tip.skin'));
  pop.appendChild(buildSettingsRow('set.palette', PALETTE_OPTS, 'agentbus-palette',
    () => localStorage.getItem('agentbus-palette') || 'default', applyPalette, 'tip.palette'));
  pop.appendChild(buildSettingsRow('set.roomView', [
    {v: 'latest', label: t('roomView.latest')},
    {v: 'projects', label: t('roomView.projects')},
  ], 'agentbus-room-view', getRoomView, applyRoomView));
  pop.appendChild(el('div', {class: 'set-sep'}));
  pop.appendChild(el('div', {class: 'set-title'}, [el('span', {text: t('set.mcp')})]));
  pop.appendChild(buildCopyRow(t('mcp.endpoint'), origin + '/mcp'));
  pop.appendChild(buildCopyRow(t('mcp.claude'), `claude mcp add --transport http huddle ${origin}/mcp`));
  pop.appendChild(buildCopyRow(t('mcp.codex'), `[mcp_servers.huddle]\nurl = "${origin}/mcp"`));
  pop.appendChild(buildCopyRow(t('mcp.stdio'), 'mcp-huddle'));
  pop.appendChild(el('div', {class: 'set-hint', text: t('mcp.hint')}));

  // ── Environment variables / spawn rules (reference; click a name to copy) ──
  pop.appendChild(el('div', {class: 'set-sep'}));
  pop.appendChild(el('div', {class: 'set-title'}, [el('span', {text: t('set.spawn')}), helpIcon('tip.spawn')]));
  pop.appendChild(buildVarRow('MCP_HUDDLE_READONLY=0', t('var.readonly')));
  pop.appendChild(buildVarRow('~/.mcp-huddle/registry.json', t('var.registryFile')));
  pop.appendChild(buildVarRow('MCP_HUDDLE_SPAWN_REGISTRY', t('var.registryEnv')));
  pop.appendChild(buildVarRow('MCP_HUDDLE_CLAUDE_ENABLED=1', t('var.claude')));
  pop.appendChild(buildVarRow('MCP_HUDDLE_ANTIGRAVITY_ENABLED=1', t('var.antigravity')));
  pop.appendChild(buildVarRow('MCP_HUDDLE_MIMO_ENABLED=0', t('var.mimo')));
  pop.appendChild(buildVarRow('MCP_HUDDLE_TOKEN', t('var.token')));
  pop.appendChild(buildVarRow('MCP_HUDDLE_HOME', t('var.home')));
  pop.appendChild(buildVarRow('PORT', t('var.port')));

  // ── Copy-paste prompt to onboard an agent into huddle ──
  pop.appendChild(el('div', {class: 'set-sep'}));
  pop.appendChild(el('div', {class: 'set-title'}, [el('span', {text: t('set.agentPrompt')}), helpIcon('tip.agentPrompt')]));
  pop.appendChild(buildPromptRow(t('agentPrompt.text').split('{origin}').join(location.origin)));
}

function setLang(lang) {
  if (!I18N[lang]) lang = 'en';
  LANG = lang;
  try { localStorage.setItem('agentbus-lang', lang); } catch (_) {}
  applyI18n();
  const pop = document.getElementById('settings-popover');
  if (pop) buildSettingsPopover(pop);  // rebuild so the popover's own labels update
}

function initSettings() {
  // Apply saved values (head script already set them pre-paint; re-assert).
  applyTheme(localStorage.getItem('agentbus-theme') || 'auto');
  applySkin(localStorage.getItem('agentbus-skin') || 'glass');
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

function roomItem(r, label, indent = 46) {
  const active = r.id === currentRoom;
  return el('div', {
    class: 'room-item' + (active ? ' active' : ''),
    dataset: {id: r.id, owner: r.owner},
    style: `padding-left:${indent}px`,
  }, [
    el('div', {class: 'room-name'}, [
      el('span', {class: `dot dot-${r.status}` + (r.status === 'open' ? ' pulse' : '')}),
      el('span', {text: label}),
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
      ? (isClosed ? t('chat.closed') : t('chat.resolved'))
      : t('chat.placeholder'),
  };
  if (isReadOnly) inputAttrs.disabled = '';
  const input = el('input', inputAttrs);

  const sendAttrs = {class: 'send-btn', id: 'btn-send', text: t('btn.send') + ' ↵'};
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
  history.replaceState(null, '', `${location.pathname}${location.search}#room=${encodeURIComponent(id)}`);
  lastId = 0;
  msgMap = {};
  agentMetaTotals = {};
  closeAgentStreams();  // abort authenticated fetch streams from previous room
  renderRooms();
  buildChatShell(rooms.find(x => x.id === id) || {id});
  await fetchMessages(true);
  await attachAgentPanels(id);  // Phase 1: live agent event stream (Codex / runner agents)
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
  resetActivityPanel('Откроется при выборе комнаты со spawned-агентами');
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
              if (node) node.textContent = event.event === 'open' ? '● live' : '↻ stream reset';
            } else if (event.event === 'error') {
              const node = status();
              if (node) node.textContent = `× ${event.data || 'stream error'}`;
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
          if (node) node.textContent = '× authentication required';
          showAuthRequired('Authentication expired. Enter the token to reconnect.');
          return;
        }
      }
      if (!streamIsCurrent(name, stream)) return;
      stream.attempt = Math.min((stream.attempt || 0) + 1, 5);
      const delayMs = Math.min(1000 * (2 ** (stream.attempt - 1)), 10000);
      const node = status();
      if (node) node.textContent = `↻ reconnecting in ${delayMs / 1000}s`;
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
      el('div', {class: 'activity-empty-title', text: 'Agent activity'}),
      el('div', {class: 'activity-empty-hint', text: emptyHint}),
    ]));
  }
  return panel;
}

async function attachAgentPanels(roomId) {
  let resp;
  try {
    resp = await apiFetch('/api/room_agents?room_id=' + encodeURIComponent(roomId));
  } catch(e) {
    resetActivityPanel('Не удалось загрузить агентов');
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

// Wake-health label for an agent panel (from /api/room_agents `health`).
function activityHealthLabel(h) {
  if (!h) return '';
  if (h.unowned_lease) return '⚠ unowned/unknown lease';
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
    const resp = await apiFetch(url);
    const data = await resp.json();

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
    await apiFetch('/api/message_post', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({room_id: currentRoom, agent: 'Human', body, kind: 'system', to: 'all'}),
    });
    inp.value = '';
    await fetchMessages(false);
  } catch(e) {
    showRequestError('Failed to send message', e);
  } finally {
    inp.disabled = false; btn.disabled = false;
    inp.focus();
  }
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
  currentRoom = null;
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
  closeAgentStreams();
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
  let data;
  try {
    const resp = await apiFetch(endpoint, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
    data = await resp.json();
  } catch(e) {
    showRequestError(`${label} failed`, e);
    return;
  }
  closeAgentStreams();
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
        document.querySelector(`#messages .msg[data-id="${item.dataset.messageId}"]`)?.scrollIntoView({block: 'center'});
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
authenticateDashboard(true)
  .then(loadRooms)
  .catch(e => showAuthRequired(e && e.message ? e.message : 'Authentication failed.'));
setInterval(tick, 3000);
