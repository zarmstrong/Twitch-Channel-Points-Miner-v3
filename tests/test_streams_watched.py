import importlib
import inspect
import logging
from types import SimpleNamespace

import pytest
import requests

from TwitchChannelPointsMiner.TwitchChannelPointsMiner import (
    TwitchChannelPointsMiner,
    _normalize_badge_drop_streamer_limit,
    _normalize_drop_progress_stall_minutes,
    _normalize_streamer_source_priority,
    _normalize_streams_watched,
)
from TwitchChannelPointsMiner.classes.Twitch import Twitch
from TwitchChannelPointsMiner.classes.Settings import Priority, StreamerSource
from TwitchChannelPointsMiner.classes.entities.Raid import Raid
from TwitchChannelPointsMiner.classes.entities.Streamer import Streamer


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


def test_drop_progress_stall_defaults_to_ten_minutes():
    mine_parameter = inspect.signature(TwitchChannelPointsMiner.mine).parameters[
        "drop_progress_stall_minutes"
    ]
    watcher_parameter = inspect.signature(
        Twitch.send_minute_watched_events
    ).parameters["drop_progress_stall_minutes"]

    assert mine_parameter.default == 10
    assert watcher_parameter.default == 10


@pytest.mark.parametrize("value", [-1, 1, 4.9, True, "10", None])
def test_drop_progress_stall_invalid_values_use_default(caplog, value):
    assert _normalize_drop_progress_stall_minutes(value) == 10
    assert "drop_progress_stall_minutes must be 0 or at least 5" in caplog.text


@pytest.mark.parametrize("value", [0, 5, 10, 12.5])
def test_drop_progress_stall_valid_values_are_preserved(caplog, value):
    assert _normalize_drop_progress_stall_minutes(value) == float(value)
    assert caplog.text == ""


def test_streamer_source_priority_default_is_immutable():
    parameter = inspect.signature(TwitchChannelPointsMiner.__init__).parameters[
        "streamer_source_priority"
    ]

    assert parameter.default == (
        StreamerSource.STREAMERS,
        StreamerSource.FOLLOWERS,
        StreamerSource.CATEGORIES,
        StreamerSource.BADGES,
    )


def _watch_streamer(
    username,
    from_category=False,
    drops_eligible=False,
    from_badge_campaign=False,
    from_followers=False,
    favorite=False,
    points=0,
    points_limit=None,
    watch_streak=False,
):
    stream = SimpleNamespace(
        update_elapsed=lambda: 0,
        spade_url=f"https://spade.test/{username}",
        encode_payload=lambda: "payload",
        campaigns=[],
        campaigns_ids=[],
        game={"displayName": username},
        game_name=lambda: username,
        watch_streak_missing=watch_streak,
        minute_watched=0,
    )
    return SimpleNamespace(
        username=username,
        is_online=True,
        is_watching=False,
        online_at=0,
        from_category=from_category,
        from_badge_campaign=from_badge_campaign,
        from_followers=from_followers,
        channel_points=points,
        offline_at=0,
        stream=stream,
        settings=SimpleNamespace(
            claim_drops=drops_eligible,
            favorite=favorite,
            points_limit=points_limit,
            watch_streak=watch_streak,
        ),
        drops_condition=lambda: drops_eligible,
    )


def _run_one_watch_iteration(
    monkeypatch,
    streamers,
    streams_watched,
    source_priority=None,
    priority=None,
    drop_inventory_progress=None,
    drop_watch_health=None,
    drop_progress_stall_minutes=10,
    now=None,
):
    twitch = Twitch.__new__(Twitch)
    twitch.running = True
    twitch.user_agent = "test-agent"
    twitch.completed_drop_campaigns = set()
    twitch.category_campaign_eligibility = {
        (
            twitch._Twitch__slugify(streamer.stream.game_name()),
            streamer.username,
        ): (1, 1)
        for streamer in streamers
        if streamer.from_category and streamer.drops_condition()
    }
    twitch.twitchdrops_app_campaigns = {}
    twitch.drop_inventory_progress = drop_inventory_progress or {}
    twitch.drop_inventory_progress_updated_at = (
        now if drop_inventory_progress and now is not None else 0
    )
    twitch.drop_watch_health = drop_watch_health or {}
    posted = []

    if now is not None:
        twitch_module = importlib.import_module(
            "TwitchChannelPointsMiner.classes.Twitch"
        )
        monkeypatch.setattr(twitch_module.time, "time", lambda: now)

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
        priority or [Priority.ORDER],
        streams_watched=streams_watched,
        source_priority=source_priority,
        drop_progress_stall_minutes=drop_progress_stall_minutes,
    )
    return posted


