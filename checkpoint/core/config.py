"""Plumbing defaults. Entrypoints outside core parse the environment."""

import math
from dataclasses import dataclass, fields


@dataclass(frozen=True)
class Config:
    APP_PORT: int = 8200
    ENGINE_AUDIO_PORT: int = 8766
    DEEPGRAM_REGION: str = "eu"
    ENGINE_TIMEOUT_S: float = 5
    CHAT_SEND_TIMEOUT_S: float = 5
    STT_AUDIO_QUEUE_SIZE: int = 4
    STT_LAG_WINDOW: int = 100
    STT_MAX_UTTERANCE_WORDS: int = 1000
    STT_HEARTBEAT_S: float = 2
    STT_HTTP_TIMEOUT_S: float = 2
    STT_RECONNECT_MIN_S: float = 0.5
    STT_RECONNECT_MAX_S: float = 8
    TWITCH_CHAT_QUEUE_SIZE: int = 100
    TWITCH_DEDUP_SIZE: int = 1000
    TWITCH_IO_TIMEOUT_S: float = 5
    TWITCH_KEEPALIVE_GRACE_S: float = 1
    TWITCH_RECONNECT_MIN_S: float = 0.5
    TWITCH_RECONNECT_MAX_S: float = 8

    def __post_init__(self):
        if self.DEEPGRAM_REGION not in {"eu", "us"}:
            raise ValueError("Invalid DEEPGRAM_REGION")
        for item in fields(self):
            if item.type is str:
                continue
            value = getattr(self, item.name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError(f"Invalid {item.name}")
            if item.type is int and (not isinstance(value, int) or value < 1):
                raise ValueError(f"Invalid {item.name}")
            if item.type is float and item.name != "TWITCH_KEEPALIVE_GRACE_S":
                if value <= 0:
                    raise ValueError(f"Invalid {item.name}")
        if max(self.APP_PORT, self.ENGINE_AUDIO_PORT) > 65535:
            raise ValueError("Invalid port")
        if self.STT_RECONNECT_MAX_S < self.STT_RECONNECT_MIN_S:
            raise ValueError("Invalid STT reconnect bounds")
        if self.TWITCH_RECONNECT_MAX_S < self.TWITCH_RECONNECT_MIN_S:
            raise ValueError("Invalid Twitch reconnect bounds")
