# AGENTS.md

Checkpoint: a Twitch tool for the Codex x TwitchCon Hackathon. While the streamer is focused, chat messages are saved. At a checkpoint, the streamer answers them out loud, the words appear on an on-stream card, and the bot posts them as a threaded reply under each viewer's message.

**Read `SPEC.md` first.** It is the source of truth for architecture, messages, behaviour and the work packages (WP1–WP7). When asked to "do WPn", implement exactly that package and meet its acceptance criteria. If the spec is wrong or unclear, say so and propose a spec change; don't silently diverge.

## Stack
- Python 3.12, managed with `uv`; the package is `checkpoint/`.
- FastAPI + uvicorn for the server, `httpx` for Helix, and `websockets` for EventSub, Deepgram streaming and the mic page.
- STT: Deepgram Flux (`flux-general-en`, EU endpoint) over plain `websockets`, with no SDK, no local model and no GPU.
- Web: plain HTML/CSS/JS in `web/`, with no framework and no build step. The pages run as OBS browser sources.
- Sounds are synthesized at runtime with the WebAudio API; there are no audio files.
- Dev only: Playwright (Chromium) for headless screenshots (`scripts/shoot.py`).
- **Reused code:** `checkpoint/stt`, `web/mic` and `checkpoint/twitch_api` are adapted from the author's other entry in this hackathon (Minefield). Keep their tested behaviour: reconnects, one-attempt chat sends, token refresh rules. Change names and ports only where needed.

## Commands
```
./scripts/run_all.sh                      # server + STT engine (needs .env)
./scripts/run_all.sh --demo               # no-key demo: debug chat + typed answers, no Twitch, no engine
uv sync --locked                          # install
uv run pytest                             # tests
uv run ruff check . && uv run ruff format .
uv run python -m checkpoint.server        # server (127.0.0.1:8200, mic page at /mic)
uv run python -m checkpoint.stt --mic     # STT engine, live (open http://localhost:8200/mic in Chrome on Windows)
uv run python scripts/shoot.py PAGE       # screenshots of overlay/dock into data/shots/
uv run python scripts/chat_script.py FILE # send a timed list of chat messages from the test accounts
uv run pytest -m live                     # live checks against Twitch (needs .env and data/tokens.json)
```

The default ports are `APP_PORT=8200` and `ENGINE_AUDIO_PORT=8766`. Both can be overridden by environment variables.

## Rules
- **Demo scope:** this is a hackathon demo built in under two days. Prefer the simplest design that is also robust. A dropped connection, a bad message or a slow client never crashes the server or blocks the flow, and recovery happens on its own through reconnects and snapshots. Add no abstractions, config options or edge cases the spec does not ask for. If something needs more complexity, say so first.
- **Never invent an answer:** a reply quotes only the streamer's transcribed words. Do not add any LLM, summary, paraphrase, autocorrect or "cleanup" of the transcript. The only allowed fallbacks are a plain receipt or no reply.
- **Nothing is sent without the streamer:** every reply comes from a card the streamer answered, or from an earlier answer they clicked. Never auto-reply.
- **No STT biasing:** never send chat text, queued questions or any word list to Deepgram (`keyterm`, keywords, prompts).
- **What reaches the stream:** message text appears on the overlay only on the open card and its bounded answered exit (SPEC §8). Blocklisted, deleted or filtered messages never reach the overlay. Never log message text or transcripts at INFO.
- **No persistence:** state lives in memory only. The only runtime file is `data/tokens.json`. Requested engine logs (`--save-hyps`) go under `data/`. Never commit `data/`, tokens, `.env` or API keys.
- **Pure core:** `checkpoint/core` stays pure: no I/O, no network, no clock reads and no mutable process-global state. It is fully unit-tested. All tunable numbers live in `checkpoint/core/config.py`. Entrypoints read env overrides and inject validated config and explicit times.
- **Verify the external APIs:** check Twitch and Deepgram endpoints, scopes, parameters and payloads against https://dev.twitch.tv/docs and https://developers.deepgram.com before coding them, and record the source URL and verification date in a comment. Twitch failures are logged without message text or credentials and never crash or block the flow.
- **Language:** all code, comments, UI text, commit messages and docs are in English.
- **Assets:** assets must be your own or CC0; record their source in `web/assets/CREDITS.md`. No third-party trademarks.

