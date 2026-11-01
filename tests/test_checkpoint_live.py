"""Explicit WP6 acceptance: one real viewer question and its exact threaded quote.

Refresh test2 using the documented CLI before running. Never authorize in pytest.
"""

import asyncio
import os
import socket

import httpx
import pytest
import uvicorn

from checkpoint.core.text import parse_blocklist
from checkpoint.server.__main__ import config_from_env
from checkpoint.server.app import ROOT, create_app
from checkpoint.server.twitch import Twitch
from checkpoint.twitch_api.auth import TOKEN_PATH, TwitchAuth, settings
from checkpoint.twitch_api.helix import Helix

pytestmark = pytest.mark.live

QUESTION = "how long have you been coding?"
ANSWER = "About ten years now, mostly Python."
EXPECTED_REPLY = f'🎙 answered on stream: "{ANSWER}"'


def test_real_question_gets_exact_quote_in_its_twitch_thread():
    if not TOKEN_PATH.exists():
        pytest.skip("Authorize bot and test2 with the CLI first")
    client_id, logins = settings()
    broadcaster = os.environ.get("TWITCH_BROADCASTER_LOGIN", "").strip().lower()
    assert logins["test2"] == "pixelmoth01", "Use the screen-safe test2 account"
    assert logins["bot"] == "checkpoint_bott"
    assert broadcaster, "Configure the broadcaster login"
    config = config_from_env(os.environ)

    async def until(predicate):
        async with asyncio.timeout(30):
            while not predicate():
                await asyncio.sleep(0.02)

    async def scenario():
        async with httpx.AsyncClient(timeout=config.TWITCH_IO_TIMEOUT_S) as twitch_http:
            auth = TwitchAuth(twitch_http, client_id, logins)
            if not {"bot", "test2"}.issubset(auth.read_tokens()):
                pytest.skip("Authorize bot and test2 with the CLI first")
            identity = await auth.validate("test2")
            assert identity["login"] == "pixelmoth01"
            helix = Helix(auth, config, broadcaster)
            observed_replies, outcomes = [], []

            class ObservedTwitch(Twitch):
                async def send(self, intent):
                    result = await super().send(intent)
                    outcomes.append(result)
                    return result

            async def twitch_factory(runtime):
                glue = ObservedTwitch(runtime, helix)
                original = glue.eventsub.on_chat

                def observe(event):
                    # Verified 2026-11-01: reply.parent_message_id identifies
                    # the real thread; bot messages still pass through intake.
                    # https://dev.twitch.tv/docs/eventsub/eventsub-reference/#channel-chat-message-event
                    if event["chatter_user_login"].lower() == logins["bot"]:
                        observed_replies.append(event)
                    original(event)

                glue.eventsub.on_chat = observe
                await glue.run()

            app = create_app(
                config=config,
                bot_login=logins["bot"],
                broadcaster_login=broadcaster,
                blocklist=parse_blocklist(
                    (ROOT / "checkpoint/core/blocklist.txt").read_text()
                ),
                twitch_factory=twitch_factory,
            )
            runtime = app.state.runtime
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            serving = asyncio.create_task(server.serve(sockets=[listener]))
            try:
                await until(lambda: server.started)
                await until(lambda: runtime.status["eventsub"] == "connected")
                async with httpx.AsyncClient(
                    base_url=f"http://127.0.0.1:{port}", timeout=5
                ) as http:
                    assert (
                        await http.post("/dock/focus", json={"on": True})
                    ).is_success
                    # Verified 2026-11-01: user:write:chat, is_sent and message_id;
                    # replies use reply_parent_message_id. Exactly one attempt.
                    # https://dev.twitch.tv/docs/api/reference/#send-chat-message
                    question = await helix.send_message(QUESTION, account="test2")
                    assert question is not None and question["is_sent"] is True
                    parent = question["message_id"]
                    assert isinstance(parent, str) and parent
                    await until(lambda: runtime.core.queue.get(parent) is not None)
                    assert (await http.post("/dock/checkpoint")).is_success
                    # Never answer another viewer accidentally if chat is busy.
                    assert runtime.core.card.message.message_id == parent
                    assert not outcomes, "No reply before the explicit typed action"
                    assert (
                        await http.post("/dock/typed", json={"text": ANSWER})
                    ).is_success
                    pending = next(iter(runtime.core.pending.values()))
                    assert pending.reply.text == EXPECTED_REPLY
                    assert pending.reply.reply_parent_message_id == parent
                    await until(lambda: bool(outcomes))
                    assert len(outcomes) == 1
                    assert outcomes[0] is not None and outcomes[0]["is_sent"] is True
                    reply_id = outcomes[0]["message_id"]
                    await until(
                        lambda: any(
                            event["message_id"] == reply_id
                            for event in observed_replies
                        )
                    )
                    reply = next(
                        e for e in observed_replies if e["message_id"] == reply_id
                    )
                    assert reply["message"]["text"] == EXPECTED_REPLY
                    assert reply["reply"]["parent_message_id"] == parent
                    print(
                        "Threaded reply confirmed from checkpoint_bott to pixelmoth01."
                    )
            finally:
                server.should_exit = True
                try:
                    await asyncio.wait_for(serving, 10)
                finally:
                    listener.close()
                    if not serving.done():
                        serving.cancel()
                        await asyncio.gather(serving, return_exceptions=True)

    asyncio.run(scenario())
