"""Finite-timeout Helix calls; chat delivery is strictly best effort."""

import asyncio
import logging

import httpx

from .auth import AuthError

BASE = "https://api.twitch.tv/helix"
LOG = logging.getLogger(__name__)


class Helix:
    def __init__(self, auth, config, broadcaster_login):
        self.auth = auth
        self.config = config
        self.broadcaster_login = broadcaster_login.lower()
        self.broadcaster_id = None
        self.bot_id = None

    async def request(self, method, path, **kwargs):
        # Verified 2026-10-30: Helix user token + matching Client-Id.
        # https://dev.twitch.tv/docs/api/reference/#create-eventsub-subscription
        # Only idempotent reads / subscription setup use authentication retry.
        async with asyncio.timeout(self.config.CHAT_SEND_TIMEOUT_S):
            for attempt in range(2):
                record = self.auth.saved_account("bot")
                response = await self.auth.client.request(
                    method,
                    BASE + path,
                    headers={
                        "Authorization": f"Bearer {record['access_token']}",
                        "Client-Id": self.auth.client_id,
                    },
                    **kwargs,
                )
                if response.status_code != 401 or attempt:
                    return response
                await self.auth.refresh(
                    "bot", rejected_access_token=record["access_token"]
                )

    async def initialize(self):
        checked = await self.auth.validate("bot")
        # Verified 2026-10-30: repeated login parameters resolve IDs at runtime.
        # https://dev.twitch.tv/docs/api/reference/#get-users
        response = await self.request(
            "GET",
            "/users",
            params=[
                ("login", self.broadcaster_login),
                ("login", self.auth.login_name("bot")),
            ],
        )
        if response.status_code != 200:
            raise AuthError("Cannot resolve Twitch users.")
        data = response.json()["data"]
        users = {item["login"].lower(): item["id"] for item in data}
        self.broadcaster_id = users[self.broadcaster_login]
        self.bot_id = users[self.auth.login_name("bot")]
        if self.bot_id != checked["user_id"]:
            raise AuthError("Twitch bot identity mismatch.")

    async def subscribe(self, session_id):
        # Verified 2026-10-30: channel.chat.message v1, user:read:chat token.
        # https://dev.twitch.tv/docs/eventsub/eventsub-subscription-types/#channelchatmessage
        response = await self.request(
            "POST",
            "/eventsub/subscriptions",
            json={
                "type": "channel.chat.message",
                "version": "1",
                "condition": {
                    "broadcaster_user_id": self.broadcaster_id,
                    "user_id": self.bot_id,
                },
                "transport": {"method": "websocket", "session_id": session_id},
            },
        )
        if response.status_code != 202:
            raise AuthError("Twitch subscription failed.")

    async def send_message(self, message, account="bot"):
        # Verified 2026-10-30: max 500 chars, sender matches the user token;
        # user:write:chat. No for_source_only with user tokens.
        # https://dev.twitch.tv/docs/api/reference/#send-chat-message
        try:
            async with asyncio.timeout(self.config.CHAT_SEND_TIMEOUT_S):
                record = self.auth.saved_account(account)
                response = await self.auth.client.post(
                    BASE + "/chat/messages",
                    headers={
                        "Authorization": f"Bearer {record['access_token']}",
                        "Client-Id": self.auth.client_id,
                    },
                    json={
                        "broadcaster_id": self.broadcaster_id,
                        "sender_id": record["user_id"],
                        "message": message,
                    },
                )
                if response.status_code != 200:
                    LOG.warning("Twitch chat failed status=%d", response.status_code)
                    if response.status_code == 401:
                        await self.auth.refresh(
                            account, rejected_access_token=record["access_token"]
                        )
                    return None
                result = response.json()["data"][0]
                if type(result["is_sent"]) is not bool:
                    raise ValueError("Invalid chat response")
                # Provider free text could echo private input. Log presence only.
                LOG.info(
                    "Twitch chat is_sent=%s drop_reason=%s",
                    result["is_sent"],
                    "reported" if result.get("drop_reason") else "none",
                )
                return result
        except (
            AuthError,
            httpx.HTTPError,
            TimeoutError,
            ValueError,
            KeyError,
            IndexError,
            TypeError,
        ):
            LOG.warning("Twitch chat failed; message will not be retried")
            return None
