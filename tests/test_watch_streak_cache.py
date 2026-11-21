import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from TwitchChannelPointsMiner.WatchStreakCache import (
    STALE_SESSION_TTL_SECONDS,
    WatchStreakCache,
)
from TwitchChannelPointsMiner.classes.Chat import ChatPresence
from TwitchChannelPointsMiner.classes.entities.Streamer import Streamer
from TwitchChannelPointsMiner.classes.gql.Errors import RetryError
from TwitchChannelPointsMiner.classes.Twitch import (
    WATCH_STREAK_RECONCILE_AFTER_MINUTES,
    WATCH_STREAK_RECONCILE_INTERVAL_SECONDS,
    Twitch,
)
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
    _refresh_live_stream(streamer, "broadcast-1")
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


def test_mid_stream_reconcile_waits_for_watched_minutes(tmp_path):
    streamer, _ = make_streak_streamer(tmp_path)
    streamer.is_online = True
    streamer.stream.minute_watched = WATCH_STREAK_RECONCILE_AFTER_MINUTES - 0.1
    calls = []

    def get_stream_live_status(channel_id):
        calls.append(channel_id)
        raise AssertionError("watch streak lookup should wait for watched time")

    twitch = Twitch.__new__(Twitch)
    twitch.gql = SimpleNamespace(get_stream_live_status=get_stream_live_status)

    twitch._reconcile_due_watch_streaks([streamer], now=2_000_000_000)

    assert calls == []
    assert streamer.stream.watch_streak_missing is True


def test_mid_stream_reconcile_observes_the_check_cooldown(tmp_path):
    streamer, _ = make_streak_streamer(tmp_path)
    streamer.is_online = True
    streamer.stream.minute_watched = WATCH_STREAK_RECONCILE_AFTER_MINUTES
    now = 2_000_000_000
    streamer.stream.watch_streak_checked_at = (
        now - WATCH_STREAK_RECONCILE_INTERVAL_SECONDS + 1
    )
    calls = []

    def get_stream_live_status(channel_id):
        calls.append(channel_id)
        return SimpleNamespace(stream=None)

    twitch = Twitch.__new__(Twitch)
    twitch.gql = SimpleNamespace(get_stream_live_status=get_stream_live_status)

    twitch._reconcile_due_watch_streaks([streamer], now=now)
    assert calls == []

    streamer.stream.watch_streak_checked_at = (
        now - WATCH_STREAK_RECONCILE_INTERVAL_SECONDS
    )
    twitch._reconcile_due_watch_streaks([streamer], now=now)
    assert calls == ["channel-1"]


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


def _utc_datetime(timestamp):
    # Twitch timestamps are parsed as naive UTC datetimes.
    return datetime.fromtimestamp(timestamp, timezone.utc).replace(tzinfo=None)


def _pin_clock(monkeypatch, clock):
    for module in (
        "TwitchChannelPointsMiner.classes.entities.Streamer",
        "TwitchChannelPointsMiner.classes.entities.Stream",
        "TwitchChannelPointsMiner.WatchStreakCache",
    ):
        monkeypatch.setattr(f"{module}.time.time", lambda: clock[0])


def _refresh_live_stream(streamer, broadcast_id):
    streamer.stream.update(
        broadcast_id=broadcast_id,
        title="title",
        game={},
        tags=[],
        viewers_count=1,
    )


def test_late_offline_detection_does_not_invent_a_continuation(tmp_path, monkeypatch):
    # Broadcast A is last seen live at T+600, but the miner (offline, or with
    # a dropped websocket) only notices it ended 45 minutes later. B starts
    # two minutes after that: the real gap is far past 30 minutes.
    clock = [2_000_000_000.0]
    _pin_clock(monkeypatch, clock)
    cache = WatchStreakCache.load(tmp_path / "watch-streak.json", "viewer")
    streamer = make_streamer("channel")
    streamer.watch_streak_cache = cache
    _refresh_live_stream(streamer, "broadcast-1")
    streamer.set_online()
    streamer.update_history("WATCH_STREAK", earned=450)
    clock[0] += 600
    _refresh_live_stream(streamer, "broadcast-1")

    clock[0] += 45 * 60
    streamer.set_offline()
    clock[0] += 120
    _refresh_live_stream(streamer, "broadcast-2")
    streamer.set_online()

    assert cache.get("channel", "broadcast-1").ended_at == 2_000_000_600
    assert streamer.stream.watch_streak_missing is True


