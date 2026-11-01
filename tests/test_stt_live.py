"""Explicit WP2 acceptance: a recorded TTS WAV reaches the real HTTP intake."""

import asyncio
import logging
import os
import socket
import sys
from dataclasses import replace
from pathlib import Path

import pytest
import uvicorn

from checkpoint.core.config import Config
from checkpoint.server.app import create_app

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
