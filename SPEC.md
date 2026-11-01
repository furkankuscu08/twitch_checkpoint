# Checkpoint: technical spec (draft v2)

> Answer chat out loud. Every viewer gets your own words, threaded under their message.

When a streamer is deep in focused work (coding, drawing, building), chat piles up unanswered. Checkpoint collects those messages while the streamer works. At a checkpoint, the streamer answers them one by one **by voice**, without leaving the work. Streaming speech recognition (Deepgram Flux) writes the spoken answer onto an on-stream card, and the bot posts it as a threaded reply under the viewer's own message: `🎙 answered on stream: "…"`.

**Checkpoint never invents an answer.** There is no LLM, no summary and no rewording. A reply quotes only what the streamer said, and the streamer can cancel the quote before it is sent. AI chat responders answer *in* the streamer's name; Checkpoint only quotes the streamer.

This file is the source of truth for the build. Work packages (WP1–WP7) and their acceptance criteria are in §7.

---

## 1. Components

```
[Windows]  mic ──► mic page (Chrome tab, http://localhost:8200/mic)
                      │  16 kHz mono int16 PCM over WebSocket
                      ▼
[WSL]      STT engine (checkpoint.stt) ──80 ms chunks──► Deepgram Flux (EU endpoint)
                      │                ◄──Update / EndOfTurn──┘
                      │  hypotheses (HTTP POST)
                      ▼
[WSL]      server (checkpoint.server, FastAPI, 127.0.0.1:8200)
              ├── queue + card state (checkpoint.core)
              ├── /overlay  (OBS browser source, WebSocket)
              ├── /dock     (OBS custom dock, WebSocket + POST)
              ├── /mic      (mic page, opened in Chrome on Windows)
              └── Twitch    (EventSub WebSocket: chat in, deletions in; Helix Send Chat Message out)
```

- `APP_PORT` (8200) and `ENGINE_AUDIO_PORT` (8766) differ from Minefield's, so both can run on one machine. Both can be overridden by environment variables.
- Nothing is exposed to the internet: no tunnel, no public page.
- **Reused from Minefield, the author's other entry in this hackathon** (adapted and renamed):
  - the mic page;
  - the STT engine: Flux backend only, with reconnects, heartbeat, hypothesis mapping and recorded-message fixtures;
  - `twitch_api`: device-code auth, token store and refresh, EventSub client, Helix client.
- **New in Checkpoint:** the queue, the card state machine, the reply builder, the earlier-answer matcher and all of the UI.
- **README note:** the README has a "Relation to Minefield" table (shared plumbing vs new parts). The video does not mention Minefield.
- **Visual identity:** Checkpoint has its own palette, font and vocabulary (save-point, not explosions).
- **State:** the server holds all state in memory. The only runtime file is `data/tokens.json`. Requested engine logs (`--save-hyps`) also go under `data/`.

---

## 2. Repository layout

```
AGENTS.md  SPEC.md  README.md  pyproject.toml
checkpoint/
  core/        pure logic, no I/O: config, text utils, queue, card state machine, reply builder, earlier-answer matcher
  core/blocklist.txt
  stt/         engine (from Minefield): mic page WS, Deepgram Flux client, hypotheses, CLI
  server/      FastAPI app, WebSocket hub, dock/overlay/mic routes, Twitch glue
  twitch_api/  (from Minefield) auth, EventSub client, Helix calls
scripts/run_all.sh     server + engine in one command; --demo uses server --demo (debug chat, no keys/Twitch/engine)
                      normal mode forwards extra arguments to the engine, e.g. --save-hyps data/rehearsal.jsonl
scripts/chat_script.py dev only: sends a timed list of chat messages from the test accounts
scripts/shoot.py       dev only: headless screenshots of overlay and dock
web/overlay/  web/dock/  web/mic/  web/assets/   plain HTML/CSS/JS, no build step
tests/        tests/fixtures/deepgram/
data/         gitignored
```

