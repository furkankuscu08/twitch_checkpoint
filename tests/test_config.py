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


def test_wp1_defaults_and_valid_zero_slack():
    config = Config()
    expected = dict(
        PER_USER_PENDING=2,
        QUEUE_MAX=30,
        ATTRIB_SLACK_S=0.3,
        ECHO_MIN_SHARE=0.6,
        ANSWER_GRACE_S=2.5,
        CANCEL_WINDOW_S=5,
        MIN_QUOTE_WORDS=2,
        QUOTE_MAX_CHARS=300,
        CHAT_MAX_CHARS=500,
        ANSWERED_MAX=50,
        MATCH_MIN_JACCARD=0.5,
        SUMMARY_S=5,
        WS_CLIENT_QUEUE_SIZE=100,
    )
    assert all(getattr(config, key) == value for key, value in expected.items())
    assert replace(config, ATTRIB_SLACK_S=0).ATTRIB_SLACK_S == 0


@pytest.mark.parametrize(
    "changes",
    [
        dict(ECHO_MIN_SHARE=1.1),
        dict(MATCH_MIN_JACCARD=-0.1),
        dict(QUEUE_MAX=0),
        dict(PER_USER_PENDING=True),
        dict(ANSWERED_MAX=0),
        dict(ATTRIB_SLACK_S=-0.1),
        dict(CANCEL_WINDOW_S=0),
    ],
)
def test_wp1_config_rejects_invalid_values(changes):
    with pytest.raises(ValueError):
        replace(Config(), **changes)
