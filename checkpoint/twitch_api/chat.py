"""Chat commands mutate the game synchronously; external delivery is queued."""

import asyncio
import logging

import httpx

from .auth import AuthError
from .eventsub import EventSub

LOG = logging.getLogger(__name__)
NETWORK_ERRORS = (
    AuthError,
    httpx.HTTPError,
    TimeoutError,
    OSError,
    ValueError,
    KeyError,
    TypeError,
    IndexError,
)


class Twitch:
    def __init__(self, runtime, helix):
        self.runtime = runtime
        self.helix = helix
        self.config = runtime.config
        self.queue = asyncio.Queue(self.config.TWITCH_CHAT_QUEUE_SIZE)
        self.eventsub = EventSub(helix, self.config, self.handle_chat, self.health)

    def health(self, value):
        if self.runtime.status["eventsub"] != value:
            self.runtime.status["eventsub"] = value
            self.runtime.hub.broadcast(dict(self.runtime.status), "dock")

    def enqueue(self, text):
        try:
            self.queue.put_nowait(text)
        except asyncio.QueueFull:
            LOG.warning("Twitch chat queue full; outgoing message dropped")

    def handle_chat(self, event):
        if event["chatter_user_id"] == self.helix.bot_id:
            return
        parts = event["message"]["text"].split()
        if not parts:
            return
        command = parts[0].lower()
        owner = event["chatter_user_login"]
        if command == "!mine" and len(parts) <= 2:
            if len(parts) == 1:
                url = self.runtime.status["tunnel_url"]
                if not self.runtime.game.running:
                    reply = "No active round"
                elif not url:
                    reply = "Plant page unavailable"
                else:
                    reply = f"pick your mine: {url}"
            else:
                try:
                    events = self.runtime.plants.redeem(
                        parts[1], owner, self.runtime.clock()
                    )
                    self.runtime.emit(events)
                    reply = "mine planted 💣 arming…"
                except ValueError as error:
                    # Core rejection strings contain no selected word or input.
                    reply = str(error)
            self.enqueue(f"@{owner} {reply}")
        elif command == "!defuse" and len(parts) == 1:
            if any(
                badge["set_id"] in {"broadcaster", "moderator"}
                for badge in event["badges"]
            ):
                self.runtime.emit(self.runtime.game.false_alarm(self.runtime.clock()))
            else:
                self.enqueue(f"@{owner} Only broadcaster or moderators can defuse")

    async def send_loop(self):
        while True:
            text = await self.queue.get()
            try:
                await self.helix.send_message(text)
            except NETWORK_ERRORS:
                LOG.warning("Twitch chat failed; message will not be retried")
            finally:
                self.queue.task_done()

    async def validate_loop(self):
        # Verified 2026-10-30: startup validation and hourly thereafter.
        # https://dev.twitch.tv/docs/authentication/validate-tokens/
        while True:
            await asyncio.sleep(self.config.TWITCH_VALIDATE_S)
            try:
                async with asyncio.timeout(self.config.TWITCH_IO_TIMEOUT_S):
                    await self.helix.auth.validate("bot")
            except NETWORK_ERRORS:
                LOG.warning("Twitch validation failed; reconnecting")
                self.health("disconnected")
                await self.eventsub.close(self.eventsub.socket)

    async def run(self):
        tasks = []
        try:
            backoff = self.config.TWITCH_RECONNECT_MIN_S
            while True:
                try:
                    async with asyncio.timeout(self.config.TWITCH_IO_TIMEOUT_S):
                        await self.helix.initialize()
                    break
                except NETWORK_ERRORS:
                    self.health("disconnected")
                    LOG.warning("Twitch startup failed; retrying without blocking game")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, self.config.TWITCH_RECONNECT_MAX_S)
            tasks = [
                asyncio.create_task(work)
                for work in (
                    self.send_loop(),
                    self.validate_loop(),
                    self.eventsub.run(),
                )
            ]
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.health("disconnected")
