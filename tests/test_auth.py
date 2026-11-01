import asyncio
import json
import re
import stat
from urllib.parse import parse_qs

import httpx
import pytest

from minefield.twitch_api import auth

DEVICE = "https://id.twitch.tv/oauth2/device"
TOKEN = "https://id.twitch.tv/oauth2/token"
VALIDATE = "https://id.twitch.tv/oauth2/validate"
SCOPES = ["user:read:chat", "user:write:chat"]


def token_pair(access="access-secret", refresh="refresh-secret"):
    return {
        "access_token": access,
        "refresh_token": refresh,
        "expires_in": 14400,
        "scope": SCOPES,
        "token_type": "bearer",
    }


def identity(**changes):
    return {
        "client_id": "client-id",
        "login": "test_bot",
        "user_id": "resolved-bot-id",
        "scopes": SCOPES,
        "expires_in": 14000,
        **changes,
    }


def multipart_fields(request):
    assert "multipart/form-data" in request.headers["content-type"]
    return {
        name.decode(): value.decode()
        for name, value in re.findall(
            rb'name="([^"]+)"\r\n\r\n([^\r]*)', request.content
        )
    }


@pytest.fixture
def make_auth(tmp_path, monkeypatch):
    clock = [0.0]

    async def sleep(seconds):
        clock[0] += seconds

    monkeypatch.setattr(auth, "monotonic", lambda: clock[0])
    monkeypatch.setattr(auth, "sleep", sleep)

    def make(client):
        return auth.TwitchAuth(
            client=client,
            client_id="client-id",
            logins={"bot": "test_bot", "test": "test_viewer"},
            token_path=tmp_path / "tokens.json",
        )

    return make


@pytest.fixture
def device_flow(respx_mock):
    respx_mock.post(DEVICE).respond(
        200,
        json={
            "device_code": "device-secret",
            "user_code": "ABCD",
            "verification_uri": "https://www.twitch.tv/activate",
            "interval": 5,
            "expires_in": 1800,
        },
    )
    respx_mock.get(VALIDATE).respond(200, json=identity())
    return respx_mock.post(TOKEN)


def test_device_flow_polls_then_validates_and_saves(
    make_auth, device_flow, respx_mock, tmp_path
):
    device_flow.side_effect = [
        httpx.Response(400, json={"message": "authorization_pending"}),
        httpx.Response(200, json=token_pair()),
    ]
    codes = []

    async def scenario():
        async with httpx.AsyncClient() as client:
            manager = make_auth(client)
            result = await manager.login("bot", lambda *args: codes.append(args))
            assert result["login"] == "test_bot"
            assert result["user_id"] == "resolved-bot-id"

    asyncio.run(scenario())
    assert codes == [("https://www.twitch.tv/activate", "ABCD")]
    assert multipart_fields(respx_mock.calls[0].request) == {
        "client_id": "client-id",
        "scopes": " ".join(SCOPES),
    }
    assert multipart_fields(device_flow.calls[0].request) == {
        "client_id": "client-id",
        "scopes": " ".join(SCOPES),
        "device_code": "device-secret",
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
    }
    assert len(device_flow.calls) == 2
    assert (
        respx_mock.calls[-1].request.headers["authorization"] == "OAuth access-secret"
    )
    saved = json.loads((tmp_path / "tokens.json").read_text())
    assert saved["bot"]["refresh_token"] == "refresh-secret"
    assert stat.S_IMODE((tmp_path / "tokens.json").stat().st_mode) == 0o600


def test_test_account_requests_only_write_scope(make_auth, device_flow, respx_mock):
    device_flow.respond(200, json=token_pair())
    respx_mock.get(VALIDATE).respond(
        200,
        json=identity(
            login="test_viewer",
            user_id="resolved-viewer-id",
            scopes=["user:write:chat"],
        ),
    )

    async def scenario():
        async with httpx.AsyncClient() as client:
            await make_auth(client).login("test", lambda *_: None)

    asyncio.run(scenario())
    assert multipart_fields(respx_mock.calls[0].request)["scopes"] == "user:write:chat"


