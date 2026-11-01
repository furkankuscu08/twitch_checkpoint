"""Bounded PCM input. Discontinuities invalidate the entire recognition session."""

import asyncio
import time
import wave

SAMPLE_RATE = 16000
FRAME_BYTES = 640  # 20 ms, mono signed little-endian int16.
CHUNK_BYTES = 2560  # 80 ms; protocol audio format, not a tuning parameter.


def wav_chunks(path):
    with wave.open(str(path), "rb") as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (
            1,
            2,
            SAMPLE_RATE,
        ):
            raise ValueError("WAV must be uncompressed 16 kHz mono int16 PCM")
        if audio.getcomptype() != "NONE":
            raise ValueError("WAV must be uncompressed 16 kHz mono int16 PCM")
        samples = 0
        while pcm := audio.readframes(CHUNK_BYTES // 2):
            yield samples / SAMPLE_RATE, pcm
            samples += len(pcm) // 2


class AudioInput:
    def __init__(self, config):
        self.config = config
        self.queue = asyncio.Queue(maxsize=config.STT_AUDIO_QUEUE_SIZE)
        self.available = asyncio.Event()
        self.changed = asyncio.Event()
        self.ready = asyncio.Event()
        self.generation = 0
        self.connected = False
        self.accepting = False
        self.eof = False
        self.anchor = None
        self.samples = 0
        self.buffer = bytearray()
        self.last_activity = time.monotonic()

    def reset(self):
        self.generation += 1
        self.accepting = False
        self.ready.clear()
        self.anchor = None
        self.samples = 0
        self.buffer.clear()
        while not self.queue.empty():
            self.queue.get_nowait()
        self.changed.set()

    def start(self, wall):
        self.reset()
        self.eof = False
        self.connected = True
        self.anchor = wall
        self.last_activity = time.monotonic()
        self.available.set()

    def activate(self):
        # Connection setup may have dropped audio. Anchor the first resumed sample.
        self.anchor = None
        self.samples = 0
        self.buffer.clear()
        self.last_activity = time.monotonic()
        self.accepting = True
        self.ready.set()

    def stop(self):
        self.reset()
        self.connected = False
        self.available.clear()

    def feed(self, pcm, wall):
        if not isinstance(pcm, bytes) or not pcm or len(pcm) % 2:
            raise ValueError("Invalid PCM frame")
        if not self.connected or not self.accepting:
            return
        # Short losses can accumulate without any single long inactivity gap.
        # Compare received PCM duration with its wall clock before accepting it.
        drift = (
            wall - self.anchor - (self.samples + len(self.buffer) // 2) / SAMPLE_RATE
            if self.anchor is not None
            else 0
        )
        if (
            max(time.monotonic() - self.last_activity, drift)
            >= self.config.ENGINE_TIMEOUT_S
        ):
            self.reset()
            return
        self.last_activity = time.monotonic()
        if self.anchor is None:
            self.anchor = wall
        self.buffer.extend(pcm)
        while len(self.buffer) >= CHUNK_BYTES:
            if self.queue.full():
                self.reset()
                return
            chunk = bytes(self.buffer[:CHUNK_BYTES])
            del self.buffer[:CHUNK_BYTES]
            self.queue.put_nowait((self.anchor + self.samples / SAMPLE_RATE, chunk))
            self.samples += CHUNK_BYTES // 2

    async def finish(self):
        if self.accepting and self.buffer:
            await self.queue.put(
                (self.anchor + self.samples / SAMPLE_RATE, bytes(self.buffer))
            )
            self.buffer.clear()
        self.eof = True
        await self.queue.put(None)
