import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from TwitchChannelPointsMiner.WatchStreakCache import (
    STALE_SESSION_TTL_SECONDS,
    WatchStreakCache,
)
from TwitchChannelPointsMiner.classes.Chat import ChatPresence
from TwitchChannelPointsMiner.classes.entities.Streamer import Streamer
from TwitchChannelPointsMiner.classes.gql.Errors import RetryError
from TwitchChannelPointsMiner.classes.Twitch import Twitch
from TwitchChannelPointsMiner.constants import WATCH_STREAK_STREAM_GAP_SECONDS


def make_streamer(username):
    return Streamer(username, settings=SimpleNamespace(chat=ChatPresence.NEVER))


def make_streak_streamer(tmp_path, username="channel", broadcast_id="broadcast-1"):
    cache = WatchStreakCache.load(tmp_path / "watch-streak.json", "viewer")
    streamer = Streamer(
        username,
        settings=SimpleNamespace(
            chat=ChatPresence.NEVER,
            watch_streak=True,
        ),
    )
    streamer.channel_id = "channel-1"
    streamer.watch_streak_cache = cache
    streamer.stream.broadcast_id = broadcast_id
    streamer.stream.watch_streak_missing = True
    return streamer, cache


def twitch_with_streak_responses(live_stream, milestone):
    gql = SimpleNamespace(
        get_stream_live_status=lambda channel_id: SimpleNamespace(
            channel_id=channel_id, stream=live_stream
        ),
        get_reward_list=lambda channel_id: SimpleNamespace(
            watch_streak_milestone=milestone
        ),
    )
    twitch = Twitch.__new__(Twitch)
    twitch.gql = gql
    return twitch


def test_claimed_broadcast_is_restored_after_restart(tmp_path, monkeypatch):
    cache_path = tmp_path / "watch-streak.json"
    cache = WatchStreakCache.load(cache_path, "Viewer")
    streamer = make_streamer("Channel")
    streamer.watch_streak_cache = cache
    streamer.stream.broadcast_id = "broadcast-1"

    monkeypatch.setattr(
        "TwitchChannelPointsMiner.classes.entities.Streamer.time.time",
        lambda: 2_000_000_000.0,
    )
    monkeypatch.setattr(
        "TwitchChannelPointsMiner.WatchStreakCache.time.time",
        lambda: 2_000_000_000.0,
    )
    streamer.set_online()
    streamer.update_history("WATCH_STREAK", earned=450)

    restored = WatchStreakCache.load(cache_path, "viewer")
    restarted_streamer = make_streamer("channel")
    restarted_streamer.watch_streak_cache = restored
    restarted_streamer.stream.broadcast_id = "broadcast-1"
    restarted_streamer.set_online()

    assert restarted_streamer.stream.watch_streak_missing is False
    assert restored.get("channel", "broadcast-1").claimed is True
    assert restored.get("channel", "broadcast-1").claimed_at == 2_000_000_000


def test_new_broadcast_gets_a_new_pending_session(tmp_path):
    cache = WatchStreakCache.load(tmp_path / "watch-streak.json", "viewer")
    old_session = cache.ensure("channel", "broadcast-1", started_at=100)
    cache.mark_claimed("channel", "broadcast-1", claimed_at=110)

    new_session = cache.ensure("channel", "broadcast-2", started_at=200)

    assert old_session.ended_at == 200
    assert new_session.claimed is False


def test_short_restart_inherits_the_claimed_logical_stream(tmp_path):
    start = 2_000_000_000.0
    cache = WatchStreakCache.load(tmp_path / "watch-streak.json", "viewer")
    cache.ensure("channel", "broadcast-1", started_at=start)
    cache.mark_claimed("channel", "broadcast-1", claimed_at=start + 60)
    cache.mark_ended("channel", "broadcast-1", ended_at=start + 1000)

    cache.ensure("channel", "broadcast-2", started_at=start + 1100)

    assert cache.claimed_at_for_logical_stream("channel", "broadcast-2") == start + 60


