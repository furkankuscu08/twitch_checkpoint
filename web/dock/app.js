const $ = id => document.getElementById(id);
const demo = new URLSearchParams(location.search).get('demo') === '1';
const state = {
  focus: false, queue: [], card: null, pending: [], status: {},
  stats: {answer_grace_s: 2.5, cancel_window_s: 5, summary_s: 5, summary: null, summary_until: null},
};
let serverOffset = 0;
let busy = false;
let connected = demo;
let retry = 500;
const now = () => Date.now() / 1000 + serverOffset;
const setText = (id, text) => { $(id).textContent = text || ''; };
const latestPending = () => [...state.pending].sort((a, b) => b.send_at - a.send_at)[0];

function notice(text, success = false) {
  setText('notice', text);
  $('notice').classList.toggle('success', success);
}

function controls() {
  const hasCard = Boolean(state.card);
  const unavailable = busy || !connected;
  $('focus').disabled = unavailable;
  $('focus').setAttribute('aria-pressed', String(state.focus));
  $('focus').lastElementChild.textContent = state.focus ? '●' : '○';
  $('checkpoint').hidden = hasCard;
  $('checkpoint').disabled = unavailable || state.queue.length === 0;
  $('next').hidden = $('skip').hidden = !hasCard;
  $('next').disabled = $('skip').disabled = unavailable || !hasCard;
  $('typed').disabled = unavailable || !hasCard;
  $('typed-send').disabled = unavailable || !hasCard || !$('typed').value.trim();
  $('clear').disabled = unavailable || (!hasCard && state.queue.length === 0);
  const pending = latestPending();
  $('cancel').disabled = unavailable || !pending || pending.kind !== 'quote' || pending.send_at <= now();
  document.querySelectorAll('.earlier').forEach(button => {
    button.disabled = unavailable || button.dataset.messageId === state.card?.id;
  });
}

function timers() {
  const pending = latestPending();
  if (pending) {
    const seconds = Math.max(0, pending.send_at - now());
    setText('pending-time', seconds > 0 ? `in ${Math.ceil(seconds)}s` : 'now…');
    $('pending-progress').value = Math.min(1, seconds / state.stats.cancel_window_s);
  }
  const grace = state.card?.grace_until;
  $('grace').hidden = !grace;
  if (grace) {
    const seconds = Math.max(0, grace - now());
    setText('grace-count', `${seconds.toFixed(1)}s`);
    $('grace-progress').value = Math.min(1, seconds / state.stats.answer_grace_s);
  }
  if (state.stats.summary && state.stats.summary_until <= now()) {
    state.stats.summary = null;
    $('summary').hidden = true;
  }
  controls();
}

function render() {
  const card = state.card;
  $('empty-card').hidden = Boolean(card);
  $('card-content').hidden = !card;
  setText('card-position', card ? `CARD ${card.index}/${card.total}` : 'SAVE POINT');
  setText('card-name', card ? `@${card.user_name}` : '');
  $('card-name').style.setProperty('--viewer-color', card?.color || 'var(--cp-accent)');
  setText('card-question', card?.text);
  setText('echo', card?.echo_text);
  setText('answer-final', card?.final_text);
  setText('answer-partial', card?.partial_text ? `${card.final_text ? ' ' : ''}${card.partial_text}` : '');
  const pending = latestPending();
  $('pending').hidden = !pending;
  setText('pending-name', pending ? `@${pending.user_name}` : '');
  setText('pending-text', pending?.text?.replace(/^🎙 /u, ''));
  $('pending-text').classList.toggle('voice-reply', Boolean(pending?.text?.startsWith('🎙 ')));
  setText('pending-count', state.pending.length > 1 ? `${state.pending.length} pending` : '');
  setText('queue-count', String(state.queue.length));
  $('empty-queue').hidden = state.queue.length !== 0;
  const queue = [...state.queue].sort((a, b) => Number(b.kind === 'question') - Number(a.kind === 'question'));
  $('queue-list').replaceChildren(...queue.map(message => {
    const item = document.createElement('li');
    item.className = `queue-row${message.id === card?.id ? ' current' : ''}`;
    item.style.setProperty('--viewer-color', message.color || 'var(--cp-accent)');
    const meta = document.createElement('div');
    meta.className = 'queue-meta';
    const kind = document.createElement('span');
    kind.className = 'queue-kind';
    kind.textContent = message.kind === 'question' ? '?' : '◆';
    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = `@${message.user_name}`;
    meta.append(kind, name);
    if (message.id === card?.id) {
      const label = document.createElement('span');
      label.className = 'on-card'; label.textContent = 'ON CARD'; meta.append(label);
    }
    const text = document.createElement('p');
    text.className = 'queue-text'; text.textContent = message.text;
    item.append(meta, text);
    if (message.earlier) {
      const button = document.createElement('button');
      button.className = 'earlier';
      button.dataset.messageId = message.id;
      button.textContent = `◆ send answer from ${message.earlier.ago_min} min ago`;
      button.setAttribute('aria-label', `Send answer from ${message.earlier.ago_min} min ago`);
      button.addEventListener('click', () => perform('earlier', {id: message.id}));
      item.append(button);
    }
    return item;
  }));
  for (const element of document.querySelectorAll('[data-status]')) {
    const status = state.status[element.dataset.status] === 'connected' ? 'connected' : 'disconnected';
    element.dataset.state = status;
    element.title = `${element.dataset.status}: ${status}`;
  }
  $('summary').replaceChildren();
  $('summary').hidden = !state.stats.summary;
  if (state.stats.summary) {
    const label = document.createElement('strong');
    label.textContent = 'CHECKPOINT SAVED ✓';
    const {answered, earlier, skipped} = state.stats.summary;
    $('summary').append(label, `${answered} answered · ${earlier} from earlier · ${skipped} skipped`);
  }
  timers();
}

