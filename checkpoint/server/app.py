"""Microphone assets and validated engine intake; WP3 adds card state handling."""

import logging
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, model_validator

from checkpoint.core.config import Config

ROOT = Path(__file__).resolve().parents[2]
LOG = logging.getLogger(__name__)


class Input(BaseModel):
    model_config = ConfigDict(strict=True, allow_inf_nan=False, extra="forbid")


class Word(Input):
    text: str = Field(min_length=1)
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    prob: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def ordered_times(self):
        if self.end < self.start:
            raise ValueError("Word end precedes start")
        return self


class Hypothesis(Input):
    utt_id: str = Field(min_length=1)
    seq: int = Field(ge=0)
    final: bool
    words: list[Word]
    utt_start_wall: float
    emitted_wall: float | None = None
    lag_ms: float | None = None


class Status(Input):
    ok: bool
    backend: Literal["flux"]
    deepgram: Literal["connected", "disconnected"]
    mic: Literal["connected", "disconnected"]
    lag_ms_p95: float = Field(ge=0)
    reconnects: int = Field(ge=0)


class Abort(Input):
    utt_id: str = Field(min_length=1)


def create_app(*, config=None):
    config = config if config is not None else Config()
    app = FastAPI()

    @app.get("/mic", response_class=HTMLResponse)
    async def mic():
        page = (ROOT / "web/mic/index.html").read_text()
        replacements = {
            "__ENGINE_AUDIO_PORT__": config.ENGINE_AUDIO_PORT,
            "__STT_AUDIO_QUEUE__": config.STT_AUDIO_QUEUE_SIZE,
            "__STT_RECONNECT_MIN__": config.STT_RECONNECT_MIN_S,
            "__STT_RECONNECT_MAX__": config.STT_RECONNECT_MAX_S,
        }
        for placeholder, value in replacements.items():
            page = page.replace(placeholder, str(value))
        return page

    @app.post("/stt/hypothesis")
    async def hypothesis(body: Hypothesis):
        if len(body.words) > config.STT_MAX_UTTERANCE_WORDS:
            raise HTTPException(422, "Utterance word limit")
        LOG.debug("STT hypothesis %s", body.model_dump())
        return {"ok": True}

    @app.post("/stt/status")
    async def status(body: Status):
        LOG.debug("STT status %s", body.model_dump())
        return {"ok": True}

    @app.post("/stt/abort")
    async def abort(body: Abort):
        LOG.debug("STT abort %s", body.model_dump())
        return {"ok": True}

    app.mount("/assets", StaticFiles(directory=ROOT / "web/assets"), name="assets")
    app.mount("/mic", StaticFiles(directory=ROOT / "web/mic"), name="mic")
    return app
