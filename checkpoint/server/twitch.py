"""Checkpoint glue using the copied send, validation and health patterns."""

import asyncio
import logging

import httpx

from checkpoint.twitch_api.auth import TwitchAuth
from checkpoint.twitch_api.eventsub import EventSub
from checkpoint.twitch_api.helix import Helix

from .runtime import NETWORK_ERRORS

LOG = logging.getLogger(__name__)


class Twitch:
    def __init__(self, runtime, helix):
        self.runtime, self.helix = runtime, helix
        self.config = runtime.config
        self.ready = False
        self.eventsub = EventSub(
            helix,
            self.config,
            runtime.collect,
            runtime.health,
            on_delete=runtime.delete,
        )

    async def send(self, intent):
        # Injected IDs are never forwarded to Helix, including earlier replies.
        if intent.debug or not self.ready:
            return None
        return await self.helix.send_message(
            intent.reply.text,
            reply_parent_message_id=intent.reply.reply_parent_message_id,
        )

    async def validate_loop(self):
        # Verified 2026-11-01: startup validation and hourly thereafter.
        # https://dev.twitch.tv/docs/authentication/validate-tokens/
        while True:
            await asyncio.sleep(self.config.TWITCH_VALIDATE_S)
            try:
                async with asyncio.timeout(self.config.TWITCH_IO_TIMEOUT_S):
                    await self.helix.auth.validate("bot")
            except NETWORK_ERRORS:
                self.ready = False
                LOG.warning("Twitch validation failed; reconnecting")
                self.runtime.health("disconnected")
                await self.eventsub.close(self.eventsub.socket)
                # Recheck identity and refresh rules before re-enabling sends.
                await self.initialize()

    async def initialize(self):
        backoff = self.config.TWITCH_RECONNECT_MIN_S
        while True:
            try:
                async with asyncio.timeout(self.config.TWITCH_IO_TIMEOUT_S):
                    await self.helix.initialize()
                self.ready = True
                return
            except NETWORK_ERRORS:
                self.ready = False
                self.runtime.health("disconnected")
                LOG.warning("Twitch startup failed; retrying without blocking cards")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.config.TWITCH_RECONNECT_MAX_S)

    async def run(self):
        tasks = []
        self.runtime.sender = self.send
        try:
            await self.initialize()
            tasks = [
                asyncio.create_task(self.validate_loop()),
                asyncio.create_task(self.eventsub.run()),
            ]
            await asyncio.gather(*tasks)
        finally:
            self.ready = False
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.eventsub.close(self.eventsub.socket)
            self.runtime.health("disconnected")


def factory(client_id, logins, broadcaster_login):
    async def run(runtime):
        async with httpx.AsyncClient(
            timeout=runtime.config.TWITCH_IO_TIMEOUT_S
        ) as client:
            auth = TwitchAuth(client, client_id, logins)
            await Twitch(runtime, Helix(auth, runtime.config, broadcaster_login)).run()

    return run
