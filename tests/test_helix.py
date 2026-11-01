import asyncio
import json
import logging
from dataclasses import replace

import httpx
import pytest

from minefield.core.config import Config
from minefield.twitch_api.auth import TwitchAuth
from minefield.twitch_api.helix import Helix

BASE = "https://api.twitch.tv/helix"


def manager(client, tmp_path):
    auth = TwitchAuth(
        client, "client", {"bot": "bot", "test": "viewer"}, tmp_path / "tokens.json"
    )
    auth.save(
        "bot",
        {
            "access_token": "access-secret",
            "refresh_token": "refresh-secret",
            "user_id": "20",
        },
    )
    return auth


def test_initialize_resolves_ids_and_subscribes(respx_mock, tmp_path):
    respx_mock.get("https://id.twitch.tv/oauth2/validate").respond(
        200,
        json={
            "client_id": "client",
            "login": "bot",
            "user_id": "20",
            "scopes": ["user:read:chat", "user:write:chat"],
            "expires_in": 3600,
        },
    )
    users = respx_mock.get(BASE + "/users").respond(
        200,
        json={
            "data": [
                {"id": "10", "login": "channel"},
                {"id": "20", "login": "bot"},
            ]
        },
    )
    subscribe = respx_mock.post(BASE + "/eventsub/subscriptions").respond(
        202, json={"data": [{"status": "enabled"}]}
    )

    async def scenario():
        async with httpx.AsyncClient() as client:
            helix = Helix(manager(client, tmp_path), Config(), "channel")
            await helix.initialize()
            assert helix.broadcaster_id == "10" and helix.bot_id == "20"
            await helix.subscribe("session")

    asyncio.run(scenario())
    assert users.calls[0].request.url.params.get_list("login") == ["channel", "bot"]
    request = subscribe.calls[0].request
    assert request.headers["authorization"] == "Bearer access-secret"
    assert request.headers["client-id"] == "client"
    assert json.loads(request.content) == {
        "type": "channel.chat.message",
        "version": "1",
        "condition": {"broadcaster_user_id": "10", "user_id": "20"},
        "transport": {"method": "websocket", "session_id": "session"},
    }


@pytest.mark.parametrize(
    "result",
    [
        {"message_id": "sent", "is_sent": True},
        {
            "message_id": "",
            "is_sent": False,
            "drop_reason": {"code": "automod_held", "message": "private-pineapple"},
        },
    ],
)
def test_send_shape_outcome_and_safe_log(result, respx_mock, tmp_path, caplog):
    route = respx_mock.post(BASE + "/chat/messages").respond(
        200, json={"data": [result]}
    )

    async def scenario():
        async with httpx.AsyncClient() as client:
            helix = Helix(manager(client, tmp_path), Config(), "channel")
            helix.broadcaster_id = "10"
            assert await helix.send_message("private-pineapple") == result

    with caplog.at_level(logging.INFO):
        asyncio.run(scenario())
    assert json.loads(route.calls[0].request.content) == {
        "broadcaster_id": "10",
        "sender_id": "20",
        "message": "private-pineapple",
    }
    assert "is_sent=" in caplog.text
    assert "private-pineapple" not in caplog.text and "access-secret" not in caplog.text


@pytest.mark.parametrize(
    "failure", [429, 503, "network", "bad-json", "bad-shape", "timeout", "401"]
)
def test_failure_never_retries_message_and_recovers_next_send(
    failure, respx_mock, tmp_path, caplog
):
    calls = []

    async def reply(request):
        calls.append(request)
        if len(calls) > 1:
            return httpx.Response(
                200, json={"data": [{"is_sent": True, "message_id": "next"}]}
            )
        if failure == "network":
            raise httpx.ConnectError("private-pineapple access-secret")
        if failure == "timeout":
            await asyncio.sleep(1)
        if failure == "bad-json":
            return httpx.Response(200, text="private-pineapple")
        if failure == "bad-shape":
            return httpx.Response(200, json={"data": [{}]})
        return httpx.Response(
            int(failure) if isinstance(failure, int) or failure == "401" else 503
        )

    respx_mock.post(BASE + "/chat/messages").mock(side_effect=reply)
    refreshed = []

    async def scenario():
        async with httpx.AsyncClient() as client:
            auth = manager(client, tmp_path)

            async def refresh(account, **kwargs):
                refreshed.append(account)
                auth.save(
                    account,
                    {**auth.saved_account(account), "access_token": "next-secret"},
                )

            auth.refresh = refresh
            helix = Helix(auth, replace(Config(), CHAT_SEND_TIMEOUT_S=0.02), "channel")
            helix.broadcaster_id = "10"
            assert await helix.send_message("private-pineapple") is None
            assert len(calls) == 1
            assert (await helix.send_message("next"))["is_sent"]

    with caplog.at_level(logging.INFO):
        asyncio.run(scenario())
    assert len(calls) == 2
    assert refreshed == (["bot"] if failure == "401" else [])
    if failure == "401":
        assert calls[1].headers["authorization"] == "Bearer next-secret"
    assert "private-pineapple" not in caplog.text and "access-secret" not in caplog.text


def test_total_deadline_includes_auth_recovery(respx_mock, tmp_path):
    route = respx_mock.post(BASE + "/chat/messages").respond(401)

    async def scenario():
        async with httpx.AsyncClient() as client:
            auth = manager(client, tmp_path)

            async def refresh(*args, **kwargs):
                await asyncio.sleep(1)

            auth.refresh = refresh
            helix = Helix(auth, replace(Config(), CHAT_SEND_TIMEOUT_S=0.02), "channel")
            helix.broadcaster_id = "10"
            await asyncio.wait_for(helix.send_message("hello"), 0.1)

    asyncio.run(scenario())
    assert route.call_count == 1
