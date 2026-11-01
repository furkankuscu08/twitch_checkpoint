"""Local assets and strict HTTP/WebSocket boundaries for Checkpoint."""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, model_validator

from checkpoint.core.config import Config

from .runtime import Runtime

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


class Focus(Input):
    on: bool


class Id(Input):
    id: str = Field(min_length=1)


class Typed(Input):
    text: str = Field(min_length=1)


class DebugChat(Input):
    user: str = Field(min_length=1, pattern=r"^[a-zA-Z0-9_]+$")
    text: str = Field(min_length=1)
    color: str = Field(default="", pattern=r"^(#[0-9a-fA-F]{6})?$")
    kind: Literal["question", "other"] | None = None


def create_app(
    *,
    config=None,
    clock=time.time,
    sender=None,
    blocklist=(),
    bot_login="checkpoint_bott",
    broadcaster_login="izleyici0808",
    debug=False,
    twitch_factory=None,
):
    config = config if config is not None else Config()
    runtime = Runtime(
        config,
        clock=clock,
        sender=sender,
        blocklist=blocklist,
        bot_login=bot_login,
        broadcaster_login=broadcaster_login,
    )

    @asynccontextmanager
    async def lifespan(app):
        tasks = [
            asyncio.create_task(runtime.run_ticks()),
            asyncio.create_task(runtime.send_loop()),
        ]
        if twitch_factory is not None:
            tasks.append(asyncio.create_task(twitch_factory(runtime)))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await runtime.hub.close()

    app = FastAPI(lifespan=lifespan)
    app.state.runtime = runtime

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
        runtime.hypothesis(body.model_dump())
        return {"ok": True}

    @app.post("/stt/status")
    async def status(body: Status):
        LOG.debug("STT status %s", body.model_dump())
        runtime.engine_status(body.model_dump())
        return {"ok": True}

    @app.post("/stt/abort")
    async def abort(body: Abort):
        LOG.debug("STT abort %s", body.model_dump())
        runtime.abort(body.utt_id)
        return {"ok": True}

    @app.post("/dock/focus")
    async def focus(body: Focus):
        runtime.core.set_focus(body.on)
        runtime.refresh()
        return {"ok": True}

    @app.post("/dock/cancel")
    async def cancel(body: Id):
        if not runtime.core.cancel_quote(body.id, now=clock()):
            raise HTTPException(409, "Quote unavailable")
        runtime.refresh("dock")
        return {"ok": True}

    @app.post("/dock/earlier")
    async def earlier(body: Id):
        if not runtime.earlier(body.id):
            raise HTTPException(409, "Earlier answer unavailable")
        return {"ok": True}

    @app.post("/dock/typed")
    async def typed(body: Typed):
        if not body.text.strip():
            raise HTTPException(422, "Answer is empty")
        runtime.emit(runtime.core.typed(body.text, now=clock()))
        return {"ok": True}

    def action(name):
        async def route():
            runtime.emit(getattr(runtime.core, name)(now=clock()))
            return {"ok": True}

        return route

    for name in ("checkpoint", "next", "skip", "clear"):
        app.post("/dock/" + name)(action(name))

    if debug:

        @app.post("/dock/debug/chat")
        async def debug_chat(body: DebugChat):
            runtime.debug_counter += 1
            message_id = f"debug-{runtime.debug_counter}"
            event = dict(
                message_id=message_id,
                chatter_user_id=body.user,
                chatter_user_login=body.user.lower(),
                chatter_user_name=body.user,
                message_type="text",
                color=body.color,
                reply=None,
                message=dict(
                    text=body.text, fragments=[dict(type="text", text=body.text)]
                ),
            )
            accepted = runtime.collect(event, debug=True)
            return {"ok": True, "id": message_id, "queued": accepted}

    @app.websocket("/ws/{audience}")
    async def websocket(socket: WebSocket, audience: str):
        if audience not in ("overlay", "dock"):
            await socket.close(code=1008)
            return
        await socket.accept()
        client = runtime.hub.add(socket, audience, runtime.snapshot(audience))
        try:
            while True:
                await socket.receive_text()
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            runtime.hub.remove(client)

    for name in ("overlay", "dock", "assets", "mic"):
        directory = ROOT / "web" / name
        if directory.is_dir():
            app.mount(
                "/" + name, StaticFiles(directory=directory, html=True), name=name
            )
    return app
