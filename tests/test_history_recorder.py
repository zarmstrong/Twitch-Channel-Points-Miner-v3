import json
import sqlite3
import os
from pathlib import Path

import pytest

import TwitchChannelPointsMiner.history as history_module
from TwitchChannelPointsMiner.history.recorder import HistoryRecorder
from TwitchChannelPointsMiner.history.store import HistoryStore

NOW = 1_700_000_000.0


class FakeClock:
    def __init__(self, value=NOW):
        self.value = value

    def __call__(self):
        return self.value


@pytest.fixture
def recorder(tmp_path):
    analytics = tmp_path / "analytics"
    analytics.mkdir()
    cache = tmp_path / "watch_streak_cache.alice.json"
    store = HistoryStore(analytics / "history.sqlite3")
    clock = FakeClock()
    instance = HistoryRecorder(
        store,
        analytics_path=analytics,
        streak_cache_path=cache,
        username="alice",
        clock=clock,
    )
    instance._prepare()
    yield instance
    instance._shutdown()


def _write_json(path, payload, timestamp):
    target = Path(path)
    target.write_text(json.dumps(payload), encoding="utf-8")
    os.utime(target, (timestamp, timestamp))


def test_prepare_closes_sessions_left_by_a_previous_run(tmp_path):
    analytics = tmp_path / "analytics"
    analytics.mkdir()
    store = HistoryStore(analytics / "history.sqlite3")
    store.initialize()
    session_id = store.start_watch_session("alice", "points", NOW)
    store.tick_watch_session(session_id, "alice", NOW + 30, 30)
    store.upsert_stream_session("alice", "b1", NOW)

    recorder = HistoryRecorder(
        store,
        analytics_path=analytics,
        streak_cache_path=tmp_path / "missing.json",
        username="alice",
        clock=FakeClock(NOW + 100),
    )
    recorder._prepare()
    try:
        assert store.overview(days=1, now=NOW + 100)["active_watch_sessions"] == 0
        assert store.overview(days=1, now=NOW + 100)["live_streams"] == 0
    finally:
        recorder._shutdown()


def test_recorder_imports_series_and_annotations(recorder):
    analytics = recorder.analytics_path
    path = os.path.join(analytics, "alice.json")
    _write_json(
        path,
        {
            "version": 1,
            "series": [
                {"x": int(NOW * 1000), "y": 1000, "z": "Watch"},
                {"x": int((NOW + 60) * 1000), "y": 1050, "z": "WATCH"},
            ],
            "annotations": [
                {
                    "x": int((NOW + 120) * 1000),
                    "label": {"text": "+50 - WATCH_STREAK"},
                    "borderColor": "#45c1ff",
                },
                {
                    "x": int((NOW + 180) * 1000),
                    "label": {"text": "A prediction won"},
                    "borderColor": "#36b535",
                },
            ],
        },
        NOW + 1,
    )

    recorder._ingest_analytics(NOW + 2)
    overview = recorder.store.overview(days=30, now=NOW + 120)
    assert overview["window"]["points_gained"] == 50
    assert overview["window"]["streak_count"] == 1
    event_types = {
        item["type"] for item in recorder.store.events(days=30, now=NOW + 180)["events"]
    }
    assert event_types == {"watch_streak", "win"}

    # Re-reading an unchanged file must not duplicate anything.
    recorder._ingest_analytics(NOW + 2)
    overview = recorder.store.overview(days=30, now=NOW + 120)
    assert overview["window"]["points_gained"] == 50
    assert overview["window"]["streak_count"] == 1

    _write_json(
        path,
        {
            "version": 1,
            "series": [
                {"x": int(NOW * 1000), "y": 1000, "z": "Watch"},
                {"x": int((NOW + 60) * 1000), "y": 1050, "z": "WATCH"},
                {
                    "x": int((NOW + 300) * 1000),
                    "y": 1075,
                    "z": "WATCH",
                },
            ],
            "annotations": [
                {
                    "x": int((NOW + 120) * 1000),
                    "label": {"text": "+50 - WATCH_STREAK"},
                }
            ],
        },
        NOW + 3,
    )
    recorder._ingest_analytics(NOW + 4)
    overview = recorder.store.overview(days=30, now=NOW + 300)
    assert overview["window"]["points_gained"] == 75
    assert overview["window"]["streak_count"] == 1


def test_recorder_tracks_watch_sessions_from_now_watching(recorder):
    path = os.path.join(recorder.analytics_path, "now_watching.json")
    clock = recorder.clock

    clock.value = NOW + 10
    _write_json(
        path,
        [{"username": "alice", "reason": "points", "game": "Chess"}],
        clock.value,
    )
    recorder._poll_now_watching()
    assert (
        recorder.store.overview(days=30, now=clock.value)["active_watch_sessions"] == 1
    )

    clock.value = NOW + 40
    _write_json(
        path,
        [{"username": "alice", "reason": "points", "game": "Chess"}],
        clock.value,
    )
    recorder._poll_now_watching()

    clock.value = NOW + 50
    _write_json(path, [], clock.value)
    recorder._poll_now_watching()

    sessions = recorder.store.sessions(kind="watch", days=30, now=clock.value)
    assert sessions["total"] == 1
    # Watched from the first snapshot until the one that dropped alice.
    assert sessions["sessions"][0]["watch_seconds"] == 40
    assert sessions["sessions"][0]["ended_at"] == NOW + 50


def test_recorder_ignores_live_sessions_until_streamer_is_watched(recorder):
    clock = recorder.clock
    clock.value = NOW + 30
    payload = {
        "version": 1,
        "sessions": [
            {
                "account_name": "alice",
                "streamer_login": "bob",
                "broadcast_id": "b1",
                "started_at": NOW,
                "claimed": False,
                "claimed_at": None,
                "ended_at": None,
            }
        ],
    }
    _write_json(recorder.streak_cache_path, payload, clock.value)
    recorder._ingest_streak_cache()

    assert recorder.store.sessions(kind="stream", days=1, now=clock.value)["total"] == 0

    path = os.path.join(recorder.analytics_path, "now_watching.json")
    clock.value = NOW + 40
    _write_json(path, [{"username": "bob", "reason": "points"}], clock.value)
    recorder._poll_now_watching()

    streams = recorder.store.sessions(kind="stream", days=1, now=clock.value)
    assert streams["total"] == 1
    assert streams["sessions"][0]["streamer"] == "bob"
    assert streams["sessions"][0]["active"] is True


def test_recorder_imports_streak_cache_sessions(recorder):
    clock = recorder.clock
    clock.value = NOW + 130
    payload = {
        "version": 1,
        "sessions": [
            {
                "account_name": "alice",
                "streamer_login": "bob",
                "broadcast_id": "b1",
                "started_at": NOW,
                "claimed": True,
                "claimed_at": NOW + 120,
                "ended_at": None,
            }
        ],
    }
    _write_json(recorder.streak_cache_path, payload, clock.value)

    recorder._ingest_streak_cache()
    recorder._ingest_streak_cache()

    sessions = recorder.store.sessions(kind="stream", days=1, now=clock.value)
    assert sessions["total"] == 1
    assert sessions["sessions"][0]["active"] is True

    events = recorder.store.events(days=1, now=clock.value)
    assert events["total"] == 1
    assert events["events"][0]["type"] == "watch_streak"


def test_recorder_restores_live_badge_for_a_broadcast_it_restarted_into(tmp_path):
    analytics = tmp_path / "analytics"
    analytics.mkdir()
    cache = tmp_path / "watch_streak_cache.alice.json"
    store = HistoryStore(analytics / "history.sqlite3")
    store.initialize()
    # A previous run watched bob on broadcast b1 and then stopped.
    store.start_watch_session("bob", "points", NOW)
    store.upsert_stream_session("bob", "b1", NOW, last_seen_at=NOW + 30)
    _write_json(
        cache,
        {
            "version": 1,
            "sessions": [
                {
                    "account_name": "alice",
                    "streamer_login": "bob",
                    "broadcast_id": "b1",
                    "started_at": NOW,
                    "claimed": False,
                    "claimed_at": None,
                    "ended_at": None,
                }
            ],
        },
        NOW + 10,
    )

    clock = FakeClock(NOW + 100)
    recorder = HistoryRecorder(
        store,
        analytics_path=analytics,
        streak_cache_path=cache,
        username="alice",
        clock=clock,
    )
    recorder._prepare()
    try:
        # The stale cache entry must not become a phantom live row.
        assert store.overview(days=1, now=clock.value)["live_streams"] == 0

        # Watching bob again proves the cached broadcast is still live.
        now_watching = os.path.join(analytics, "now_watching.json")
        clock.value = NOW + 160
        _write_json(
            now_watching,
            [{"username": "bob", "reason": "points"}],
            clock.value,
        )
        recorder._poll_now_watching()

        assert store.overview(days=1, now=clock.value)["live_streams"] == 1
        streamers = store.streamers(days=1, now=clock.value)
        assert streamers[0]["streamer"] == "bob"
        assert streamers[0]["live_stream"] is not None
        assert streamers[0]["live_stream"]["broadcast_id"] == "b1"

        # The miner noticing the streamer offline closes the row again.
        clock.value = NOW + 400
        _write_json(
            cache,
            {
                "version": 1,
                "sessions": [
                    {
                        "account_name": "alice",
                        "streamer_login": "bob",
                        "broadcast_id": "b1",
                        "started_at": NOW,
                        "claimed": False,
                        "claimed_at": None,
                        "ended_at": NOW + 380,
                    }
                ],
            },
            clock.value,
        )
        recorder._ingest_streak_cache()

        assert store.overview(days=1, now=clock.value)["live_streams"] == 0
        assert store.streamers(days=1, now=clock.value)[0]["live_stream"] is None
    finally:
        recorder._shutdown()


def test_recorder_does_not_resume_a_broadcast_that_ended_while_offline(tmp_path):
    analytics = tmp_path / "analytics"
    analytics.mkdir()
    cache = tmp_path / "watch_streak_cache.alice.json"
    store = HistoryStore(analytics / "history.sqlite3")
    store.initialize()
    store.start_watch_session("bob", "points", NOW)
    store.upsert_stream_session("bob", "b1", NOW, last_seen_at=NOW + 30)
    _write_json(
        cache,
        {
            "version": 1,
            "sessions": [
                {
                    "account_name": "alice",
                    "streamer_login": "bob",
                    "broadcast_id": "b1",
                    "started_at": NOW,
                    "claimed": False,
                    "claimed_at": None,
                    "ended_at": None,
                }
            ],
        },
        NOW + 10,
    )

    clock = FakeClock(NOW + 100)
    recorder = HistoryRecorder(
        store,
        analytics_path=analytics,
        streak_cache_path=cache,
        username="alice",
        clock=clock,
    )
    recorder._prepare()
    try:
        # bob is watched again, but on a new broadcast b2.
        now_watching = os.path.join(analytics, "now_watching.json")
        clock.value = NOW + 200
        store.upsert_stream_session("bob", "b2", NOW + 150)
        _write_json(
            cache,
            {
                "version": 1,
                "sessions": [
                    {
                        "account_name": "alice",
                        "streamer_login": "bob",
                        "broadcast_id": "b1",
                        "started_at": NOW,
                        "claimed": False,
                        "claimed_at": None,
                        "ended_at": NOW + 140,
                    },
                    {
                        "account_name": "alice",
                        "streamer_login": "bob",
                        "broadcast_id": "b2",
                        "started_at": NOW + 150,
                        "claimed": False,
                        "claimed_at": None,
                        "ended_at": None,
                    },
                ],
            },
            clock.value,
        )
        recorder._ingest_streak_cache()
        _write_json(
            now_watching,
            [{"username": "bob", "reason": "points"}],
            clock.value,
        )
        recorder._poll_now_watching()

        sessions = store.sessions(kind="stream", days=1, now=clock.value)
        active = {item["broadcast_id"]: item["active"] for item in sessions["sessions"]}
        assert active == {"b1": False, "b2": True}
    finally:
        recorder._shutdown()