def test_short_restart_seen_live_until_the_end_is_inherited(tmp_path, monkeypatch):
    clock = [2_000_000_000.0]
    _pin_clock(monkeypatch, clock)
    cache = WatchStreakCache.load(tmp_path / "watch-streak.json", "viewer")
    streamer = make_streamer("channel")
    streamer.watch_streak_cache = cache
    _refresh_live_stream(streamer, "broadcast-1")
    streamer.set_online()
    streamer.update_history("WATCH_STREAK", earned=450)
    clock[0] += 600
    _refresh_live_stream(streamer, "broadcast-1")

    clock[0] += 60
    streamer.set_offline()
    clock[0] += 5 * 60
    _refresh_live_stream(streamer, "broadcast-2")
    streamer.set_online()

    assert streamer.stream.watch_streak_missing is False


def test_late_streak_event_after_stream_down_is_not_carried_over(tmp_path, monkeypatch):
    # A's WATCH_STREAK event arrives just after A's stream-down, and B's
    # stream-up is missed: the confirmation is A's, not B's.
    clock = [2_000_000_000.0]
    _pin_clock(monkeypatch, clock)
    cache = WatchStreakCache.load(tmp_path / "watch-streak.json", "viewer")
    streamer = make_streamer("channel")
    streamer.watch_streak_cache = cache
    _refresh_live_stream(streamer, "broadcast-1")
    streamer.set_online()
    clock[0] += 600
    streamer.set_offline()
    clock[0] += 5
    streamer.update_history("WATCH_STREAK", earned=450)

    clock[0] += 40 * 60
    _refresh_live_stream(streamer, "broadcast-2")
    streamer.set_online()

    assert streamer.stream.watch_streak_missing is True
    assert cache.get("channel", "broadcast-2").claimed is False


def test_mid_stream_reconcile_looks_up_one_channel_per_cycle(tmp_path):
    first, _ = make_streak_streamer(tmp_path, username="first")
    second, _ = make_streak_streamer(tmp_path, username="second")
    calls = []
    for streamer in (first, second):
        streamer.is_online = True
        streamer.stream.minute_watched = WATCH_STREAK_RECONCILE_AFTER_MINUTES
        streamer.channel_id = f"{streamer.username}-id"

    def get_stream_live_status(channel_id):
        calls.append(channel_id)
        return SimpleNamespace(stream=None)

    twitch = Twitch.__new__(Twitch)
    twitch.running = True
    twitch.gql = SimpleNamespace(get_stream_live_status=get_stream_live_status)

    # The watch loop passes the real clock, which reconcile_watch_streak()
    # also uses to stamp each check.
    twitch._reconcile_due_watch_streaks([first, second])
    assert calls == ["first-id"]
    twitch._reconcile_due_watch_streaks([first, second])
    assert calls == ["first-id", "second-id"]


def test_mid_stream_reconcile_stops_on_shutdown(tmp_path):
    streamer, _ = make_streak_streamer(tmp_path)
    streamer.is_online = True
    streamer.stream.minute_watched = 30
    twitch = Twitch.__new__(Twitch)
    twitch.running = False
    twitch.gql = SimpleNamespace()

    twitch._reconcile_due_watch_streaks([streamer], now=2_000_000_000, max_minutes=20)

    assert streamer.stream.watch_streak_missing is True


def test_late_noticed_continuation_uses_twitch_start_time(tmp_path):
    # A claimed broadcast ends, its continuation starts two minutes later,
    # and the miner (restarted, say) only notices it an hour later. The gap
    # from the noticed time is too long; Twitch's own start time is not.
    start = 2_000_000_000.0
    streamer, cache = make_streak_streamer(tmp_path, broadcast_id="broadcast-2")
    cache.ensure("channel", "broadcast-1", started_at=start)
    cache.mark_claimed("channel", "broadcast-1", claimed_at=start + 60)
    cache.mark_ended("channel", "broadcast-1", ended_at=start + 1000)
    cache.ensure("channel", "broadcast-2", started_at=start + 1000 + 3600)
    assert cache.claimed_at_for_logical_stream("channel", "broadcast-2") is None

    created_at = start + 1000 + 120
    live_stream = SimpleNamespace(
        id="broadcast-2", created_at=_utc_datetime(created_at)
    )
    twitch = twitch_with_streak_responses(live_stream, None)
    twitch.gql.get_reward_list = lambda channel_id: pytest.fail(
        "a continuation of a claimed stream needs no milestone lookup"
    )

    reconciled = twitch.reconcile_watch_streak(streamer)

    assert reconciled is True
    assert streamer.stream.watch_streak_missing is False
    assert cache.get("channel", "broadcast-2").started_at == created_at
    assert cache.claimed_at_for_logical_stream("channel", "broadcast-2") == start + 60


