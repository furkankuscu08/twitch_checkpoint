"""Public-client Twitch device authorization and account-scoped token storage."""

import argparse
import json
import os
import sys
import tempfile
from asyncio import Lock, run, sleep
from collections.abc import Callable
from pathlib import Path
from time import monotonic, time

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
TOKEN_PATH = ROOT / "data" / "tokens.json"
DEVICE_URL = "https://id.twitch.tv/oauth2/device"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"
SCOPES = {
    "bot": ("user:read:chat", "user:write:chat"),
    "test": ("user:write:chat",),
    "test2": ("user:write:chat",),
    "test3": ("user:write:chat",),
}


class AuthError(Exception):
    """A safe, actionable error; never include API bodies or token values."""


class InvalidAccessToken(AuthError):
    """The validation endpoint returned 401; refresh may recover the session."""


def settings() -> tuple[str, dict[str, str]]:
    load_dotenv(ROOT / ".env")
    client_id = os.environ.get("TWITCH_CLIENT_ID", "").strip()
    if not client_id:
        raise AuthError("Set TWITCH_CLIENT_ID in .env before device-code login.")
    return client_id, {
        account: os.environ.get(f"TWITCH_{account.upper()}_LOGIN", "").strip().lower()
        for account in SCOPES
    }


def payload(response: httpx.Response) -> dict:
    try:
        result = response.json()
    except ValueError:
        raise AuthError("Twitch returned an invalid authentication response.") from None
    if not isinstance(result, dict):
        raise AuthError("Twitch returned an invalid authentication response.")
    return result


def token_pair(data: dict) -> dict:
    if not all(
        isinstance(data.get(key), str) and data[key]
        for key in ("access_token", "refresh_token")
    ) or not (type(data.get("expires_in")) is int and data["expires_in"] > 0):
        raise AuthError("Twitch returned an incomplete token pair; run login again.")
    return {
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        "expires_at": time() + data["expires_in"],
    }


