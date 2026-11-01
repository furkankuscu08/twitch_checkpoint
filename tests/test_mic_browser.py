"""One fake-audio Chromium test: PCM delivery and engine restart recovery."""

import asyncio
import socket
import threading
import time
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import uvicorn
import websockets

from minefield.core.config import Config
from minefield.server.app import create_app
from minefield.stt.engine import Engine

pytestmark = pytest.mark.browser


def test_fake_mic_frames_arrive_and_page_reconnects_after_engine_restart():
    from playwright.sync_api import expect, sync_playwright

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    received = []
    state = {}
    game_listener = socket.socket()
    game_listener.bind(("127.0.0.1", 0))
    game_port = game_listener.getsockname()[1]
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        mic_port = reservation.getsockname()[1]
    config = replace(Config(), GAME_PORT=game_port, STT_MIC_PORT=mic_port)

    class Connection:
        async def __aenter__(self):
            index = len(received)
            received.append(0)

            class Deepgram:
                async def send(self, raw):
                    if isinstance(raw, bytes):
                        assert len(raw) == 2560
                        received[index] += 1

                def __aiter__(self):
                    return self

                async def __anext__(self):
                    await asyncio.Future()

            return Deepgram()

        async def __aexit__(self, *args):
            pass

    async def start_engine():
        http = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200))
        )
        engine = Engine(
            "fake-key", config, http, connector=lambda *a, **k: Connection()
        )
        state["http"] = http
        state["engine"] = asyncio.create_task(engine.run())
        state["mic"] = await websockets.serve(engine.mic, "127.0.0.1", mic_port)

    async def stop_engine():
        state["mic"].close()
        await state["mic"].wait_closed()
        state["engine"].cancel()
        await asyncio.gather(state["engine"], return_exceptions=True)
        await state["http"].aclose()

    async def setup():
        server = uvicorn.Server(
            uvicorn.Config(create_app(config=config), log_level="error")
        )
        state["server"] = server
        state["serving"] = asyncio.create_task(server.serve(sockets=[game_listener]))
        while not server.started:
            await asyncio.sleep(0.01)
        await start_engine()

    async def restart():
        await stop_engine()
        await start_engine()

    async def cleanup():
        await stop_engine()
        state["server"].should_exit = True
        await state["serving"]

    def until(predicate):
        deadline = time.monotonic() + 8
        while not predicate() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert predicate()

    try:
        asyncio.run_coroutine_threadsafe(setup(), loop).result(5)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                args=[
                    "--use-fake-device-for-media-stream",
                    "--use-fake-ui-for-media-stream",
                ]
            )
            try:
                page = browser.new_page(
                    permissions=["microphone"], viewport={"width": 1000, "height": 900}
                )
                page.goto(f"http://127.0.0.1:{game_port}/mic")
                page.get_by_role("button", name="Start microphone", exact=True).click()
                expect(page.locator("#connection")).to_have_text("Connected")
                until(lambda: received and received[0] >= 3)
                asyncio.run_coroutine_threadsafe(restart(), loop).result(5)
                until(lambda: len(received) >= 2 and received[-1] >= 3)
                expect(page.locator("#connection")).to_have_text("Connected")
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth"
                )
                shots = Path(__file__).resolve().parents[1] / "data/shots"
                shots.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(shots / "mic-fake.png"))
                page.get_by_role("button", name="Stop microphone", exact=True).click()
                expect(page.locator("#connection")).to_have_text("Stopped")
            finally:
                browser.close()
    finally:
        if "mic" in state:
            asyncio.run_coroutine_threadsafe(cleanup(), loop).result(8)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)
        loop.close()