def test_recorder_reason_change_starts_a_new_watch_session(recorder):
    path = os.path.join(recorder.analytics_path, "now_watching.json")
    clock = recorder.clock

    clock.value = NOW + 10
    _write_json(path, [{"username": "alice", "reason": "points"}], clock.value)
    recorder._poll_now_watching()

    clock.value = NOW + 35
    _write_json(path, [{"username": "alice", "reason": "streak"}], clock.value)
    recorder._poll_now_watching()

    clock.value = NOW + 50
    _write_json(path, [], clock.value)
    recorder._poll_now_watching()

    sessions = recorder.store.sessions(kind="watch", days=1, now=clock.value)
    assert sessions["total"] == 2
    reasons = sorted(item["reason"] for item in sessions["sessions"])
    assert reasons == ["points", "streak"]


def test_recorder_marks_every_online_streamer_as_live_after_restart(tmp_path):
    analytics = tmp_path / "analytics"
    analytics.mkdir()
    cache = tmp_path / "watch_streak_cache.alice.json"
    store = HistoryStore(analytics / "history.sqlite3")
    store.initialize()
    # bob was watched before the restart, carol only has imported point history
    # from a build that predates the watch-session tables.
    store.start_watch_session("bob", "points", NOW)
    store.upsert_stream_session("bob", "b1", NOW, last_seen_at=NOW + 30)
    store.record_points("carol", NOW, balance=500, delta=50)
    # dave was watched too, but he is offline now.
    store.start_watch_session("dave", "points", NOW)
    store.upsert_stream_session("dave", "d1", NOW, last_seen_at=NOW + 30)
    _write_json(
        cache,
        {
            "version": 1,
            "sessions": [
                {
                    "account_name": "alice",
                    "streamer_login": "bob",
                    "broadcast_id": "b1",
                    "started_at": NOW,
                    "claimed": True,
                    "claimed_at": NOW + 120,
                    "ended_at": None,
                },
                {
                    "account_name": "alice",
                    "streamer_login": "carol",
                    "broadcast_id": "c1",
                    "started_at": NOW,
                    "claimed": False,
                    "claimed_at": None,
                    "ended_at": None,
                },
                {
                    "account_name": "alice",
                    "streamer_login": "dave",
                    "broadcast_id": "d1",
                    "started_at": NOW,
                    "claimed": False,
                    "claimed_at": None,
                    "ended_at": NOW + 60,
                },
                {
                    "account_name": "alice",
                    "streamer_login": "eve",
                    "broadcast_id": "e1",
                    "started_at": NOW,
                    "claimed": False,
                    "claimed_at": None,
                    "ended_at": None,
                },
            ],
        },
        NOW + 10,
    )

    clock = FakeClock(NOW + 100)
    recorder = HistoryRecorder(
        store,
        analytics_path=analytics,
        streak_cache_path=cache,
        username="alice",
        clock=clock,
        online_streamers=lambda: [
            {"username": "bob", "broadcast_id": "b1", "online_at": NOW + 100},
            {"username": "carol", "broadcast_id": "c1", "online_at": NOW + 100},
            {"username": "dave", "broadcast_id": "d1", "is_online": False},
            {"username": "eve", "broadcast_id": "e1", "online_at": NOW + 100},
        ],
    )
    recorder._prepare()
    try:
        assert store.overview(days=1, now=clock.value)["live_streams"] == 0

        recorder._poll_online_streamers(clock.value)

        rows = {
            item["streamer"]: item for item in store.streamers(days=1, now=clock.value)
        }
        # Restarting mid-broadcast keeps the live badge without waiting for the
        # next offline -> online transition, and the streak claimed on that
        # broadcast is still reported for it.
        assert rows["bob"]["live_stream"]["broadcast_id"] == "b1"
        assert rows["bob"]["live_streak_at"] == NOW + 120
        # Point history from before the feature existed still proves watching.
        assert rows["carol"]["live_stream"]["broadcast_id"] == "c1"
        # Offline and never-tracked streamers must not become phantom live rows.
        assert rows["dave"]["live_stream"] is None
        assert "eve" not in rows
    finally:
        recorder._shutdown()


def test_recorder_accepts_a_live_snapshot_of_streamer_objects(tmp_path):
    analytics = tmp_path / "analytics"
    analytics.mkdir()
    store = HistoryStore(analytics / "history.sqlite3")
    store.initialize()
    store.start_watch_session("bob", "points", NOW)

    class _Stream:
        broadcast_id = "b1"
        game = {"displayName": "Chess"}
        title = "Ranked"

    class _Streamer:
        username = "Bob"
        is_online = True
        online_at = NOW + 50
        stream = _Stream()

    clock = FakeClock(NOW + 100)
    recorder = HistoryRecorder(
        store,
        analytics_path=analytics,
        streak_cache_path=tmp_path / "missing.json",
        username="alice",
        clock=clock,
        online_streamers=lambda: [_Streamer()],
    )
    recorder._prepare()
    try:
        recorder._poll_online_streamers(clock.value)
        rows = {
            item["streamer"]: item for item in store.streamers(days=1, now=clock.value)
        }
        assert rows["bob"]["live_stream"]["broadcast_id"] == "b1"
        assert rows["bob"]["live_stream"]["game"] == "Chess"
        assert rows["bob"]["live_stream"]["title"] == "Ranked"
    finally:
        recorder._shutdown()


def test_recorder_skips_watches_recorded_only_for_drops(recorder):
    clock = recorder.clock
    clock.value = NOW + 40
    path = os.path.join(recorder.analytics_path, "now_watching.json")
    _write_json(
        path,
        [
            {"username": "bob", "reason": "points"},
            {"username": "carol", "reason": "drops"},
            {"username": "dave", "reason": "badge"},
        ],
        clock.value,
    )

    recorder._poll_now_watching()

    streamers = [
        item["streamer"] for item in recorder.store.streamers(days=1, now=clock.value)
    ]
    # Only the configured/follower watch survives; category and badge farming
    # channels must not create history rows.
    assert streamers == ["bob"]
    sessions = recorder.store.sessions(kind="watch", days=1, now=clock.value)
    assert [item["streamer"] for item in sessions["sessions"]] == ["bob"]
    assert "carol" not in recorder._tracked_streamers
    assert "dave" not in recorder._tracked_streamers


def test_recorder_skips_streamers_reported_as_drop_farmed(tmp_path):
    analytics = tmp_path / "analytics"
    analytics.mkdir()
    store = HistoryStore(analytics / "history.sqlite3")
    clock = FakeClock(NOW + 40)
    recorder = HistoryRecorder(
        store,
        analytics_path=analytics,
        streak_cache_path=tmp_path / "missing.json",
        username="alice",
        clock=clock,
        drop_streamers=lambda: ["Carol"],
    )
    recorder._prepare()
    try:
        path = os.path.join(analytics, "now_watching.json")
        _write_json(
            path,
            [
                {"username": "bob", "reason": "points"},
                # The reason alone would look like a normal watch, but the
                # miner reported the channel as a Drops discovery.
                {"username": "carol", "reason": "points"},
            ],
            clock.value,
        )

        recorder._poll_now_watching()

        streamers = [
            item["streamer"] for item in store.streamers(days=1, now=clock.value)
        ]
        assert streamers == ["bob"]
    finally:
        recorder._shutdown()


def test_prepare_removes_legacy_drop_farmed_streamers(tmp_path):
    analytics = tmp_path / "analytics"
    analytics.mkdir()
    store = HistoryStore(analytics / "history.sqlite3")
    store.initialize()
    # A previous build recorded a category farming channel as a normal watch.
    store.start_watch_session("bob", "drops", NOW)
    store.upsert_stream_session("bob", "b1", NOW)
    store.start_watch_session("carol", "points", NOW)

    recorder = HistoryRecorder(
        store,
        analytics_path=analytics,
        streak_cache_path=tmp_path / "missing.json",
        username="alice",
        clock=FakeClock(NOW + 60),
    )
    recorder._prepare()
    try:
        streamers = [item["streamer"] for item in store.streamers(days=1, now=NOW + 60)]
        assert streamers == ["carol"]
        assert recorder._tracked_streamers == {"carol"}
    finally:
        recorder._shutdown()


def test_recorder_ignores_analytics_written_for_drop_farmed_streamers(tmp_path):
    analytics = tmp_path / "analytics"
    analytics.mkdir()
    store = HistoryStore(analytics / "history.sqlite3")
    clock = FakeClock(NOW + 60)
    recorder = HistoryRecorder(
        store,
        analytics_path=analytics,
        streak_cache_path=tmp_path / "missing.json",
        username="alice",
        clock=clock,
        drop_streamers=lambda: ["carol"],
    )
    recorder._prepare()
    try:
        _write_json(
            os.path.join(analytics, "carol.json"),
            {
                "version": 1,
                "series": [
                    {"x": int(NOW * 1000), "y": 1000, "z": "Watch"},
                    {"x": int((NOW + 30) * 1000), "y": 1050, "z": "Watch"},
                ],
            },
            NOW + 1,
        )

        recorder._refresh_streamer_state()
        recorder._ingest_analytics(NOW + 60)

        assert store.streamers(days=1, now=clock.value) == []
        assert store.overview(days=1, now=clock.value)["window"]["points_gained"] == 0
    finally:
        recorder._shutdown()


def test_first_run_import_is_idempotent(recorder):
    analytics = recorder.analytics_path
    path = os.path.join(analytics, "alice.json")
    _write_json(
        path,
        {
            "version": 1,
            "series": [
                {"x": int(NOW * 1000), "y": 1000, "z": "Watch"},
                {"x": int((NOW + 60) * 1000), "y": 1050, "z": "Watch"},
            ],
            "annotations": [
                {
                    "x": int((NOW + 120) * 1000),
                    "label": {"text": "+50 - WATCH_STREAK"},
                    "borderColor": "#45c1ff",
                }
            ],
        },
        NOW + 1,
    )

    recorder._ingest_analytics(NOW + 2)
    recorder._ingest_analytics(NOW + 3)

    overview = recorder.store.overview(days=30, now=NOW + 120)
    assert overview["window"]["points_gained"] == 50
    assert overview["window"]["streak_count"] == 1
    events = recorder.store.events(days=30, now=NOW + 120)
    assert events["total"] == 1