class TwitchAuth:
    """One shared auth instance per event loop, using a finite-timeout HTTP client."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        client_id: str,
        logins: dict[str, str],
        token_path: Path | None = None,
    ):
        self.client = client
        self.client_id = client_id
        self.logins = logins
        self.token_path = token_path if token_path is not None else TOKEN_PATH
        self.locks = {account: Lock() for account in SCOPES}

    def login_name(self, account: str) -> str:
        if account not in SCOPES:
            raise AuthError("Choose a configured Twitch account for login.")
        login = self.logins.get(account, "").strip().lower()
        if not login:
            raise AuthError(f"Set TWITCH_{account.upper()}_LOGIN in .env first.")
        return login

    def read_tokens(self) -> dict:
        try:
            data = json.loads(self.token_path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            raise AuthError(
                "Cannot read tokens.json; restore it or run login again."
            ) from None
        if not isinstance(data, dict) or not all(
            isinstance(record, dict) for record in data.values()
        ):
            raise AuthError("Invalid tokens.json; restore it before running login.")
        return data

    def saved_account(self, account: str) -> dict:
        self.login_name(account)
        record = self.read_tokens().get(account)
        if not record or not all(
            isinstance(record.get(key), str) and record[key]
            for key in ("access_token", "refresh_token")
        ):
            raise AuthError(
                f"No token pair for {account}; run device-code login first."
            )
        return record

    def save(self, account: str, record: dict) -> None:
        data = self.read_tokens()
        data[account] = record
        temporary = None
        try:
            self.token_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor, temporary = tempfile.mkstemp(
                prefix=".tokens-", dir=self.token_path.parent
            )
            # mkstemp creates the replacement with owner-only permissions (0600).
            with os.fdopen(descriptor, "w") as output:
                json.dump(data, output, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.token_path)
        except OSError:
            raise AuthError(
                "Cannot save tokens.json; check disk access and log in again."
            ) from None
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    async def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        try:
            return await self.client.request(method, url, **kwargs)
        except httpx.HTTPError:
            raise AuthError(
                "Twitch authentication request failed; try again."
            ) from None

    async def checked_identity(self, account: str, access_token: str) -> dict:
        # Verified 2026-10-29: GET /validate accepts Authorization: OAuth <token>.
        # Call at session startup and hourly in the WP9 integration.
        # https://dev.twitch.tv/docs/authentication/validate-tokens/
        response = await self.request(
            "GET", VALIDATE_URL, headers={"Authorization": f"OAuth {access_token}"}
        )
        if response.status_code == 401:
            raise InvalidAccessToken("Twitch access token is invalid.")
        if response.status_code != 200:
            raise AuthError("Twitch token validation failed; try again.")
        data = payload(response)
        scopes = data.get("scopes")
        if (
            not isinstance(data.get("client_id"), str)
            or not isinstance(data.get("login"), str)
            or not isinstance(data.get("user_id"), str)
            or not data["user_id"]
            or not isinstance(scopes, list)
            or not all(isinstance(scope, str) for scope in scopes)
            or type(data.get("expires_in")) is not int
            or data["expires_in"] <= 0
        ):
            raise AuthError("Twitch returned an invalid token validation response.")
        if data["client_id"] != self.client_id:
            raise AuthError(
                "Token belongs to a different Twitch app; check TWITCH_CLIENT_ID."
            )
        expected = self.login_name(account)
        if data["login"].lower() != expected:
            raise AuthError(
                f"Expected {expected}; authorized as {data['login']} instead. "
                "Switch browser accounts and run login again."
            )
        if not set(SCOPES[account]).issubset(scopes):
            raise AuthError(
                f"Missing required Twitch scopes; run {account} login again."
            )
        return {
            "client_id": data["client_id"],
            "login": data["login"].lower(),
            "user_id": data["user_id"],
            "scopes": scopes,
            "expires_at": time() + data["expires_in"],
        }

    async def login(self, account: str, on_code: Callable[[str, str], None]) -> dict:
        self.login_name(account)
        async with self.locks[account]:
            # Verified 2026-10-29: Public client, no secret or redirect URI.
            # /device and device-token exchange examples use multipart fields:
            # client_id, scopes; exchange adds device_code and the RFC grant_type.
            # https://dev.twitch.tv/docs/authentication/getting-tokens-oauth/#device-code-grant-flow
            fields = {"client_id": self.client_id, "scopes": " ".join(SCOPES[account])}
            response = await self.request(
                "POST",
                DEVICE_URL,
                files={key: (None, value) for key, value in fields.items()},
            )
            if response.status_code != 200:
                raise AuthError("Cannot start device login; check TWITCH_CLIENT_ID.")
            device = payload(response)
            if not all(
                isinstance(device.get(key), str) and device[key]
                for key in ("device_code", "user_code", "verification_uri")
            ) or not all(
                type(device.get(key)) is int and device[key] > 0
                for key in ("interval", "expires_in")
            ):
                raise AuthError("Twitch returned incomplete device login instructions.")
            deadline = monotonic() + device["expires_in"]
            on_code(device["verification_uri"], device["user_code"])
            fields.update(
                device_code=device["device_code"],
                grant_type="urn:ietf:params:oauth:grant-type:device_code",
            )
            while monotonic() < deadline:
                await sleep(min(device["interval"], deadline - monotonic()))
                if monotonic() >= deadline:
                    break
                response = await self.request(
                    "POST",
                    TOKEN_URL,
                    files={key: (None, value) for key, value in fields.items()},
                )
                data = payload(response)
                if response.status_code == 200:
                    pair = token_pair(data)
                    checked = await self.checked_identity(account, pair["access_token"])
                    record = {**pair, **checked}
                    self.save(account, record)
                    return record
                if (
                    response.status_code == 400
                    and data.get("message") == "authorization_pending"
                ):
                    continue
                raise AuthError(
                    "Device authorization failed or was denied; run login again."
                )
            raise AuthError("Device code expired; run login again.")

    async def validate(self, account: str) -> dict:
        """Validate a saved session; callers invoke at startup and hourly thereafter."""
        record = self.saved_account(account)
        try:
            checked = await self.checked_identity(account, record["access_token"])
        except InvalidAccessToken:
            return await self.refresh(
                account, rejected_access_token=record["access_token"]
            )
        return {**record, **checked}

    async def refresh(
        self, account: str, *, rejected_access_token: str | None = None
    ) -> dict:
        self.login_name(account)
        async with self.locks[account]:
            record = self.saved_account(account)
            if (
                rejected_access_token is not None
                and record["access_token"] != rejected_access_token
            ):
                checked = await self.checked_identity(account, record["access_token"])
                return {**record, **checked}
            # Verified 2026-10-30: Public-client refresh needs no client_secret;
            # form encoding escapes refresh tokens. Public DCF refresh tokens are
            # single-use, expire after 30 days, and must be replaced after refresh.
            # https://dev.twitch.tv/docs/authentication/refresh-tokens/
            # https://dev.twitch.tv/docs/authentication/getting-tokens-oauth/#device-code-grant-flow
            response = await self.request(
                "POST",
                TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "grant_type": "refresh_token",
                    "refresh_token": record["refresh_token"],
                },
            )
            if response.status_code != 200:
                raise AuthError("Token refresh failed; run device-code login again.")
            updated = {**record, **token_pair(payload(response))}
            # Persist rotation before another network await can fail or be cancelled.
            self.save(account, updated)
            checked = await self.checked_identity(account, updated["access_token"])
            updated.update(checked)
            self.save(account, updated)
            return updated


async def cli(account: str, *, refresh: bool = False) -> None:
    client_id, logins = settings()
    async with httpx.AsyncClient() as client:
        manager = TwitchAuth(client, client_id, logins)
        expected = manager.login_name(account)
        if refresh:
            record = await manager.refresh(account)
            print(f"Refreshed and validated {record['login']} ({account}).")
            return
        print(f"Authorize as {expected} ({account}).", flush=True)

        def show_code(uri: str, code: str) -> None:
            print(
                f"Open: {uri}\nDevice code: {code}\nWaiting for authorization...",
                flush=True,
            )

        record = await manager.login(account, show_code)
        print(f"Saved and validated {record['login']} ({account}) in data/tokens.json.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", choices=SCOPES, required=True)
    parser.add_argument("--refresh", action="store_true", help="Refresh a saved login")
    args = parser.parse_args(argv)
    try:
        run(cli(args.account, refresh=args.refresh))
    except AuthError as error:
        print(f"Authentication failed: {error}", file=sys.stderr)
        if args.refresh:
            print(
                "Run device-code login: uv run python -m minefield.twitch_api.auth "
                f"--account {args.account}",
                file=sys.stderr,
            )
        return 1
    except KeyboardInterrupt:
        print("Device login cancelled.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