def test_twitch_start_time_does_not_invent_a_continuation(tmp_path):
    start = 2_000_000_000.0
    streamer, cache = make_streak_streamer(tmp_path, broadcast_id="broadcast-2")
    cache.ensure("channel", "broadcast-1", started_at=start)
    cache.mark_claimed("channel", "broadcast-1", claimed_at=start + 60)
    cache.mark_ended("channel", "broadcast-1", ended_at=start + 1000)
    cache.ensure("channel", "broadcast-2", started_at=start + 1000 + 3600)
    live_stream = SimpleNamespace(
        id="broadcast-2", created_at=_utc_datetime(start + 1000 + 3500)
    )
    twitch = twitch_with_streak_responses(live_stream, None)

    reconciled = twitch.reconcile_watch_streak(streamer)

    assert reconciled is False
    assert streamer.stream.watch_streak_missing is True


def test_unconfirmed_streak_stops_being_pending_at_the_cap(tmp_path, caplog):
    caplog.set_level("INFO")
    streamer, cache = make_streak_streamer(tmp_path)
    streamer.is_online = True
    streamer.stream.minute_watched = 20
    twitch = Twitch.__new__(Twitch)
    twitch.gql = SimpleNamespace(
        get_stream_live_status=lambda channel_id: pytest.fail(
            "a streak past the cap needs no further lookups"
        )
    )

    twitch._reconcile_due_watch_streaks([streamer], now=2_000_000_000, max_minutes=20)

    assert streamer.stream.watch_streak_missing is False
    # Giving up is not a claim: a later broadcast must not inherit it.
    assert cache.get("channel", "broadcast-1") is None
    assert "watch_streak_max_minutes=20" in caplog.text


def test_unconfirmed_streak_below_the_cap_keeps_checking(tmp_path):
    streamer, _ = make_streak_streamer(tmp_path)
    streamer.is_online = True
    streamer.stream.minute_watched = 19.9
    calls = []

    def get_stream_live_status(channel_id):
        calls.append(channel_id)
        return SimpleNamespace(stream=None)

    twitch = Twitch.__new__(Twitch)
    twitch.gql = SimpleNamespace(get_stream_live_status=get_stream_live_status)

    twitch._reconcile_due_watch_streaks([streamer], now=2_000_000_000, max_minutes=20)

    assert calls == ["channel-1"]
    assert streamer.stream.watch_streak_missing is True


def test_streak_only_channel_past_its_window_is_not_rechecked(tmp_path):
    streamer, _ = make_streak_streamer(tmp_path)
    streamer.settings.streak_only = True
    streamer.is_online = True
    streamer.stream.minute_watched = 10
    twitch = Twitch.__new__(Twitch)
    twitch.gql = SimpleNamespace(
        get_stream_live_status=lambda channel_id: pytest.fail(
            "a streak-only channel past its window is no longer watched"
        )
    )

    twitch._reconcile_due_watch_streaks([streamer], now=2_000_000_000, max_minutes=20)

    assert streamer.stream.watch_streak_missing is True


def test_offline_end_uses_the_request_time_of_the_last_live_refresh(
    tmp_path, monkeypatch
):
    # The live response is applied 20 seconds after its request; the stream
    # may have ended right after the request, so that is the safe end.
    clock = [2_000_000_000.0]
    _pin_clock(monkeypatch, clock)
    cache = WatchStreakCache.load(tmp_path / "watch-streak.json", "viewer")
    streamer = make_streamer("channel")
    streamer.watch_streak_cache = cache
    _refresh_live_stream(streamer, "broadcast-1")
    streamer.set_online()
    requested_at = clock[0] + 600
    clock[0] = requested_at + 20
    streamer.stream.update(
        broadcast_id="broadcast-1",
        title="title",
        game={},
        tags=[],
        viewers_count=1,
        seen_live_at=requested_at,
    )

    clock[0] += 60
    streamer.set_offline()

    assert cache.get("channel", "broadcast-1").ended_at == requested_at


