"""Explicit WP2 acceptance: a recorded TTS WAV reaches the real HTTP intake."""

import asyncio
import logging
import os
import socket
import sys
import time
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import uvicorn
from dotenv import load_dotenv

from checkpoint.core.config import Config
from checkpoint.server.app import create_app
from checkpoint.stt.audio import wav_chunks
from checkpoint.stt.engine import Engine

pytestmark = pytest.mark.live
ROOT = Path(__file__).resolve().parents[1]


def test_wav_cli_hypotheses_reach_server(caplog):
    wav = ROOT / "data/recordings/wp2-tts.wav"
    if not wav.exists():
        pytest.skip("Provide the recorded 16 kHz mono TTS WAV in data/recordings")

    async def scenario():
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        app = create_app(config=replace(Config(), APP_PORT=port))
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        process = None
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    await asyncio.sleep(0.01)
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "checkpoint.stt",
                "--wav",
                str(wav),
                "--save-hyps",
                "data/wp2-wav-hyps.jsonl",
                cwd=ROOT,
                env=dict(os.environ, APP_PORT=str(port)),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(process.communicate(), 50)
            assert process.returncode == 0, "STT CLI failed; inspect safe engine logs"
            records = [r for r in caplog.records if r.name == "checkpoint.server.app"]
            hypotheses = [r.args for r in records if r.msg == "STT hypothesis %s"]
            finals = [h for h in hypotheses if h["final"]]
            assert len(finals) == 3
            assert len({h["utt_id"] for h in finals}) == 3
            for h, expected in zip(
                finals, ("pineapple", "volcano", "banana"), strict=True
            ):
                assert expected in " ".join(w["text"] for w in h["words"]).lower()
                assert all(0 <= w["start"] <= w["end"] for w in h["words"])
            for utt_id in {h["utt_id"] for h in hypotheses}:
                group = [h for h in hypotheses if h["utt_id"] == utt_id]
                assert [h["seq"] for h in group] == list(range(len(group)))
                assert len({h["utt_start_wall"] for h in group}) == 1
            statuses = [r.args for r in records if r.msg == "STT status %s"]
            assert any(s["deepgram"] == "connected" for s in statuses)
            assert statuses[-1]["deepgram"] == "disconnected"
        finally:
            if process is not None and process.returncode is None:
                process.terminate()
                await process.communicate()
            server.should_exit = True
            await serving

    with caplog.at_level(logging.DEBUG, logger="checkpoint.server.app"):
        asyncio.run(scenario())


def test_live_mic_source_stop_requests_final_from_flux(caplog):
    """Use recorded PCM to exercise mic teardown while a Flux turn is open."""
    wav = ROOT / "data/recordings/wp2-tts.wav"
    if not wav.exists():
        pytest.skip("Provide the recorded 16 kHz mono TTS WAV in data/recordings")
    load_dotenv(ROOT / ".env")
    key = os.environ.get("DEEPGRAM_API_KEY")
    if not key:
        pytest.skip("DEEPGRAM_API_KEY is required for the explicit live check")

    def hypotheses():
        return [r.args for r in caplog.records if r.msg == "STT hypothesis %s"]

    async def until(predicate):
        async with asyncio.timeout(10):
            while not predicate():
                await asyncio.sleep(0.01)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app())
        ) as http:
            engine = Engine(key, Config(), http)
            engine.mic_socket = object()
            engine.audio.start(time.time())
            running = asyncio.create_task(engine.run())
            try:
                await until(lambda: engine.audio.accepting)
                started = time.monotonic()
                for offset, pcm in wav_chunks(wav):
                    if offset >= 1.6:
                        break
                    await asyncio.sleep(max(0, started + offset - time.monotonic()))
                    engine.audio.feed(pcm, time.time())
                await until(lambda: bool(hypotheses()))
                assert not any(h["final"] for h in hypotheses())
                original_id = hypotheses()[0]["utt_id"]
                engine.mic_socket = None
                engine.audio.stop()
                await until(lambda: any(h["final"] for h in hypotheses()))
                finals = [h for h in hypotheses() if h["final"]]
                assert len(finals) == 1
                assert finals[0]["utt_id"] == original_id
                assert finals[0]["words"]
                assert not any(r.msg == "STT abort %s" for r in caplog.records)
            finally:
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)

    with caplog.at_level(logging.DEBUG, logger="checkpoint.server.app"):
        asyncio.run(scenario())