@pytest.mark.parametrize(
    "changes, reason",
    [
        ({"login": "wrong_account"}, "Expected test_bot; authorized as wrong_account"),
        ({"client_id": "wrong_client"}, "different Twitch app"),
        ({"scopes": ["user:write:chat"]}, "Missing required Twitch scopes"),
        ({"user_id": None}, "invalid token validation response"),
        ({"expires_in": "broken"}, "invalid token validation response"),
    ],
)
def test_unverified_tokens_are_not_stored(
    changes, reason, make_auth, device_flow, respx_mock, tmp_path
):
    device_flow.respond(200, json=token_pair())
    respx_mock.get(VALIDATE).respond(200, json=identity(**changes))

    async def scenario():
        async with httpx.AsyncClient() as client:
            with pytest.raises(auth.AuthError, match=reason):
                await make_auth(client).login("bot", lambda *_: None)

    asyncio.run(scenario())
    assert not (tmp_path / "tokens.json").exists()


@pytest.mark.parametrize("message", ["access_denied", "invalid device code"])
def test_device_denial_does_not_leak_response_or_tokens(
    message, make_auth, device_flow, tmp_path
):
    device_flow.respond(400, json={"message": message, "secret": "access-secret"})

    async def scenario():
        async with httpx.AsyncClient() as client:
            with pytest.raises(auth.AuthError) as error:
                await make_auth(client).login("bot", lambda *_: None)
            assert "access-secret" not in str(error.value)

    asyncio.run(scenario())
    assert not (tmp_path / "tokens.json").exists()


def test_device_polling_stops_at_expiry(make_auth, respx_mock, tmp_path):
    respx_mock.post(DEVICE).respond(
        200,
        json={
            "device_code": "secret",
            "user_code": "ABCD",
            "verification_uri": "https://www.twitch.tv/activate",
            "interval": 5,
            "expires_in": 6,
        },
    )
    polling = respx_mock.post(TOKEN).respond(
        400, json={"message": "authorization_pending"}
    )

    async def scenario():
        async with httpx.AsyncClient() as client:
            with pytest.raises(auth.AuthError, match="expired"):
                await make_auth(client).login("bot", lambda *_: None)

    asyncio.run(scenario())
    assert polling.call_count == 1
    assert not (tmp_path / "tokens.json").exists()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not JSON"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={"interval": 0}),
        httpx.Response(503, text="access-secret"),
        httpx.ConnectError("access-secret"),
        httpx.ReadTimeout("access-secret"),
    ],
)
def test_bad_device_responses_are_safe(response, make_auth, respx_mock):
    respx_mock.post(DEVICE).mock(side_effect=response)

    async def scenario():
        async with httpx.AsyncClient() as client:
            with pytest.raises(auth.AuthError) as error:
                await make_auth(client).login("bot", lambda *_: None)
            assert "access-secret" not in str(error.value)

    asyncio.run(scenario())


def seed_tokens(tmp_path):
    saved = {
        "bot": {**token_pair(), "login": "test_bot"},
        "test": {**token_pair("other-access", "other-refresh"), "login": "test_viewer"},
    }
    (tmp_path / "tokens.json").write_text(json.dumps(saved))
    return saved


def test_validation_401_rotates_tokens_without_secret(make_auth, respx_mock, tmp_path):
    original = seed_tokens(tmp_path)
    original["bot"]["refresh_token"] = "refresh-secret+/=%"
    (tmp_path / "tokens.json").write_text(json.dumps(original))
    validation = respx_mock.get(VALIDATE).mock(
        side_effect=[
            httpx.Response(401, json={"message": "invalid access token"}),
            httpx.Response(200, json=identity()),
        ]
    )
    refresh = respx_mock.post(TOKEN).respond(
        200, json=token_pair("new-access", "new-refresh+/=")
    )

    async def scenario():
        async with httpx.AsyncClient() as client:
            result = await make_auth(client).validate("bot")
            assert result["access_token"] == "new-access"

    asyncio.run(scenario())
    assert validation.call_count == 2
    assert parse_qs(refresh.calls[0].request.content.decode()) == {
        "client_id": ["client-id"],
        "grant_type": ["refresh_token"],
        "refresh_token": ["refresh-secret+/=%"],
    }
    saved = json.loads((tmp_path / "tokens.json").read_text())
    assert saved["bot"]["refresh_token"] == "new-refresh+/="
    assert saved["test"] == original["test"]