def test_short_restart_chains_back_to_an_older_claim(tmp_path):
    start = 2_000_000_000.0
    cache = WatchStreakCache.load(tmp_path / "watch-streak.json", "viewer")
    cache.ensure("channel", "broadcast-1", started_at=start)
    cache.mark_claimed("channel", "broadcast-1", claimed_at=start + 60)
    cache.mark_ended("channel", "broadcast-1", ended_at=start + 1000)
    cache.ensure("channel", "broadcast-2", started_at=start + 1100)
    cache.mark_ended("channel", "broadcast-2", ended_at=start + 1500)

    cache.ensure("channel", "broadcast-3", started_at=start + 1600)

    assert cache.claimed_at_for_logical_stream("channel", "broadcast-3") == start + 60


def test_long_restart_does_not_inherit_the_previous_claim(tmp_path):
    start = 2_000_000_000.0
    cache = WatchStreakCache.load(tmp_path / "watch-streak.json", "viewer")
    cache.ensure("channel", "broadcast-1", started_at=start)
    cache.mark_claimed("channel", "broadcast-1", claimed_at=start + 60)
    cache.mark_ended("channel", "broadcast-1", ended_at=start + 1000)

    cache.ensure(
        "channel",
        "broadcast-2",
        started_at=start + 1000 + WATCH_STREAK_STREAM_GAP_SECONDS,
    )

    assert cache.claimed_at_for_logical_stream("channel", "broadcast-2") is None


def test_short_restart_keeps_an_unclaimed_stream_pending(tmp_path):
    start = 2_000_000_000.0
    cache = WatchStreakCache.load(tmp_path / "watch-streak.json", "viewer")
    cache.ensure("channel", "broadcast-1", started_at=start)
    cache.mark_ended("channel", "broadcast-1", ended_at=start + 1000)

    cache.ensure("channel", "broadcast-2", started_at=start + 1100)

    assert cache.claimed_at_for_logical_stream("channel", "broadcast-2") is None


def test_unobserved_restart_does_not_inherit_the_previous_claim(tmp_path):
    start = 2_000_000_000.0
    cache = WatchStreakCache.load(tmp_path / "watch-streak.json", "viewer")
    cache.ensure("channel", "broadcast-1", started_at=start)
    cache.mark_claimed("channel", "broadcast-1", claimed_at=start + 60)

    # The miner never saw the first broadcast end. ensure() closes it at the
    # second broadcast's start, so the zero gap must not prove a continuation.
    cache.ensure("channel", "broadcast-2", started_at=start + 1000)

    assert cache.claimed_at_for_logical_stream("channel", "broadcast-2") is None


def test_short_restart_after_a_claim_does_not_rearm_the_streamer(tmp_path, monkeypatch):
    cache = WatchStreakCache.load(tmp_path / "watch-streak.json", "viewer")
    streamer = make_streamer("channel")
    streamer.watch_streak_cache = cache

    current_time = [2_000_000_000.0]
    monkeypatch.setattr(
        "TwitchChannelPointsMiner.classes.entities.Streamer.time.time",
        lambda: current_time[0],
    )
    monkeypatch.setattr(
        "TwitchChannelPointsMiner.WatchStreakCache.time.time",
        lambda: current_time[0],
    )

    streamer.stream.broadcast_id = "broadcast-1"
    streamer.set_online()
    streamer.update_history("WATCH_STREAK", earned=450)
    current_time[0] += 100
    streamer.set_offline()

    # Restart two minutes after the previous broadcast ended; Twitch still
    # treats it as the same already-claimed stream.
    current_time[0] += 120
    streamer.stream.broadcast_id = "broadcast-2"
    streamer.set_online()

    assert streamer.stream.watch_streak_missing is False
    assert cache.get("channel", "broadcast-2").claimed is False


def test_streak_confirmed_before_online_transition_is_not_refarmed(
    tmp_path, monkeypatch
):
    cache = WatchStreakCache.load(tmp_path / "watch-streak.json", "viewer")
    streamer = make_streamer("channel")
    streamer.watch_streak_cache = cache

    current_time = [2_000_000_000.0]
    monkeypatch.setattr(
        "TwitchChannelPointsMiner.classes.entities.Streamer.time.time",
        lambda: current_time[0],
    )
    monkeypatch.setattr(
        "TwitchChannelPointsMiner.WatchStreakCache.time.time",
        lambda: current_time[0],
    )

    streamer.stream.broadcast_id = "broadcast-1"
    streamer.set_online()
    streamer.set_offline()

    # The next broadcast starts, but Twitch confirms the streak before the
    # miner has refreshed broadcast_id past the previous broadcast. The
    # confirmation must still count for the broadcast about to be tracked.
    current_time[0] += 200
    streamer.stream_up = current_time[0] - 50
    streamer.update_history("WATCH_STREAK", earned=450)
    assert streamer.watch_streak_confirmed_at == current_time[0]

    current_time[0] += 1
    streamer.stream.broadcast_id = "broadcast-2"
    streamer.set_online()

    assert streamer.stream.watch_streak_missing is False
    assert cache.get("channel", "broadcast-2").claimed is True