---

## 3. Behaviour

### Focus and checkpoint
- **Focus on/off** (dock) starts and stops collecting messages. Focus also shows the badge on the overlay. Turning focus off keeps the queue. **Clear** removes waiting and captured messages and closes the open card silently, without creating a reply; already pending replies remain unchanged.
- **Checkpoint** (dock) works whenever the queue is not empty, whether focus is on or off. It takes the current queue as this checkpoint's list. Messages arriving during a checkpoint wait for the next one.

### Collecting
A `channel.chat.message` event is queued only if all of these hold:
- `message_type` is `text`.
- The sender is not the bot and not the broadcaster.
- The text does not start with `!`.
- It is not a reply, or it replies to the broadcaster (`reply.parent_user_login`). This keeps viewer-to-viewer replies and "thanks!" replies to the bot out.
- At least one `text` fragment contains a letter. Emote-only messages are skipped, because emote names appear in `message.text` as plain words.
- It contains no URL.
- No word is on the blocklist (§3 Blocklist).
- It is not **noise**: after ignoring emote/mention fragments and @mentions in
  text fragments, drop only when every word is a stopword or a fixed greeting /
  reaction: `hi hello hey heya hiya yo sup howdy lol lul lmao rofl ha gg ggs wp o7
  pog poggers w first chat everyone guys gm gn morning evening night good nice
  thanks thank ty thx welcome bye cya`. Exact repetitions of `ha` also count.
  Any other word keeps the message eligible. Question words and `?` do not exempt
  noise. The fixed set lives separately from matcher stopwords, with no toggle.

**Caps:**
- At most `PER_USER_PENDING` (2) pending messages per viewer; a third one is dropped.
- At most `QUEUE_MAX` (30) messages in total; when the queue is full, new messages are dropped. Both caps include unanswered messages in the active checkpoint, including the open card.
- Dropped messages are logged without their text; noise uses `reason=noise` at DEBUG.

**Order:** questions first, then the rest, each group oldest first. A message is a question if it ends with `?` or starts with a question word (`what, why, how, when, where, who, which, is, are, do, does, can, will, should`) after skipping consecutive leading greeting/reaction words and @mentions. This classification affects ordering and earlier-answer matching only; it does not exempt noise from filtering.

**Stored per message:** `message_id`, `user_login`, `user_name`, `color` (empty → a fallback palette colour chosen by login), `text`, `kind` (question or other), and `received_at`, which comes from an explicit clock passed in.

**Moderation:** a `channel.chat.message_delete` event removes the matching message from the queue. If one of them is the open card, that card closes silently with no reply.

### The card
At a checkpoint the server opens the first message as a **card**. The card shows `@user_name`, the text and "n/total".

**Answer attribution, by word time:**
- A word belongs to the open card if its spoken time (`utt_start_wall + word.start`) is at least `card_opened_at − ATTRIB_SLACK_S` (0.3 s).
- **Next** pressed mid-turn closes the card at that moment, using the live partial currently shown. Words starting before that boundary remain on the old card; words starting at or after it go to the next card. Attribution slack never assigns the same words to both cards. A closed card's reply is not revised by later hypotheses.

So a sentence that runs from "okay, checkpoint" straight into the answer is not lost, and the engine and server share one machine clock.

**Question echo:**
- Streamers often read the question aloud first. The first finished turn on a card is an **echo** only if the question has at least 3 distinct content words, at least `ECHO_MIN_SHARE` (0.6) of the question's content words appear in the turn, and at least `ECHO_MIN_SHARE` of the turn's own content words appear in the question. Both shares use normalized distinct content words. A question followed by a substantive answer in the same turn fails the mostly-question condition and is quoted in full as heard; do not strip its question prefix.
- An echo is shown greyed on the card. It is not quoted, and it does not count as an answer for the grace rule.

