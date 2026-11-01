"""Validated defaults. Entrypoints outside core parse the environment."""

import math
from dataclasses import dataclass, fields

ECHO_MIN_CONTENT_WORDS = 3
MATCH_MIN_SHARED_WORDS = 2
SECONDS_PER_MINUTE = 60


@dataclass(frozen=True)
class Config:
    APP_PORT: int = 8200
    ENGINE_AUDIO_PORT: int = 8766
    DEEPGRAM_REGION: str = "eu"
    PER_USER_PENDING: int = 2
    QUEUE_MAX: int = 30
    ATTRIB_SLACK_S: float = 0.3
    ECHO_MIN_SHARE: float = 0.6
    ANSWER_GRACE_S: float = 2.5
    CANCEL_WINDOW_S: float = 5
    MIN_QUOTE_WORDS: int = 2
    QUOTE_MAX_CHARS: int = 300
    CHAT_MAX_CHARS: int = 500
    ANSWERED_MAX: int = 50
    MATCH_MIN_JACCARD: float = 0.5
    SUMMARY_S: float = 5
    WS_CLIENT_QUEUE_SIZE: int = 100
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
            if item.type is float and item.name not in {
                "TWITCH_KEEPALIVE_GRACE_S",
                "ATTRIB_SLACK_S",
                "ECHO_MIN_SHARE",
                "MATCH_MIN_JACCARD",
            }:
                if value <= 0:
                    raise ValueError(f"Invalid {item.name}")
        if not 0 <= self.ECHO_MIN_SHARE <= 1 or not 0 <= self.MATCH_MIN_JACCARD <= 1:
            raise ValueError("Content word shares must be between zero and one")
        if max(self.APP_PORT, self.ENGINE_AUDIO_PORT) > 65535:
            raise ValueError("Invalid port")
        if self.STT_RECONNECT_MAX_S < self.STT_RECONNECT_MIN_S:
            raise ValueError("Invalid STT reconnect bounds")
        if self.TWITCH_RECONNECT_MAX_S < self.TWITCH_RECONNECT_MIN_S:
            raise ValueError("Invalid Twitch reconnect bounds")