def test_update_stream_records_the_request_time_as_seen_live(monkeypatch):
    clock = [2_000_000_000.0]
    monkeypatch.setattr(
        "TwitchChannelPointsMiner.classes.Twitch.time.time", lambda: clock[0]
    )
    monkeypatch.setattr(
        "TwitchChannelPointsMiner.classes.entities.Stream.time.time",
        lambda: clock[0],
    )
    streamer = make_streamer("channel")
    streamer.settings.claim_drops = False

    def get_stream_info(target):
        clock[0] += 20
        return {
            "stream": {"id": "broadcast-1", "tags": [], "viewersCount": 1},
            "broadcastSettings": {"title": "title", "game": {}},
        }

    monkeypatch.setattr(
        Twitch, "get_stream_info", lambda self, target: get_stream_info(target)
    )
    twitch = Twitch.__new__(Twitch)
    twitch.twitch_login = SimpleNamespace(get_user_id=lambda: "viewer-id")

    twitch.update_stream(streamer)

    assert streamer.stream.live_seen_at == 2_000_000_000.0


def test_reconcile_ignores_a_milestone_after_the_broadcast_changed(tmp_path):
    streamer, cache = make_streak_streamer(tmp_path)
    live_stream = SimpleNamespace(
        id="broadcast-1",
        created_at=datetime(2026, 10, 26, 17, 45, 7),
    )
    milestone = SimpleNamespace(
        achievement_timestamp=datetime(2026, 10, 26, 17, 50, 0),
        expires_at=None,
    )
    twitch = twitch_with_streak_responses(live_stream, milestone)

    def get_reward_list(channel_id):
        # Another thread moves the stream to a new broadcast meanwhile.
        streamer.stream.broadcast_id = "broadcast-2"
        return SimpleNamespace(watch_streak_milestone=milestone)

    twitch.gql.get_reward_list = get_reward_list

    reconciled = twitch.reconcile_watch_streak(streamer)

    assert reconciled is False
    assert streamer.stream.watch_streak_missing is True
    assert cache.get("channel", "broadcast-1") is None
    assert cache.get("channel", "broadcast-2") is None


def _twitch_moving_to_broadcast(monkeypatch, clock, broadcast_id, gap_seconds):
    """A Twitch whose next stream refresh shows a new broadcast."""
    reconciled = []

    def update_stream(self, streamer):
        clock[0] += gap_seconds
        _refresh_live_stream(streamer, broadcast_id)

    monkeypatch.setattr(Twitch, "update_stream", update_stream)
    monkeypatch.setattr(
        Twitch, "reconcile_watch_streak", lambda self, s: reconciled.append(s)
    )
    monkeypatch.setattr(
        "TwitchChannelPointsMiner.classes.Twitch.time.time", lambda: clock[0]
    )
    twitch = Twitch.__new__(Twitch)
    twitch.category_campaign_eligibility = {}
    return twitch, reconciled


def _claimed_live_streamer(tmp_path, monkeypatch, clock):
    _pin_clock(monkeypatch, clock)
    cache = WatchStreakCache.load(tmp_path / "watch-streak.json", "viewer")
    streamer = make_streamer("channel")
    streamer.watch_streak_cache = cache
    _refresh_live_stream(streamer, "broadcast-1")
    streamer.set_online()
    streamer.stream.minute_watched = 15
    streamer.update_history("WATCH_STREAK", earned=450)
    return streamer, cache


def test_new_broadcast_without_an_offline_gets_fresh_streak_state(
    tmp_path, monkeypatch
):
    # The machine slept through A's end; when it wakes, B is live and the
    # miner never saw an offline. B must not inherit A's minutes or claim.
    clock = [2_000_000_000.0]
    streamer, cache = _claimed_live_streamer(tmp_path, monkeypatch, clock)
    seen_live_at = streamer.stream.live_seen_at
    twitch, reconciled = _twitch_moving_to_broadcast(
        monkeypatch, clock, "broadcast-2", gap_seconds=3 * 60 * 60
    )

    twitch.check_streamer_online(streamer)

    assert streamer.is_online is True
    assert streamer.stream.broadcast_id == "broadcast-2"
    assert streamer.stream.minute_watched == 0
    assert streamer.stream.watch_streak_missing is True
    assert cache.get("channel", "broadcast-1").ended_at == seen_live_at
    assert cache.get("channel", "broadcast-2").claimed is False
    # The milestone check is queued for the minute watcher's budget rather
    # than run here.
    assert reconciled == []
    assert streamer.stream.watch_streak_check_due is True