def test_streak_confirmed_before_previous_broadcast_ended_is_refarmed(
    tmp_path, monkeypatch
):
    cache = WatchStreakCache.load(tmp_path / "watch-streak.json", "viewer")
    streamer = make_streamer("channel")
    streamer.watch_streak_cache = cache

    current_time = [2_000_000_000.0]
    monkeypatch.setattr(
        "TwitchChannelPointsMiner.classes.entities.Streamer.time.time",
        lambda: current_time[0],
    )
    monkeypatch.setattr(
        "TwitchChannelPointsMiner.WatchStreakCache.time.time",
        lambda: current_time[0],
    )

    streamer.stream.broadcast_id = "broadcast-1"
    streamer.set_online()
    streamer.update_history("WATCH_STREAK", earned=450)
    current_time[0] += 1
    streamer.set_offline()

    # A new broadcast begins without the miner seeing a fresh stream-up
    # message, so the stale stream_up timestamp must not make the previous
    # broadcast's confirmation look like it belongs to this one.
    current_time[0] += WATCH_STREAK_STREAM_GAP_SECONDS + 1
    streamer.stream.broadcast_id = "broadcast-2"
    streamer.set_online()

    assert streamer.stream.watch_streak_missing is True
    assert cache.get("channel", "broadcast-2").claimed is False


def test_streak_awarded_before_miner_connected_is_treated_as_claimed(tmp_path):
    streamer, cache = make_streak_streamer(tmp_path)
    live_stream = SimpleNamespace(
        id="broadcast-1",
        created_at=datetime(2026, 10, 26, 17, 45, 7),
    )
    milestone = SimpleNamespace(
        achievement_timestamp=datetime(2026, 10, 26, 17, 50, 0),
        expires_at=datetime(2099, 1, 1, 0, 0, 0),
    )
    twitch = twitch_with_streak_responses(live_stream, milestone)

    reconciled = twitch.reconcile_watch_streak(streamer)

    assert reconciled is True
    assert streamer.stream.watch_streak_missing is False
    assert cache.get("channel", "broadcast-1").claimed is True


def test_streak_from_previous_broadcast_stays_pending(tmp_path, caplog):
    caplog.set_level("INFO")
    streamer, cache = make_streak_streamer(tmp_path)
    live_stream = SimpleNamespace(
        id="broadcast-1",
        created_at=datetime(2026, 10, 26, 17, 45, 7),
    )
    milestone = SimpleNamespace(
        achievement_timestamp=datetime(2026, 10, 25, 17, 50, 0),
        expires_at=datetime(2099, 1, 1, 0, 0, 0),
    )
    twitch = twitch_with_streak_responses(live_stream, milestone)

    reconciled = twitch.reconcile_watch_streak(streamer)

    assert reconciled is False
    assert streamer.stream.watch_streak_missing is True
    assert cache.get("channel", "broadcast-1") is None
    assert "predates broadcast=broadcast-1" in caplog.text


def test_streak_check_skips_channels_without_streak_tracking(tmp_path):
    streamer, _ = make_streak_streamer(tmp_path)
    streamer.settings.watch_streak = False
    calls = []

    def get_stream_live_status(channel_id):
        calls.append(channel_id)
        raise AssertionError("watch streak lookup should not run")

    twitch = Twitch.__new__(Twitch)
    twitch.gql = SimpleNamespace(get_stream_live_status=get_stream_live_status)

    assert twitch.reconcile_watch_streak(streamer) is False
    assert calls == []


def test_streak_check_failure_leaves_streak_pending(tmp_path):
    streamer, cache = make_streak_streamer(tmp_path)
    twitch = Twitch.__new__(Twitch)
    twitch.gql = SimpleNamespace(
        get_stream_live_status=lambda channel_id: (_ for _ in ()).throw(
            RetryError("WithIsStreamLiveQuery", [])
        )
    )

    reconciled = twitch.reconcile_watch_streak(streamer)

    assert reconciled is False
    assert streamer.stream.watch_streak_missing is True
    assert cache.get("channel", "broadcast-1") is None