**Live answer:** finished turns (minus the echo) plus the latest partial words of the open turn. The overlay shows partial words dimmed.

**Closing:**
- **Auto close:** `ANSWER_GRACE_S` (2.5 s) after an `EndOfTurn`, if no hypothesis with a new `utt_id` has arrived and the answer is not empty. A ring on the card drains during the grace time, so the streamer sees it closing and can keep talking.
- **Next** (dock) closes the card at once. With an empty answer, Next sends a plain receipt; the streamer may have answered by showing something.
- **Skip** (dock) closes the card with no reply.
- **Typed answer** (dock) replaces the voice answer for the open card and closes it. It is for use without a mic, and is never used in the recorded demo.

Closing with an answer stamps ✓ on the card, and the next card opens.

### The reply
When a card closes with ✓, its reply is pending for `CANCEL_WINDOW_S` (5 s). The dock shows the most recent pending reply with a countdown and a **Cancel quote** button. Several replies can be pending at the same time; each has its own id.

- **Quote reply:** `🎙 answered on stream: "<answer>"`. It is sent with `reply_parent_message_id` set to the viewer's `message_id`.
- **Plain receipt:** `🎙 answered on stream, @<user_name>`, also threaded. The name keeps back-to-back receipts from being identical. It is sent instead of a quote when any of these apply:
  - the answer has fewer than `MIN_QUOTE_WORDS` (2) words;
  - a word in the answer is on the blocklist;
  - the streamer pressed Cancel quote.
- **Length:** the quote, including its ellipsis, is cut to `QUOTE_MAX_CHARS` (300) at a word boundary, ending with `…`. The full reply also fits `CHAT_MAX_CHARS`; if no complete word fits, use a plain receipt.
- **The quote's text** is the transcript as heard: the words' text joined with spaces, keeping Flux's own punctuation. No other edits.
- **Sending:**
  - one attempt per reply, with a 5 s total timeout and no retry;
  - a `400` (the parent message was probably deleted) is logged and the reply is dropped, never re-sent unthreaded;
  - failures are logged without the message text and never block the flow.

### Answered earlier
Only a quote reply whose `CANCEL_WINDOW_S` has passed becomes an **answered pair**: question text, the exact quote, and the card-closing time. Before that deadline it is unavailable for earlier-answer matching. Plain receipts never become answered pairs, including short, blocklisted and cancelled answers. At most `ANSWERED_MAX` (50) pairs are kept, and only in memory.

**Matching** (pure):
- When a question is queued, compare it with the answered questions.
- Both texts first skip leading greeting/reaction words and @mentions, then are reduced to lowercased content words: stopwords and punctuation are removed and a simple plural `s` is stripped. Matcher stopwords and echo content-word rules stay unchanged.
- It is a match if they share at least `min(2, |A|, |B|)` content words and the Jaccard overlap is at least `MATCH_MIN_JACCARD` (0.5).
- The best match wins; ties go to the most recent.
- Example that must match: "what editor is that?" and "which editor do you use?".

**On a match:**
- The queue row in the dock gets a chip: `📌 send answer from N min ago`.
- One click replies under the new message with `📌 answered earlier on stream (N min ago): "<quote>"`. It also removes the message from the queue and shows a short toast on the overlay.
- Not clicking leaves the message in the queue as normal.
- Nothing is ever sent without the streamer's click.

### Blocklist
`checkpoint/core/blocklist.txt` is a short list of slurs and profanity, matched on normalized whole words. It is used twice: on incoming messages (a match is never queued, so it never reaches the stream) and on answers (a match sends a plain receipt).

### Summary
When the last card of a checkpoint closes, the overlay shows **CHECKPOINT SAVED ✓** with "N answered · N from earlier · N skipped" for `SUMMARY_S` (5 s). Answered and skipped count cards in this checkpoint; moderation removals are not skips, and replacing a quote with a receipt does not undo an answered card. From earlier counts successful earlier-answer sends since the previous checkpoint summary, including sends while collecting before Checkpoint was pressed. Reset that counter whenever a summary is emitted. Clear ends an active checkpoint with its current counts and a summary, without answering any remaining card.