def _drop_progress(current=5):
    return (
        (
            "campaign-1",
            "Example campaign",
            "drop-1",
            "Example drop",
            current,
            15,
        ),
    )


def _drop_watch_health(username, progress=None, last_progress_at=0):
    return {
        "game": {
            "username": username,
            "progress": progress or _drop_progress(),
            "last_progress_at": last_progress_at,
            "blocked_until": {},
            "rotation_from": None,
            "waiting_for_alternative": None,
        }
    }


def test_minute_watcher_prioritizes_favorites(monkeypatch):
    posted = _run_one_watch_iteration(
        monkeypatch,
        [
            _watch_streamer("first"),
            _watch_streamer("favorite", favorite=True),
        ],
        streams_watched=1,
        priority=[Priority.FAVORITE, Priority.ORDER],
    )

    assert posted == ["https://spade.test/favorite"]


def test_minute_watcher_fills_slot_after_selecting_favorite(monkeypatch):
    posted = _run_one_watch_iteration(
        monkeypatch,
        [
            _watch_streamer("first"),
            _watch_streamer("favorite", favorite=True),
            _watch_streamer("third"),
        ],
        streams_watched=2,
        priority=[Priority.FAVORITE, Priority.ORDER],
    )

    assert posted == ["https://spade.test/first", "https://spade.test/favorite"]


def test_minute_watcher_skips_streamers_at_their_points_limit(monkeypatch):
    posted = _run_one_watch_iteration(
        monkeypatch,
        [
            _watch_streamer("capped", points=500, points_limit=500),
            _watch_streamer("eligible", points=499, points_limit=500),
        ],
        streams_watched=2,
    )

    assert posted == ["https://spade.test/eligible"]


def test_pending_watch_streak_bypasses_points_limit(monkeypatch):
    posted = _run_one_watch_iteration(
        monkeypatch,
        [
            _watch_streamer(
                "capped-streak",
                points=500,
                points_limit=500,
                watch_streak=True,
            )
        ],
        streams_watched=1,
        priority=[Priority.STREAK],
    )

    assert posted == ["https://spade.test/capped-streak"]


def test_pending_watch_streak_honors_explicit_zero_timestamp():
    streamer = _watch_streamer("streak", watch_streak=True)
    streamer.offline_at = -60

    assert Twitch._has_pending_watch_streak(streamer, now=0) is False


def test_minute_watcher_posts_to_two_explicit_streamers(monkeypatch):
    posted = _run_one_watch_iteration(
        monkeypatch,
        [_watch_streamer("one"), _watch_streamer("two")],
        streams_watched=2,
    )

    assert posted == ["https://spade.test/one", "https://spade.test/two"]


def test_drop_priority_applies_across_streamer_sources(monkeypatch):
    posted = _run_one_watch_iteration(
        monkeypatch,
        [
            _watch_streamer("configured-one"),
            _watch_streamer("pathofexile"),
            _watch_streamer("live-drop", from_category=True, drops_eligible=True),
        ],
        streams_watched=2,
        priority=[Priority.DROPS, Priority.ORDER],
    )

    assert posted == [
        "https://spade.test/configured-one",
        "https://spade.test/live-drop",
    ]


def test_minute_watcher_marks_only_selected_streamers_as_watched(monkeypatch):
    streamers = [_watch_streamer("selected"), _watch_streamer("waiting")]

    _run_one_watch_iteration(monkeypatch, streamers, streams_watched=1)

    assert streamers[0].is_watching is True
    assert streamers[1].is_watching is False


