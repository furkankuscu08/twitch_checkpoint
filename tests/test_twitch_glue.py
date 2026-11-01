import asyncio
from dataclasses import replace
from types import SimpleNamespace

from checkpoint.core.config import Config


def test_startup_retry_validation_health_and_debug_suppression():
    from checkpoint.server.twitch import Twitch

    async def run():
        calls, health, sent, deleted = [], [], [], []
        config = replace(Config(), TWITCH_RECONNECT_MIN_S=0.001, TWITCH_VALIDATE_S=0.01)
        runtime = SimpleNamespace(
            config=config,
            collect=lambda _: None,
            health=health.append,
            delete=deleted.append,
        )

        async def initialize():
            calls.append("initialize")
            if len(calls) == 1:
                raise OSError("secret")

        async def validate(account):
            calls.append("validate")
            raise OSError("secret")

        async def send(text, **kwargs):
            sent.append((text, kwargs))
            return {"is_sent": True}

        helix = SimpleNamespace(
            initialize=initialize,
            send_message=send,
            auth=SimpleNamespace(validate=validate),
        )
        twitch = Twitch(runtime, helix)
        await twitch.initialize()
        assert twitch.ready and calls == ["initialize", "initialize"]
        intent = SimpleNamespace(
            debug=True,
            reply=SimpleNamespace(
                text="Exact answer.", reply_parent_message_id="parent"
            ),
        )
        assert await twitch.send(intent) is None and not sent
        intent.debug = False
        assert (await twitch.send(intent))["is_sent"]
        assert sent == [("Exact answer.", {"reply_parent_message_id": "parent"})]
        task = asyncio.create_task(twitch.validate_loop())
        await asyncio.sleep(0.025)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert "validate" in calls and "disconnected" in health

    asyncio.run(run())
