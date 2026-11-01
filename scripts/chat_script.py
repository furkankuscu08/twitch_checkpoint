"""Send a JSON list of {account, delay_s, text}; print one outcome per entry.

Usage: uv run python scripts/chat_script.py FILE
Delay is relative to the previous attempt's completion. Refresh viewer tokens
before rehearsal using checkpoint.twitch_api.auth; this helper never refreshes.
"""

import asyncio
import json
import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

# Support the documented direct-script invocation from an uninstalled checkout.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from checkpoint.core.config import Config  # noqa: E402
from checkpoint.server.__main__ import config_from_env  # noqa: E402
from checkpoint.twitch_api.auth import AuthError, TwitchAuth, settings  # noqa: E402
from checkpoint.twitch_api.helix import BASE  # noqa: E402

ACCOUNTS = ("test", "test2", "test3")
ERRORS = (
    AuthError,
    httpx.HTTPError,
    TimeoutError,
    OSError,
    ValueError,
    KeyError,
    TypeError,
    IndexError,
)


@dataclass(frozen=True)
class Entry:
    account: str
    delay_s: float
    text: str


def validate_script(data, config):
    """Copy and validate the complete script before any delay or request."""
    if not isinstance(data, list) or not data:
        raise ValueError("Expected a nonempty script list")
    entries = []
    for item in data:
        if not isinstance(item, dict) or set(item) != {"account", "delay_s", "text"}:
            raise ValueError("Invalid script entry")
        account, delay, text = item["account"], item["delay_s"], item["text"]
        if not isinstance(account, str) or account not in ACCOUNTS:
            raise ValueError("Invalid test account")
        if type(delay) not in (int, float) or delay < 0:
            raise ValueError("Invalid delay")
        try:
            finite = math.isfinite(delay)
        except OverflowError:
            finite = False
        if not finite:
            raise ValueError("Invalid delay")
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text) > config.CHAT_MAX_CHARS
        ):
            raise ValueError("Invalid chat text")
        entries.append(Entry(account, delay, text))
    return tuple(entries)


async def attempt(entry, auth, config, broadcaster):
    """One bounded attempt, including validation and ID resolution, no retry."""
    async with asyncio.timeout(config.CHAT_SEND_TIMEOUT_S):
        record = auth.saved_account(entry.account)
        # Verified 2026-11-01: identity, matching client ID and user:write:chat.
        # checked_identity performs validation only; it never refreshes tokens.
        # https://dev.twitch.tv/docs/authentication/validate-tokens/
        identity = await auth.checked_identity(entry.account, record["access_token"])
        headers = {
            "Authorization": f"Bearer {record['access_token']}",
            "Client-Id": auth.client_id,
        }
        # Verified 2026-11-01: Get Users accepts a user token and login filter.
        # https://dev.twitch.tv/docs/api/reference/#get-users
        response = await auth.client.get(
            BASE + "/users", headers=headers, params={"login": broadcaster}
        )
        response.raise_for_status()
        users = response.json()["data"]
        if not isinstance(users, list):
            raise ValueError("Invalid users response")
        user = next(
            (
                item
                for item in users
                if isinstance(item, dict)
                and isinstance(item.get("login"), str)
                and item["login"].lower() == broadcaster
            ),
            None,
        )
        if user is None:
            raise ValueError("Missing broadcaster")
        broadcaster_id = user["id"]
        if not isinstance(broadcaster_id, str) or not broadcaster_id:
            raise ValueError("Missing broadcaster")
        # Verified 2026-11-01: user:write:chat, max 500 characters, sender must
        # match the user token. A single POST; no retry or automatic refresh.
        # https://dev.twitch.tv/docs/api/reference/#send-chat-message
        response = await auth.client.post(
            BASE + "/chat/messages",
            headers=headers,
            json={
                "broadcaster_id": broadcaster_id,
                "sender_id": identity["user_id"],
                "message": entry.text,
            },
        )
        response.raise_for_status()
        return response.json()["data"][0]["is_sent"] is True


async def run(data, auth, config, broadcaster, *, sleep=asyncio.sleep):
    entries = validate_script(data, config)
    if not isinstance(broadcaster, str) or not broadcaster.strip():
        raise ValueError("Missing broadcaster login")
    broadcaster = broadcaster.strip().lower()
    results = []
    for entry in entries:
        await sleep(entry.delay_s)
        try:
            sent = await attempt(entry, auth, config, broadcaster)
        except ERRORS:
            sent = False
        results.append(sent)
        print("success" if sent else "failure", flush=True)
    return results


async def cli(path):
    # Validate before loading workspace settings or constructing a network client.
    data = json.loads(Path(path).read_text())
    validate_script(data, Config())
    try:
        client_id, logins = settings()
        config = config_from_env(os.environ)
        validate_script(data, config)
        broadcaster = os.environ.get("TWITCH_BROADCASTER_LOGIN", "")
        if not broadcaster.strip():
            raise ValueError("Missing broadcaster login")
    except ERRORS:
        for _ in data:
            print("failure", flush=True)
        return [False] * len(data)
    async with httpx.AsyncClient(timeout=config.TWITCH_IO_TIMEOUT_S) as client:
        auth = TwitchAuth(client, client_id, logins)
        return await run(data, auth, config, broadcaster)


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("failure", flush=True)
        return 1
    try:
        results = asyncio.run(cli(args[0]))
        return 0 if all(results) else 1
    except ERRORS:
        print("failure", flush=True)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