function receive(event) {
  if (!event || typeof event !== 'object') return;
  switch (event.type) {
    case 'snapshot':
      connected = true;
      if (!demo) setText('connection', 'Connected');
      state.focus = Boolean(event.focus);
      state.queue = event.queue || [];
      state.card = event.card;
      state.pending = event.pending || [];
      state.status = event.status || {};
      state.stats = {...state.stats, ...event.stats};
      if (Number.isFinite(event.stats?.server_now)) serverOffset = event.stats.server_now - Date.now() / 1000;
      break;
    case 'queued':
      state.queue = state.queue.filter(row => row.id !== event.id);
      state.queue.push(event);
      break;
    case 'removed':
      state.queue = state.queue.filter(row => row.id !== event.id);
      if (state.card?.id === event.id) state.card = null;
      break;
    case 'card_open': state.card = event; break;
    case 'answer':
      if (state.card?.id === event.id) state.card = {...state.card, ...event, text: state.card.text};
      break;
    case 'card_close':
      if (state.card?.id === event.id) state.card = null;
      state.queue = state.queue.filter(row => row.id !== event.id);
      break;
    case 'reply_pending':
      state.pending = state.pending.filter(row => row.id !== event.id);
      state.pending.push(event);
      break;
    case 'reply_sent':
      state.pending = state.pending.filter(row => row.id !== event.id);
      notice(event.is_sent ? `Reply saved in @${event.user_name}'s thread.` : 'Reply not sent.', event.is_sent);
      break;
    case 'earlier_sent':
      state.queue = state.queue.filter(row => row.id !== event.id);
      notice(`@${event.user_name} received your answer from ${event.ago_min} min ago.`, true);
      break;
    case 'summary':
      state.stats.summary = event;
      state.stats.summary_until = now() + state.stats.summary_s;
      break;
    case 'status': state.status = event; break;
    default: return;
  }
  render();
}

async function perform(action, data = {}) {
  if (busy || !connected) return;
  busy = true; notice(''); controls();
  try {
    if (demo) demoAction(action, data);
    else {
      const response = await fetch(`/dock/${action}`, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(data), signal: AbortSignal.timeout(5000),
      });
      if (!response.ok) throw new Error('Request failed');
    }
    if (action === 'typed') $('typed').value = '';
  } catch {
    notice('Action failed. Try again.');
  } finally {
    busy = false; controls();
  }
}

$('focus').addEventListener('click', () => perform('focus', {on: !state.focus}));
for (const action of ['checkpoint', 'next', 'skip', 'clear']) {
  $(action).addEventListener('click', () => perform(action));
}
$('cancel').addEventListener('click', () => {
  const pending = latestPending();
  if (pending) perform('cancel', {id: pending.id});
});
$('typed').addEventListener('input', controls);
$('typed-form').addEventListener('submit', event => {
  event.preventDefault();
  if ($('typed').value.trim() && state.card) perform('typed', {text: $('typed').value});
});

