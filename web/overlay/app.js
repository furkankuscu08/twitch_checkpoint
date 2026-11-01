import { demoFrames } from './demo.js';

const EXIT_TEXT_MS = 650;
const PILL_MS = 300;
const FEEDBACK_MS = 3000;
const params = new URLSearchParams(location.search);
const demo = params.get('demo') === '1';
const fixedStep = demo && params.has('step');
const $ = id => document.getElementById(id);
const model = {
  focus: false, queue: [], card: null,
  stats: { answer_grace_s: 2.5, summary_s: 5 },
  summary: null, summaryUntil: null,
};
let serverOffset = 0;
let frozenNow = null;
let visibleCard = null;
let exitTimer = null;
let toastUntil = 0;
let tickUntil = 0;
let audio = null;
const now = () => frozenNow ?? Date.now() / 1000 + serverOffset;
const color = value => /^#[0-9a-f]{6}$/i.test(value ?? '') ? value : 'var(--cp-accent)';
const string = value => typeof value === 'string' ? value : '';

function element(tag, className, text = '') {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.textContent = text;
  return node;
}

function chime(answered = false) {
  try {
    const Audio = window.AudioContext || window.webkitAudioContext;
    if (!Audio) return;
    audio ??= new Audio();
    if (audio.state === 'suspended') audio.resume().catch(() => {});
    const start = audio.currentTime;
    [answered ? 784 : 523.25, answered ? 1046.5 : 783.99].forEach((frequency, index) => {
      const tone = audio.createOscillator();
      const gain = audio.createGain();
      tone.type = 'sine'; tone.frequency.value = frequency;
      gain.gain.setValueAtTime(0, start + index * .08);
      gain.gain.linearRampToValueAtTime(.035, start + index * .08 + .015);
      gain.gain.exponentialRampToValueAtTime(.001, start + index * .08 + .3);
      tone.connect(gain); gain.connect(audio.destination);
      tone.start(start + index * .08); tone.stop(start + index * .08 + .32);
    });
  } catch { /* A locked-down browser may disable sound; visuals still work. */ }
}

function queueRow(value) {
  if (!value || typeof value.id !== 'string') return null;
  return { id: value.id, user_name: string(value.user_name),
    color: color(value.color), kind: value.kind === 'question' ? 'question' : 'other' };
}

function cleanCard(value) {
  const row = queueRow(value);
  if (!row || typeof value.text !== 'string') return null;
  return { ...row, text: value.text, index: value.index, total: value.total,
    echo_text: string(value.echo_text), final_text: string(value.final_text),
    partial_text: string(value.partial_text),
    grace_until: Number.isFinite(value.grace_until) ? value.grace_until : null };
}

function renderBadge(animateId = null) {
  const rows = model.queue.filter(row => row.id !== model.card?.id);
  $('badge').hidden = !model.focus && !model.card && !visibleCard && !tickUntil;
  $('badge-label').textContent = model.focus ? 'FOCUS' : 'CHECKPOINT';
  $('queue-count').textContent = rows.length;
  $('crystals').replaceChildren(...rows.slice(0, 10).map(row => {
    const crystal = element('span', `crystal${row.id === animateId ? ' new' : ''}`, '◆');
    crystal.style.color = row.color;
    crystal.title = `@${row.user_name}`;
    crystal.setAttribute('aria-label', `@${row.user_name}`);
    return crystal;
  }));
}

function updateWords(node, text) {
  if (node.textContent === text) return;
  const old = node.textContent.match(/\S+|\s+/g) || [];
  const tokens = text.match(/\S+|\s+/g) || [];
  let common = 0;
  while (common < old.length && old[common] === tokens[common]) common++;
  node.replaceChildren(...tokens.map((token, index) => /\S/.test(token)
    ? element('span', `word${index >= common ? ' new' : ''}`, token)
    : document.createTextNode(token)));
}

function updateAnswer() {
  if (!model.card || visibleCard?.exiting || visibleCard?.id !== model.card.id) return;
  $('echo-text').textContent = model.card.echo_text;
  updateWords($('final-text'), model.card.final_text);
  updateWords($('partial-text'), model.card.partial_text);
  $('caret').hidden = !model.card.partial_text;
  const clip = document.querySelector('.answer-clip');
  clip.scrollTop = clip.scrollHeight;
  updateTimers();
}

function clearVisible() {
  clearTimeout(exitTimer); exitTimer = null;
  visibleCard?.node.remove(); visibleCard = null;
  $('card-host').replaceChildren();
}

