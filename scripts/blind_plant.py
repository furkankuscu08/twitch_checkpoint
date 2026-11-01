"""Plant once per viewer without disclosing the selections. Run as a module."""

import asyncio
import json
import os
import random
import sys

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from minefield.core.plants import ALPHABET
from minefield.server.__main__ import config_from_env
from minefield.twitch_api.auth import AuthError, TwitchAuth, settings
from minefield.twitch_api.helix import BASE

ACCOUNTS = ("test", "test2", "test3")
ERRORS = (
    AuthError,
    httpx.HTTPError,
    TimeoutError,
    WebSocketException,
    OSError,
    ValueError,
    KeyError,
    TypeError,
    IndexError,
)


def credential_warning(owner, account):
    print(
        f"WARNING {owner}: token missing, invalid or unavailable. Refresh: "
        f"uv run python -m minefield.twitch_api.auth --account {account} --refresh",
        file=sys.stderr,
        flush=True,
    )


async def run(auth, config, broadcaster, *, rng=None, accounts=ACCOUNTS):
    """Fixed, independent attempts; no game mutations outside the real chat path."""
    rng = rng or random.SystemRandom()
    owners = {
        account: auth.logins.get(account, "").strip().lower() or account
        for account in accounts
    }
    results = {account: False for account in accounts}
    pending = {}
    closed = asyncio.Event()
    plant_url = f"http://127.0.0.1:{config.PLANT_PORT}"

    async def request(method, url, **kwargs):
        limit = (
            config.CHAT_SEND_TIMEOUT_S
            if url == BASE + "/chat/messages"
            else config.TWITCH_IO_TIMEOUT_S
        )
        async with asyncio.timeout(limit):
            response = await auth.client.request(method, url, **kwargs)
            if response.status_code == 401 and url.startswith(BASE):
                raise AuthError("Viewer authorization failed")
            response.raise_for_status()
            return response.json()

    async def observe(socket):
        try:
            async for raw in socket:
                try:
                    event = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("type") == "reveal" or (
                    event.get("type") == "snapshot"
                    and event.get("round", {}).get("running") is False
                ):
                    break
                if event.get("type") == "mine_planted":
                    owner = event.get("owner")
                    if isinstance(owner, str) and owner in pending:
                        future = pending[owner]
                        if not future.done():
                            future.set_result(True)
        except ERRORS:
            pass
        finally:
            closed.set()
            for future in pending.values():
                if not future.done():
                    future.set_result(False)

    async def attempt(account, existing):
        owner = owners[account]
        try:
            await asyncio.sleep(rng.uniform(0, config.BLIND_PLANT_WINDOW_S))
            if closed.is_set() or owner in existing:
                return
            record = auth.saved_account(account)
            # Verified 2026-10-30: validate identity, client ID and write scope.
            # https://dev.twitch.tv/docs/authentication/validate-tokens/
            async with asyncio.timeout(config.TWITCH_IO_TIMEOUT_S):
                identity = await auth.checked_identity(account, record["access_token"])
            headers = {
                "Authorization": f"Bearer {record['access_token']}",
                "Client-Id": auth.client_id,
            }
            # Verified 2026-10-30: Get Users accepts a viewer user token.
            # https://dev.twitch.tv/docs/api/reference/#get-users
            users = await request(
                "GET", BASE + "/users", headers=headers, params={"login": broadcaster}
            )
            broadcaster_id = users["data"][0]["id"]
            if not isinstance(broadcaster_id, str) or not broadcaster_id:
                raise ValueError("Missing broadcaster")
            hand = await request("GET", plant_url + "/api/hand")
            cards = hand["cards"]
            if (
                not isinstance(cards, list)
                or not cards
                or not all(isinstance(card, str) for card in cards)
            ):
                raise ValueError("Invalid hand")
            pick = await request(
                "POST",
                plant_url + "/api/pick",
                json={"hand_id": hand["hand_id"], "word": rng.choice(cards)},
            )
            code = pick["code"]
            if (
                not isinstance(code, str)
                or len(code) != 4
                or any(char not in ALPHABET for char in code)
            ):
                raise ValueError("Invalid code")
            if closed.is_set():
                return
            future = asyncio.get_running_loop().create_future()
            pending[owner] = future
            # Verified 2026-10-30: user:write:chat, sender matches user token.
            # One POST, no retry; bodies/errors are never printed.
            # https://dev.twitch.tv/docs/api/reference/#send-chat-message
            sent = await request(
                "POST",
                BASE + "/chat/messages",
                headers=headers,
                json={
                    "broadcaster_id": broadcaster_id,
                    "sender_id": identity["user_id"],
                    "message": f"!mine {code}",
                },
            )
            if sent["data"][0]["is_sent"] is not True:
                return
            async with asyncio.timeout(config.BLIND_PLANT_CONFIRM_TIMEOUT_S):
                results[account] = await future
        except AuthError:
            credential_warning(owner, account)
        except ERRORS:
            pass
        finally:
            pending.pop(owner, None)
            print(
                f"{owner}: {'success' if results[account] else 'failure'}", flush=True
            )

    try:
        async with connect(
            f"ws://127.0.0.1:{config.GAME_PORT}/ws/dock",
            open_timeout=config.TWITCH_IO_TIMEOUT_S,
            close_timeout=config.TWITCH_IO_TIMEOUT_S,
            max_queue=config.WS_CLIENT_QUEUE_SIZE,
        ) as socket:
            async with asyncio.timeout(config.TWITCH_IO_TIMEOUT_S):
                snapshot = json.loads(await socket.recv())
            if (
                snapshot["type"] != "snapshot"
                or snapshot["round"]["running"] is not True
            ):
                raise ValueError("No active round")
            existing = {
                mine["owner"]
                for mine in snapshot["mines"]
                if mine["status"] in {"arming", "armed", "sparking"}
                and mine["owner"] in owners.values()
            }
            observer = asyncio.create_task(observe(socket), name="blind-plant-dock")
            workers = [
                asyncio.create_task(
                    attempt(account, existing), name=f"blind-plant-{account}"
                )
                for account in accounts
            ]
            try:
                await asyncio.gather(*workers)
            finally:
                for task in [observer, *workers]:
                    task.cancel()
                await asyncio.gather(observer, *workers, return_exceptions=True)
    except ERRORS:
        for owner in owners.values():
            print(f"{owner}: failure", flush=True)
    return results


async def cli():
    client_id, logins = settings()
    config = config_from_env(os.environ)
    broadcaster = os.environ.get("TWITCH_BROADCASTER_LOGIN", "").strip().lower()
    async with httpx.AsyncClient(timeout=config.TWITCH_IO_TIMEOUT_S) as client:
        auth = TwitchAuth(client, client_id, logins)
        return await run(auth, config, broadcaster)


def main():
    try:
        results = asyncio.run(cli())
        return 0 if all(results.values()) else 1
    except ERRORS:
        for account in ACCOUNTS:
            print(f"{account}: failure", flush=True)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
