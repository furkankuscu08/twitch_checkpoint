"""Timed chat helper checks use only MockTransport and temporary credentials."""

import asyncio
import importlib
import json
import subprocess
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from checkpoint.core.config import Config
from checkpoint.twitch_api.auth import TwitchAuth

ACCOUNTS = ("test", "test2", "test3")
LOGINS = {account: f"viewer_{account}" for account in ACCOUNTS}


def module():
    return importlib.import_module("scripts.chat_script")


def entries():
    return [
        {"account": account, "delay_s": index / 2, "text": f"private question {index}"}
        for index, account in enumerate(ACCOUNTS)
    ]


@asynccontextmanager
async def scenario(tmp_path, *, fault=None):
    state = SimpleNamespace(requests=[], sent=[], delays=[], closed=False)
    records = {
        account: {
            "access_token": f"private-access-{account}",
            "refresh_token": f"private-refresh-{account}",
            "login": login,
            "user_id": "stale-id-not-to-use",
        }
        for account, login in LOGINS.items()
    }
    if fault == "missing":
        del records["test"]
    if fault == "stored_identity":
        records["test"]["login"] = "other_viewer"
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps(records))
    original = path.read_bytes()

    async def handle(request):
        state.requests.append(request)
        if request.url.path == "/oauth2/validate":
            account = request.headers["authorization"].removeprefix("OAuth private-access-")
            if account == "test" and fault == "expired":
                return httpx.Response(401, text="private-provider-body")
            if account == "test" and fault == "validation_timeout":
                await asyncio.sleep(1)
            return httpx.Response(
                200,
                json={
                    "login": "wrong-viewer" if account == "test" and fault == "identity" else LOGINS[account],
                    "client_id": "wrong-client" if account == "test" and fault == "client_id" else "client-id",
                    "user_id": f"id-{account}",
                    "scopes": [] if account == "test" and fault == "scope" else ["user:write:chat"],
                    "expires_in": 3600,
                },
            )
        assert request.headers["client-id"] == "client-id"
        account = request.headers["authorization"].removeprefix("Bearer private-access-")
        if request.url.path == "/helix/users":
            assert request.url.params["login"] == "channel"
            if account == "test" and fault == "users":
                return httpx.Response(200, json={"data": []})
            if account == "test" and fault == "users_identity":
                return httpx.Response(200, json={"data": [{"id": "channel-id", "login": "other"}]})
            return httpx.Response(200, json={"data": [{"id": "channel-id", "login": "channel"}]})
        assert request.method == "POST" and request.url.path == "/helix/chat/messages"
        data = json.loads(request.content)
        assert data == {
            "broadcaster_id": "channel-id",
            "sender_id": f"id-{account}",
            "message": f"private question {ACCOUNTS.index(account)}",
        }
        state.sent.append(account)
        if account == "test":
            if fault in {"400", "401"}:
                return httpx.Response(int(fault), text="private-provider-body")
            if fault == "timeout":
                try:
                    await asyncio.sleep(1)
                finally:
                    state.closed = True
            if fault == "network":
                raise httpx.ConnectError("private-network-error", request=request)
            if fault == "malformed":
                return httpx.Response(200, json={"data": []})
            if fault == "drop":
                return httpx.Response(200, json={"data": [{"is_sent": False, "drop_reason": {"message": "private question"}}]})
            if fault == "nonbool":
                return httpx.Response(200, json={"data": [{"is_sent": "true"}]})
        return httpx.Response(200, json={"data": [{"is_sent": True}]})

    async def sleep(delay):
        state.delays.append(delay)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        state.auth = TwitchAuth(client, "client-id", LOGINS, path)
        state.config = replace(Config(), CHAT_SEND_TIMEOUT_S=0.03)
        state.sleep = sleep
        yield state
    assert path.read_bytes() == original


def test_timed_file_order_identity_payload_and_quiet_output(tmp_path, capsys):
    helper = module()

    async def run():
        async with scenario(tmp_path) as state:
            assert await helper.run(entries(), state.auth, state.config, "CHANNEL", sleep=state.sleep) == [True, True, True]
            assert state.delays == [0, 0.5, 1]
            assert state.sent == list(ACCOUNTS)
            assert len(state.requests) == 9

    asyncio.run(run())
    output = capsys.readouterr()
    assert output.out == "success\nsuccess\nsuccess\n"
    assert output.err == ""


