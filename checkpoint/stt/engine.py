"""Async audio/session lifecycle. External failures never block the game server."""

import asyncio
import json
import logging
import math
import time
import uuid
from collections import deque

import httpx
import websockets

from .audio import FRAME_BYTES, AudioInput, wav_chunks
from .backends import Mapper, connection_url, number

LOG = logging.getLogger(__name__)


class SourceChanged(Exception):
    """The current PCM timeline is no longer valid."""


class Engine:
    def __init__(
        self,
        key,
        config,
        http,
        *,
        connector=websockets.connect,
        clock=time.time,
        raw_log=None,
        hyp_log=None,
    ):
        self.key = key
        self.config = config
        self.http = http
        self.connector = connector
        self.clock = clock
        self.raw_log = raw_log
        self.hyp_log = hyp_log
        self.audio = AudioInput(config)
        self.engine_id = uuid.uuid4().hex
        self.session = 0
        self.reconnects = 0
        self.connected = False
        self.mapper = None
        self.mic_socket = None
        self.closing = False
        self.lags = deque(maxlen=config.STT_LAG_WINDOW)

    def status(self):
        ordered = sorted(self.lags)
        p95 = ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)] if ordered else 0
        return dict(
            ok=True,
            backend=self.config.STT_BACKEND,
            deepgram="connected" if self.connected else "disconnected",
            mic="connected" if self.audio.connected else "disconnected",
            lag_ms_p95=max(0, p95),
            reconnects=self.reconnects,
        )

    async def _post(self, path, body):
        try:
            async with asyncio.timeout(self.config.STT_HTTP_TIMEOUT_S):
                response = await self.http.post(
                    f"http://127.0.0.1:{self.config.GAME_PORT}/stt/{path}", json=body
                )
                response.raise_for_status()
            return True
        except (httpx.HTTPError, TimeoutError, OSError):
            LOG.warning("Game server %s delivery failed", path)
            return False

    async def _heartbeat(self):
        while True:
            status = self.status()
            await self._post("status", status)
            LOG.info(
                "backend=%s deepgram=%s mic=%s lag_p95_ms=%.0f reconnects=%d",
                status["backend"],
                status["deepgram"],
                status["mic"],
                status["lag_ms_p95"],
                status["reconnects"],
            )
            await asyncio.sleep(self.config.STT_HEARTBEAT_S)

    def _save(self, sink, row):
        if sink is not None:
            sink.write(json.dumps(row) + "\n")
            sink.flush()

    async def _abort(self):
        if self.mapper is not None:
            utt_id = self.mapper.abort()
            if utt_id is not None:
                await self._post("abort", {"utt_id": utt_id})
        self.mapper = None

    async def _send(self, socket, generation):
        while True:
            try:
                item = await asyncio.wait_for(
                    self.audio.queue.get(), self.config.STT_HEARTBEAT_S * 2
                )
            except TimeoutError:
                # Verified 2026-10-29: KeepAlive is documented only for Nova.
                # https://developers.deepgram.com/docs/audio-keep-alive
                if self.config.STT_BACKEND == "nova3":
                    await socket.send(json.dumps({"type": "KeepAlive"}))
                continue
            if self.audio.generation != generation:
                raise SourceChanged
            if item is None:
                self.closing = True
                await socket.send(json.dumps({"type": "CloseStream"}))
                return
            wall, pcm = item
            if self.mapper is None:
                self.mapper = Mapper(
                    self.config.STT_BACKEND,
                    self.engine_id,
                    self.session,
                    wall,
                    config=self.config,
                )
            await socket.send(pcm)

    async def _receive(self, socket, generation):
        try:
            async for raw in socket:
                if self.audio.generation != generation:
                    raise SourceChanged
                now = self.clock()
                message = json.loads(raw)
                if not isinstance(message, dict):
                    raise ValueError("Invalid Deepgram message")
                self._save(self.raw_log, {"received_wall": now, "message": message})
                if message.get("type") == "Error":
                    raise ValueError("Deepgram reported an error")
                if self.mapper is None:
                    continue
                for hypothesis in self.mapper.process(message, now):
                    self._save(self.hyp_log, hypothesis)
                    self.lags.append(hypothesis["lag_ms"])
                    if not await self._post("hypothesis", hypothesis):
                        await self._post("abort", {"utt_id": hypothesis["utt_id"]})
                        self.mapper.abort()
                        raise SourceChanged
        except websockets.ConnectionClosed:
            if not self.closing:
                raise

    async def _watch(self, generation):
        while self.audio.generation == generation:
            try:
                await asyncio.wait_for(
                    self.audio.changed.wait(), self.config.ENGINE_TIMEOUT_S
                )
                self.audio.changed.clear()
            except TimeoutError:
                if (
                    not self.audio.eof
                    and time.monotonic() - self.audio.last_activity
                    >= self.config.ENGINE_TIMEOUT_S
                ):
                    self.audio.stop()
                    if self.mic_socket is not None:
                        await self.mic_socket.close(code=1013)
        raise SourceChanged

    async def _session(self):
        # Verified 2026-10-29: Token auth, EU v1/v2 and raw linear16, 80 ms.
        # https://developers.deepgram.com/changelog/2025/12/3
        # https://developers.deepgram.com/docs/flux/quickstart
        async with self.connector(
            connection_url(self.config),
            additional_headers={"Authorization": f"Token {self.key}"},
            max_queue=self.config.STT_AUDIO_QUEUE_SIZE,
            open_timeout=self.config.ENGINE_TIMEOUT_S,
            close_timeout=self.config.STT_HTTP_TIMEOUT_S,
        ) as socket:
            if not self.audio.connected:
                return
            self.connected = True
            self.closing = False
            generation = self.audio.generation
            self.audio.changed.clear()
            self.audio.activate()
            sender = asyncio.create_task(self._send(socket, generation))
            reader = asyncio.create_task(self._receive(socket, generation))
            watcher = asyncio.create_task(self._watch(generation))
            try:
                done, _ = await asyncio.wait(
                    (sender, reader, watcher), return_when=asyncio.FIRST_COMPLETED
                )
                # Check failures before allowing a normal end-of-WAV drain.
                for task in done:
                    task.result()
                if sender in done and self.closing:
                    # Flux CloseStream flushes interim Updates, not EndOfTurn.
                    # Never synthesize a final from this expected close.
                    # https://developers.deepgram.com/docs/flux/close-stream
                    await asyncio.wait_for(reader, self.config.ENGINE_TIMEOUT_S)
                elif not self.closing:
                    raise SourceChanged
            finally:
                for task in (sender, reader, watcher):
                    task.cancel()
                await asyncio.gather(sender, reader, watcher, return_exceptions=True)
                if not self.closing:
                    try:
                        await asyncio.wait_for(
                            socket.send(json.dumps({"type": "CloseStream"})),
                            self.config.STT_HTTP_TIMEOUT_S,
                        )
                    except (OSError, TimeoutError, websockets.ConnectionClosed):
                        pass

    async def _sessions(self):
        delay = self.config.STT_RECONNECT_MIN_S
        while True:
            await self.audio.available.wait()
            self.session += 1
            if self.session > 1:
                self.reconnects += 1
            try:
                await self._session()
                delay = self.config.STT_RECONNECT_MIN_S
            except websockets.InvalidStatus as exc:
                if exc.response.status_code in {401, 403}:
                    raise ValueError(
                        "Deepgram rejected authentication or access"
                    ) from None
                LOG.warning("Deepgram connection rejected")
            except (
                OSError,
                TimeoutError,
                websockets.WebSocketException,
                ValueError,
                KeyError,
                TypeError,
                IndexError,
                SourceChanged,
            ):
                LOG.warning(
                    "Recognition session interrupted; discarding pending speech"
                )
            finally:
                self.connected = False
                self.audio.reset()
                await self._abort()
            if self.audio.eof:
                return
            if self.audio.connected:
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.config.STT_RECONNECT_MAX_S)

    async def _wav(self, path):
        chunks = iter(wav_chunks(path))
        first = next(chunks, None)  # Validate before waiting for a connection.
        if first is None:
            raise ValueError("WAV contains no audio")
        self.audio.start(self.clock())
        await self.audio.ready.wait()
        started = time.monotonic()
        offset, pcm = first
        while True:
            await asyncio.sleep(max(0, started + offset - time.monotonic()))
            self.audio.feed(pcm, self.clock())
            next_chunk = next(chunks, None)
            if next_chunk is None:
                await asyncio.sleep(len(pcm) / 32000)
                break
            offset, pcm = next_chunk
        await self.audio.finish()

    async def run(self, wav_path=None):
        heartbeat = asyncio.create_task(self._heartbeat())
        sessions = asyncio.create_task(self._sessions())
        producer = (
            asyncio.create_task(self._wav(wav_path)) if wav_path is not None else None
        )
        try:
            if producer is not None:
                await asyncio.gather(producer, sessions)
            else:
                await sessions
        finally:
            for task in (heartbeat, sessions, producer):
                if task is not None:
                    task.cancel()
            await asyncio.gather(
                *(t for t in (heartbeat, sessions, producer) if t is not None),
                return_exceptions=True,
            )
            self.connected = False
            self.audio.stop()
            await self._abort()
            status = dict(self.status(), ok=False)
            await self._post("status", status)

    async def mic(self, socket):
        try:
            if socket.request.path != "/audio":
                await socket.close(code=1008)
                return
            hello = json.loads(
                await asyncio.wait_for(socket.recv(), self.config.ENGINE_TIMEOUT_S)
            )
            if (
                not isinstance(hello, dict)
                or hello.get("type") != "hello"
                or hello.get("sample_rate") != 16000
            ):
                raise ValueError("Invalid mic hello")
            number(hello["client_start_wall"])
            old = self.mic_socket
            self.mic_socket = socket
            self.audio.start(self.clock())
            if old is not None:
                await old.close(code=1012)
            async for pcm in socket:
                if self.mic_socket is not socket:
                    break
                if not isinstance(pcm, bytes) or len(pcm) != FRAME_BYTES:
                    raise ValueError("Invalid mic PCM frame")
                self.audio.feed(pcm, self.clock())
        except (
            ValueError,
            KeyError,
            TypeError,
            TimeoutError,
            websockets.ConnectionClosed,
        ):
            await socket.close(code=1008)
        finally:
            if self.mic_socket is socket:
                self.mic_socket = None
                self.audio.stop()