def test_quick_new_broadcast_without_an_offline_keeps_the_claim(tmp_path, monkeypatch):
    clock = [2_000_000_000.0]
    streamer, _ = _claimed_live_streamer(tmp_path, monkeypatch, clock)
    twitch, _ = _twitch_moving_to_broadcast(
        monkeypatch, clock, "broadcast-2", gap_seconds=5 * 60
    )

    twitch.check_streamer_online(streamer)

    assert streamer.stream.broadcast_id == "broadcast-2"
    assert streamer.stream.watch_streak_missing is False


def test_same_broadcast_refresh_keeps_streak_state(tmp_path, monkeypatch):
    clock = [2_000_000_000.0]
    streamer, _ = _claimed_live_streamer(tmp_path, monkeypatch, clock)
    twitch, reconciled = _twitch_moving_to_broadcast(
        monkeypatch, clock, "broadcast-1", gap_seconds=120
    )

    twitch.check_streamer_online(streamer)

    assert streamer.stream.minute_watched == 15
    assert streamer.stream.watch_streak_missing is False
    assert reconciled == []


def test_cap_does_not_give_up_a_streak_reset_for_a_new_broadcast(tmp_path, caplog):
    from TwitchChannelPointsMiner.classes.entities.Stream import Stream

    class ResetDuringCheck(Stream):
        # Another thread starts the next broadcast between the minute count
        # being read and the streak being given up.
        def stop_waiting_for_watch_streak(self, max_minutes):
            self.init_watch_streak()
            return super().stop_waiting_for_watch_streak(max_minutes)

    caplog.set_level("INFO")
    streamer, _ = make_streak_streamer(tmp_path)
    streamer.stream = ResetDuringCheck()
    streamer.stream.broadcast_id = "broadcast-2"
    streamer.stream.minute_watched = 25
    streamer.is_online = True
    twitch = Twitch.__new__(Twitch)
    twitch.running = True

    twitch._reconcile_due_watch_streaks([streamer], max_minutes=20)

    assert streamer.stream.watch_streak_missing is True
    assert "no longer prioritizing" not in caplog.text


def test_queued_milestone_checks_skip_the_wait_but_keep_the_budget(tmp_path):
    first, _ = make_streak_streamer(tmp_path, username="first")
    second, _ = make_streak_streamer(tmp_path, username="second")
    calls = []
    for streamer in (first, second):
        streamer.is_online = True
        streamer.channel_id = f"{streamer.username}-id"
        # A new broadcast: nothing watched yet, check queued.
        streamer.stream.watch_streak_check_due = True

    def get_stream_live_status(channel_id):
        calls.append(channel_id)
        return SimpleNamespace(stream=None)

    twitch = Twitch.__new__(Twitch)
    twitch.running = True
    twitch.gql = SimpleNamespace(get_stream_live_status=get_stream_live_status)

    twitch._reconcile_due_watch_streaks([first, second])
    assert calls == ["first-id"]
    assert first.stream.watch_streak_check_due is False
    twitch._reconcile_due_watch_streaks([first, second])
    assert calls == ["first-id", "second-id"]
    # Back to the usual wait for watched minutes.
    twitch._reconcile_due_watch_streaks([first, second])
    assert calls == ["first-id", "second-id"]


def test_confirming_a_streak_for_an_older_broadcast_changes_nothing():
    from TwitchChannelPointsMiner.classes.entities.Stream import Stream

    stream = Stream()
    stream.broadcast_id = "broadcast-2"

    assert stream.confirm_watch_streak_for("broadcast-1") is False
    assert stream.watch_streak_missing is True
    assert stream.confirm_watch_streak_for("broadcast-2") is True
    assert stream.watch_streak_missing is False


