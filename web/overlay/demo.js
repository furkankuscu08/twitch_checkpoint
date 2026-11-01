/* Original scripted demo fixtures. They never connect to a server or provider. */
export function demoFrames() {
  const now = Date.now() / 1000;
  const accent = getComputedStyle(document.documentElement).getPropertyValue('--cp-accent').trim();
  const rows = ['Ana', 'Bob', 'Cy', 'Dee'].map((user_name, index) => ({
    id: `demo-${index + 1}`, user_name, color: accent, kind: index === 3 ? 'other' : 'question',
  }));
  const questions = [
    'What editor and colour theme do you use?',
    'How do you keep your focus while chat is moving?',
    'Can you show your keyboard shortcuts?',
    'That save point sound is lovely.',
  ];
  const card = (index = 0) => ({ type: 'card_open', ...rows[index], text: questions[index], index: index + 1, total: 4 });
  const answer = (index = 0, changes = {}) => ({ type: 'answer', id: rows[index].id, echo_text: '', final_text: '', partial_text: '', grace_until: null, ...changes });
  const snapshot = (changes = {}) => ({
    type: 'snapshot', focus: true, queue: rows, card: null, pending: [],
    stats: { server_now: now, answer_grace_s: 2.5, cancel_window_s: 5, summary_s: 5, summary: null, summary_until: null },
    ...changes,
  });
  const open = (index = 0, changes = {}) => snapshot({ queue: rows.slice(index), card: { ...card(index), ...answer(index, changes) } });
  const text = "It's VS Code with my own dark theme, and the font is a system monospace.";
  return [
    { label: 'Idle', events: [snapshot({ focus: false, queue: [] })], hold: 1000 },
    { label: 'Focus', events: [snapshot({ queue: [] })], hold: 1400 },
    { label: 'Four saved messages', events: [snapshot()], hold: 1800 },
    { label: 'Open checkpoint', events: [snapshot(), card()], hold: 1800 },
    { label: 'Question echo — not quoted', events: [open(0, { echo_text: questions[0] })], hold: 2400 },
    { label: 'Words as spoken', events: [open(0, { echo_text: questions[0], final_text: "It's VS Code with my own dark theme,", partial_text: 'and the font is' })], hold: 2800 },
    { label: 'Finished answer and grace', events: [open(0, { final_text: text, grace_until: now + 2.5 })], hold: 1800 },
    { label: 'Answered stamp', events: [open(0, { final_text: text }), { type: 'card_close', id: rows[0].id, result: 'answered' }], hold: 1100 },
    { label: 'Text-free reply pill', phase: 'pill', events: [open(0, { final_text: text }), { type: 'card_close', id: rows[0].id, result: 'answered' }], hold: 1000 },
    { label: 'Second card', events: [open(1, { partial_text: 'I save the questions while I work.' })], hold: 2200 },
    { label: 'Second answer', events: [open(1, { final_text: 'I save the questions while I work, then answer at a checkpoint.' }), { type: 'card_close', id: rows[1].id, result: 'answered' }], hold: 1100 },
    { label: 'Skip with no reply', events: [open(2), { type: 'card_close', id: rows[2].id, result: 'skipped' }], hold: 1400 },
    { label: 'Fourth card', events: [open(3, { final_text: 'Thank you for watching and keeping me company.' })], hold: 2000 },
    { label: 'Cancel quote — plain receipt', events: [open(3, { final_text: 'Thank you for watching and keeping me company.' }), { type: 'card_close', id: rows[3].id, result: 'answered' }], hold: 1100 },
    { label: 'Checkpoint saved', events: [snapshot({ queue: [] }), { type: 'summary', answered: 3, earlier: 0, skipped: 1 }], hold: 3000 },
    { label: 'Repeated question waits for a click', events: [snapshot({ queue: [{ ...rows[0], id: 'repeat', user_name: 'Mehmet' }] })], hold: 1800 },
    { label: 'Earlier answer sent', events: [snapshot({ queue: [] }), { type: 'earlier_sent', id: 'repeat', user_name: 'Mehmet', ago_min: 12 }], hold: 2600 },
    { label: 'Threaded reply confirmation', events: [snapshot({ queue: [] }), { type: 'reply_sent', id: 'reply-1', user_name: 'Ana', kind: 'quote', is_sent: true }], hold: 2200 },
    { label: 'Answer once summary', events: [snapshot({ queue: [] }), { type: 'summary', answered: 3, earlier: 1, skipped: 1 }], hold: 3000 },
  ];
}
