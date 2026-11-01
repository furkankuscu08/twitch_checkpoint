"""Real local HTTP/WS UI journey; Twitch delivery is an injected recorder."""

import asyncio
import socket
import threading
from dataclasses import replace

import httpx
import pytest
import uvicorn

from checkpoint.core.config import Config
from checkpoint.server.app import create_app

pytestmark = pytest.mark.browser


def test_dock_and_overlay_share_server_state_without_real_twitch(speech):
    from playwright.sync_api import expect, sync_playwright

    now, sent = [1000.0], []

    async def sender(intent):
        sent.append(intent)
        return {"is_sent": True}

    app = create_app(
        debug=True,
        clock=lambda: now[0],
        sender=sender,
        config=replace(Config(), SERVER_TICK_S=0.01),
    )
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    base = f"http://127.0.0.1:{listener.getsockname()[1]}"
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    serving = asyncio.run_coroutine_threadsafe(server.serve(sockets=[listener]), loop)

    async def ready():
        while not server.started:
            await asyncio.sleep(0.005)

    asyncio.run_coroutine_threadsafe(ready(), loop).result(5)

    def step(time):
        async def advance():
            now[0] = time
            app.state.runtime.tick()
            await app.state.runtime.delivery.join()

        asyncio.run_coroutine_threadsafe(advance(), loop).result(5)

    try:
        with httpx.Client(base_url=base) as http, sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                dock = browser.new_page(viewport={"width": 400, "height": 900})
                overlay = browser.new_page(viewport={"width": 1920, "height": 1080})
                errors = []
                for page in (dock, overlay):
                    page.set_default_timeout(3000)
                    page.on("pageerror", lambda error: errors.append(str(error)))
                dock.goto(base + "/dock")
                overlay.goto(base + "/overlay")
                dock.get_by_role("button", name="Focus", exact=True).click()
                questions = [
                    "How do Python callbacks schedule tasks?",
                    "What editor is that?",
                    "Can you show this later?",
                ]
                for i, text in enumerate(questions):
                    assert http.post(
                        "/dock/debug/chat", json={"user": f"viewer{i}", "text": text}
                    ).is_success
                dock.get_by_role("button", name="Checkpoint", exact=True).click()
                expect(dock.locator("#card-question")).to_have_text(questions[0])
                echo = speech(questions[0], wall=1000.0, final=True)
                assert http.post("/stt/hypothesis", json=echo).is_success
                expect(dock.locator("#echo")).to_have_text(questions[0])
                step(1001.0)
                partial = speech(
                    "They run on the event loop.", wall=1001.0, utt_id="u2"
                )
                assert http.post("/stt/hypothesis", json=partial).is_success
                expect(dock.locator("#answer-partial")).to_have_text(
                    "They run on the event loop."
                )
                assert http.post(
                    "/stt/hypothesis", json={**partial, "seq": 1, "final": True}
                ).is_success
                step(1003.5)
                expect(dock.locator("#pending")).to_be_visible()
                dock.get_by_role("button", name="Cancel quote", exact=True).click()
                dock.get_by_label("Typed answer", exact=True).fill(
                    "I use my favourite editor."
                )
                dock.get_by_role("button", name="Send typed answer").click()
                expect(overlay.locator("#final-text")).to_have_text(
                    "I use my favourite editor."
                )
                dock.get_by_role("button", name="Skip", exact=True).click()
                step(1008.5)
                assert [i.reply.kind for i in sent] == ["plain", "quote"]
                assert all(i.debug for i in sent)
                assert http.post(
                    "/dock/debug/chat",
                    json={"user": "repeater", "text": "Which editor do you use?"},
                ).is_success
                dock.get_by_role("button", name="Send answer from 0 min ago").click()
                expect(dock.locator("#notice")).to_contain_text("received your answer")
                assert len(sent) == 3 and sent[-1].earlier
                assert http.post(
                    "/dock/debug/chat",
                    json={"user": "lastviewer", "text": "One more checkpoint?"},
                ).is_success
                dock.get_by_role("button", name="Checkpoint", exact=True).click()
                dock.get_by_role("button", name="Skip", exact=True).click()
                expect(dock.locator("#summary")).to_contain_text("1 from earlier")

                async def disconnect_overlay():
                    clients = [
                        c
                        for c in app.state.runtime.hub.clients
                        if c.audience == "overlay"
                    ]
                    for client in clients:
                        app.state.runtime.hub.remove(client)
                    return clients

                old = asyncio.run_coroutine_threadsafe(
                    disconnect_overlay(), loop
                ).result(5)

                async def restored():
                    async with asyncio.timeout(3):
                        while not any(
                            c.audience == "overlay" and c not in old
                            for c in app.state.runtime.hub.clients
                        ):
                            await asyncio.sleep(0.01)

                asyncio.run_coroutine_threadsafe(restored(), loop).result(4)
                assert dock.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth"
                )
                assert not errors
            finally:
                browser.close()
    finally:
        server.should_exit = True
        serving.result(5)
        loop.call_soon_threadsafe(loop.stop)
        thread.join()
        loop.close()
