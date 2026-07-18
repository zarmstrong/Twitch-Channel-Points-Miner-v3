import importlib
import inspect
from types import SimpleNamespace

import pytest
import requests

from TwitchChannelPointsMiner.TwitchChannelPointsMiner import (
    TwitchChannelPointsMiner,
    _normalize_badge_drop_streamer_limit,
    _normalize_streamer_source_priority,
    _normalize_streams_watched,
)
from TwitchChannelPointsMiner.classes.Twitch import Twitch
from TwitchChannelPointsMiner.classes.Settings import Priority, StreamerSource


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


def _watch_streamer(
    username,
    from_category=False,
    drops_eligible=False,
    from_badge_campaign=False,
    from_followers=False,
):
    stream = SimpleNamespace(
        update_elapsed=lambda: 0,
        spade_url=f"https://spade.test/{username}",
        encode_payload=lambda: "payload",
        campaigns=[],
        campaigns_ids=[],
        game={"displayName": username},
        game_name=lambda: username,
    )
    return SimpleNamespace(
        username=username,
        is_online=True,
        online_at=0,
        from_category=from_category,
        from_badge_campaign=from_badge_campaign,
        from_followers=from_followers,
        channel_points=0,
        stream=stream,
        settings=SimpleNamespace(claim_drops=drops_eligible),
        drops_condition=lambda: drops_eligible,
    )


def _run_one_watch_iteration(
    monkeypatch,
    streamers,
    streams_watched,
    source_priority=None,
):
    twitch = Twitch.__new__(Twitch)
    twitch.running = True
    twitch.user_agent = "test-agent"
    twitch.completed_drop_campaigns = set()
    twitch.category_campaign_eligibility = {
        (streamer.username, streamer.username): (1, 1)
        for streamer in streamers
        if streamer.from_category and streamer.drops_condition()
    }
    twitch.twitchdrops_app_campaigns = {}
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
        source_priority=source_priority,
    )
    return posted


def test_minute_watcher_posts_to_two_explicit_streamers(monkeypatch):
    posted = _run_one_watch_iteration(
        monkeypatch,
        [_watch_streamer("one"), _watch_streamer("two")],
        streams_watched=2,
    )

    assert posted == ["https://spade.test/one", "https://spade.test/two"]


def test_minute_watcher_uses_second_slot_for_explicit_stream(monkeypatch):
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

    assert posted == [
        "https://spade.test/explicit",
        "https://spade.test/category",
    ]


def test_minute_watcher_stops_completed_category_stream(monkeypatch):
    posted = _run_one_watch_iteration(
        monkeypatch,
        [_watch_streamer("completed-category", from_category=True)],
        streams_watched=1,
    )

    assert posted == []


def test_minute_watcher_ignores_stale_campaigns_after_category_completion(
    monkeypatch,
):
    streamer = _watch_streamer(
        "completed-category", from_category=True, drops_eligible=True
    )
    streamer.stream.game_name = lambda: "Completed Game"
    streamer.stream.campaigns_ids = ["campaign-1"]
    streamer.settings.claim_drops = True

    posted = _run_one_watch_iteration(
        monkeypatch,
        [streamer, _watch_streamer("next-streamer")],
        streams_watched=1,
    )

    assert posted == ["https://spade.test/next-streamer"]


def test_minute_watcher_backfills_slot_after_extra_category_stream(monkeypatch):
    posted = _run_one_watch_iteration(
        monkeypatch,
        [
            _watch_streamer("category-one", True, True),
            _watch_streamer("category-two", True, True),
            _watch_streamer("explicit"),
        ],
        streams_watched=2,
    )

    assert posted == [
        "https://spade.test/explicit",
        "https://spade.test/category-one",
    ]


def test_badge_source_can_be_given_first_priority(monkeypatch):
    posted = _run_one_watch_iteration(
        monkeypatch,
        [
            _watch_streamer("explicit"),
            _watch_streamer("category", True, True),
            _watch_streamer("badge", True, True, True),
        ],
        streams_watched=1,
        source_priority=[
            StreamerSource.BADGES,
            StreamerSource.STREAMERS,
            StreamerSource.CATEGORIES,
        ],
    )

    assert posted == ["https://spade.test/badge"]


def test_follower_source_can_be_prioritized_over_explicit_streamers(monkeypatch):
    posted = _run_one_watch_iteration(
        monkeypatch,
        [
            _watch_streamer("explicit"),
            _watch_streamer("followed", from_followers=True),
        ],
        streams_watched=1,
        source_priority=[
            StreamerSource.FOLLOWERS,
            StreamerSource.STREAMERS,
        ],
    )

    assert posted == ["https://spade.test/followed"]
def test_watched_streamer_log_includes_selection_reason(monkeypatch):
    messages = []
    twitch_module = importlib.import_module(
        "TwitchChannelPointsMiner.classes.Twitch"
    )
    monkeypatch.setattr(
        twitch_module.logger,
        "info",
        lambda message, **kwargs: messages.append(message),
    )
    streamers = [
        _watch_streamer("explicit"),
        _watch_streamer("campaign", True, True),
        _watch_streamer("badge", True, True, True),
    ]

    _run_one_watch_iteration(
        monkeypatch,
        streamers,
        streams_watched=2,
        source_priority=[
            StreamerSource.BADGES,
            StreamerSource.STREAMERS,
            StreamerSource.CATEGORIES,
        ],
    )

    watch_message = next(
        message for message in messages if "Watching for points:" in message
    )
    assert "badge (badge drop)" in watch_message
    assert "explicit (streamer)" in watch_message
    assert "badge (badge drop; badge drops)" in watch_message


def test_source_priority_appends_omitted_sources():
    assert _normalize_streamer_source_priority([StreamerSource.BADGES]) == [
        StreamerSource.BADGES,
        StreamerSource.STREAMERS,
        StreamerSource.FOLLOWERS,
        StreamerSource.CATEGORIES,
    ]


@pytest.mark.parametrize("value", [0, 3, True, "1", None])
def test_badge_drop_streamer_limit_rejects_values_other_than_one_or_two(
    caplog, value
):
    assert _normalize_badge_drop_streamer_limit(value) == 1
    assert "badge_drop_streamer_limit must be either 1 or 2" in caplog.text


@pytest.mark.parametrize("value", [1, 2])
def test_badge_drop_streamer_limit_accepts_one_or_two(caplog, value):
    assert _normalize_badge_drop_streamer_limit(value) == value
    assert caplog.text == ""