Overlay and dock hide zero counts and join only positive counts with ` · `, in
answered / from earlier / skipped order. For example: `3 answered · 1 skipped`.
If every count is zero, show only **CHECKPOINT SAVED ✓**, with no empty count line.
The numeric summary payload and counter/reset semantics are unchanged.

---

## 4. Messages and endpoints

### Engine → server
These are the same as Minefield:
- `POST /stt/hypothesis`, with `utt_id`, `seq`, `final`, `words[{text,start,end,prob}]` and `utt_start_wall`;
- `POST /stt/status`, the heartbeat;
- `POST /stt/abort`.

Duplicate or out-of-order `seq` values are ignored. An aborted utterance's partial words are dropped from the live answer.

### Server → overlay/dock
WebSocket `/ws/overlay` and `/ws/dock`, JSON with `type`:

| type | payload | overlay | dock |
| --- | --- | --- | --- |
| `snapshot` | `focus`, `queue:[{id, user_name, color, kind}]`, `card`, `pending`, `stats` | ✓ | ✓ (+ text, earlier matches) |
| `queued` | `id, user_name, color, kind` | ✓ | ✓ (+ text, earlier match) |
| `removed` | `id` (deleted by a moderator) | ✓ | ✓ |
| `card_open` | `id, user_name, color, kind, text, index, total` | ✓ | ✓ |
| `answer` | `id, echo_text, final_text, partial_text, grace_until` | ✓ | ✓ |
| `card_close` | `id, result: answered/skipped/removed` | ✓ | ✓ |
| `reply_pending` | `id, kind: quote/plain, text, send_at` | — | ✓ |
| `reply_sent` | `id, kind, is_sent, user_name` | ✓ (small tick on the badge) | ✓ |
| `earlier_sent` | `id, user_name, ago_min` | ✓ (toast) | ✓ |
| `summary` | `answered, earlier, skipped` | ✓ | ✓ |
| `status` | `mic, engine, deepgram, eventsub` | — | ✓ |

- **What the overlay shows:** crystals only (colour and name). Message text is shown only on the open card, with the short answered-card exit described in §8.
- **Frozen UI contract:** snapshot `card` is null or the `card_open` fields (without `type`) combined with the current `answer` fields. Overlay `pending` is always `[]`; overlay queue rows have no text. Dock queue rows add `text` and `earlier: null | {quote, ago_min}`; `queued` has the same audience-specific row fields. Dock pending rows and `reply_pending` add `message_id` and `user_name` to `id, kind, text, send_at`. Snapshot `stats` is `{server_now, answer_grace_s, cancel_window_s, summary_s, summary, summary_until}`, where summary is null or `{answered, earlier, skipped}`. Dock snapshots also contain `status: {mic, engine, deepgram, eventsub}`, each `connected` or `disconnected`.
- **State refresh:** replacement snapshots update focus, cancellation, summary expiry and changed earlier matches/ages, without replaying animations. `reply_sent.user_name` supports a text-free thread confirmation. Earlier clicks remove the queue row immediately; only a successful send emits `earlier_sent` and increments the counter.
- **Debug delivery:** injected message IDs retain their debug origin, including earlier replies. Production records delivery metadata without calling Helix, reports `is_sent: false`, and does not increment the successful-earlier count. Tests may inject a recorder to inspect intents; suppression has its own test.
- **Local demos:** overlay and dock work with `?demo=1&step=N` without a server, WebSocket or POST. Omitting step runs a timed demo. Both expose `window.checkpointReady` and `window.checkpointDemoStepCount` to screenshot tooling.
- **Snapshots:** every client gets one when it connects. It restores the current state without replaying animations.
- **Client limits:** each client has a queue of at most 100 messages; a full queue drops only that client.
- **Bad input:** malformed input is rejected at the boundary and never crashes the server.