function connect() {
  setText('connection', 'Connecting…');
  const socket = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/dock`);
  socket.onopen = () => { retry = 500; setText('connection', 'Syncing…'); controls(); };
  socket.onmessage = event => {
    try { receive(JSON.parse(event.data)); } catch { /* A bad frame must not stop reconnects. */ }
  };
  socket.onerror = () => socket.close();
  socket.onclose = () => {
    connected = false; setText('connection', 'Reconnecting…'); controls();
    setTimeout(connect, retry); retry = Math.min(8000, retry * 2);
  };
}

const demoRows = [
  {id: 'ana', user_name: 'ana', color: 'var(--cp-accent)', kind: 'question', text: 'What editor is that?', earlier: null},
  {id: 'cy', user_name: 'cy', color: 'var(--cp-accent-muted)', kind: 'question', text: 'How do you stay focused while coding?', earlier: null},
  {id: 'dee', user_name: 'dee', color: 'var(--cp-text)', kind: 'other', text: 'Nice music. This is such a calm stream.', earlier: null},
  {id: 'bob', user_name: 'bob', color: 'var(--cp-muted)', kind: 'question', text: 'Which font do you use?', earlier: null},
];
function demoCard() {
  return {...demoRows[0], index: 1, total: 4, echo_text: '', final_text: '', partial_text: '', grace_until: null};
}
function demoSnapshot(step) {
  const time = Date.now() / 1000;
  const event = {
    type: 'snapshot', focus: step !== 0, queue: step ? structuredClone(demoRows) : [], card: null, pending: [],
    stats: {server_now: time, answer_grace_s: 2.5, cancel_window_s: 5, summary_s: 5, summary: null, summary_until: null},
    status: {mic: 'connected', engine: 'connected', deepgram: 'connected', eventsub: 'connected'},
  };
  if (step === 2 || step === 3) event.card = {...demoCard(), echo_text: 'What editor is that?', final_text: "It's VS Code with the system font.", partial_text: step === 2 ? 'I like to keep' : '', grace_until: step === 3 ? time + 2.5 : null};
  if (step === 4 || step === 5) {
    event.queue = event.queue.slice(2);
    event.pending = [
      {id: 'reply-1', message_id: 'ana', user_name: 'ana', kind: 'quote', text: '🎙 answered on stream: "It’s VS Code with the system font."', send_at: time + 3},
      {id: 'reply-2', message_id: 'cy', user_name: 'cy', kind: step === 5 ? 'plain' : 'quote', text: step === 5 ? '🎙 answered on stream, @cy' : '🎙 answered on stream: "I save chat and answer when I reach a checkpoint."', send_at: time + 5},
    ];
  }
  if (step === 6) event.queue = [{...demoRows[0], id: 'mehmet', user_name: 'mehmet', text: 'Which editor do you use?', earlier: {quote: 'It’s VS Code with the system font.', ago_min: 12}}];
  if (step === 7) { event.queue = []; event.stats.summary = {answered: 3, earlier: 1, skipped: 1}; event.stats.summary_until = time + 5; }
  if (step === 8) event.status = {mic: 'disconnected', engine: 'disconnected', deepgram: 'disconnected', eventsub: 'disconnected'};
  return event;
}
function demoAction(action, data) {
  switch (action) {
    case 'focus': state.focus = data.on; break;
    case 'checkpoint': state.card = {...state.queue[0], index: 1, total: state.queue.length}; break;
    case 'next': case 'skip': case 'typed': {
      const card = state.card;
      if (!card) break;
      state.queue = state.queue.filter(row => row.id !== card.id);
      if (action !== 'skip') state.pending.push({id: `demo-${Date.now()}`, message_id: card.id, user_name: card.user_name, kind: 'quote', text: data.text || card.final_text || '🎙 answered on stream', send_at: now() + 5});
      state.card = state.queue.length ? {...state.queue[0], index: (card.index || 1) + 1, total: card.total} : null;
      break;
    }
    case 'cancel': {
      const pending = state.pending.find(row => row.id === data.id);
      if (pending) { pending.kind = 'plain'; pending.text = `🎙 answered on stream, @${pending.user_name}`; }
      break;
    }
    case 'earlier': {
      const row = state.queue.find(row => row.id === data.id);
      if (row) receive({type: 'earlier_sent', id: row.id, user_name: row.user_name, ago_min: row.earlier.ago_min});
      break;
    }
    case 'clear': state.queue = []; state.card = null; break;
  }
  render();
}

window.checkpointDemoStepCount = 9;
if (demo) {
  $('demo-label').hidden = false; setText('connection', 'Demo · local preview');
  const query = new URLSearchParams(location.search);
  let step = Math.max(0, Math.min(8, Number(query.get('step')) || 0));
  receive(demoSnapshot(step));
  if (!query.has('step')) setInterval(() => { step = (step + 1) % 9; receive(demoSnapshot(step)); }, 4500);
} else {
  render(); connect();
}
setInterval(timers, 100);
window.checkpointReady = true;
