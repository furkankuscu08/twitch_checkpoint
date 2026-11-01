import asyncio
import json
from collections import deque
from dataclasses import replace

import httpx
import pytest

from minefield.core.config import Config
from minefield.twitch_api.eventsub import EventSub


def packet(kind, **payload):
    return json.dumps({"metadata": {"message_type": kind}, "payload": payload})


def welcome(session):
    return packet(
        "session_welcome", session={"id": session, "keepalive_timeout_seconds": 0.02}
    )


def notification(message_id="delivery", text="!mine"):
    return json.dumps(
        {
            "metadata": {
                "message_type": "notification",
                "message_id": message_id,
                "subscription_type": "channel.chat.message",
                "subscription_version": "1",
            },
            "payload": {
                "event": {
                    "broadcaster_user_id": "10",
                    "chatter_user_id": "30",
                    "chatter_user_login": "viewer",
                    "message_id": "chat-" + message_id,
                    "message": {"text": text},
                    "badges": [],
                }
            },
        }
    )


class Socket:
    def __init__(self, messages):
        self.messages = deque(messages)
        self.closed = False
        self.on_wait = None

    async def recv(self):
        if self.messages:
            message = self.messages.popleft()
            if isinstance(message, Exception):
                raise message
            return message
        if self.on_wait:
            self.on_wait()
        await asyncio.Future()

    async def close(self):
        self.closed = True


class Helix:
    broadcaster_id = "10"
    bot_id = "20"

    def __init__(self):
        self.subscriptions = []

    async def subscribe(self, session):
        self.subscriptions.append(session)


def test_fresh_reconnect_timeout_dedup_and_bad_messages():
    async def scenario():
        sockets = [
            Socket([welcome("a"), "bad JSON", notification(), notification()]),
            Socket([welcome("b"), notification(), notification("second")]),
        ]
        connected = []
        seen, health = [], []
        ready = asyncio.Event()

        async def connect(url, **kwargs):
            assert kwargs["ping_interval"] is None
            connected.append(url)
            socket = sockets[len(connected) - 1]
            if len(connected) == 2:
                socket.on_wait = ready.set
            return socket

        helix = Helix()
        eventsub = EventSub(
            helix,
            replace(Config(), TWITCH_RECONNECT_MIN_S=0.001, TWITCH_KEEPALIVE_GRACE_S=0),
            seen.append,
            health.append,
            connect,
        )
        task = asyncio.create_task(eventsub.run())
        try:
            await asyncio.wait_for(ready.wait(), 1)
            assert helix.subscriptions == ["a", "b"]
            assert len(seen) == 2
            assert "disconnected" in health and health[-1] == "connected"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert all(socket.closed for socket in sockets)

    asyncio.run(scenario())


@pytest.mark.parametrize("path", ["/ws", ""])
def test_handoff_keeps_old_reader_until_new_welcome_without_resubscribe(path):
    async def scenario():
        reconnect_url = f"wss://eventsub.wss.twitch.tv{path}?handoff=example"
        old = Socket(
            [
                welcome("a"),
                packet("session_reconnect", session={"reconnect_url": reconnect_url}),
                notification("during-handoff"),
            ]
        )
        new = Socket([welcome("b"), notification("after-handoff")])
        seen = []
        ready = asyncio.Event()
        attempts = []

        async def connect(url, **kwargs):
            attempts.append(url)
            if len(attempts) == 1:
                return old
            assert url == reconnect_url and not old.closed
            # Yield so the old reader can process an event during connection setup.
            await asyncio.sleep(0.005)
            return new

        new.on_wait = ready.set
        helix = Helix()
        eventsub = EventSub(helix, Config(), seen.append, lambda _: None, connect)
        task = asyncio.create_task(eventsub.run())
        try:
            await asyncio.wait_for(ready.wait(), 1)
            assert helix.subscriptions == ["a"]
            assert len(seen) == 2 and old.closed
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert new.closed

    asyncio.run(scenario())


def test_notification_boundary_and_bounded_dedup():
    helix = Helix()
    seen = []
    eventsub = EventSub(
        helix, replace(Config(), TWITCH_DEDUP_SIZE=2), seen.append, lambda _: None
    )
    for raw in ["null", "[]", "{}", "not-json", packet("notification", event={})]:
        eventsub.notification(json.loads(raw) if raw != "not-json" else {})
    for number in range(4):
        eventsub.notification(json.loads(notification(str(number))))
    assert len(seen) == 4 and len(eventsub.seen) == 2
    event = json.loads(notification("foreign"))
    event["payload"]["event"]["broadcaster_user_id"] = "other"
    eventsub.notification(event)
    assert len(seen) == 4


@pytest.mark.parametrize("failure", ["subscribe", "revocation", "drop"])
def test_network_failure_or_revocation_recovers(failure):
    async def scenario():
        first = [welcome("a")]
        if failure == "revocation":
            first.append(packet("revocation"))
        elif failure == "drop":
            first.append(OSError("network failure"))
        sockets = [Socket(first), Socket([welcome("b"), notification()])]
        ready = asyncio.Event()
        sockets[1].on_wait = ready.set
        attempts = []

        async def connect(*args, **kwargs):
            result = sockets[len(attempts)]
            attempts.append(result)
            return result

        helix = Helix()
        original = helix.subscribe

        async def subscribe(session):
            if failure == "subscribe" and session == "a":
                raise httpx.ConnectError("must not escape reader task")
            await original(session)

        helix.subscribe = subscribe
        eventsub = EventSub(
            helix,
            replace(Config(), TWITCH_RECONNECT_MIN_S=0.001),
            lambda _: None,
            lambda _: None,
            connect,
        )
        task = asyncio.create_task(eventsub.run())
        try:
            await asyncio.wait_for(ready.wait(), 1)
            assert helix.subscriptions[-1] == "b"
            assert sockets[0].closed
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert sockets[1].closed

    asyncio.run(scenario())


def test_small_keepalive_delivery_jitter_does_not_force_reconnect():
    async def scenario():
        ready = asyncio.Event()

        class DelayedSocket(Socket):
            async def recv(self):
                if self.messages:
                    return self.messages.popleft()
                await asyncio.sleep(0.025)
                ready.set()
                return packet("session_keepalive")

        socket = DelayedSocket([welcome("a")])
        attempts = []

        async def connect(*args, **kwargs):
            attempts.append(True)
            return socket

        config = replace(Config(), TWITCH_RECONNECT_MIN_S=0.001)
        helix = Helix()
        eventsub = EventSub(helix, config, lambda _: None, lambda _: None, connect)
        task = asyncio.create_task(eventsub.run())
        try:
            await asyncio.wait_for(ready.wait(), 1)
            assert len(attempts) == 1 and helix.subscriptions == ["a"]
            assert not socket.closed
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