### Dock → server
`POST` routes:
- `/dock/focus {"on":bool}`
- `/dock/checkpoint`
- `/dock/next`
- `/dock/skip`
- `/dock/cancel {"id"}`
- `/dock/clear`
- `/dock/earlier {"id"}`
- `/dock/typed {"text"}`

**Debug only**, behind `CHECKPOINT_DEBUG=1`:
- `/dock/debug/chat {"user","text","color"?,"kind"?}` injects a chat message as if it came from EventSub.
- Replies to injected messages are logged, not sent.

---

## 5. Speech

- **Engine:** taken from Minefield. Plain `websockets`, `flux-general-en`, EU region, `mip_opt_out=true`, default end-of-turn settings (`eot_threshold` 0.7, `eot_timeout_ms` 5000; verified 1 Nov), reconnect with backoff, heartbeat, `--save-hyps`. The engine sends no StartOfTurn: a new turn shows up as a hypothesis with a new `utt_id`.
- **No biasing:** chat text and queued questions are never sent to Deepgram, as `keyterm` or prompts. Checkpoint quotes what was said, not what was expected.
- **Provider failures:** rejected handshakes, including 401/403, keep the engine
  heartbeat alive with Deepgram disconnected and bounded reconnect backoff. They
  never terminate the card server or discard its queue and pending replies.
- **Mic stop/replacement:** when the provider connection is healthy, send its queued PCM and unfinished 80 ms chunk before requesting `ForceEndTurn`, then wait up to `STT_HTTP_TIMEOUT_S` for the actual final hypothesis before closing. Request it even after a previous final when a new Update has not yet arrived. A `FORCE_END_TURN_NO_ACTIVE_TURN` warning completes the drain without inventing a hypothesis. The whole source drain is bounded by `ENGINE_TIMEOUT_S`; a replacement starts its new timeline only after the old session is cleaned up. `CloseStream` alone does not finalize a turn. Failed drains, broken provider connections and lost PCM continuity abort the partial turn; never synthesize a final from an Update. Verified 1 Nov 2026: https://developers.deepgram.com/docs/flux/force-end-turn and https://developers.deepgram.com/docs/flux/close-stream.
- **Answer assembly** (pure, in `core`): word-time attribution, echo detection, final and partial text. Word confidence is never used to edit text.

---

## 6. Twitch

Verify every endpoint, scope and payload against https://dev.twitch.tv/docs before coding, and record URL and date in comments. A review checked the points below on 1 Nov 2026; re-check them while coding.

- **Accounts:**
  - Bot `checkpoint_bott`, a moderator in the broadcaster's channel, with `user:read:chat` and `user:write:chat`.
  - The three test viewers from Minefield, reused, with `user:write:chat`.
  - The broadcaster needs no token.
- **EventSub**, on one WebSocket with Minefield's reconnect rules. Each subscription has the condition `{broadcaster_user_id, user_id: bot}` and needs `user:read:chat`:
  - `channel.chat.message` v1. Fields used: `message_id`, `chatter_user_login`, `chatter_user_name`, `color` (may be empty), `message.text`, `message.fragments[].type`, `message_type` and `reply.parent_user_login`.
  - `channel.chat.message_delete` v1.
- **Send Chat Message:** `POST /helix/chat/messages` as the bot, with `reply_parent_message_id` (optional field; an invalid parent returns 400). The limit is 500 characters. Emote names in the text become emotes. Log `is_sent` and `drop_reason`.
- Twitch adds the parent @mention to the received reply text. The bot submits
  only the specified reply body; the live test checks that exact body before
  sending and the exact body plus Twitch's mention in EventSub. Verified
  1 Nov 2026: https://dev.twitch.tv/docs/chat/irc-migration/.