@pytest.mark.parametrize("fault", ["missing", "stored_identity", "expired", "identity", "client_id", "scope", "users", "users_identity", "400", "401", "timeout", "validation_timeout", "network", "malformed", "drop", "nonbool"])
def test_failure_is_one_attempt_and_later_entries_continue(tmp_path, capsys, fault):
    helper = module()

    async def run():
        async with scenario(tmp_path, fault=fault) as state:
            assert await helper.run(entries(), state.auth, state.config, "channel", sleep=state.sleep) == [False, True, True]
            assert state.sent.count("test") <= 1
            assert state.sent[-2:] == ["test2", "test3"]
            assert all(request.url.path != "/oauth2/token" for request in state.requests)
            if fault == "timeout":
                assert state.closed

    asyncio.run(run())
    output = capsys.readouterr()
    assert output.out == "failure\nsuccess\nsuccess\n"
    assert output.err == ""


@pytest.mark.parametrize("bad", [None, {}, "private", [], [None], [{"account": "bot", "delay_s": 0, "text": "hello"}], [{"account": "test", "delay_s": True, "text": "hello"}], [{"account": "test", "delay_s": -1, "text": "hello"}], [{"account": "test", "delay_s": float("nan"), "text": "hello"}], [{"account": "test", "delay_s": float("inf"), "text": "hello"}], [{"account": "test", "delay_s": "0", "text": "hello"}], [{"account": "test", "delay_s": 0, "text": " "}], [{"account": "test", "delay_s": 0, "text": "a" * 501}], [{"account": "test", "delay_s": 0}], [{"account": "test", "delay_s": 0, "text": "hello", "extra": 1}]])
def test_invalid_script_rejected_before_any_delay_or_network(tmp_path, bad):
    helper = module()

    async def run():
        async with scenario(tmp_path) as state:
            with pytest.raises(ValueError):
                await helper.run(bad, state.auth, state.config, "channel", sleep=state.sleep)
            assert state.requests == []
            assert state.delays == []

    asyncio.run(run())


def test_entire_script_is_validated_before_first_send(tmp_path):
    helper = module()

    async def run():
        async with scenario(tmp_path) as state:
            with pytest.raises(ValueError):
                await helper.run([entries()[0], {"account": "bot"}], state.auth, state.config, "channel", sleep=state.sleep)
            assert state.requests == []

    asyncio.run(run())


def test_cancel_during_attempt_stops_later_entries_without_retry(tmp_path, capsys):
    helper = module()

    async def run():
        async with scenario(tmp_path, fault="timeout") as state:
            task = asyncio.create_task(helper.run(entries(), state.auth, Config(), "channel", sleep=state.sleep))
            while not state.sent:
                await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert state.sent == ["test"]
            assert state.closed

    asyncio.run(run())
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("content", ["not-json", '[{"account":"bot"}]', '[{"account":"test","delay_s":NaN,"text":"hello"}]'])
def test_main_invalid_file_never_loads_credentials(tmp_path, monkeypatch, capsys, content):
    helper = module()
    path = tmp_path / "script.json"
    path.write_text(content)

    def forbidden():
        pytest.fail("invalid input must not load workspace settings")

    monkeypatch.setattr(helper, "settings", forbidden)
    assert helper.main([str(path)]) == 1
    output = capsys.readouterr()
    assert output.out == "failure\n" and output.err == ""


def test_main_success_invokes_cli_once_and_propagates_failure(monkeypatch, capsys):
    helper = module()
    calls = []

    async def cli(path):
        calls.append(path)
        return [True, False]

    monkeypatch.setattr(helper, "cli", cli)
    assert helper.main(["script.json"]) == 1
    assert calls == ["script.json"]
    assert capsys.readouterr().out == ""


def test_direct_script_entrypoint_runs_without_package_install(tmp_path):
    helper = module()
    result = subprocess.run(
        [str(Path(__import__("sys").executable)), str(Path(helper.__file__).resolve()), str(tmp_path / "missing.json")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout == "failure\n"
    assert result.stderr == ""