def test_recorder_retries_database_initialization(tmp_path):
    class FlakyStore:
        def __init__(self):
            self.ready = False
            self.calls = 0

        def initialize(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("database is locked")
            self.ready = True

    store = FlakyStore()
    recorder = HistoryRecorder(
        store,
        analytics_path=tmp_path,
        streak_cache_path=tmp_path / "cache.json",
        username="alice",
    )
    recorder.interval = 0.01

    assert recorder._initialize_store() is True
    assert store.calls == 2


def test_poll_failure_is_logged_and_survives(recorder, monkeypatch):
    def fail():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(recorder, "poll_once", fail)
    recorder._poll_guarded()


def test_start_history_is_non_fatal_when_recorder_cannot_start(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("unwritable analytics directory")

    monkeypatch.setattr(history_module, "_recorder", None)
    monkeypatch.setattr(history_module, "_store", None)
    monkeypatch.setattr(
        "TwitchChannelPointsMiner.history.recorder.build_recorder",
        fail,
    )

    assert history_module.start_history("alice") is None


def test_retention_days_prefers_environment_override(monkeypatch):
    monkeypatch.setenv("TCPM_HISTORY_RETENTION_DAYS", "12")
    assert history_module._history_retention_days(730) == 12


def test_retention_days_uses_configured_value(monkeypatch):
    monkeypatch.delenv("TCPM_HISTORY_RETENTION_DAYS", raising=False)
    assert history_module._history_retention_days(45) == 45
    assert history_module._history_retention_days(0) == 0


def test_retention_days_rejects_invalid_values(monkeypatch):
    monkeypatch.setenv("TCPM_HISTORY_RETENTION_DAYS", "not-a-number")
    assert history_module._history_retention_days("also-bad") == 730


def _cache_payload(*sessions):
    return {
        "version": 1,
        "sessions": [
            {
                "account_name": "alice",
                "claimed": False,
                "claimed_at": None,
                "ended_at": None,
                **session,
            }
            for session in sessions
        ],
    }


def test_series_entries_sharing_a_second_are_all_imported(recorder):
    # The miner writes x in whole seconds, so a spend written later in the
    # same second as an earn shares its timestamp.
    path = os.path.join(recorder.analytics_path, "bob.json")
    series = [
        {"x": int(NOW * 1000), "y": 1000, "z": "Watch"},
        {"x": int((NOW + 60) * 1000), "y": 1050, "z": "Watch"},
    ]
    _write_json(path, {"series": series, "annotations": []}, NOW + 61)
    recorder._ingest_analytics(NOW + 61)

    series.append({"x": int((NOW + 60) * 1000), "y": 1000, "z": "Prediction"})
    _write_json(path, {"series": series, "annotations": []}, NOW + 62)
    recorder._ingest_analytics(NOW + 62)
    # Re-reading the same entries must not count them again.
    _write_json(path, {"series": series, "annotations": []}, NOW + 63)
    recorder._ingest_analytics(NOW + 63)

    window = recorder.store.overview(days=1, now=NOW + 63)["window"]
    assert window["points_gained"] == 50
    assert window["points_spent"] == 50
    assert recorder.store.latest_point("bob")["balance"] == 1000


def test_annotations_sharing_a_second_are_all_imported(recorder):
    path = os.path.join(recorder.analytics_path, "bob.json")
    x = int((NOW + 60) * 1000)
    annotations = [{"x": x, "label": {"text": "+10 - WATCH_STREAK"}}]
    _write_json(path, {"series": [], "annotations": annotations}, NOW + 61)
    recorder._ingest_analytics(NOW + 61)

    annotations.append({"x": x, "label": {"text": "Prediction won"}})
    _write_json(path, {"series": [], "annotations": annotations}, NOW + 62)
    recorder._ingest_analytics(NOW + 62)

    assert recorder.store.events(days=1, now=NOW + 62)["total"] == 2


def test_failed_cache_import_is_retried_without_a_cache_change(recorder, monkeypatch):
    recorder._tracked_streamers.add("bob")
    _write_json(
        recorder.streak_cache_path,
        _cache_payload(
            {"streamer_login": "bob", "broadcast_id": "b1", "started_at": NOW}
        ),
        NOW + 10,
    )
    original = recorder.store.upsert_stream_session

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(recorder.store, "upsert_stream_session", locked)
    with pytest.raises(sqlite3.OperationalError):
        recorder._ingest_streak_cache(NOW + 20)

    monkeypatch.setattr(recorder.store, "upsert_stream_session", original)
    recorder._ingest_streak_cache(NOW + 35)

    assert recorder.store.sessions(kind="stream", days=1, now=NOW + 35)["total"] == 1


def test_startup_failure_after_initialization_is_retried(tmp_path, monkeypatch):
    analytics = tmp_path / "analytics"
    analytics.mkdir()
    store = HistoryStore(analytics / "history.sqlite3")
    recorder = HistoryRecorder(
        store,
        analytics_path=analytics,
        streak_cache_path=tmp_path / "missing.json",
        username="alice",
        clock=FakeClock(),
    )
    recorder.interval = 0.01
    original = store.close_open_streams
    calls = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "close_open_streams", flaky)

    assert recorder._start() is True
    try:
        assert len(calls) == 2
        assert recorder._run_id is not None
        # The failed attempt did not leave a run row behind.
        assert store.sessions(kind="run", days=1, now=NOW)["total"] == 1
    finally:
        recorder._shutdown()


def test_streamer_removed_from_the_snapshot_is_no_longer_live(tmp_path):
    analytics = tmp_path / "analytics"
    analytics.mkdir()
    store = HistoryStore(analytics / "history.sqlite3")
    store.initialize()
    store.start_watch_session("bob", "points", NOW - 600)
    snapshot = [
        {"username": "bob", "broadcast_id": "b1", "online_at": NOW, "is_online": True}
    ]
    clock = FakeClock(NOW + 30)
    recorder = HistoryRecorder(
        store,
        analytics_path=analytics,
        streak_cache_path=tmp_path / "missing.json",
        username="alice",
        clock=clock,
        online_streamers=lambda: list(snapshot),
    )
    recorder._prepare()
    try:
        recorder.poll_once()
        assert store.overview(days=1, now=clock.value)["live_streams"] == 1

        # bob is removed from the config, so the miner stops reporting him.
        snapshot.clear()
        clock.value = NOW + 60
        recorder.poll_once()

        overview = store.overview(days=1, now=clock.value)
        assert overview["live_streams"] == 0
        stream = store.sessions(kind="stream", days=1, now=clock.value)["sessions"][0]
        assert stream["ended_at"] == NOW + 60
    finally:
        recorder._shutdown()


def test_deleted_history_is_not_imported_again(recorder):
    clock = recorder.clock
    path = os.path.join(recorder.analytics_path, "bob.json")
    series = [
        {"x": int(NOW * 1000), "y": 1000, "z": "Watch"},
        {"x": int((NOW + 60) * 1000), "y": 1050, "z": "Watch"},
    ]
    _write_json(path, {"series": series, "annotations": []}, NOW + 61)
    _write_json(
        recorder.streak_cache_path,
        _cache_payload(
            {
                "streamer_login": "bob",
                "broadcast_id": "b1",
                "started_at": NOW,
                "claimed": True,
                "claimed_at": NOW + 30,
            }
        ),
        NOW + 61,
    )
    clock.value = NOW + 70
    recorder.poll_once()
    assert recorder.store.overview(days=1, now=clock.value)["window"][
        "points_gained"
    ] == (50)

    clock.value = NOW + 100
    recorder.delete_streamer("bob")

    # The JSON and the cache still hold the deleted data; a later change to
    # either must only bring in what happened after the delete.
    series.append({"x": int((NOW + 200) * 1000), "y": 1060, "z": "Watch"})
    _write_json(path, {"series": series, "annotations": []}, NOW + 201)
    _write_json(
        recorder.streak_cache_path,
        _cache_payload(
            {
                "streamer_login": "bob",
                "broadcast_id": "b1",
                "started_at": NOW,
                "claimed": True,
                "claimed_at": NOW + 30,
            }
        ),
        NOW + 202,
    )
    clock.value = NOW + 210
    recorder.poll_once()

    window = recorder.store.overview(days=1, now=clock.value)["window"]
    assert window["points_gained"] == 10
    assert window["streak_count"] == 0
    assert recorder.store.events(days=1, now=clock.value)["total"] == 0
    assert recorder.store.sessions(kind="stream", days=1, now=clock.value)["total"] == (
        0
    )


def test_delete_waits_for_an_import_in_progress(recorder, monkeypatch):
    import threading

    path = os.path.join(recorder.analytics_path, "bob.json")
    series = [
        {"x": int(NOW * 1000), "y": 1000, "z": "Watch"},
        {"x": int((NOW + 60) * 1000), "y": 1050, "z": "Watch"},
    ]
    _write_json(path, {"series": series, "annotations": []}, NOW + 61)
    recorder.clock.value = NOW + 70
    read_done = threading.Event()
    release = threading.Event()
    original = recorder._ingest_series

    def slow_ingest(*args, **kwargs):
        # The file is already read; hold the import here like a slow write.
        read_done.set()
        release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(recorder, "_ingest_series", slow_ingest)
    poll = threading.Thread(target=recorder.poll_once)
    poll.start()
    assert read_done.wait(5)

    deleted = []
    delete = threading.Thread(
        target=lambda: deleted.append(recorder.delete_streamer("bob"))
    )
    delete.start()
    delete.join(0.2)
    # The delete must wait for the import, not run in the middle of it.
    assert delete.is_alive()
    release.set()
    poll.join(5)
    delete.join(5)

    assert deleted and deleted[0]["points"] == 1
    window = recorder.store.overview(days=1, now=NOW + 70)["window"]
    assert window["points_gained"] == 0


def test_deleting_an_active_watch_starts_a_new_session(recorder):
    clock = recorder.clock
    path = os.path.join(recorder.analytics_path, "now_watching.json")
    _write_json(path, [{"username": "bob", "reason": "points"}], NOW + 1)
    clock.value = NOW + 1
    recorder._poll_now_watching()

    recorder.delete_streamer("bob")

    _write_json(path, [{"username": "bob", "reason": "points"}], NOW + 61)
    clock.value = NOW + 61
    recorder._poll_now_watching()
    _write_json(path, [{"username": "bob", "reason": "points"}], NOW + 121)
    clock.value = NOW + 121
    recorder._poll_now_watching()

    sessions = recorder.store.sessions(kind="watch", days=1, now=clock.value)
    assert sessions["total"] == 1
    assert sessions["sessions"][0]["watch_seconds"] == 60
    window = recorder.store.overview(days=1, now=clock.value)["window"]
    assert window["watch_seconds"] == 60


def test_failed_watch_close_is_retried(recorder, monkeypatch):
    clock = recorder.clock
    path = os.path.join(recorder.analytics_path, "now_watching.json")
    _write_json(path, [{"username": "bob", "reason": "points"}], NOW + 1)
    clock.value = NOW + 1
    recorder._poll_now_watching()
    original = recorder.store.end_watch_session

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(recorder.store, "end_watch_session", locked)
    _write_json(path, [], NOW + 61)
    clock.value = NOW + 61
    with pytest.raises(sqlite3.OperationalError):
        recorder._poll_now_watching()
    assert "bob" in recorder._active_watch

    monkeypatch.setattr(recorder.store, "end_watch_session", original)
    _write_json(path, [], NOW + 91)
    clock.value = NOW + 91
    recorder._poll_now_watching()

    assert recorder._active_watch == {}
    assert recorder.store.overview(days=1, now=clock.value)[
        "active_watch_sessions"
    ] == (0)


def test_startup_import_waits_for_the_miner_streamers(tmp_path):
    analytics = tmp_path / "analytics"
    analytics.mkdir()
    cache = tmp_path / "watch_streak_cache.alice.json"
    store = HistoryStore(analytics / "history.sqlite3")
    _write_json(
        cache,
        _cache_payload(
            {
                "streamer_login": "carol",
                "broadcast_id": "c1",
                "started_at": NOW,
                "claimed": True,
                "claimed_at": NOW + 30,
                "ended_at": NOW + 600,
            }
        ),
        NOW + 600,
    )
    loaded = []
    clock = FakeClock(NOW + 700)
    recorder = HistoryRecorder(
        store,
        analytics_path=analytics,
        streak_cache_path=cache,
        username="alice",
        clock=clock,
        # Before the miner loads its streamers, no Drops channel is known.
        drop_streamers=lambda: ["carol"] if loaded else [],
        streamers_ready=lambda: bool(loaded),
    )
    recorder._prepare()
    try:
        recorder.poll_once()
        assert store.events(days=1, now=clock.value)["total"] == 0

        loaded.append(True)
        clock.value = NOW + 715
        recorder.poll_once()

        assert store.events(days=1, now=clock.value)["total"] == 0
        assert store.streamers(days=1, now=clock.value) == []
    finally:
        recorder._shutdown()


def test_cache_entries_past_retention_are_not_counted_twice(tmp_path):
    analytics = tmp_path / "analytics"
    analytics.mkdir()
    cache = tmp_path / "watch_streak_cache.alice.json"
    store = HistoryStore(analytics / "history.sqlite3")
    payload = _cache_payload(
        {
            "streamer_login": "bob",
            "broadcast_id": "b1",
            "started_at": NOW,
            "claimed": True,
            "claimed_at": NOW + 30,
            "ended_at": NOW + 600,
        }
    )
    _write_json(cache, payload, NOW + 600)
    clock = FakeClock(NOW + 700)
    recorder = HistoryRecorder(
        store,
        analytics_path=analytics,
        streak_cache_path=cache,
        username="alice",
        clock=clock,
        retention_days=1,
    )
    store.initialize()
    store.start_watch_session("bob", "points", NOW)
    recorder._prepare()
    try:
        assert store.overview(days=30, now=clock.value)["lifetime"]["streak_count"] == 1

        # Two days later retention prunes the raw rows, and the cache (which
        # keeps entries for a week) is rewritten.
        clock.value = NOW + 2 * 24 * 60 * 60
        recorder.poll_once()
        _write_json(cache, payload, clock.value + 1)
        clock.value += 15
        recorder.poll_once()

        lifetime = store.overview(days=30, now=clock.value)["lifetime"]
        assert lifetime["streak_count"] == 1
        streams = store.streamers(days=30, now=clock.value)[0]
        assert streams["stream_sessions"] == 1
    finally:
        recorder._shutdown()


def test_repeated_identical_changes_in_one_second_all_count(recorder):
    path = os.path.join(recorder.analytics_path, "bob.json")
    x = int((NOW + 60) * 1000)
    series = [
        {"x": int(NOW * 1000), "y": 1000, "z": "Watch"},
        {"x": x, "y": 1050, "z": "Watch"},
        {"x": x, "y": 1000, "z": "Prediction"},
        {"x": x, "y": 1050, "z": "Refund"},
    ]
    _write_json(path, {"series": series, "annotations": []}, NOW + 61)
    recorder._ingest_analytics(NOW + 61)

    window = recorder.store.overview(days=1, now=NOW + 61)["window"]
    assert window["points_gained"] == 100
    assert window["points_spent"] == 50


def test_live_snapshot_after_a_delete_does_not_restore_the_deleted_part(tmp_path):
    analytics = tmp_path / "analytics"
    analytics.mkdir()
    store = HistoryStore(analytics / "history.sqlite3")
    store.initialize()
    store.start_watch_session("bob", "points", NOW)
    snapshot = [
        {"username": "bob", "broadcast_id": "b1", "online_at": NOW, "is_online": True}
    ]
    clock = FakeClock(NOW + 600)
    recorder = HistoryRecorder(
        store,
        analytics_path=analytics,
        streak_cache_path=tmp_path / "missing.json",
        username="alice",
        clock=clock,
        online_streamers=lambda: list(snapshot),
    )
    recorder._prepare()
    try:
        recorder.poll_once()
        clock.value = NOW + 900
        recorder.delete_streamer("bob")

        # bob is still watched and still live.
        _write_json(
            analytics / "now_watching.json",
            [{"username": "bob", "reason": "points"}],
            NOW + 915,
        )
        clock.value = NOW + 915
        recorder.poll_once()

        streams = store.sessions(kind="stream", days=1, now=clock.value)["sessions"]
        assert [item["started_at"] for item in streams] == [NOW + 900]
    finally:
        recorder._shutdown()


def test_retired_drops_channel_stays_excluded(tmp_path):
    analytics = tmp_path / "analytics"
    analytics.mkdir()
    store = HistoryStore(analytics / "history.sqlite3")
    drops = ["carol"]
    snapshot = [{"username": "bob"}, {"username": "carol"}]
    clock = FakeClock(NOW + 100)

    def make_recorder():
        return HistoryRecorder(
            store,
            analytics_path=analytics,
            streak_cache_path=tmp_path / "missing.json",
            username="alice",
            clock=clock,
            online_streamers=lambda: list(snapshot),
            drop_streamers=lambda: list(drops),
        )

    recorder = make_recorder()
    recorder._prepare()
    try:
        recorder.poll_once()
        # The miner retires carol, but her analytics JSON stays behind.
        drops.clear()
        snapshot[:] = [{"username": "bob"}]
        _write_json(
            analytics / "carol.json",
            {
                "series": [
                    {"x": int(NOW * 1000), "y": 1000},
                    {"x": int((NOW + 60) * 1000), "y": 1050},
                ]
            },
            NOW + 61,
        )
        clock.value = NOW + 115
        recorder.poll_once()
        assert store.streamers(days=1, now=clock.value) == []
    finally:
        recorder._shutdown()

    # A restart remembers her too.
    recorder = make_recorder()
    recorder._prepare()
    try:
        clock.value = NOW + 130
        recorder.poll_once()
        assert store.streamers(days=1, now=clock.value) == []

        # Adding her to the config makes her a tracked streamer again.
        snapshot.append({"username": "carol"})
        clock.value = NOW + 145
        recorder.poll_once()
        assert [item["streamer"] for item in store.streamers(days=1, now=NOW)] == [
            "carol"
        ]
    finally:
        recorder._shutdown()


def test_longer_retention_after_a_restart_does_not_recount_pruned_streaks(tmp_path):
    analytics = tmp_path / "analytics"
    analytics.mkdir()
    cache = tmp_path / "watch_streak_cache.alice.json"
    store = HistoryStore(analytics / "history.sqlite3")
    store.initialize()
    store.start_watch_session("bob", "points", NOW)
    _write_json(
        cache,
        _cache_payload(
            {
                "streamer_login": "bob",
                "broadcast_id": "b1",
                "started_at": NOW,
                "claimed": True,
                "claimed_at": NOW + 30,
                "ended_at": NOW + 600,
            }
        ),
        NOW + 600,
    )
    clock = FakeClock(NOW + 700)

    def make_recorder(retention_days):
        return HistoryRecorder(
            store,
            analytics_path=analytics,
            streak_cache_path=cache,
            username="alice",
            clock=clock,
            retention_days=retention_days,
        )

    recorder = make_recorder(1)
    recorder._prepare()
    clock.value = NOW + 2 * 24 * 60 * 60
    recorder.poll_once()
    recorder._shutdown()

    # Restart with a longer window; the cache still holds the claim.
    recorder = make_recorder(0)
    recorder._prepare()
    try:
        recorder.poll_once()
        lifetime = store.overview(days=30, now=clock.value)["lifetime"]
        assert lifetime["streak_count"] == 1
    finally:
        recorder._shutdown()


def test_corruption_found_while_running_rebuilds_the_database(recorder, monkeypatch):
    recorder.delete_streamer("bob")
    original = recorder.poll_once
    calls = []

    def corrupt():
        calls.append(1)
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(recorder, "poll_once", corrupt)
    recorder._poll_guarded()
    monkeypatch.setattr(recorder, "poll_once", original)

    folder = Path(recorder.store.path).parent
    assert list(folder.glob("history.sqlite3.bak-*"))
    assert recorder._run_id is not None
    # The rebuilt database still knows bob was deleted.
    assert "bob" in recorder.store.deletion_cutoffs()
    recorder.poll_once()


def test_first_change_after_a_dashboard_delete_is_counted(recorder):
    clock = recorder.clock
    path = os.path.join(recorder.analytics_path, "bob.json")
    _write_json(
        path,
        {
            "series": [
                {"x": int(NOW * 1000), "y": 1000},
                {"x": int((NOW + 60) * 1000), "y": 1050},
            ]
        },
        NOW + 61,
    )
    clock.value = NOW + 70
    recorder.poll_once()

    # The dashboard deletes History and the JSON; the miner then starts a new
    # file with its next change.
    clock.value = NOW + 100
    recorder.delete_streamer("bob")
    os.remove(path)
    _write_json(
        path, {"series": [{"x": int((NOW + 200) * 1000), "y": 1060}]}, NOW + 201
    )
    clock.value = NOW + 210
    recorder.poll_once()

    window = recorder.store.overview(days=1, now=clock.value)["window"]
    assert window["points_gained"] == 10


def test_import_cursor_read_failure_does_not_replay_the_file(recorder, monkeypatch):
    path = os.path.join(recorder.analytics_path, "bob.json")
    _write_json(
        path,
        {
            "series": [
                {"x": int(NOW * 1000), "y": 1000},
                {"x": int((NOW + 60) * 1000), "y": 1050},
            ]
        },
        NOW + 61,
    )
    recorder._ingest_analytics(NOW + 61)
    _write_json(
        path,
        {
            "series": [
                {"x": int(NOW * 1000), "y": 1000},
                {"x": int((NOW + 60) * 1000), "y": 1050},
                {"x": int((NOW + 120) * 1000), "y": 1060},
            ]
        },
        NOW + 121,
    )

    def locked(source):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(recorder.store, "get_import_state", locked)
    with pytest.raises(sqlite3.OperationalError):
        recorder._ingest_analytics(NOW + 121)


def test_a_failed_rebuild_is_prepared_again_on_the_next_poll(recorder, monkeypatch):
    original = recorder._prepare
    attempts = []

    def flaky_prepare():
        attempts.append(1)
        if len(attempts) == 1:
            raise sqlite3.OperationalError("database is locked")
        original()

    def corrupt():
        raise sqlite3.DatabaseError("database disk image is malformed")

    poll = recorder.poll_once
    monkeypatch.setattr(recorder, "_prepare", flaky_prepare)
    monkeypatch.setattr(recorder, "poll_once", corrupt)
    recorder._poll_guarded()
    assert recorder._run_id is None

    monkeypatch.setattr(recorder, "poll_once", poll)
    recorder._poll_guarded()
    assert len(attempts) == 2
    assert recorder._run_id is not None


def test_drop_classifier_reads_the_same_snapshot(tmp_path):
    analytics = tmp_path / "analytics"
    analytics.mkdir()
    store = HistoryStore(analytics / "history.sqlite3")
    snapshot = [
        {"username": "bob", "drops": False},
        {"username": "carol", "drops": True},
    ]

    def drop_list():
        raise AssertionError("the separate Drops list must not be read")

    recorder = HistoryRecorder(
        store,
        analytics_path=analytics,
        streak_cache_path=tmp_path / "missing.json",
        username="alice",
        clock=FakeClock(NOW + 100),
        online_streamers=lambda: list(snapshot),
        drop_streamers=drop_list,
        is_drop_streamer=lambda item: item["drops"],
    )
    recorder._prepare()
    try:
        recorder.poll_once()
        assert recorder._drop_farmed == {"carol"}
        # carol is retired: she leaves the snapshot but stays excluded.
        snapshot.pop()
        recorder.poll_once()
        assert recorder._drop_farmed == {"carol"}
        assert store.drop_farmed_streamers() == {"carol"}
    finally:
        recorder._shutdown()


def test_delete_while_history_is_starting_is_refused(monkeypatch):
    class Starting:
        store = None

        def is_alive(self):
            return True

    monkeypatch.setattr(history_module, "_recorder", Starting())
    monkeypatch.setattr(history_module, "_store", None)

    with pytest.raises(history_module.HistoryUnavailableError):
        history_module.delete_streamer_history("bob")


def test_delete_without_a_running_recorder_clears_the_database(tmp_path, monkeypatch):
    path = tmp_path / "history.sqlite3"
    store = HistoryStore(path)
    store.initialize()
    store.record_points("bob", NOW, 100, 10)
    monkeypatch.setattr(history_module, "_recorder", None)
    monkeypatch.setattr(history_module, "_store", None)

    deleted = history_module.delete_streamer_history("bob", database_path=str(path))

    assert deleted["points"] == 1
    assert "bob" in store.deletion_cutoffs()
    assert history_module.delete_streamer_history("bob") is None


def test_delete_imports_pending_changes_before_the_json_goes(recorder):
    clock = recorder.clock
    path = os.path.join(recorder.analytics_path, "bob.json")
    series = [
        {"x": int(NOW * 1000), "y": 1000},
        {"x": int((NOW + 60) * 1000), "y": 1010},
    ]
    _write_json(path, {"series": series}, NOW + 61)
    clock.value = NOW + 62
    recorder.poll_once()
    # The miner writes 1050, and the dashboard deletes before the next poll.
    series.append({"x": int((NOW + 70) * 1000), "y": 1050})
    _write_json(path, {"series": series}, NOW + 71)
    clock.value = NOW + 72
    recorder.delete_streamer("bob")
    os.remove(path)

    _write_json(
        path, {"series": [{"x": int((NOW + 200) * 1000), "y": 1060}]}, NOW + 201
    )
    clock.value = NOW + 210
    recorder.poll_once()

    assert recorder.store.overview(days=1, now=clock.value)["window"][
        "points_gained"
    ] == (10)


def test_entries_written_after_a_clock_correction_are_imported(recorder):
    path = os.path.join(recorder.analytics_path, "bob.json")
    series = [
        {"x": int(NOW * 1000), "y": 1000},
        {"x": int((NOW + 60) * 1000), "y": 1010},
    ]
    _write_json(path, {"series": series}, NOW + 61)
    recorder._ingest_analytics(NOW + 61)
    # The host clock was set back 30 seconds before the next change.
    series.append({"x": int((NOW + 30) * 1000), "y": 1030})
    _write_json(path, {"series": series}, NOW + 62)
    recorder._ingest_analytics(NOW + 62)

    assert recorder.store.overview(days=1, now=NOW + 62)["window"]["points_gained"] == (
        30
    )


def test_leftover_json_of_an_untracked_channel_is_not_imported(tmp_path):
    analytics = tmp_path / "analytics"
    analytics.mkdir()
    store = HistoryStore(analytics / "history.sqlite3")
    for name in ("bob", "oldbadge"):
        _write_json(
            analytics / f"{name}.json",
            {
                "series": [
                    {"x": int(NOW * 1000), "y": 1000},
                    {"x": int((NOW + 60) * 1000), "y": 1050},
                ]
            },
            NOW + 61,
        )
    recorder = HistoryRecorder(
        store,
        analytics_path=analytics,
        streak_cache_path=tmp_path / "missing.json",
        username="alice",
        clock=FakeClock(NOW + 100),
        # oldbadge was farmed for a badge campaign in an earlier run; this
        # run has not discovered it (yet).
        online_streamers=lambda: [{"username": "bob"}],
    )
    recorder._prepare()
    try:
        recorder.poll_once()
        assert [item["streamer"] for item in store.streamers(days=1, now=NOW)] == [
            "bob"
        ]
    finally:
        recorder._shutdown()


def test_new_broadcast_without_an_offline_gap_starts_when_first_seen(tmp_path):
    analytics = tmp_path / "analytics"
    analytics.mkdir()
    store = HistoryStore(analytics / "history.sqlite3")
    store.initialize()
    store.start_watch_session("bob", "points", NOW)
    snapshot = [
        {"username": "bob", "broadcast_id": "b1", "online_at": NOW, "is_online": True}
    ]
    clock = FakeClock(NOW + 600)
    recorder = HistoryRecorder(
        store,
        analytics_path=analytics,
        streak_cache_path=tmp_path / "missing.json",
        username="alice",
        clock=clock,
        online_streamers=lambda: list(snapshot),
    )
    recorder._prepare()
    try:
        recorder.poll_once()
        # The stream restarted while the miner kept bob online; online_at
        # still belongs to b1.
        snapshot[0] = dict(snapshot[0], broadcast_id="b2")
        clock.value = NOW + 900
        recorder.poll_once()

        streams = {
            item["broadcast_id"]: item
            for item in store.sessions(kind="stream", days=1, now=clock.value)[
                "sessions"
            ]
        }
        assert streams["b1"]["started_at"] == NOW
        assert streams["b1"]["ended_at"] == NOW + 900
        assert streams["b2"]["started_at"] == NOW + 900
        assert streams["b2"]["active"] is True
    finally:
        recorder._shutdown()


def test_failed_snapshot_write_is_retried_without_a_new_snapshot(recorder, monkeypatch):
    clock = recorder.clock
    path = os.path.join(recorder.analytics_path, "now_watching.json")
    _write_json(path, [{"username": "bob", "reason": "points"}], NOW + 1)
    original = recorder.store.start_watch_session

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(recorder.store, "start_watch_session", locked)
    clock.value = NOW + 2
    with pytest.raises(sqlite3.OperationalError):
        recorder._poll_now_watching()

    monkeypatch.setattr(recorder.store, "start_watch_session", original)
    clock.value = NOW + 17
    recorder._poll_now_watching()

    assert recorder.store.overview(days=1, now=clock.value)[
        "active_watch_sessions"
    ] == (1)


def test_a_short_watch_still_records_its_time(recorder):
    clock = recorder.clock
    path = os.path.join(recorder.analytics_path, "now_watching.json")
    _write_json(path, [{"username": "bob", "reason": "points"}], NOW + 1)
    clock.value = NOW + 2
    recorder._poll_now_watching()
    _write_json(path, [], NOW + 61)
    clock.value = NOW + 62
    recorder._poll_now_watching()

    session = recorder.store.sessions(kind="watch", days=1, now=clock.value)[
        "sessions"
    ][0]
    assert session["watch_seconds"] == 60


def test_analytics_json_is_read_under_the_miner_file_lock(recorder, monkeypatch):
    from TwitchChannelPointsMiner.classes.Settings import ANALYTICS_FILE_MUTEX
    from TwitchChannelPointsMiner.history import recorder as recorder_module

    path = os.path.join(recorder.analytics_path, "bob.json")
    _write_json(path, {"series": [{"x": int(NOW * 1000), "y": 1}]}, NOW + 1)
    locked = []
    original = recorder_module._read_json

    def read(target):
        locked.append(ANALYTICS_FILE_MUTEX.locked())
        return original(target)

    monkeypatch.setattr(recorder_module, "_read_json", read)
    recorder._ingest_analytics(NOW + 2)

    assert locked == [True]
    assert not ANALYTICS_FILE_MUTEX.locked()


def test_a_failed_import_writes_nothing_so_the_retry_is_exact(recorder, monkeypatch):
    path = os.path.join(recorder.analytics_path, "bob.json")
    x = int(NOW * 1000)
    series = [{"x": x, "y": 1000}, {"x": x, "y": 1050}, {"x": x, "y": 1060}]
    _write_json(path, {"series": series}, NOW + 1)
    original = HistoryStore._insert_points
    calls = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise sqlite3.OperationalError("disk I/O error")
        return original(*args, **kwargs)

    monkeypatch.setattr(HistoryStore, "_insert_points", staticmethod(flaky))
    with pytest.raises(sqlite3.OperationalError):
        recorder._ingest_analytics(NOW + 2)
    monkeypatch.setattr(HistoryStore, "_insert_points", original)
    recorder._ingest_analytics(NOW + 3)

    window = recorder.store.overview(days=1, now=NOW + 3)["window"]
    assert window["points_gained"] == 60
    assert window["points_spent"] == 0


def test_broadcast_open_at_startup_stays_closed_once_backfill_tracks_it(tmp_path):
    analytics = tmp_path / "analytics"
    analytics.mkdir()
    cache = tmp_path / "watch_streak_cache.alice.json"
    store = HistoryStore(analytics / "history.sqlite3")
    bob_session = {"streamer_login": "bob", "broadcast_id": "b1", "started_at": NOW}
    _write_json(cache, _cache_payload(bob_session), NOW + 10)
    _write_json(
        analytics / "bob.json",
        {
            "series": [
                {"x": int(NOW * 1000), "y": 1},
                {"x": int((NOW + 5) * 1000), "y": 2},
            ]
        },
        NOW + 10,
    )
    clock = FakeClock(NOW + 3600)
    recorder = HistoryRecorder(
        store,
        analytics_path=analytics,
        streak_cache_path=cache,
        username="alice",
        clock=clock,
        # bob is configured but offline now.
        online_streamers=lambda: [{"username": "bob", "is_online": False}],
    )
    recorder._prepare()
    try:
        recorder.poll_once()  # the backfill makes bob tracked
        # Another channel's entry rewrites the cache; bob's old one is still open.
        _write_json(
            cache,
            _cache_payload(
                bob_session,
                {"streamer_login": "carol", "broadcast_id": "c1", "started_at": NOW},
            ),
            NOW + 3610,
        )
        clock.value = NOW + 3615
        recorder.poll_once()

        assert store.overview(days=1, now=clock.value)["live_streams"] == 0
    finally:
        recorder._shutdown()


def test_delete_removes_the_json_under_the_miner_file_lock(recorder):
    from TwitchChannelPointsMiner.classes.Settings import ANALYTICS_FILE_MUTEX

    path = os.path.join(recorder.analytics_path, "bob.json")
    series = [
        {"x": int(NOW * 1000), "y": 1000},
        {"x": int((NOW + 60) * 1000), "y": 1010},
    ]
    _write_json(path, {"series": series}, NOW + 61)
    recorder.clock.value = NOW + 70
    seen = {}

    def remove():
        seen["locked"] = ANALYTICS_FILE_MUTEX.locked()
        seen["points"] = recorder.store.overview(days=1, now=NOW + 70)["window"][
            "points_gained"
        ]
        os.remove(path)

    # The change at NOW + 60 was never imported by a poll.
    recorder.delete_streamer("bob", remove_source=remove)

    assert seen == {"locked": True, "points": 0}
    assert not os.path.exists(path)
    # Its balance is in the kept cursor, so the next file starts from 1010.
    state = recorder.store.get_import_state("analytics:bob.json:series")
    assert state["last_balance"] == 1010


def test_entries_in_the_delete_second_count_as_deleted(recorder):
    clock = recorder.clock
    path = os.path.join(recorder.analytics_path, "bob.json")
    series = [{"x": int(NOW * 1000), "y": 1000}]
    _write_json(path, {"series": series}, NOW + 1)
    clock.value = NOW + 100.4
    recorder.delete_streamer("bob")

    # x NOW + 100 may be a reward stamped just before the delete and written
    # just after it, so it stays deleted; its balance still counts.
    series.append({"x": int((NOW + 100) * 1000), "y": 1010})
    series.append({"x": int((NOW + 101) * 1000), "y": 1015})
    _write_json(path, {"series": series}, NOW + 102)
    clock.value = NOW + 103
    recorder.poll_once()

    assert recorder.store.overview(days=1, now=clock.value)["window"][
        "points_gained"
    ] == (5)


class _RecordingLock:
    def __init__(self):
        self.held = False
        self.entries = 0

    def __enter__(self):
        self.held = True
        self.entries += 1
        return self

    def __exit__(self, *exc):
        self.held = False
        return False


def test_snapshot_and_cache_are_read_under_the_miner_locks(tmp_path, monkeypatch):
    from TwitchChannelPointsMiner.history import recorder as recorder_module

    analytics = tmp_path / "analytics"
    analytics.mkdir()
    cache = tmp_path / "watch_streak_cache.alice.json"
    watching_lock = _RecordingLock()
    cache_lock = _RecordingLock()
    recorder = HistoryRecorder(
        HistoryStore(analytics / "history.sqlite3"),
        analytics_path=analytics,
        streak_cache_path=cache,
        username="alice",
        clock=FakeClock(NOW + 20),
        source_locks={"now_watching": watching_lock, "streak_cache": cache_lock},
    )
    recorder._prepare()
    try:
        _write_json(analytics / "now_watching.json", [], NOW + 10)
        _write_json(cache, _cache_payload(), NOW + 10)
        held = []
        original = recorder_module._read_json

        def read(path):
            held.append((os.path.basename(path), watching_lock.held, cache_lock.held))
            return original(path)

        monkeypatch.setattr(recorder_module, "_read_json", read)
        recorder.poll_once()

        assert ("now_watching.json", True, False) in held
        assert (os.path.basename(cache), False, True) in held
    finally:
        recorder._shutdown()


def test_recreated_json_with_the_same_length_and_second_is_read(recorder):
    clock = recorder.clock
    path = os.path.join(recorder.analytics_path, "bob.json")
    x = int((NOW + 60) * 1000)
    _write_json(path, {"series": [{"x": x, "y": 1000}]}, NOW + 60)
    clock.value = NOW + 60.2
    recorder.poll_once()

    clock.value = NOW + 60.5
    recorder.delete_streamer("bob", remove_source=lambda: os.remove(path))
    # The new file starts with an entry at the old one's position and second.
    # That entry is in the delete's second, so it is not recorded, but it
    # must not be mistaken for the old one either.
    series = [{"x": x, "y": 1010}, {"x": int((NOW + 61) * 1000), "y": 1020}]
    _write_json(path, {"series": series}, NOW + 62)
    clock.value = NOW + 63
    recorder.poll_once()

    assert recorder.store.overview(days=1, now=clock.value)["window"][
        "points_gained"
    ] == (10)


def test_watch_from_a_snapshot_written_before_a_delete_starts_at_the_delete(
    recorder,
):
    clock = recorder.clock
    path = os.path.join(recorder.analytics_path, "now_watching.json")
    _write_json(path, [{"username": "bob", "reason": "points"}], NOW + 80)
    clock.value = NOW + 84
    recorder.delete_streamer("bob")
    clock.value = NOW + 90
    recorder._poll_now_watching()

    session = recorder.store.sessions(kind="watch", days=1, now=clock.value)[
        "sessions"
    ][0]
    assert session["started_at"] == NOW + 84


def test_claim_earlier_in_the_delete_second_stays_deleted(recorder):
    clock = recorder.clock
    recorder._tracked_streamers.add("bob")
    claim = {
        "streamer_login": "bob",
        "broadcast_id": "b1",
        "started_at": NOW,
        "claimed": True,
        "claimed_at": NOW + 100.2,
        "ended_at": NOW + 200,
    }
    _write_json(recorder.streak_cache_path, _cache_payload(claim), NOW + 101)
    clock.value = NOW + 100.8
    recorder.delete_streamer("bob")
    _write_json(recorder.streak_cache_path, _cache_payload(claim), NOW + 201)
    clock.value = NOW + 210
    recorder._ingest_streak_cache(clock.value)

    assert recorder.store.events(days=1, now=clock.value)["total"] == 0


def test_a_failed_schema_rebuild_is_prepared_again(recorder, monkeypatch):
    original = HistoryStore._initialize_schema
    calls = []

    def flaky(self):
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("disk I/O error")
        return original(self)

    def corrupt():
        raise sqlite3.DatabaseError("database disk image is malformed")

    poll = recorder.poll_once
    monkeypatch.setattr(HistoryStore, "_initialize_schema", flaky)
    monkeypatch.setattr(recorder, "poll_once", corrupt)
    recorder._poll_guarded()
    assert recorder._run_id is None

    monkeypatch.setattr(recorder, "poll_once", poll)
    recorder._poll_guarded()
    assert recorder._run_id is not None
    assert recorder.store.sessions(kind="run", days=1, now=NOW)["total"] == 1


def test_stopped_history_stays_readable_and_deletes_through_the_recorder(
    recorder, monkeypatch
):
    from flask import Flask

    from TwitchChannelPointsMiner.history import web as history_web

    monkeypatch.setattr(history_module, "_recorder", recorder)
    monkeypatch.setattr(history_module, "_store", recorder.store)
    history_module.stop_history()

    assert history_module.get_store() is recorder.store
    assert history_module.is_recording() is False
    app = Flask(__name__)
    history_web.register_history(app)
    health = app.test_client().get("/history/health")
    assert health.status_code == 200
    assert health.get_json()["recording"] is False

    calls = []
    monkeypatch.setattr(
        recorder, "delete_streamer", lambda name, **kwargs: calls.append(name) or {}
    )
    history_module.delete_streamer_history("bob")
    assert calls == ["bob"]


def test_delete_without_a_recorder_imports_pending_changes_first(tmp_path, monkeypatch):
    import time

    # The standalone delete stamps its cutoff with the real clock.
    base = time.time() - 100
    analytics = tmp_path / "analytics"
    analytics.mkdir()
    path = analytics / "bob.json"
    store = HistoryStore(analytics / "history.sqlite3")
    store.initialize()
    series = [
        {"x": int(base * 1000), "y": 1000},
        {"x": int((base + 60) * 1000), "y": 1010},
    ]
    _write_json(path, {"series": series}, base + 61)
    first = HistoryRecorder(
        store,
        analytics_path=analytics,
        streak_cache_path=tmp_path / "missing.json",
        username="alice",
        clock=FakeClock(base + 70),
    )
    first._prepare()
    first.poll_once()
    first._shutdown()
    # The miner wrote 1050 after History stopped; then the user deletes.
    series.append({"x": int((base + 80) * 1000), "y": 1050})
    _write_json(path, {"series": series}, base + 81)
    monkeypatch.setattr(history_module, "_recorder", None)
    monkeypatch.setattr(history_module, "_store", None)
    history_module.delete_streamer_history(
        "bob",
        database_path=str(analytics / "history.sqlite3"),
        remove_source=lambda: os.remove(path),
    )

    _write_json(
        path, {"series": [{"x": int((base + 200) * 1000), "y": 1060}]}, base + 201
    )
    second = HistoryRecorder(
        store,
        analytics_path=analytics,
        streak_cache_path=tmp_path / "missing.json",
        username="alice",
        clock=FakeClock(base + 210),
    )
    second._prepare()
    try:
        second.poll_once()
        assert store.overview(days=1, now=base + 210)["window"]["points_gained"] == 10
    finally:
        second._shutdown()


def test_live_title_and_category_changes_are_recorded(tmp_path):
    analytics = tmp_path / "analytics"
    analytics.mkdir()
    store = HistoryStore(analytics / "history.sqlite3")
    store.initialize()
    store.start_watch_session("bob", "points", NOW)
    entry = {
        "username": "bob",
        "broadcast_id": "b1",
        "online_at": NOW,
        "is_online": True,
        "title": "Morning",
        "game": "Chess",
    }
    snapshot = [entry]
    clock = FakeClock(NOW + 30)
    recorder = HistoryRecorder(
        store,
        analytics_path=analytics,
        streak_cache_path=tmp_path / "missing.json",
        username="alice",
        clock=clock,
        online_streamers=lambda: list(snapshot),
    )
    recorder._prepare()
    try:
        recorder.poll_once()
        snapshot[0] = dict(entry, title="Evening", game="Go")
        clock.value = NOW + 60
        recorder.poll_once()

        live = store.streamers(days=1, now=clock.value)[0]["live_stream"]
        assert (live["title"], live["game"]) == ("Evening", "Go")
    finally:
        recorder._shutdown()


def test_first_watch_is_linked_to_its_cached_live_broadcast(tmp_path):
    analytics = tmp_path / "analytics"
    analytics.mkdir()
    cache = tmp_path / "watch_streak_cache.alice.json"
    store = HistoryStore(analytics / "history.sqlite3")
    _write_json(
        cache,
        _cache_payload(
            {"streamer_login": "bob", "broadcast_id": "b1", "started_at": NOW}
        ),
        NOW + 10,
    )
    clock = FakeClock(NOW + 20)
    recorder = HistoryRecorder(
        store,
        analytics_path=analytics,
        streak_cache_path=cache,
        username="alice",
        clock=clock,
    )
    recorder._prepare()
    try:
        _write_json(
            analytics / "now_watching.json",
            [{"username": "bob", "reason": "points"}],
            NOW + 25,
        )
        clock.value = NOW + 30
        recorder._poll_now_watching()

        watch = store.sessions(kind="watch", days=1, now=clock.value)["sessions"][0]
        assert watch["broadcast_id"] == "b1"
    finally:
        recorder._shutdown()


def test_stopping_history_ends_live_broadcasts(tmp_path):
    analytics = tmp_path / "analytics"
    analytics.mkdir()
    store = HistoryStore(analytics / "history.sqlite3")
    store.initialize()
    store.start_watch_session("bob", "points", NOW)
    clock = FakeClock(NOW + 30)
    recorder = HistoryRecorder(
        store,
        analytics_path=analytics,
        streak_cache_path=tmp_path / "missing.json",
        username="alice",
        clock=clock,
        online_streamers=lambda: [
            {"username": "bob", "broadcast_id": "b1", "online_at": NOW}
        ],
    )
    recorder._prepare()
    recorder.poll_once()
    assert store.overview(days=1, now=clock.value)["live_streams"] == 1

    clock.value = NOW + 45
    recorder._shutdown()

    assert store.overview(days=1, now=NOW + 600)["live_streams"] == 0


def test_snapshot_time_and_contents_are_read_together(recorder, monkeypatch):
    path = os.path.join(recorder.analytics_path, "now_watching.json")
    lock = _RecordingLock()
    recorder.source_locks["now_watching"] = lock
    _write_json(path, [{"username": "bob", "reason": "points"}], NOW + 10)
    held = []
    original = os.path.getmtime

    def getmtime(target):
        if os.fspath(target) == path:
            held.append(lock.held)
        return original(target)

    monkeypatch.setattr(os.path, "getmtime", getmtime)
    recorder.clock.value = NOW + 15
    recorder._poll_now_watching()

    assert held and all(held)


def test_delete_catches_up_a_channel_no_longer_configured(tmp_path):
    analytics = tmp_path / "analytics"
    analytics.mkdir()
    store = HistoryStore(analytics / "history.sqlite3")
    path = analytics / "bob.json"
    series = [
        {"x": int(NOW * 1000), "y": 1000},
        {"x": int((NOW + 10) * 1000), "y": 1010},
    ]
    _write_json(path, {"series": series}, NOW + 11)
    snapshot = [{"username": "bob"}]
    clock = FakeClock(NOW + 20)
    recorder = HistoryRecorder(
        store,
        analytics_path=analytics,
        streak_cache_path=tmp_path / "missing.json",
        username="alice",
        clock=clock,
        online_streamers=lambda: list(snapshot),
    )
    recorder._prepare()
    try:
        recorder.poll_once()
        # bob leaves the config with a change History has not imported.
        snapshot.clear()
        series.append({"x": int((NOW + 30) * 1000), "y": 1050})
        _write_json(path, {"series": series}, NOW + 31)
        clock.value = NOW + 35
        recorder.poll_once()
        clock.value = NOW + 40
        recorder.delete_streamer("bob", remove_source=lambda: os.remove(path))

        # bob is configured again and earns 10.
        snapshot.append({"username": "bob"})
        _write_json(
            path, {"series": [{"x": int((NOW + 100) * 1000), "y": 1060}]}, NOW + 101
        )
        clock.value = NOW + 105
        recorder.poll_once()

        assert store.overview(days=1, now=clock.value)["window"]["points_gained"] == 10
    finally:
        recorder._shutdown()


def test_watch_continuing_into_a_new_broadcast_starts_a_new_session(recorder):
    clock = recorder.clock
    store = recorder.store
    path = os.path.join(recorder.analytics_path, "now_watching.json")
    store.upsert_stream_session("bob", "b1", NOW)
    _write_json(path, [{"username": "bob", "reason": "points"}], NOW + 10)
    clock.value = NOW + 11
    recorder._poll_now_watching()

    # bob restarts the stream between two watch cycles.
    store.upsert_stream_session("bob", "b2", NOW + 50)
    _write_json(path, [{"username": "bob", "reason": "points"}], NOW + 70)
    clock.value = NOW + 71
    recorder._poll_now_watching()

    sessions = store.sessions(kind="watch", days=1, now=clock.value)["sessions"]
    assert sorted(item["broadcast_id"] for item in sessions) == ["b1", "b2"]
    assert [item["broadcast_id"] for item in sessions if item["active"]] == ["b2"]


def test_unreadable_json_at_delete_does_not_inflate_the_next_file(recorder):
    clock = recorder.clock
    path = os.path.join(recorder.analytics_path, "bob.json")
    series = [
        {"x": int(NOW * 1000), "y": 1000},
        {"x": int((NOW + 10) * 1000), "y": 1050},
    ]
    _write_json(path, {"series": series}, NOW + 11)
    clock.value = NOW + 12
    recorder.poll_once()
    # The file now holds changes History cannot read.
    Path(path).write_text("{not json", encoding="utf-8")
    os.utime(path, (NOW + 20, NOW + 20))
    clock.value = NOW + 30
    recorder.delete_streamer("bob", remove_source=lambda: os.remove(path))

    new = [
        {"x": int((NOW + 100) * 1000), "y": 1110},
        {"x": int((NOW + 110) * 1000), "y": 1120},
    ]
    _write_json(path, {"series": new}, NOW + 111)
    clock.value = NOW + 115
    recorder.poll_once()

    # 1110 is a new baseline; only the known +10 counts.
    assert recorder.store.overview(days=1, now=clock.value)["window"][
        "points_gained"
    ] == (10)


def test_watch_started_before_its_broadcast_row_is_linked_later(tmp_path):
    analytics = tmp_path / "analytics"
    analytics.mkdir()
    store = HistoryStore(analytics / "history.sqlite3")
    store.initialize()
    store.start_watch_session("bob", "points", NOW)
    clock = FakeClock(NOW + 30)
    recorder = HistoryRecorder(
        store,
        analytics_path=analytics,
        streak_cache_path=tmp_path / "missing.json",
        username="alice",
        clock=clock,
        online_streamers=lambda: [
            {"username": "bob", "broadcast_id": "b1", "online_at": NOW}
        ],
    )
    recorder._prepare()
    try:
        recorder.poll_once()
        clock.value = NOW + 60
        recorder.delete_streamer("bob")
        # Still watched and live: the next poll sees the watch before the
        # live snapshot recreates the broadcast row.
        watching = analytics / "now_watching.json"
        _write_json(watching, [{"username": "bob", "reason": "points"}], NOW + 70)
        clock.value = NOW + 75
        recorder.poll_once()
        _write_json(watching, [{"username": "bob", "reason": "points"}], NOW + 90)
        clock.value = NOW + 95
        recorder.poll_once()

        watch = store.sessions(kind="watch", days=1, now=clock.value)["sessions"][0]
        assert watch["broadcast_id"] == "b1"
    finally:
        recorder._shutdown()


def test_shutdown_credits_the_last_snapshot(recorder):
    clock = recorder.clock
    path = os.path.join(recorder.analytics_path, "now_watching.json")
    _write_json(path, [{"username": "bob", "reason": "points"}], NOW + 20)
    clock.value = NOW + 30
    recorder.poll_once()
    # The miner refreshes the snapshot, then stops before the next poll.
    _write_json(path, [{"username": "bob", "reason": "points"}], NOW + 35)
    clock.value = NOW + 36
    recorder._shutdown()

    session = recorder.store.sessions(kind="watch", days=1, now=clock.value)[
        "sessions"
    ][0]
    assert session["watch_seconds"] >= 15
    assert session["active"] is False


def test_removed_streamer_keeps_what_it_earned_before(tmp_path):
    analytics = tmp_path / "analytics"
    analytics.mkdir()
    store = HistoryStore(analytics / "history.sqlite3")
    path = analytics / "bob.json"
    series = [
        {"x": int(NOW * 1000), "y": 1000},
        {"x": int((NOW + 10) * 1000), "y": 1050},
    ]
    _write_json(path, {"series": series}, NOW + 11)
    snapshot = [{"username": "bob"}]
    clock = FakeClock(NOW + 20)
    recorder = HistoryRecorder(
        store,
        analytics_path=analytics,
        streak_cache_path=tmp_path / "missing.json",
        username="alice",
        clock=clock,
        online_streamers=lambda: list(snapshot),
    )
    recorder._prepare()
    try:
        recorder.poll_once()
        series.append({"x": int((NOW + 30) * 1000), "y": 1060})
        _write_json(path, {"series": series}, NOW + 31)
        snapshot.clear()  # removed from the config before the next poll
        clock.value = NOW + 35
        recorder.poll_once()

        assert store.overview(days=1, now=clock.value)["window"]["points_gained"] == 60
    finally:
        recorder._shutdown()


def test_backfill_imports_cached_broadcasts_without_a_cache_change(tmp_path):
    analytics = tmp_path / "analytics"
    analytics.mkdir()
    cache = tmp_path / "watch_streak_cache.alice.json"
    store = HistoryStore(analytics / "history.sqlite3")
    _write_json(
        cache,
        _cache_payload(
            {
                "streamer_login": "bob",
                "broadcast_id": "b1",
                "started_at": NOW,
                "ended_at": NOW + 600,
            }
        ),
        NOW + 600,
    )
    _write_json(
        analytics / "bob.json",
        {
            "series": [
                {"x": int(NOW * 1000), "y": 1},
                {"x": int((NOW + 5) * 1000), "y": 2},
            ]
        },
        NOW + 10,
    )
    clock = FakeClock(NOW + 700)
    recorder = HistoryRecorder(
        store,
        analytics_path=analytics,
        streak_cache_path=cache,
        username="alice",
        clock=clock,
    )
    recorder._prepare()
    try:
        recorder.poll_once()
        streams = store.sessions(kind="stream", days=1, now=clock.value)["sessions"]
        assert [item["broadcast_id"] for item in streams] == ["b1"]
    finally:
        recorder._shutdown()


def test_open_cache_entry_of_a_removed_streamer_is_not_live_again(tmp_path):
    analytics = tmp_path / "analytics"
    analytics.mkdir()
    cache = tmp_path / "watch_streak_cache.alice.json"
    store = HistoryStore(analytics / "history.sqlite3")
    store.initialize()
    store.start_watch_session("bob", "points", NOW - 600)
    snapshot = [
        {"username": "bob", "broadcast_id": "b1", "online_at": NOW, "is_online": True}
    ]
    clock = FakeClock(NOW + 30)
    recorder = HistoryRecorder(
        store,
        analytics_path=analytics,
        streak_cache_path=cache,
        username="alice",
        clock=clock,
        retention_days=1,
        online_streamers=lambda: list(snapshot),
    )
    recorder._prepare()
    # Removing bob from the config does not end his streak-cache entry.
    payload = _cache_payload(
        {"streamer_login": "bob", "broadcast_id": "b1", "started_at": NOW}
    )
    try:
        _write_json(cache, payload, NOW + 20)
        recorder.poll_once()
        snapshot.clear()
        clock.value = NOW + 60
        recorder.poll_once()

        # Retention prunes the closed row, then the cache is rewritten.
        clock.value = NOW + 2 * 24 * 60 * 60
        recorder.poll_once()
        _write_json(cache, payload, clock.value + 1)
        clock.value += 15
        recorder.poll_once()

        overview = store.overview(days=30, now=clock.value)
        assert overview["live_streams"] == 0
        assert store.sessions(kind="stream", days=30, now=clock.value)["total"] == 0
    finally:
        recorder._shutdown()


def test_live_snapshot_read_during_set_online_keeps_one_broadcast(tmp_path):
    from types import SimpleNamespace

    analytics = tmp_path / "analytics"
    analytics.mkdir()
    store = HistoryStore(analytics / "history.sqlite3")
    store.initialize()
    store.start_watch_session("bob", "points", NOW)

    class Streamer:
        # The miner already stored the new broadcast id; set_online runs on
        # another thread while History reads online_at.
        def __init__(self):
            self.username = "bob"
            self.stream = SimpleNamespace(broadcast_id="b2", game=None, title=None)
            self.is_online = False
            self._online_at = NOW - 3600
            self._raced = False

        @property
        def online_at(self):
            value = self._online_at
            if not self._raced:
                self._raced = True
                self._online_at = NOW
                self.is_online = True
            return value

    streamer = Streamer()
    clock = FakeClock(NOW + 30)
    recorder = HistoryRecorder(
        store,
        analytics_path=analytics,
        streak_cache_path=tmp_path / "missing.json",
        username="alice",
        clock=clock,
        online_streamers=lambda: [streamer],
    )
    recorder._prepare()
    try:
        recorder.poll_once()
        clock.value = NOW + 60
        recorder.poll_once()

        live = store.streamers(days=1, now=clock.value)[0]["live_stream"]
        assert live["broadcast_id"] == "b2"
        assert live["started_at"] == NOW
    finally:
        recorder._shutdown()


def test_failed_first_backfill_is_retried(recorder, monkeypatch):
    _write_json(
        recorder.streak_cache_path,
        _cache_payload(
            {"streamer_login": "bob", "broadcast_id": "b1", "started_at": NOW}
        ),
        NOW + 10,
    )
    recorder._ingest_streak_cache(NOW + 20)
    path = os.path.join(recorder.analytics_path, "bob.json")
    series = [
        {"x": int(NOW * 1000), "y": 1000, "z": "Watch"},
        {"x": int((NOW + 60) * 1000), "y": 1050, "z": "Watch"},
    ]
    _write_json(path, {"series": series, "annotations": []}, NOW + 61)
    original = recorder.store.upsert_stream_session

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(recorder.store, "upsert_stream_session", locked)
    with pytest.raises(sqlite3.OperationalError):
        recorder._ingest_analytics(NOW + 62)

    monkeypatch.setattr(recorder.store, "upsert_stream_session", original)
    recorder._ingest_analytics(NOW + 75)

    assert recorder.store.sessions(kind="stream", days=1, now=NOW + 75)["total"] == 1
    assert recorder.store.latest_point("bob")["balance"] == 1050


def test_cleared_live_category_is_recorded(tmp_path):
    analytics = tmp_path / "analytics"
    analytics.mkdir()
    store = HistoryStore(analytics / "history.sqlite3")
    store.initialize()
    store.start_watch_session("bob", "points", NOW)
    entry = {
        "username": "bob",
        "broadcast_id": "b1",
        "online_at": NOW,
        "is_online": True,
        "title": "Morning",
        "game": "Chess",
    }
    snapshot = [entry]
    clock = FakeClock(NOW + 30)
    recorder = HistoryRecorder(
        store,
        analytics_path=analytics,
        streak_cache_path=tmp_path / "missing.json",
        username="alice",
        clock=clock,
        online_streamers=lambda: list(snapshot),
    )
    recorder._prepare()
    try:
        recorder.poll_once()
        snapshot[0] = dict(entry, game={})
        clock.value = NOW + 60
        recorder.poll_once()

        live = store.streamers(days=1, now=clock.value)[0]["live_stream"]
        assert (live["title"], live["game"]) == ("Morning", None)
    finally:
        recorder._shutdown()


def test_unreadable_watch_snapshot_is_read_again(recorder, monkeypatch):
    from TwitchChannelPointsMiner.history import recorder as recorder_module

    clock = recorder.clock
    path = os.path.join(recorder.analytics_path, "now_watching.json")
    _write_json(path, [{"username": "bob", "reason": "points"}], NOW + 1)
    clock.value = NOW + 2
    recorder._poll_now_watching()

    _write_json(
        path,
        [
            {"username": "bob", "reason": "points"},
            {"username": "carol", "reason": "points"},
        ],
        NOW + 30,
    )
    original = recorder_module._read_json

    def denied(target):
        raise PermissionError("in use")

    monkeypatch.setattr(recorder_module, "_read_json", denied)
    clock.value = NOW + 31
    recorder._poll_now_watching()
    monkeypatch.setattr(recorder_module, "_read_json", original)
    clock.value = NOW + 46
    recorder._poll_now_watching()

    overview = recorder.store.overview(days=1, now=clock.value)
    assert overview["active_watch_sessions"] == 2


def test_observed_end_replaces_one_inferred_from_the_next_broadcast(tmp_path):
    analytics = tmp_path / "analytics"
    analytics.mkdir()
    cache = tmp_path / "watch_streak_cache.alice.json"
    store = HistoryStore(analytics / "history.sqlite3")
    store.initialize()
    store.start_watch_session("bob", "points", NOW - 600)
    snapshot = [
        {"username": "bob", "broadcast_id": "b1", "online_at": NOW, "is_online": True}
    ]
    clock = FakeClock(NOW + 30)
    recorder = HistoryRecorder(
        store,
        analytics_path=analytics,
        streak_cache_path=cache,
        username="alice",
        clock=clock,
        online_streamers=lambda: list(snapshot),
    )
    recorder._prepare()
    try:
        recorder.poll_once()
        # The cache could not be written across a short restart.
        snapshot[0] = {
            "username": "bob",
            "broadcast_id": "b2",
            "online_at": NOW + 1000,
            "is_online": True,
        }
        clock.value = NOW + 1010
        recorder.poll_once()

        _write_json(
            cache,
            _cache_payload(
                {
                    "streamer_login": "bob",
                    "broadcast_id": "b1",
                    "started_at": NOW,
                    "ended_at": NOW + 900,
                    "claimed": True,
                    "claimed_at": NOW + 60,
                },
                {
                    "streamer_login": "bob",
                    "broadcast_id": "b2",
                    "started_at": NOW + 1000,
                },
            ),
            NOW + 1020,
        )
        clock.value = NOW + 1030
        recorder.poll_once()

        streams = store.sessions(kind="stream", days=1, now=clock.value)["sessions"]
        ended = {item["broadcast_id"]: item["ended_at"] for item in streams}
        assert ended["b1"] == NOW + 900
        assert store.streamers(days=1, now=clock.value)[0]["live_streak_at"] == (
            NOW + 60
        )
    finally:
        recorder._shutdown()


def test_shutdown_imports_changes_written_after_the_last_poll(recorder):
    path = os.path.join(recorder.analytics_path, "bob.json")
    series = [
        {"x": int(NOW * 1000), "y": 1000, "z": "Watch"},
        {"x": int((NOW + 60) * 1000), "y": 1050, "z": "Watch"},
    ]
    _write_json(path, {"series": series, "annotations": []}, NOW + 61)
    recorder.clock.value = NOW + 62
    recorder.poll_once()

    series.append({"x": int((NOW + 120) * 1000), "y": 1075, "z": "Watch"})
    _write_json(path, {"series": series, "annotations": []}, NOW + 121)
    recorder.clock.value = NOW + 122
    recorder._shutdown()

    assert recorder.store.latest_point("bob")["balance"] == 1075


def test_streamer_removed_before_it_was_seen_live_is_not_live(tmp_path):
    analytics = tmp_path / "analytics"
    analytics.mkdir()
    cache = tmp_path / "watch_streak_cache.alice.json"
    store = HistoryStore(analytics / "history.sqlite3")
    store.initialize()
    store.start_watch_session("bob", "points", NOW - 600)
    # bob went online and was removed from the config before History polled.
    snapshot = [
        {"username": "carol", "broadcast_id": "c1", "online_at": NOW, "is_online": True}
    ]
    clock = FakeClock(NOW + 10)
    recorder = HistoryRecorder(
        store,
        analytics_path=analytics,
        streak_cache_path=cache,
        username="alice",
        clock=clock,
        online_streamers=lambda: list(snapshot),
    )
    recorder._prepare()
    try:
        _write_json(
            cache,
            _cache_payload(
                {"streamer_login": "bob", "broadcast_id": "b1", "started_at": NOW}
            ),
            NOW + 20,
        )
        clock.value = NOW + 30
        recorder.poll_once()
        clock.value = NOW + 1000
        recorder.poll_once()

        assert store.open_broadcast("bob") is None
        assert store.overview(days=1, now=clock.value)["live_streams"] == 0
    finally:
        recorder._shutdown()


def test_older_copy_of_an_analytics_file_adds_no_spend(recorder):
    path = os.path.join(recorder.analytics_path, "bob.json")
    series = [
        {"x": int(NOW * 1000), "y": 1000, "z": "Watch"},
        {"x": int((NOW + 60) * 1000), "y": 1050, "z": "Watch"},
        {"x": int((NOW + 120) * 1000), "y": 1100, "z": "Watch"},
    ]
    _write_json(path, {"series": series, "annotations": []}, NOW + 121)
    recorder.clock.value = NOW + 130
    recorder.poll_once()

    # An older backup of the file is put back.
    _write_json(path, {"series": series[:2], "annotations": []}, NOW + 140)
    recorder.clock.value = NOW + 150
    recorder.poll_once()

    window = recorder.store.overview(days=1, now=recorder.clock.value)["window"]
    assert window["points_gained"] == 100
    assert window["points_spent"] == 0