def test_legacy_cache_end_does_not_prove_a_continuation(tmp_path):
    # Written before ended_seen_live existed: ended_at was when the miner
    # noticed the offline, possibly long after the real end.
    start = 2_000_000_000.0
    path = tmp_path / "watch-streak.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "sessions": [
                    {
                        "account_name": "viewer",
                        "streamer_login": "channel",
                        "broadcast_id": "broadcast-1",
                        "started_at": start,
                        "claimed": True,
                        "claimed_at": start + 60,
                        "ended_at": start + 2700,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    cache = WatchStreakCache.load(path, "viewer")

    cache.ensure("channel", "broadcast-2", started_at=start + 2820)

    assert cache.get("channel", "broadcast-1").claimed is True
    assert cache.claimed_at_for_logical_stream("channel", "broadcast-2") is None


def test_seen_live_end_survives_a_restart(tmp_path):
    start = 2_000_000_000.0
    path = tmp_path / "watch-streak.json"
    cache = WatchStreakCache.load(path, "viewer")
    cache.ensure("channel", "broadcast-1", started_at=start)
    cache.mark_claimed("channel", "broadcast-1", claimed_at=start + 60)
    cache.mark_ended("channel", "broadcast-1", ended_at=start + 1000)

    restored = WatchStreakCache.load(path, "viewer")
    restored.ensure("channel", "broadcast-2", started_at=start + 1100)

    assert restored.get("channel", "broadcast-1").ended_seen_live is True
    assert restored.claimed_at_for_logical_stream("channel", "broadcast-2") == (
        start + 60
    )


def test_offline_without_a_live_refresh_does_not_prove_a_continuation(
    tmp_path, monkeypatch
):
    clock = [2_000_000_000.0]
    _pin_clock(monkeypatch, clock)
    cache = WatchStreakCache.load(tmp_path / "watch-streak.json", "viewer")
    streamer = make_streamer("channel")
    streamer.watch_streak_cache = cache
    streamer.stream.broadcast_id = "broadcast-1"
    streamer.set_online()
    streamer.update_history("WATCH_STREAK", earned=450)
    clock[0] += 600
    # No stream refresh: the offline time is only when the miner noticed.
    streamer.set_offline()
    clock[0] += 120
    streamer.stream.broadcast_id = "broadcast-2"
    streamer.set_online()

    assert cache.get("channel", "broadcast-1").ended_seen_live is False
    assert streamer.stream.watch_streak_missing is True


def test_mid_stream_reconcile_reaches_every_due_channel(tmp_path, monkeypatch):
    # Ten due channels, one lookup per 20-second cycle and a 3-minute
    # cooldown: a fixed scan order would keep rechecking the first nine.
    clock = [2_000_000_000.0]
    monkeypatch.setattr(
        "TwitchChannelPointsMiner.classes.Twitch.time.time", lambda: clock[0]
    )
    streamers = []
    for index in range(10):
        streamer, _ = make_streak_streamer(tmp_path, username=f"channel{index}")
        streamer.is_online = True
        streamer.channel_id = f"channel{index}-id"
        streamer.stream.minute_watched = WATCH_STREAK_RECONCILE_AFTER_MINUTES
        streamers.append(streamer)
    calls = []

    def get_stream_live_status(channel_id):
        calls.append(channel_id)
        return SimpleNamespace(stream=None)

    twitch = Twitch.__new__(Twitch)
    twitch.running = True
    twitch.gql = SimpleNamespace(get_stream_live_status=get_stream_live_status)

    for _ in range(30):
        twitch._reconcile_due_watch_streaks(streamers)
        clock[0] += 20

    assert {f"channel{index}-id" for index in range(10)} <= set(calls)


class _RefreshBetweenReads:
    """A stream another thread refreshes right after its ID is read alone."""

    def __init__(self, stream, on_id_read):
        object.__setattr__(self, "_stream", stream)
        object.__setattr__(self, "_on_id_read", on_id_read)

    def __getattr__(self, name):
        value = getattr(self._stream, name)
        if name == "broadcast_id" and self._on_id_read is not None:
            on_id_read = self._on_id_read
            object.__setattr__(self, "_on_id_read", None)
            on_id_read()
        return value

    def __setattr__(self, name, value):
        setattr(self._stream, name, value)


def test_previous_broadcast_snapshot_is_one_consistent_read(tmp_path, monkeypatch):
    # The websocket thread refreshes the stream to B (an hour after A was last
    # seen live) between this thread's read of A's ID and of its last-seen
    # time. A's end must still be A's own last-seen time.
    clock = [2_000_000_000.0]
    streamer, cache = _claimed_live_streamer(tmp_path, monkeypatch, clock)
    seen_live_at = streamer.stream.live_seen_at
    real_stream = streamer.stream

    def other_thread_refresh():
        if real_stream.broadcast_id == "broadcast-1":
            clock[0] += 60 * 60
            _refresh_live_stream(streamer, "broadcast-2")

    streamer.stream = _RefreshBetweenReads(real_stream, other_thread_refresh)
    twitch, _ = _twitch_moving_to_broadcast(
        monkeypatch, clock, "broadcast-2", gap_seconds=60 * 60
    )

    twitch.check_streamer_online(streamer)

    assert cache.get("channel", "broadcast-1").ended_at == seen_live_at
    assert real_stream.watch_streak_missing is True