def test_simultaneous_401s_only_refresh_once(make_auth, respx_mock, tmp_path):
    seed_tokens(tmp_path)
    both_validating_old_token = asyncio.Event()
    old_requests = []

    async def validate(request):
        if request.headers["authorization"] == "OAuth access-secret":
            old_requests.append(request)
            if len(old_requests) == 2:
                both_validating_old_token.set()
            await both_validating_old_token.wait()
            return httpx.Response(401, json={"message": "invalid access token"})
        return httpx.Response(200, json=identity())

    respx_mock.get(VALIDATE).mock(side_effect=validate)
    refresh = respx_mock.post(TOKEN).respond(
        200, json=token_pair("new-access", "new-refresh")
    )

    async def scenario():
        async with httpx.AsyncClient() as client:
            manager = make_auth(client)
            results = await asyncio.gather(
                manager.validate("bot"), manager.validate("bot")
            )
            assert all(result["access_token"] == "new-access" for result in results)

    asyncio.run(scenario())
    assert len(old_requests) == 2
    assert refresh.call_count == 1


def test_rotated_pair_survives_validation_network_failure(
    make_auth, respx_mock, tmp_path
):
    seed_tokens(tmp_path)
    respx_mock.post(TOKEN).respond(200, json=token_pair("new-access", "new-refresh"))
    respx_mock.get(VALIDATE).mock(side_effect=httpx.ConnectError("offline"))

    async def scenario():
        async with httpx.AsyncClient() as client:
            with pytest.raises(auth.AuthError):
                await make_auth(client).refresh("bot")

    asyncio.run(scenario())
    assert (
        json.loads((tmp_path / "tokens.json").read_text())["bot"]["refresh_token"]
        == "new-refresh"
    )


def test_invalid_refresh_preserves_store_and_requests_login(
    make_auth, respx_mock, tmp_path
):
    original = seed_tokens(tmp_path)
    respx_mock.post(TOKEN).respond(400, json={"message": "Invalid refresh token"})

    async def scenario():
        async with httpx.AsyncClient() as client:
            with pytest.raises(auth.AuthError, match="login"):
                await make_auth(client).refresh("bot")

    asyncio.run(scenario())
    assert json.loads((tmp_path / "tokens.json").read_text()) == original


def test_atomic_write_failure_leaves_old_store_and_no_tempfile(
    make_auth, device_flow, tmp_path, monkeypatch
):
    original = seed_tokens(tmp_path)
    device_flow.respond(200, json=token_pair())

    def fail_replace(*_):
        raise OSError("disk failure")

    monkeypatch.setattr(auth.os, "replace", fail_replace)

    async def scenario():
        async with httpx.AsyncClient() as client:
            with pytest.raises(auth.AuthError, match="save"):
                await make_auth(client).login("bot", lambda *_: None)

    asyncio.run(scenario())
    assert json.loads((tmp_path / "tokens.json").read_text()) == original
    assert list(tmp_path.iterdir()) == [tmp_path / "tokens.json"]


def test_cli_stores_working_token_without_printing_secrets(
    monkeypatch, device_flow, tmp_path, capsys, make_auth
):
    device_flow.respond(200, json=token_pair())
    monkeypatch.setattr(auth, "load_dotenv", lambda *_: None)
    monkeypatch.setenv("TWITCH_CLIENT_ID", "client-id")
    monkeypatch.setenv("TWITCH_BOT_LOGIN", "test_bot")
    monkeypatch.setattr(auth, "TOKEN_PATH", tmp_path / "tokens.json")
    assert auth.main(["--account", "bot"]) == 0
    output = capsys.readouterr().out
    assert "ABCD" in output and "test_bot" in output and "validated" in output
    for secret in ("access-secret", "refresh-secret", "device-secret"):
        assert secret not in output


