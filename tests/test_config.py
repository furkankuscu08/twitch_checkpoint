import math
from dataclasses import replace

import pytest

from checkpoint.core.config import Config
from checkpoint.server.__main__ import config_from_env


def test_defaults_and_entrypoint_overrides():
    config = Config()
    assert (config.APP_PORT, config.ENGINE_AUDIO_PORT) == (8200, 8766)
    assert config.DEEPGRAM_REGION == "eu"
    assert config.CHAT_SEND_TIMEOUT_S == 5
    overridden = config_from_env(
        {"APP_PORT": "18200", "ENGINE_AUDIO_PORT": "18766", "STT_HEARTBEAT_S": "0.5"}
    )
    assert (overridden.APP_PORT, overridden.ENGINE_AUDIO_PORT) == (18200, 18766)
    assert overridden.STT_HEARTBEAT_S == 0.5
    assert config_from_env({"GAME_PORT": "8100", "STT_MIC_PORT": "8765"}) == config
    assert not hasattr(config, "GAME_PORT") and not hasattr(config, "STT_BACKEND")


@pytest.mark.parametrize(
    "changes",
    [
        {"APP_PORT": 0},
        {"APP_PORT": 65536},
        {"ENGINE_AUDIO_PORT": True},
        {"ENGINE_AUDIO_PORT": 1.5},
        {"DEEPGRAM_REGION": "other"},
        {"STT_AUDIO_QUEUE_SIZE": 0},
        {"STT_AUDIO_QUEUE_SIZE": 1.5},
        {"STT_HTTP_TIMEOUT_S": 0},
        {"ENGINE_TIMEOUT_S": -1},
        {"CHAT_SEND_TIMEOUT_S": math.nan},
        {"STT_HEARTBEAT_S": math.inf},
        {"TWITCH_IO_TIMEOUT_S": False},
        {"STT_RECONNECT_MIN_S": 9, "STT_RECONNECT_MAX_S": 8},
        {"TWITCH_RECONNECT_MIN_S": 9, "TWITCH_RECONNECT_MAX_S": 8},
    ],
)
def test_invalid_config_is_rejected(changes):
    with pytest.raises(ValueError):
        replace(Config(), **changes)


@pytest.mark.parametrize("value", ["abc", "", "1.5", "65536"])
def test_invalid_env_port_is_rejected(value):
    with pytest.raises(ValueError):
        config_from_env({"APP_PORT": value})