- **Not used:** clips, markers, polls, pins, whispers, Extensions.

---

## 7. Work packages and acceptance

Each WP is done when its acceptance criteria pass and `uv run pytest` and `uv run ruff check` are clean.

| WP | Scope | Acceptance |
| --- | --- | --- |
| WP2 | **Port from Minefield:** `stt` (Flux only), `web/mic`, `twitch_api`. Rename, new ports, auth for `checkpoint_bott` | Carried-over tests pass under the new names. `python -m checkpoint.twitch_api.auth --account bot` stores a working token. Speaking into the mic page produces hypotheses. |
| WP1 | **`core`:** config, text utils (normalize, content words, blocklist), queue (filters, caps, order, removal), card state machine (attribution, echo, grace close, next, skip, typed), reply builder, earlier-answer matcher, summary counts | Unit tests cover every filter in §3 Collecting, both caps, the question-first order, and removal of a queued message and of the open card. They cover attribution with a turn that starts before the card opens and with Next mid-turn. They check that an echo is not quoted and does not start the grace timer. Grace close happens only after `EndOfTurn` with a non-empty answer, and a new `utt_id` within grace keeps the card open. Next with an empty answer gives a plain receipt; typed text replaces voice. An aborted utterance is dropped. Tests check quote vs plain receipt (short answer, blocklisted, cancelled) and the cut at a word boundary under 500 characters. They check the earlier match: the example pair matches, unrelated questions don't, best match wins and ties go to recency, with `ANSWERED_MAX` pruning. Summary counts are also tested. |
| WP3 | **`server`:** app, WS hub, routes, hypothesis intake, pending replies with per-id cancel, EventSub in (2 subscriptions), threaded replies out, debug chat | With `CHECKPOINT_DEBUG=1` and a fake Twitch client: inject 3 messages, start a checkpoint and post hypotheses, and `card_open`, `answer`, `card_close` and `reply_pending` reach a test WS client. After 5 s the fake client gets a reply with the right `reply_parent_message_id`. `/dock/cancel` sends the plain receipt instead. A delete event removes a queued message and closes the open card silently. A send failure or `400` is logged and the flow continues. Reconnect snapshots and the 100-message client cap work. |
| WP4 | **`web/overlay`**, with the layout in §8: focus badge with crystals, the card with live words, grace ring, ✓ stamp and fly-out to the right edge, reply tick, earlier toast, CHECKPOINT SAVED summary, synthesized chimes. Also `?demo=1` with `&step=N`, and `scripts/shoot.py` | 1920×1080 with a transparent background. It renders every applicable §4 message and shows message text only on the open card or its bounded answered exit (§8). The card stays in the lower right, at most 45% of the width, and never covers the centre. `shoot.py overlay` saves one PNG per demo step, and the card text is readable in it. |
| WP5 | **`web/dock`**, with the layout in §8: status line, focus toggle, Checkpoint/Next/Skip, pending reply with countdown and Cancel quote, queue list with text and earlier-answer chips, typed box, Clear | Works as an OBS custom dock at 400 px without overflow (checked with `shoot.py dock`). |
| WP6 | **Live checks and ops:** `live` test, `scripts/chat_script.py`, `scripts/run_all.sh` (+ `--demo`, using the explicit server `--demo` flag), README with the "Relation to Minefield" table | A `live` test as `test2` (`pixelmoth01`) sends "how long have you been coding?"; the checkpoint answers with typed text "About ten years now, mostly Python.", and the bot's exact quote is threaded under that message (`is_sent: true`, parent ID and quote verified through EventSub). Refresh the viewer token first using the documented `--refresh` command. `chat_script.py FILE` validates the complete JSON list of `{account, delay_s, text}` before network activity, waits each delay after the preceding attempt completes, sends from test/test2/test3 with one attempt each, and prints only success or failure per entry. `run_all.sh --demo` runs without any key or Twitch/engine even with credentials in `.env`, and the README's no-key path works from a fresh clone. Normal mode forwards extra engine arguments such as `--save-hyps`. |
| WP7 | **Polish and rehearsal fixes** | Two uninterrupted checkpoints on the real mic, with 5+ messages each from `chat_script.py`. Check the saved hypotheses for cut-off answers, and tune `ANSWER_GRACE_S` and `ATTRIB_SLACK_S` if needed. |