function showCard(animate = true) {
  if (visibleCard?.exiting) return;
  if (!model.card) { clearVisible(); renderBadge(); return; }
  if (visibleCard?.id === model.card.id) { updateAnswer(); return; }
  clearVisible();
  const card = model.card;
  const node = element('article', `card${animate ? ' opening' : ''}`);
  node.dataset.id = card.id;
  const head = element('header', 'card-head');
  const label = element('div', 'card-label', '◆ CHECKPOINT');
  label.append(element('span', 'card-index', `${card.index}/${card.total}`));
  const viewer = element('div', 'viewer');
  viewer.style.color = card.color;
  viewer.append(element('span', 'viewer-dot'), element('span', 'viewer-name', `@${card.user_name}`));
  head.append(label, viewer);
  const question = element('p', '', card.text); question.id = 'question';
  const area = element('div', 'answer-area');
  const echo = element('p'); echo.id = 'echo-text';
  const clip = element('div', 'answer-clip');
  const line = element('p', 'answer-lines');
  const final = element('span'); final.id = 'final-text';
  const partial = element('span'); partial.id = 'partial-text';
  const caret = element('span'); caret.id = 'caret'; caret.hidden = true;
  line.append(final, partial, caret); clip.append(line);
  const grace = element('div'); grace.id = 'grace'; grace.hidden = true;
  grace.setAttribute('aria-label', 'Answer closing countdown');
  area.append(echo, clip, grace);
  const stamp = element('span', '', '✓'); stamp.id = 'stamp'; stamp.hidden = true;
  stamp.setAttribute('aria-label', 'Answered on stream');
  node.append(head, question, area, stamp);
  $('card-host').append(node);
  if (animate) {
    const flight = element('span', 'crystal-flight', '◆');
    flight.style.color = card.color;
    flight.setAttribute('aria-hidden', 'true');
    $('card-host').append(flight);
    setTimeout(() => flight.remove(), 520);
  }
  visibleCard = { id: card.id, node, exiting: false };
  updateAnswer(); renderBadge();
}

function finishExit(freeze = false) {
  if (!visibleCard?.exiting) return;
  clearVisible();
  showCard();
  const pill = element('div', 'fly-pill', '✓');
  pill.setAttribute('aria-label', 'Answer queued for reply');
  $('card-host').append(pill);
  if (!freeze) setTimeout(() => pill.remove(), PILL_MS);
  renderBadge();
}

function closeCard(id, result, freezeExit) {
  if (model.card?.id === id) model.card = null;
  model.queue = model.queue.filter(row => row.id !== id);
  if (visibleCard?.id === id) {
    if (result === 'answered' && !visibleCard.exiting) {
      visibleCard.exiting = true;
      visibleCard.node.classList.remove('opening');
      visibleCard.node.classList.add('exiting');
      $('stamp').hidden = false;
      $('grace').hidden = true; $('caret').hidden = true;
      chime(true);
      if (!freezeExit) exitTimer = setTimeout(() => finishExit(), EXIT_TEXT_MS);
    } else if (result !== 'answered') {
      clearVisible(); showCard();
    }
  }
  renderBadge();
}

function renderSummary() {
  $('summary').hidden = !model.summary;
  if (!model.summary) return;
  const { answered, earlier, skipped } = model.summary;
  $('summary-counts').textContent = `${answered} answered · ${earlier} from earlier · ${skipped} skipped`;
}

function updateTimers() {
  if (!visibleCard?.exiting && model.card && $('grace')) {
    const until = model.card.grace_until;
    const left = until === null ? 0 : Math.max(0, until - now());
    $('grace').hidden = left <= 0;
    $('grace').dataset.remaining = left.toFixed(3);
    $('grace').style.setProperty('--remaining', `${Math.min(1, left / model.stats.answer_grace_s) * 100}%`);
  }
  if (model.summary && model.summaryUntil !== null && now() >= model.summaryUntil) {
    model.summary = null; renderSummary();
  }
  if (toastUntil && now() >= toastUntil) { toastUntil = 0; $('earlier-toast').hidden = true; }
  if (tickUntil && now() >= tickUntil) {
    tickUntil = 0; $('thread-tick').hidden = true; renderBadge();
  }
}

