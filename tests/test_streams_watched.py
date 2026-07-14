import inspect

import pytest

from TwitchChannelPointsMiner.TwitchChannelPointsMiner import (
    TwitchChannelPointsMiner,
    _normalize_streams_watched,
)
from TwitchChannelPointsMiner.classes.Twitch import Twitch


def test_streams_watched_defaults_to_two():
    parameter = inspect.signature(TwitchChannelPointsMiner.__init__).parameters[
        "streams_watched"
    ]

    assert parameter.default == 2


@pytest.mark.parametrize("value", [0, 3, True, "1", None])
def test_streams_watched_invalid_values_use_default(caplog, value):
    assert _normalize_streams_watched(value) == 2
    assert "streams_watched must be either 1 or 2" in caplog.text


@pytest.mark.parametrize("value", [1, 2])
def test_streams_watched_supported_values_are_preserved(caplog, value):
    assert _normalize_streams_watched(value) == value
    assert caplog.text == ""


def test_minute_watcher_accepts_streams_watched_argument():
    parameter = inspect.signature(Twitch.send_minute_watched_events).parameters[
        "streams_watched"
    ]

    assert parameter.default == 2
