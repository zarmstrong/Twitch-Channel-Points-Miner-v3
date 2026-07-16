import inspect
from types import SimpleNamespace

import pytest
import requests

from TwitchChannelPointsMiner.TwitchChannelPointsMiner import (
    TwitchChannelPointsMiner,
    _normalize_streams_watched,
)
from TwitchChannelPointsMiner.classes.Twitch import Twitch
from TwitchChannelPointsMiner.classes.Settings import Priority


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


def _watch_streamer(username, from_category=False, drops_eligible=False):
    stream = SimpleNamespace(
        update_elapsed=lambda: 0,
        spade_url=f"https://spade.test/{username}",
        encode_payload=lambda: "payload",
        campaigns=[],
    )
    return SimpleNamespace(
        username=username,
        is_online=True,
        online_at=0,
        from_category=from_category,
        channel_points=0,
        stream=stream,
        settings=SimpleNamespace(claim_drops=False),
        drops_condition=lambda: drops_eligible,
    )


def _run_one_watch_iteration(monkeypatch, streamers, streams_watched):
    twitch = Twitch.__new__(Twitch)
    twitch.running = True
    twitch.user_agent = "test-agent"
    posted = []

    monkeypatch.setattr(
        requests,
        "post",
        lambda url, **kwargs: posted.append(url) or SimpleNamespace(status_code=500),
    )
    monkeypatch.setattr(
        Twitch,
        "_Twitch__chuncked_sleep",
        lambda self, *args, **kwargs: setattr(self, "running", False),
    )

    twitch.send_minute_watched_events(
        streamers,
        [Priority.ORDER],
        streams_watched=streams_watched,
    )
    return posted


def test_minute_watcher_posts_to_two_explicit_streamers(monkeypatch):
    posted = _run_one_watch_iteration(
        monkeypatch,
        [_watch_streamer("one"), _watch_streamer("two")],
        streams_watched=2,
    )

    assert posted == ["https://spade.test/one", "https://spade.test/two"]


def test_minute_watcher_limits_category_discovery_to_one_stream(monkeypatch):
    posted = _run_one_watch_iteration(
        monkeypatch,
        [
            _watch_streamer(
                "category", from_category=True, drops_eligible=True
            ),
            _watch_streamer("explicit"),
        ],
        streams_watched=2,
    )

    assert posted == ["https://spade.test/category"]


def test_minute_watcher_stops_completed_category_stream(monkeypatch):
    posted = _run_one_watch_iteration(
        monkeypatch,
        [_watch_streamer("completed-category", from_category=True)],
        streams_watched=1,
    )

    assert posted == []
