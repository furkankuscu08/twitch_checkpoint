"""No interactive logins in pytest: authorize accounts with the CLI first."""

import asyncio

import httpx
import pytest

from minefield.twitch_api.auth import TOKEN_PATH, TwitchAuth, settings


@pytest.mark.live
@pytest.mark.parametrize("account", ["bot", "test", "test2", "test3"])
def test_saved_token_validates_and_refreshes(account):
    if not TOKEN_PATH.exists():
        pytest.skip("Run the bot and test device-code logins first")
    client_id, logins = settings()

    async def scenario():
        async with httpx.AsyncClient() as client:
            manager = TwitchAuth(client, client_id, logins)
            if account not in manager.read_tokens():
                pytest.skip(f"Run device-code login for {account} first")
            initial = await manager.validate(account)
            refreshed = await manager.refresh(account)
            # Do not put token values in assertions: pytest may display failures.
            assert bool(refreshed.get("access_token"))
            assert bool(refreshed.get("refresh_token"))
            assert refreshed["user_id"] == initial["user_id"]
            assert refreshed["login"] == logins[account].lower()

    asyncio.run(scenario())