WP6 launcher acceptance: `scripts/run_all.sh --demo` runs
`python -m checkpoint.server --demo`, which explicitly enables debug intake and
disables Twitch even if `.env` contains credentials. It starts no engine and,
after readiness, seeds three neutral sample questions through the existing
`/dock/debug/chat` endpoint. Normal mode forwards extra CLI arguments to the
engine, including `--save-hyps`. Linux/WSL only. Exit must stop and reap owned
processes and release ports; startup refuses occupied required ports.

WP1 filter acceptance also includes the deliberately narrow noise keep/drop table,
with emote/mention fragments excluded and any unlisted content word preserved.
Question-order and earlier-match tests include greeting/mention prefixes and the
pair "Hi, what editor are you using" / "what editor is that?".
Summary acceptance includes browser event/snapshot tests for hidden zero counts
on both pages, plus demo screenshots with zero-earlier and all-zero summaries.

WP2 includes the minimal pure plumbing config and local server needed for `/mic`
and validated `/stt/hypothesis`, `/stt/status` and `/stt/abort` intake. Intake is
logged at DEBUG without retaining transcripts; WP1 and WP3 extend this base.
WP6 replaces the copied blind-plant helper with `scripts/chat_script.py` and its
fake-only tests; the original remains in the as-is history. WP4 supplies the
screenshot tooling.

**Order:** WP2 → WP1 → WP3 in the main thread, with WP4, WP5 and the WP6 chat script in isolated subagent worktrees after the shared contract is frozen → remaining WP6 → WP7.

---

## 8. UI

**Overlay (1920×1080)**

- **Placement:** the code fills the centre and left of the stream, and the webcam usually sits in the lower right. So everything is anchored to the lower-right corner, beside the webcam, and the code stays readable. Answering without leaving the work is the point.
- **Visual language:** a save point in a game, not alerts. Overlay, dock and mic use the main-owned `web/assets/theme.css`: microphone-derived teal accent, diamond mark, dark surfaces, system fonts, spacing and radii. Subagents propose token changes rather than introducing separate palettes.
- **Answered-card exit:** keep its question and answer visible through the ✓ stamp and shrink for less than one second total, then replace them with a text-free pill for the fly-out. This is a bounded exit of the same card, not a second text surface. Skipped and moderator-removed cards clear their text immediately. A moderation removal also clears an answered exit still in progress.

```
Idle: nothing.
Focus:                                   [⚑ FOCUS ◆◆◆◇ 4]   ┌────────────┐
                                                             │  webcam    │
                                                             └────────────┘
Checkpoint card (crystals fly from the badge into the card):
          ┌───────────────────────────────────────┐          ┌────────────┐
          │ ⚑ CHECKPOINT  1/4             ● @ana  │          │  webcam    │
          │ what editor is that?                  │          │            │
          │ ───────────────────────────────────── │          └────────────┘
          │ "It's VS Code with Tokyo Night, and   │
          │  the font is▌"  (partial words dimmed)│ ◔ grace ring
          └───────────────────────────────────────┘
✓: stamp (150 ms overshoot) → card shrinks to a pill → flies off the right edge, toward chat
   → the badge shows "↗ in @ana's thread" when reply_sent arrives.
Earlier toast (3 s, beside the badge): [📌 @mehmet ← your answer from 12 min ago]
Summary (5 s): ┌ CHECKPOINT SAVED ✓  4 answered · 1 from earlier · 1 skipped ┐
Zero earlier:  ┌ CHECKPOINT SAVED ✓  3 answered · 1 skipped ┐
All zero:      ┌ CHECKPOINT SAVED ✓ ┐
```