def test_offline_transition_is_persisted(tmp_path, monkeypatch):
    cache_path = tmp_path / "watch-streak.json"
    cache = WatchStreakCache.load(cache_path, "viewer")
    streamer = make_streamer("channel")
    streamer.watch_streak_cache = cache
    streamer.stream.broadcast_id = "broadcast-1"

    times = iter(
        [
            2_000_000_000.0,
            2_000_000_001.0,
            2_000_000_100.0,
            2_000_000_101.0,
            2_000_000_102.0,
        ]
    )
    monkeypatch.setattr(
        "TwitchChannelPointsMiner.classes.entities.Streamer.time.time",
        lambda: next(times),
    )
    streamer.set_online()
    streamer.set_offline()

    restored = WatchStreakCache.load(cache_path, "viewer")
    assert restored.get("channel", "broadcast-1").ended_at == 2_000_000_100


def test_saving_prunes_stale_sessions_without_restart(tmp_path, monkeypatch):
    cache_path = tmp_path / "watch-streak.json"
    current_time = [2_000_000_000.0]
    monkeypatch.setattr(
        "TwitchChannelPointsMiner.WatchStreakCache.time.time",
        lambda: current_time[0],
    )
    cache = WatchStreakCache.load(cache_path, "viewer")
    cache.ensure("old-channel", "old-broadcast", started_at=current_time[0])
    cache.mark_ended("old-channel", "old-broadcast", ended_at=current_time[0])

    current_time[0] += STALE_SESSION_TTL_SECONDS + 1
    cache.ensure("new-channel", "new-broadcast", started_at=current_time[0])

    assert cache.get("old-channel", "old-broadcast") is None
    assert cache.get("new-channel", "new-broadcast") is not None
    restored = WatchStreakCache.load(cache_path, "viewer")
    assert restored.get("old-channel", "old-broadcast") is None


def test_cache_filters_sessions_from_other_accounts(tmp_path):
    cache_path = tmp_path / "watch-streak.json"
    cache_path.write_text(
        json.dumps(
            {
                "version": 1,
                "sessions": [
                    {
                        "account_name": "other",
                        "streamer_login": "channel",
                        "broadcast_id": "broadcast-1",
                        "started_at": 100,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    cache = WatchStreakCache.load(cache_path, "viewer")

    assert cache.get("channel", "broadcast-1") is None


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("account_name", None),
        ("account_name", ""),
        ("streamer_login", None),
        ("streamer_login", ""),
        ("broadcast_id", None),
        ("broadcast_id", ""),
    ],
)
def test_cache_ignores_sessions_with_invalid_identity_fields(
    tmp_path, field, invalid_value
):
    cache_path = tmp_path / "watch-streak.json"
    session = {
        "account_name": "viewer",
        "streamer_login": "channel",
        "broadcast_id": "broadcast-1",
        "started_at": 100,
    }
    session[field] = invalid_value
    cache_path.write_text(
        json.dumps({"version": 1, "sessions": [session]}),
        encoding="utf-8",
    )

    cache = WatchStreakCache.load(cache_path, "viewer")

    assert cache.get("channel", "broadcast-1") is None


def test_corrupt_cache_starts_empty(tmp_path, caplog):
    caplog.set_level("WARNING")
    cache_path = tmp_path / "watch-streak.json"
    cache_path.write_text("not-json", encoding="utf-8")

    cache = WatchStreakCache.load(cache_path, "viewer")

    assert cache.get("channel", "broadcast-1") is None
    assert "starting with an empty cache" in caplog.text


def test_unsupported_cache_version_starts_empty(tmp_path, caplog):
    caplog.set_level("WARNING")
    cache_path = tmp_path / "watch-streak.json"
    cache_path.write_text(
        json.dumps(
            {
                "version": 999,
                "sessions": [
                    {
                        "account_name": "viewer",
                        "streamer_login": "channel",
                        "broadcast_id": "broadcast-1",
                        "started_at": 100,
                        "claimed": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    cache = WatchStreakCache.load(cache_path, "viewer")

    assert cache.get("channel", "broadcast-1") is None
    assert "Unsupported watch-streak cache version 999" in caplog.text