def test_minute_watcher_rotates_stalled_drop_streamer(monkeypatch, caplog):
    stalled = _watch_streamer("stalled", from_category=True, drops_eligible=True)
    replacement = _watch_streamer(
        "replacement", from_category=True, drops_eligible=True
    )
    for streamer in (stalled, replacement):
        streamer.stream.game_name = lambda: "Game"
    stalled.is_watching = True

    posted = _run_one_watch_iteration(
        monkeypatch,
        [stalled, replacement],
        streams_watched=1,
        drop_inventory_progress={"game": _drop_progress()},
        drop_watch_health=_drop_watch_health("stalled"),
        now=600,
    )

    assert posted == ["https://spade.test/replacement"]
    assert stalled.is_watching is False
    assert replacement.is_watching is True
    assert "rotating to another eligible Game channel (replacement)" in caplog.text


def test_minute_watcher_keeps_stalled_streamer_without_alternative(
    monkeypatch, caplog
):
    only_streamer = _watch_streamer(
        "only", from_category=True, drops_eligible=True
    )
    only_streamer.stream.game_name = lambda: "Game"
    only_streamer.is_watching = True

    posted = _run_one_watch_iteration(
        monkeypatch,
        [only_streamer],
        streams_watched=1,
        drop_inventory_progress={"game": _drop_progress()},
        drop_watch_health=_drop_watch_health("only"),
        now=600,
    )

    assert posted == ["https://spade.test/only"]
    assert "no other eligible live Game channel is available" in caplog.text


def test_drop_progress_advancement_resets_stall_timer(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    replacement = _watch_streamer(
        "replacement", from_category=True, drops_eligible=True
    )
    replacement.stream.game_name = lambda: "Game"
    replacement.is_watching = True
    health = _drop_watch_health(
        "replacement", progress=_drop_progress(current=5), last_progress_at=0
    )
    health["game"]["rotation_from"] = "stalled"

    posted = _run_one_watch_iteration(
        monkeypatch,
        [replacement],
        streams_watched=1,
        drop_inventory_progress={"game": _drop_progress(current=6)},
        drop_watch_health=health,
        now=600,
    )

    assert posted == ["https://spade.test/replacement"]
    assert health["game"]["last_progress_at"] == 600
    assert health["game"]["rotation_from"] is None
    assert "Drop progress resumed on replacement after rotating from stalled" in caplog.text


def test_stale_inventory_does_not_rotate_drop_streamer(monkeypatch, caplog):
    stalled = _watch_streamer("stalled", from_category=True, drops_eligible=True)
    replacement = _watch_streamer(
        "replacement", from_category=True, drops_eligible=True
    )
    for streamer in (stalled, replacement):
        streamer.stream.game_name = lambda: "Game"
    stalled.is_watching = True
    health = _drop_watch_health("stalled")
    twitch = Twitch.__new__(Twitch)
    twitch.completed_drop_campaigns = set()
    twitch.category_campaign_eligibility = {
        ("game", "stalled"): (1, 1),
        ("game", "replacement"): (1, 1),
    }
    twitch.twitchdrops_app_campaigns = {}
    twitch.drop_inventory_progress = {"game": _drop_progress()}
    twitch.drop_inventory_progress_updated_at = 300
    twitch.drop_watch_health = health

    cooldowns = twitch._Twitch__drop_progress_streamer_cooldowns(
        [stalled, replacement], now=600, stall_seconds=600
    )

    assert cooldowns == set()
    assert "Drop progress has not changed" not in caplog.text


def test_raid_is_joined_only_from_watched_streamer():
    joined_raids = []
    twitch = Twitch.__new__(Twitch)
    twitch.gql = SimpleNamespace(join_raid=joined_raids.append)
    watched = Streamer("watched")
    watched.is_watching = True
    waiting = Streamer("waiting")

    twitch.update_raid(watched, Raid("watched-raid", "target-one"))
    twitch.update_raid(waiting, Raid("waiting-raid", "target-two"))

    assert joined_raids == ["watched-raid"]


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