function receive(event, { freezeExit = false } = {}) {
  if (!event || typeof event !== 'object') return;
  switch (event.type) {
    case 'snapshot': {
      clearVisible();
      model.focus = event.focus === true;
      model.queue = Array.isArray(event.queue) ? event.queue.map(queueRow).filter(Boolean) : [];
      model.card = cleanCard(event.card);
      const stats = event.stats;
      if (stats && Number.isFinite(stats.server_now)) {
        serverOffset = stats.server_now - Date.now() / 1000;
        model.stats = {
          answer_grace_s: stats.answer_grace_s > 0 ? stats.answer_grace_s : 2.5,
          summary_s: stats.summary_s > 0 ? stats.summary_s : 5,
        };
      }
      model.summary = validSummary(stats?.summary);
      model.summaryUntil = Number.isFinite(stats?.summary_until) ? stats.summary_until : null;
      toastUntil = tickUntil = 0;
      $('earlier-toast').hidden = $('thread-tick').hidden = true;
      showCard(false); renderBadge(); renderSummary(); updateTimers();
      break;
    }
    case 'queued': {
      const row = queueRow(event);
      if (row && !model.queue.some(item => item.id === row.id)) {
        model.queue.push(row); renderBadge(row.id);
      }
      break;
    }
    case 'removed':
      model.queue = model.queue.filter(row => row.id !== event.id);
      if (model.card?.id === event.id) model.card = null;
      if (visibleCard?.id === event.id) { clearVisible(); showCard(); }
      renderBadge();
      break;
    case 'card_open': {
      const card = cleanCard(event);
      if (!card) break;
      model.card = card;
      model.summary = null; renderSummary();
      if (card.index === 1) chime();
      showCard(); renderBadge();
      break;
    }
    case 'answer':
      if (model.card?.id === event.id) {
        Object.assign(model.card, {
          echo_text: string(event.echo_text), final_text: string(event.final_text),
          partial_text: string(event.partial_text),
          grace_until: Number.isFinite(event.grace_until) ? event.grace_until : null,
        });
        updateAnswer();
      }
      break;
    case 'card_close': closeCard(event.id, event.result, freezeExit); break;
    case 'reply_sent':
      if (event.is_sent === true) {
        $('thread-tick').textContent = `↗ in @${string(event.user_name)}'s thread`;
        $('thread-tick').hidden = false;
        tickUntil = now() + FEEDBACK_MS / 1000; renderBadge();
      }
      break;
    case 'earlier_sent':
      if (typeof event.user_name === 'string' && Number.isFinite(event.ago_min)) {
        $('earlier-toast').textContent = `📌 @${event.user_name} ← your answer from ${event.ago_min} min ago`;
        $('earlier-toast').hidden = false; toastUntil = now() + FEEDBACK_MS / 1000;
      }
      break;
    case 'summary':
      model.summary = validSummary(event);
      model.summaryUntil = now() + model.stats.summary_s;
      renderSummary();
      break;
    // reply_pending and status are dock-only and never rendered here.
  }
}

function validSummary(value) {
  return value && ['answered', 'earlier', 'skipped'].every(key => Number.isInteger(value[key]) && value[key] >= 0)
    ? { answered: value.answered, earlier: value.earlier, skipped: value.skipped } : null;
}

function connect() {
  let delay = 500;
  const open = () => {
    let socket;
    try { socket = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/overlay`); }
    catch { setTimeout(open, delay); delay = Math.min(delay * 2, 8000); return; }
    socket.onopen = () => { delay = 500; };
    socket.onmessage = message => {
      try { receive(JSON.parse(message.data)); } catch { /* Ignore malformed input. */ }
    };
    socket.onerror = () => socket.close();
    socket.onclose = () => {
      // While disconnected we cannot receive moderation; await a fresh snapshot.
      model.card = null;
      clearVisible(); renderBadge();
      setTimeout(open, delay); delay = Math.min(delay * 2, 8000);
    };
  };
  open();
}

setInterval(updateTimers, 50);
if (demo) {
  const frames = demoFrames();
  window.checkpointDemoStepCount = frames.length;
  document.documentElement.classList.toggle('demo-frozen', fixedStep);
  let index = fixedStep ? Math.max(0, Math.min(frames.length - 1, Number.parseInt(params.get('step'), 10) || 0)) : 0;
  const play = () => {
    frozenNow = fixedStep ? Date.now() / 1000 : null;
    const frame = frames[index];
    const elapsed = Date.now() / 1000 - frames[0].events[0].stats.server_now;
    for (const original of frame.events) {
      const event = structuredClone(original);
      if (event.type === 'snapshot') {
        event.stats.server_now += elapsed;
        if (event.card?.grace_until != null) event.card.grace_until += elapsed;
      }
      receive(event, { freezeExit: fixedStep });
    }
    if (frame.phase === 'pill') finishExit(fixedStep);
    if (!fixedStep) setTimeout(() => {
      index = (index + 1) % frames.length;
      // The live exit already includes the pill; its still frame is not a second close.
      if (frames[index].phase === 'pill') index = (index + 1) % frames.length;
      play();
    }, frame.hold ?? 2300);
  };
  play();
} else {
  window.checkpointDemoStepCount = 0;
  connect();
}
window.checkpointReady = true;