def test_missing_client_id_fails_before_network(monkeypatch, capsys):
    monkeypatch.setattr(auth, "load_dotenv", lambda *_: None)
    monkeypatch.delenv("TWITCH_CLIENT_ID", raising=False)
    assert auth.main(["--account", "bot"]) == 1
    assert "TWITCH_CLIENT_ID" in capsys.readouterr().err


@pytest.mark.parametrize("account", ["test2", "test3"])
def test_extra_viewer_cli_keeps_other_tokens_and_requests_only_write(
    account, monkeypatch, device_flow, respx_mock, tmp_path, capsys, make_auth
):
    original = seed_tokens(tmp_path)
    login = f"viewer_{account}"
    device_flow.respond(200, json=token_pair())
    respx_mock.get(VALIDATE).respond(
        200, json=identity(login=login, scopes=["user:write:chat"])
    )
    monkeypatch.setattr(auth, "load_dotenv", lambda *_: None)
    monkeypatch.setenv("TWITCH_CLIENT_ID", "client-id")
    monkeypatch.setenv(f"TWITCH_{account.upper()}_LOGIN", login)
    monkeypatch.setattr(auth, "TOKEN_PATH", tmp_path / "tokens.json")
    assert auth.main(["--account", account]) == 0
    saved = json.loads((tmp_path / "tokens.json").read_text())
    assert all(saved[key] == value for key, value in original.items())
    assert saved[account]["login"] == login
    assert multipart_fields(respx_mock.calls[0].request)["scopes"] == "user:write:chat"
    output = capsys.readouterr().out
    assert login in output
    assert not any(secret in output for secret in ("access-secret", "refresh-secret"))


@pytest.mark.parametrize("account", ["test", "test2", "test3"])
def test_cli_explicit_refresh_does_not_start_device_login(
    account, monkeypatch, respx_mock, tmp_path, capsys
):
    original = seed_tokens(tmp_path)
    original[account] = {**token_pair(), "login": "viewer"}
    (tmp_path / "tokens.json").write_text(json.dumps(original))
    monkeypatch.setattr(auth, "load_dotenv", lambda *_: None)
    monkeypatch.setenv("TWITCH_CLIENT_ID", "client-id")
    monkeypatch.setenv(f"TWITCH_{account.upper()}_LOGIN", "viewer")
    monkeypatch.setattr(auth, "TOKEN_PATH", tmp_path / "tokens.json")
    refresh = respx_mock.post(TOKEN).respond(
        200, json=token_pair("rotated-access", "rotated-refresh")
    )
    respx_mock.get(VALIDATE).respond(
        200, json=identity(login="viewer", scopes=["user:write:chat"])
    )
    assert auth.main(["--account", account, "--refresh"]) == 0
    assert refresh.call_count == 1
    saved = json.loads((tmp_path / "tokens.json").read_text())
    assert saved[account]["refresh_token"] == "rotated-refresh"
    assert all(saved[key] == value for key, value in original.items() if key != account)
    output = capsys.readouterr().out
    assert "Refreshed and validated viewer" in output
    assert not any(secret in output for secret in ("rotated-access", "rotated-refresh"))


def test_failed_cli_refresh_prints_exact_device_login_command(
    monkeypatch, respx_mock, tmp_path, capsys
):
    seed_tokens(tmp_path)
    monkeypatch.setattr(auth, "load_dotenv", lambda *_: None)
    monkeypatch.setenv("TWITCH_CLIENT_ID", "client-id")
    monkeypatch.setenv("TWITCH_TEST_LOGIN", "test_viewer")
    monkeypatch.setattr(auth, "TOKEN_PATH", tmp_path / "tokens.json")
    respx_mock.post(TOKEN).respond(400, json={"message": "Invalid refresh token"})
    assert auth.main(["--account", "test", "--refresh"]) == 1
    assert (
        "uv run python -m minefield.twitch_api.auth --account test"
        in capsys.readouterr().err
    )
