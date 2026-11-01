"""Blind recording helper: fake provider calls and one real local dock socket."""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from websockets.asyncio.server import serve

from minefield.core.config import Config
from minefield.twitch_api.auth import TwitchAuth
from scripts import blind_plant

ACCOUNTS = ("test", "test2", "test3")
LOGINS = {account: f"viewer_{account}" for account in ACCOUNTS}


@asynccontextmanager
async def scenario(tmp_path, respx_mock, *, existing=False, fault=None, running=True):
    state = SimpleNamespace(connections=0, sent=[], picks=[], delays=[])

    async def dock(socket):
        state.connections += 1
        state.socket = socket
        mines = []
        if existing:
            mines.append({"owner": LOGINS["test"], "status": "armed"})
        await socket.send(
            json.dumps(
                {"type": "snapshot", "round": {"running": running}, "mines": mines}
            )
        )
        await socket.wait_closed()

    async with serve(dock, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        config = replace(
            Config(),
            GAME_PORT=port,
            BLIND_PLANT_WINDOW_S=0,
            BLIND_PLANT_CONFIRM_TIMEOUT_S=0.08,
            TWITCH_IO_TIMEOUT_S=0.1,
            CHAT_SEND_TIMEOUT_S=0.1,
        )
        records = {
            account: {
                "access_token": f"access-{account}",
                "refresh_token": f"refresh-{account}",
                "login": login,
                "user_id": f"id-{account}",
            }
            for account, login in LOGINS.items()
        }
        if fault == "missing":
            del records["test"]
        path = tmp_path / "tokens.json"
        path.write_text(json.dumps(records))
        state.original = path.read_bytes()

        async def validate(request):
            account = request.headers["authorization"].removeprefix("OAuth access-")
            if account == "test" and fault == "expired":
                return httpx.Response(401)
            return httpx.Response(
                200,
                json={
                    "login": "wrong_viewer"
                    if account == "test" and fault == "identity"
                    else LOGINS[account],
                    "client_id": "client-id",
                    "user_id": f"id-{account}",
                    "scopes": ["user:write:chat"],
                    "expires_in": 3600,
                },
            )

        respx_mock.get("https://id.twitch.tv/oauth2/validate").mock(
            side_effect=validate
        )
        respx_mock.get("https://api.twitch.tv/helix/users").respond(
            200, json={"data": [{"id": "channel-id", "login": "channel"}]}
        )
        respx_mock.get("http://127.0.0.1:8101/api/hand").respond(
            200,
            json={"hand_id": "hand-id", "cards": ["private-card-a", "private-card-b"]},
        )

        def pick(request):
            data = json.loads(request.content)
            state.picks.append(data)
            code = "private-card-a" if fault == "code" else "ABCD"
            return httpx.Response(200, json={"code": code, "command": f"!mine {code}"})

        respx_mock.post("http://127.0.0.1:8101/api/pick").mock(side_effect=pick)

        async def chat(request):
            data = json.loads(request.content)
            account = data["sender_id"].removeprefix("id-")
            state.sent.append(account)
            assert data["message"] == "!mine ABCD"
            assert data["broadcaster_id"] == "channel-id"
            assert request.headers["authorization"] == f"Bearer access-{account}"
            if account == "test" and fault == "slow":
                await asyncio.sleep(10)
            if account == "test" and fault == "bad":
                return httpx.Response(200, json={"data": []})
            if account == "test" and fault == "chat401":
                return httpx.Response(401)
            if account == "test" and fault == "drop":
                return httpx.Response(200, json={"data": [{"is_sent": False}]})
            if fault == "disconnect":
                await state.socket.close()
            elif not (account == "test" and fault == "unconfirmed"):
                # Confirmation can arrive before the send response.
                await state.socket.send("{bad json")
                await state.socket.send(
                    json.dumps({"type": "mine_planted", "owner": None})
                )
                await state.socket.send(
                    json.dumps({"type": "mine_planted", "owner": LOGINS[account]})
                )
            return httpx.Response(200, json={"data": [{"is_sent": True}]})

        respx_mock.post("https://api.twitch.tv/helix/chat/messages").mock(
            side_effect=chat
        )
        async with httpx.AsyncClient() as client:
            state.auth = TwitchAuth(client, "client-id", LOGINS, path)
            state.config = config
            yield state
        assert path.read_bytes() == state.original


def test_real_path_uses_one_shared_socket_and_never_outputs_selections(
    tmp_path, respx_mock, capsys
):
    async def run():
        async with scenario(tmp_path, respx_mock) as state:
            result = await blind_plant.run(state.auth, state.config, "channel")
            assert all(result.values())
            assert state.connections == 1
            assert sorted(state.sent) == sorted(ACCOUNTS)
            assert len(state.picks) == 3
            assert all(pick["hand_id"] == "hand-id" for pick in state.picks)
            assert all(
                pick["word"] in {"private-card-a", "private-card-b"}
                for pick in state.picks
            )

    asyncio.run(run())
    output = capsys.readouterr()
    assert sorted(output.out.splitlines()) == sorted(
        f"{owner}: success" for owner in LOGINS.values()
    )
    assert not output.err
    assert "private-card" not in output.out
    assert "ABCD" not in output.out
    assert "access-" not in output.out


@pytest.mark.parametrize(
    "fault",
    ["expired", "missing", "identity", "chat401", "slow", "bad", "drop", "unconfirmed"],
)
def test_one_account_failure_never_blocks_others(tmp_path, respx_mock, capsys, fault):
    async def run():
        async with scenario(tmp_path, respx_mock, fault=fault) as state:
            result = await blind_plant.run(state.auth, state.config, "channel")
            assert result == {"test": False, "test2": True, "test3": True}
            assert state.sent.count("test") <= 1

    asyncio.run(run())
    output = capsys.readouterr()
    assert "private-card" not in output.out + output.err
    if fault in {"expired", "missing", "identity", "chat401"}:
        assert "WARNING" in output.err
        assert "viewer_test" in output.err
        assert (
            "uv run python -m minefield.twitch_api.auth --account test --refresh"
            in output.err
        )


def test_existing_mine_is_skipped_without_hiding_other_successes(tmp_path, respx_mock):
    async def run():
        async with scenario(tmp_path, respx_mock, existing=True) as state:
            result = await blind_plant.run(state.auth, state.config, "channel")
            assert result == {"test": False, "test2": True, "test3": True}
            assert "test" not in state.sent

    asyncio.run(run())


def test_dock_disconnect_fails_unconfirmed_accounts(tmp_path, respx_mock):
    async def run():
        async with scenario(tmp_path, respx_mock, fault="disconnect") as state:
            result = await blind_plant.run(state.auth, state.config, "channel")
            assert not any(result.values())
            assert state.connections == 1

    asyncio.run(run())


def test_cancellation_closes_shared_socket_and_all_workers(tmp_path, respx_mock):
    async def run():
        async with scenario(tmp_path, respx_mock, fault="slow") as state:
            task = asyncio.create_task(
                blind_plant.run(state.auth, state.config, "channel")
            )
            while not state.sent:
                await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            async with asyncio.timeout(1):
                await state.socket.wait_closed()
            assert not any(
                task.get_name().startswith("blind-plant-")
                for task in asyncio.all_tasks()
            )

    asyncio.run(run())


def test_independent_random_delays_and_choices(tmp_path, respx_mock):
    class Random:
        def __init__(self):
            self.delays = []
            self.choices = []

        def uniform(self, low, high):
            self.delays.append((low, high))
            return 0

        def choice(self, cards):
            self.choices.append(tuple(cards))
            return cards[-1]

    rng = Random()

    async def run():
        async with scenario(tmp_path, respx_mock) as state:
            config = replace(state.config, BLIND_PLANT_WINDOW_S=60)
            assert all(
                (await blind_plant.run(state.auth, config, "channel", rng=rng)).values()
            )
            assert all(pick["word"] == "private-card-b" for pick in state.picks)

    asyncio.run(run())
    assert rng.delays == [(0, 60)] * 3
    assert len(rng.choices) == 3


def test_invalid_code_is_never_forwarded_to_twitch(tmp_path, respx_mock, capsys):
    async def run():
        async with scenario(tmp_path, respx_mock, fault="code") as state:
            result = await blind_plant.run(state.auth, state.config, "channel")
            assert not any(result.values())
            assert not state.sent

    asyncio.run(run())
    output = capsys.readouterr()
    assert "private-card" not in output.out + output.err


def test_inactive_round_makes_no_plant_attempts(tmp_path, respx_mock):
    async def run():
        async with scenario(tmp_path, respx_mock, running=False) as state:
            result = await blind_plant.run(state.auth, state.config, "channel")
            assert not any(result.values())
            assert not state.picks
            assert not state.sent

    asyncio.run(run())


def test_entrypoint_uses_configured_ports_and_delay(monkeypatch):
    monkeypatch.setattr(blind_plant, "settings", lambda: ("client-id", LOGINS))
    monkeypatch.setenv("GAME_PORT", "18100")
    monkeypatch.setenv("PLANT_PORT", "18101")
    monkeypatch.setenv("BLIND_PLANT_WINDOW_S", "17")
    monkeypatch.setenv("TWITCH_BROADCASTER_LOGIN", "channel")

    async def fake_run(auth, config, broadcaster):
        assert config.GAME_PORT == 18100 and config.PLANT_PORT == 18101
        assert config.BLIND_PLANT_WINDOW_S == 17
        assert broadcaster == "channel"
        return {"test": True}

    monkeypatch.setattr(blind_plant, "run", fake_run)
    assert blind_plant.main() == 0