## Twitch accounts
| Role | Login | Token scopes |
| --- | --- | --- |
| Broadcaster (owns the channel and the developer app) | `izleyici0808` | none |
| Bot (moderator in the broadcaster's channel) | `checkpoint_bott` | `user:read:chat`, `user:write:chat` |
| Test viewers | `minefield_test`, `pixelmoth01`, `mossy_otter` | `user:write:chat` |

- **Config:** logins and the app's Client ID are configured in `.env`. Resolve user IDs at runtime with Get Users, and do not hard-code them. The test viewers use the auth aliases `test`, `test2` and `test3`.
- **Before each rehearsal and recording:** refresh the viewer tokens with `uv run python -m checkpoint.twitch_api.auth --account ACCOUNT --refresh`, before starting the server.
- **Device codes:** give device codes to the human, who activates them in their own incognito Chrome. Never open activation in the agent's browser.
- **The agent's built-in browser is logged in as the broadcaster.** On twitch.tv it is read-only: never type in chat, change settings, authorize apps or open the stream key page. Use it to look at the overlay and dock pages, and to watch chat for the bot's replies.

## Dependencies and tests
- No ML, LLM or GPU dependencies.
- `DEEPGRAM_API_KEY` and `DEEPGRAM_REGION` come from `.env`. Never log the key.
- Tests that need a microphone, the network or an API key are marked `@pytest.mark.live` and skipped by default. Playwright tests are marked `@pytest.mark.browser`.
- Fake Twitch in unit tests (`respx` or a fake client). Test the Deepgram mapping with recorded messages from `tests/fixtures/deepgram/`. Never call real APIs from the default test run.
- If you cannot reach dev.twitch.tv, developers.deepgram.com or a package index, say so instead of guessing an API shape.
- Install Playwright's browser with `uv run playwright install chromium` (no `--with-deps`).

## Workflow
- **Tests first:** for each WP, write failing tests from its acceptance criteria in SPEC §7, run them, then implement.
- **Plan before a WP:** before writing code, state the plan for the WP (files, tests, open questions). Wait for approval when the human asks for plan mode.
- **Verify it yourself before asking the human.** The human's time is the bottleneck.
  - Web pages: after a change, run `scripts/shoot.py`, look at the screenshots and fix what is off before reporting.
  - Twitch: once tokens exist, run `uv run pytest -m live` yourself. Use the built-in browser (read-only) to watch the bot's threaded replies in chat.
  - After a rehearsal: read the saved hypotheses (`--save-hyps`) to find cut-off answers or echoes that were quoted, and tune config.
  - Only the human can speak into the mic, configure OBS, complete device-code logins and judge how it feels. Ask for those as a short checklist.
- **Subagents:** parallel work runs only as subagents of the main submission thread, up to three at once. Each uses its own branch and persistent WSL worktree under `../twitch_checkpoint-worktrees/`, touches only assigned files, writes tests first and commits with its WP prefix. No push, merge or live Twitch; tests use fakes and never send real chat. The main thread owns SPEC.md, AGENTS.md, core config, server, shared fixtures, web/assets/ and scripts/shoot.py. Subagents propose changes to shared files in their reports. The main thread reviews and merges at its own commit boundaries, then runs full pytest, ruff and relevant shoot.py captures. Each web subagent reports a real screenshot defect it fixed, with before/after filenames.
- **Commits:** small commits, prefixed with the WP id (e.g. `WP3: threaded replies`). Before finishing a WP, run tests and lint, and report what changed, how it was verified and anything left open.
- **Show-and-tell moment:** end each WP report with one line naming a moment worth a screenshot for the demo video, for example a failing test turning green, a `shoot.py` image or a live reply in chat. The human captures it.

## Review guidelines
When reviewing a diff or a PR, flag these as P0/P1:
- any path where a reply contains text the streamer did not say, or is sent without the streamer's action;
- message text from a blocklisted, deleted or filtered message reaching the overlay, or message text or transcripts logged at INFO;
- chat text or questions sent to Deepgram;
- I/O, network, clock reads or global state in `checkpoint/core`;
- API keys or tokens in code, logs or committed files;
- a Twitch or Deepgram failure that can crash the server or block the flow;
- divergence from SPEC.md without a spec change.
