"""One EventSub chat subscription with bounded deduplication and recovery."""

import asyncio
import json
import logging
from collections import OrderedDict
from urllib.parse import urlsplit

import httpx
import websockets

from .auth import AuthError

URL = "wss://eventsub.wss.twitch.tv/ws"
LOG = logging.getLogger(__name__)


class EventSub:
    def __init__(self, helix, config, on_chat, on_health, connect=websockets.connect):
        self.helix = helix
        self.config = config
        self.on_chat = on_chat
        self.on_health = on_health
        self.connect = connect
        self.seen = OrderedDict()
        self.socket = None

    def notification(self, data):
        try:
            meta = data["metadata"]
            if (
                meta["message_type"] != "notification"
                or meta["subscription_type"] != "channel.chat.message"
                or meta["subscription_version"] != "1"
            ):
                return
            delivery = meta["message_id"]
            event = data["payload"]["event"]
            if (
                not isinstance(delivery, str)
                or not delivery
                or delivery in self.seen
                or event["broadcaster_user_id"] != self.helix.broadcaster_id
            ):
                return
            if not all(
                isinstance(event[key], str) and event[key]
                for key in ("chatter_user_id", "chatter_user_login", "message_id")
            ):
                return
            if not isinstance(event["message"]["text"], str):
                return
            if not isinstance(event["badges"], list) or not all(
                isinstance(badge, dict) and isinstance(badge.get("set_id"), str)
                for badge in event["badges"]
            ):
                return
            self.seen[delivery] = None
            if len(self.seen) > self.config.TWITCH_DEDUP_SIZE:
                self.seen.popitem(last=False)
            self.on_chat(event)
        except (KeyError, TypeError):
            return

    async def close(self, socket):
        if socket is not None:
            try:
                async with asyncio.timeout(self.config.TWITCH_IO_TIMEOUT_S):
                    await socket.close()
            except (OSError, TimeoutError, websockets.WebSocketException):
                pass

    async def open(self, url):
        socket = None
        try:
            async with asyncio.timeout(self.config.TWITCH_IO_TIMEOUT_S):
                # Verified 2026-10-30: outgoing traffic is forbidden except Pong.
                # websockets answers server Ping automatically; disable own Ping.
                # https://dev.twitch.tv/docs/eventsub/handling-websocket-events/
                socket = await self.connect(
                    url,
                    ping_interval=None,
                    open_timeout=self.config.TWITCH_IO_TIMEOUT_S,
                    close_timeout=self.config.TWITCH_IO_TIMEOUT_S,
                    max_queue=self.config.TWITCH_CHAT_QUEUE_SIZE,
                )
                data = json.loads(await socket.recv())
                if data["metadata"]["message_type"] != "session_welcome":
                    raise ValueError("Expected EventSub welcome")
                session = data["payload"]["session"]
                if not isinstance(session["id"], str) or not session["id"]:
                    raise ValueError("Invalid EventSub session")
                timeout = session["keepalive_timeout_seconds"]
                if type(timeout) not in (int, float) or not 0 < timeout <= 600:
                    raise ValueError("Invalid EventSub keepalive")
                return socket, session["id"], timeout
        except BaseException:
            await self.close(socket)
            raise

    async def receive(self, timeout):
        # Twitch's 10 s keepalives arrived 10.01–10.31 s apart in the live
        # check on 2026-10-30. Allow bounded delivery jitter before reconnecting.
        return json.loads(
            await asyncio.wait_for(
                self.socket.recv(), timeout + self.config.TWITCH_KEEPALIVE_GRACE_S
            )
        )

    async def listen(self, timeout):
        receive = asyncio.create_task(self.receive(timeout))
        handoff = None
        try:
            while True:
                waiting = {receive} | ({handoff} if handoff else set())
                done, _ = await asyncio.wait(
                    waiting, return_when=asyncio.FIRST_COMPLETED
                )
                if receive in done:
                    try:
                        data = receive.result()
                        kind = data["metadata"]["message_type"]
                        if kind == "session_reconnect" and handoff is None:
                            # Verified 2026-10-30: keep old reader alive until new
                            # welcome. Carried subscriptions must not be recreated.
                            # https://dev.twitch.tv/docs/eventsub/handling-websocket-events/#reconnect-message
                            url = data["payload"]["session"]["reconnect_url"]
                            parsed = urlsplit(url) if isinstance(url, str) else None
                            if parsed is None or (
                                parsed.scheme != "wss"
                                or parsed.netloc != "eventsub.wss.twitch.tv"
                            ):
                                raise AuthError("Invalid EventSub reconnect URL")
                            handoff = asyncio.create_task(self.open(url))
                        elif kind == "revocation":
                            raise AuthError("EventSub subscription revoked")
                        elif kind == "notification":
                            self.notification(data)
                    except (ValueError, KeyError, TypeError):
                        LOG.warning("Invalid EventSub message ignored")
                    receive = asyncio.create_task(self.receive(timeout))
                if handoff in done:
                    replacement, _, timeout = handoff.result()
                    receive.cancel()
                    await asyncio.gather(receive, return_exceptions=True)
                    old, self.socket = self.socket, replacement
                    handoff = None
                    await self.close(old)
                    receive = asyncio.create_task(self.receive(timeout))
        finally:
            tasks = [receive] + ([handoff] if handoff else [])
            for task in tasks:
                task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            # A completed handoff may own a socket not adopted before cancellation.
            if handoff and isinstance(results[-1], tuple):
                await self.close(results[-1][0])

    async def run(self):
        backoff = self.config.TWITCH_RECONNECT_MIN_S
        while True:
            try:
                self.socket, session, timeout = await self.open(URL)
                await self.helix.subscribe(session)
                self.on_health("connected")
                backoff = self.config.TWITCH_RECONNECT_MIN_S
                await self.listen(timeout)
            except (
                httpx.HTTPError,
                OSError,
                TimeoutError,
                websockets.WebSocketException,
                AuthError,
                ValueError,
                KeyError,
                TypeError,
            ):
                LOG.warning("EventSub disconnected; reconnecting")
            finally:
                self.on_health("disconnected")
                await self.close(self.socket)
                self.socket = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self.config.TWITCH_RECONNECT_MAX_S)
