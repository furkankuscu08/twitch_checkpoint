"""Explicit WP5 live acceptance using the ignored locally generated TTS WAV."""

import asyncio
import json
import os
import socket
import sys
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import uvicorn
import websockets

from minefield.core.config import Config
from minefield.server.app import create_app

pytestmark = pytest.mark.live
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("backend", ["flux", "nova3"])
def test_wav_cli_booms_reach_overlay_socket(backend):
    wav = ROOT / "data/recordings/wp5-tts.wav"
    if not wav.exists():
        pytest.skip("Generate the short WP5 TTS WAV first")

    async def scenario():
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        app = create_app(
            config=replace(Config(), GAME_PORT=port, ARM_DELAY_S=0), debug=True
        )
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        process = None
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    await asyncio.sleep(0.01)
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
                await http.post("/dock/round/start")
                for word in ("pineapple", "volcano", "banana"):
                    response = await http.post(
                        "/dock/debug/plant", json={"owner": "tts_" + word, "word": word}
                    )
                    assert response.is_success
                await app.state.runtime.tick()
                async with websockets.connect(
                    f"ws://127.0.0.1:{port}/ws/overlay"
                ) as overlay:
                    snapshot = json.loads(await overlay.recv())
                    assert all("word" not in mine for mine in snapshot["mines"])
                    environ = dict(os.environ, GAME_PORT=str(port))
                    process = await asyncio.create_subprocess_exec(
                        sys.executable,
                        "-m",
                        "minefield.stt",
                        "--wav",
                        str(wav),
                        "--backend",
                        backend,
                        "--save-raw",
                        f"data/wp5-{backend}-raw.jsonl",
                        "--save-hyps",
                        f"data/wp5-{backend}-hyps.jsonl",
                        cwd=ROOT,
                        env=environ,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    booms = []
                    async with asyncio.timeout(45):
                        while len(booms) < 3:
                            event = json.loads(await overlay.recv())
                            if event["type"] == "boom":
                                booms.append(event)
                    await asyncio.wait_for(process.communicate(), 15)
                    assert process.returncode == 0
                    assert [event["word"] for event in booms] == [
                        "pineapple",
                        "volcano",
                        "banana",
                    ]
                    assert all(event["source"] == "stt" for event in booms)
                    assert app.state.runtime.status["deepgram"] == "disconnected"
        finally:
            if process is not None and process.returncode is None:
                process.terminate()
                await process.communicate()
            server.should_exit = True
            await serving

    asyncio.run(scenario())
