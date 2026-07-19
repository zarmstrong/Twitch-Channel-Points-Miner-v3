import json
from types import SimpleNamespace

from TwitchChannelPointsMiner.WatchStreakCache import (
    STALE_SESSION_TTL_SECONDS,
    WatchStreakCache,
)
from TwitchChannelPointsMiner.classes.Chat import ChatPresence
from TwitchChannelPointsMiner.classes.entities.Streamer import Streamer


def make_streamer(username):
    return Streamer(username, settings=SimpleNamespace(chat=ChatPresence.NEVER))


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