- **Words:** they fade in one by one with a caret, and only the last 3 lines are visible.
- **Name chip:** in the viewer's colour.
- **Glyphs:** the dock and earlier toast use the shared diamond for display prefixes unsupported by the system font. Twitch reply payloads keep the specified microphone and pin prefixes unchanged.
- **Sound:** one soft synthesized chime on CHECKPOINT and one on ✓. No idle animation.

**Dock (400 px)**

```
[mic ● stt ● chat ●]                      [Focus ◉]
┌ CARD 1/4 ─ @ana ───────────────────────┐
│ what editor is that?                   │
│ It's VS Code with Tokyo Night…         │
└────────────────────────────────────────┘
[ ✓ NEXT ]  [ SKIP ]              (CHECKPOINT when no card is open)
Sending to @bob in 4s ▓▓▓▓░   [CANCEL QUOTE]
Queue (5)
 ? @mehmet which editor do you use?   [📌 send answer from 12 min ago]
 ? @cy  how long have you…
   @dee nice music
[ type answer… ]                          [Clear]
```

---

## 9. Config defaults

| Name | Default |
| --- | --- |
| `APP_PORT` | 8200 |
| `ENGINE_AUDIO_PORT` | 8766 |
| `DEEPGRAM_REGION` | `eu` |
| `PER_USER_PENDING` | 2 |
| `QUEUE_MAX` | 30 |
| `ATTRIB_SLACK_S` | 0.3 |
| `ECHO_MIN_SHARE` | 0.6 |
| `ANSWER_GRACE_S` | 2.5 |
| `CANCEL_WINDOW_S` | 5 |
| `MIN_QUOTE_WORDS` | 2 |
| `QUOTE_MAX_CHARS` | 300 |
| `CHAT_MAX_CHARS` | 500 |
| `CHAT_SEND_TIMEOUT_S` | 5 |
| `ANSWERED_MAX` | 50 |
| `MATCH_MIN_JACCARD` | 0.5 |
| `SUMMARY_S` | 5 |
| `WS_CLIENT_QUEUE_SIZE` | 100 |
| `WS_SEND_TIMEOUT_S` | 5 |
| `SERVER_TICK_S` | 0.05 |
| `STT_UTTERANCE_HISTORY_SIZE` | 1000 |
| `TWITCH_VALIDATE_S` | 3600 |

---

## Appendix: demo beats (not part of the build; WP4's `?demo=1` follows them)

| Time | Beat |
| --- | --- |
| 0:00–0:15 | Hook: a real Twitch question → the streamer answers aloud while coding → words appear on the card → ✓ → the threaded reply under the viewer's message in real Twitch chat. "Never looked at chat." |
| 0:15–0:30 | The problem: focus streams can't watch chat. Today's fixes are manual chat breaks, or AI bots that invent answers. "Checkpoint only quotes you." |
| 0:30–1:15 | One full checkpoint with 4 cards: two answered, one Skip, and one Cancel quote → plain receipt. CHECKPOINT SAVED. Split screen with Twitch chat. |
| 1:15–1:40 | "Answer once": a viewer repeats a question → dock chip → one click → a reply quoting the earlier answer, "12 min ago". |
| 1:40–2:15 | Under the hood: mic → Flux turns → word-time attribution → card → threaded reply. Safety: no LLM, no biasing, blocklist, deleted messages never reach the stream. |
| 2:15–2:50 | Built with Codex: SPEC work packages, tests first, screenshot self-checks, live test as the test viewer. |
| 2:50–3:00 | Tagline and repo link. |

The test viewers' questions come from `scripts/chat_script.py`; the Devpost "Demo transparency" section says so.
