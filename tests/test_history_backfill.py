"""History's one-time import of the analytics JSON (#403)."""

import json
import logging
import os
import sqlite3
from contextlib import closing

import pytest

from TwitchChannelPointsMiner.history import backfill as backfill_module
from TwitchChannelPointsMiner.history.backfill import AnalyticsBackfill
from TwitchChannelPointsMiner.history.journal import save_delete_cutoff, segment_name
from TwitchChannelPointsMiner.history.recorder import HistoryRecorder, build_recorder
from TwitchChannelPointsMiner.history.store import HistoryStore, utc_day

# The first journal record; the analytics JSON from before it is imported.
T0 = 1_800_000_000.0
DAY = 24 * 60 * 60


def ms(timestamp):
    return int(timestamp * 1000)


def entry(at, balance, label="Watch"):
    return {"x": ms(at), "y": balance, "z": label}


def annotation(at, color, text):
    return {
        "x": ms(at),
        "borderColor": color,
        "label": {"style": {"color": "#000", "background": color}, "text": text},
    }


class Analytics(object):
    """The account's analytics folder: JSON files and the journal."""

    def __init__(self, root):
        self.root = str(root)
        self.journal = os.path.join(self.root, "history")
        os.makedirs(self.journal, exist_ok=True)
        self.seq = 0

    def write(self, streamer, series=(), annotations=()):
        with open(
            os.path.join(self.root, f"{streamer}.json"), "w", encoding="utf-8"
        ) as handle:
            json.dump(
                {
                    "version": 1,
                    "series": list(series),
                    "annotations": list(annotations),
                },
                handle,
            )

    def record(self, kind, at=T0, run=1, **fields):
        self.seq += 1
        line = {"v": 1, "run": run, "seq": self.seq, "at": at, "kind": kind}
        line.update(fields)
        with open(os.path.join(self.journal, segment_name(run, 1)), "ab") as handle:
            handle.write((json.dumps(line) + "\n").encode())


@pytest.fixture
def analytics(tmp_path):
    return Analytics(tmp_path)


@pytest.fixture
def tracked():
    return {"names": ["alice"]}


def recorder_for(
    analytics, tracked, batch_size=2000, step_seconds=1.0, mono=None, automatic=True
):
    store = HistoryStore(os.path.join(analytics.root, "history.sqlite3"))
    clock = mono if mono is not None else {"mono": 0.0}
    recorder = HistoryRecorder(
        store,
        analytics.journal,
        monotonic=lambda: clock["mono"],
        clock=lambda: T0 + 60,
    )
    recorder.backfill = AnalyticsBackfill(
        store,
        analytics.root,
        recorder.importer,
        lambda: tracked["names"],
        monotonic=lambda: clock["mono"],
        batch_size=batch_size,
        step_seconds=step_seconds,
        automatic=automatic,
    )
    assert recorder._initialize()
    return recorder


def query(store, sql, *params):
    with closing(sqlite3.connect(store.path)) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(sql, params)]


def totals(store, streamer="alice"):
    (row,) = query(
        store,
        """
        SELECT COALESCE(SUM(points_gained), 0) AS gained,
               COALESCE(SUM(points_spent), 0) AS spent,
               COALESCE(SUM(streak_count), 0) AS streaks,
               COALESCE(SUM(watch_seconds), 0) AS watch
        FROM daily_stats WHERE streamer = ?
        """,
        streamer,
    )
    return row


def points(store, streamer="alice"):
    return query(
        store,
        "SELECT ts, balance, delta, reason, kind FROM points "
        "WHERE streamer = ? ORDER BY ts, id",
        streamer,
    )


def events(store, streamer="alice"):
    return query(
        store,
        "SELECT ts, type, label, points_delta FROM events "
        "WHERE streamer = ? ORDER BY ts, id",
        streamer,
    )


# -- what becomes what ---------------------------------------------------------
def test_balances_before_the_journal_become_points(analytics, tracked):
    analytics.write(
        "alice",
        [
            entry(T0 - 300, 100),
            entry(T0 - 200, 110),
            entry(T0 - 100, 160, "Claim"),
            # From the journal's time on, the journal has it.
            entry(T0 + 50, 170),
        ],
    )
    analytics.record("run_start")
    recorder = recorder_for(analytics, tracked)

    assert recorder.poll_once()

    assert points(recorder.store) == [
        {
            "ts": T0 - 300,
            "balance": 100,
            "delta": None,
            "reason": "WATCH",
            "kind": "balance",
        },
        {
            "ts": T0 - 200,
            "balance": 110,
            "delta": 10,
            "reason": "WATCH",
            "kind": "earn",
        },
        {
            "ts": T0 - 100,
            "balance": 160,
            "delta": 50,
            "reason": "CLAIM",
            "kind": "earn",
        },
    ]
    assert totals(recorder.store) == {
        "gained": 60,
        "spent": 0,
        "streaks": 0,
        "watch": 0,
    }


def test_a_change_counts_only_where_it_fits_its_entry(analytics, tracked):
    analytics.write(
        "alice",
        [
            entry(T0 - 500, 1000),
            # A loss on an earning entry: spent elsewhere, not earned here.
            entry(T0 - 400, 900),
            entry(T0 - 300, 700, "Spent"),
            # A gain on a spend: not this spend's change.
            entry(T0 - 200, 750, "Spent"),
            entry(T0 - 100, 750),
        ],
    )
    analytics.record("run_start")
    recorder = recorder_for(analytics, tracked)
    recorder.poll_once()

    assert [(row["kind"], row["delta"]) for row in points(recorder.store)] == [
        ("balance", None),
        ("balance", None),
        ("spend", -200),
        ("balance", None),
        ("balance", None),
    ]
    assert totals(recorder.store)["gained"] == 0
    assert totals(recorder.store)["spent"] == 200


def test_an_unreadable_balance_restarts_the_change(analytics, tracked):
    analytics.write(
        "alice",
        [
            entry(T0 - 300, 100),
            {"x": ms(T0 - 200), "y": "bad", "z": "Watch"},
            entry(T0 - 100, 500),
            entry(T0 - 50, 510),
        ],
    )
    analytics.record("run_start")
    recorder = recorder_for(analytics, tracked)
    recorder.poll_once()

    assert [row["delta"] for row in points(recorder.store)] == [None, None, 10]


def test_streaks_and_predictions_become_events(analytics, tracked):
    analytics.write(
        "alice",
        [entry(T0 - 300, 100), entry(T0 - 200, 550, "Watch Streak")],
        [
            annotation(T0 - 200, "#45c1ff", "+450 - WATCH_STREAK"),
            annotation(T0 - 150, "#ffe045", "Decision: 1 - Will it work?"),
            annotation(T0 - 100, "#36B535", "Will it work?"),
            annotation(T0 - 90, "#ff4545", "Another"),
            annotation(T0 - 80, "#123456", "Unknown kind"),
            annotation(T0 + 10, "#ff4545", "After the journal started"),
        ],
    )
    analytics.record("run_start")
    recorder = recorder_for(analytics, tracked)
    recorder.poll_once()

    assert events(recorder.store) == [
        {
            "ts": T0 - 200,
            "type": "watch_streak",
            "label": "+450 - WATCH_STREAK",
            "points_delta": 450,
        },
        {
            "ts": T0 - 150,
            "type": "prediction_made",
            "label": "Decision: 1 - Will it work?",
            "points_delta": None,
        },
        {"ts": T0 - 100, "type": "win", "label": "Will it work?", "points_delta": None},
        {"ts": T0 - 90, "type": "lose", "label": "Another", "points_delta": None},
    ]
    assert totals(recorder.store)["streaks"] == 1
    assert totals(recorder.store)["gained"] == 450


def test_points_count_on_their_own_utc_day(analytics, tracked):
    analytics.write(
        "alice",
        [entry(T0 - 3 * DAY, 100), entry(T0 - 2 * DAY, 120), entry(T0 - DAY, 150)],
    )
    analytics.record("run_start")
    recorder = recorder_for(analytics, tracked)
    recorder.poll_once()

    rows = query(
        recorder.store,
        "SELECT day, points_gained, watch_seconds FROM daily_stats ORDER BY day",
    )
    assert rows == [
        {"day": utc_day(T0 - 3 * DAY), "points_gained": 0, "watch_seconds": 0},
        {"day": utc_day(T0 - 2 * DAY), "points_gained": 20, "watch_seconds": 0},
        {"day": utc_day(T0 - DAY), "points_gained": 30, "watch_seconds": 0},
    ]


# -- the boundary -----------------------------------------------------------------
def test_nothing_is_imported_before_history_holds_a_journal_record(analytics, tracked):
    analytics.write("alice", [entry(T0 - 300, 100), entry(T0 - 200, 110)])
    recorder = recorder_for(analytics, tracked)

    recorder.poll_once()
    assert points(recorder.store) == []

    analytics.record("run_start")
    recorder.poll_once()
    assert len(points(recorder.store)) == 2


def test_the_boundary_is_the_earliest_journal_record_and_stays(analytics, tracked):
    analytics.write("alice", [entry(T0 - 300, 100), entry(T0 - 200, 110)])
    analytics.record("run_start", at=T0 - 250)
    recorder = recorder_for(analytics, tracked)
    recorder.poll_once()
    assert [row["ts"] for row in points(recorder.store)] == [T0 - 300]

    # Retention prunes the journal's rows; the boundary does not move.
    tracked["names"] = ["alice", "bob"]
    analytics.write("bob", [entry(T0 - 300, 5), entry(T0 - 200, 6)])
    query(recorder.store, "SELECT 1")
    with closing(sqlite3.connect(recorder.store.path)) as connection, connection:
        connection.execute("DELETE FROM runs")
    recorder.poll_once()
    assert [row["ts"] for row in points(recorder.store, "bob")] == [T0 - 300]


def test_a_database_from_before_the_first_record_was_kept_uses_what_it_holds(
    analytics, tracked
):
    analytics.write("alice", [entry(T0 - 300, 100), entry(T0 - 200, 110)])
    analytics.record("run_start", at=T0 - 250)
    recorder = recorder_for(analytics, tracked)
    recorder.backfill = None
    recorder.poll_once()
    with closing(sqlite3.connect(recorder.store.path)) as connection, connection:
        connection.execute("DELETE FROM meta")

    # A later record must not become the boundary.
    analytics.record("watch_end", at=T0 + 100, streamer="alice")
    recorder.poll_once()
    (row,) = query(
        recorder.store, "SELECT value FROM meta WHERE key = 'journal_first_at'"
    )
    assert float(row["value"]) == T0 - 250

    recorder.backfill = recorder_for(analytics, tracked).backfill
    recorder.poll_once()
    assert [row["ts"] for row in points(recorder.store)] == [T0 - 300]


# -- which streamers ------------------------------------------------------------
def test_only_streamers_history_records_are_imported(analytics, tracked):
    analytics.write("alice", [entry(T0 - 300, 100), entry(T0 - 200, 110)])
    analytics.write("dropsonly", [entry(T0 - 300, 100), entry(T0 - 200, 110)])
    with open(os.path.join(analytics.root, "now_watching.json"), "w") as handle:
        json.dump(["alice"], handle)
    analytics.record("run_start")
    tracked["names"] = None
    recorder = recorder_for(analytics, tracked)

    recorder.poll_once()
    assert points(recorder.store) == []

    tracked["names"] = ["Alice"]
    recorder.poll_once()
    assert len(points(recorder.store)) == 2
    assert points(recorder.store, "dropsonly") == []

    # Added to the configuration later.
    tracked["names"] = ["alice", "dropsonly"]
    recorder.poll_once()
    assert len(points(recorder.store, "dropsonly")) == 2


def test_a_streamer_without_a_file_is_done(analytics, tracked):
    analytics.record("run_start")
    recorder = recorder_for(analytics, tracked)
    recorder.poll_once()

    assert recorder.backfill.status()["streamers_done"] == 1
    assert recorder.backfill.status()["streamers_waiting"] == 0


# -- once --------------------------------------------------------------------------
def test_a_finished_streamer_is_not_read_again(analytics, tracked, monkeypatch):
    analytics.write("alice", [entry(T0 - 300, 100), entry(T0 - 200, 110)])
    analytics.record("run_start")
    recorder = recorder_for(analytics, tracked)
    recorder.poll_once()

    reads = []
    real_read = AnalyticsBackfill._read
    monkeypatch.setattr(
        AnalyticsBackfill,
        "_read",
        lambda self, name, files, deadline: reads.append(name)
        or real_read(self, name, files, deadline),
    )
    # A restart: a new recorder on the same database.
    again = recorder_for(analytics, tracked)
    again.poll_once()
    again.poll_once()

    assert reads == []
    assert len(points(again.store)) == 2
    assert totals(again.store)["gained"] == 10


def test_steps_are_bounded_and_resume_where_they_stopped(analytics, tracked):
    series = [entry(T0 - 1000 + index, 100 + index) for index in range(7)]
    analytics.write("alice", series)
    analytics.record("run_start")
    mono = {"mono": 0.0}
    recorder = recorder_for(
        analytics, tracked, batch_size=2, step_seconds=1.0, mono=mono
    )

    def one_step_per_poll(original):
        def step(*args):
            result = original(*args)
            mono["mono"] += 5.0
            return result

        return step

    recorder.backfill._step = one_step_per_poll(recorder.backfill._step)
    counts = []
    for _ in range(5):
        recorder.poll_once()
        counts.append(len(points(recorder.store)))

    assert counts == [2, 4, 6, 7, 7]
    assert totals(recorder.store)["gained"] == 6


def test_a_failed_step_is_repeated_with_the_same_result(
    analytics, tracked, monkeypatch
):
    analytics.write(
        "alice", [entry(T0 - 1000 + index, 100 + 10 * index) for index in range(5)]
    )
    analytics.record("run_start")
    recorder = recorder_for(analytics, tracked, batch_size=2)
    recorder.importer.poll()

    real = AnalyticsBackfill._save_state
    calls = {"count": 0}

    def fail_second(self, connection, name, state):
        calls["count"] += 1
        if calls["count"] == 2:
            raise sqlite3.OperationalError("disk I/O error")
        return real(self, connection, name, state)

    monkeypatch.setattr(AnalyticsBackfill, "_save_state", fail_second)
    recorder.poll_once()
    assert len(points(recorder.store)) == 2

    recorder.poll_once()
    assert len(points(recorder.store)) == 5
    assert totals(recorder.store)["gained"] == 40


# -- deletes -----------------------------------------------------------------------
def test_a_streamer_deleted_beside_the_journal_is_never_imported(analytics, tracked):
    analytics.write("alice", [entry(T0 - 300, 100), entry(T0 - 200, 110)])
    analytics.record("run_start")
    save_delete_cutoff(analytics.journal, "alice", "", 0, T0 + 5)
    recorder = recorder_for(analytics, tracked)
    recorder.poll_once()

    assert points(recorder.store) == []
    assert recorder.backfill.status()["streamers_done"] == 1


def test_a_streamer_deleted_in_the_database_is_never_imported(analytics, tracked):
    analytics.write("alice", [entry(T0 - 300, 100), entry(T0 - 200, 110)])
    analytics.record("run_start")
    recorder = recorder_for(analytics, tracked)
    recorder.store.record_delete("alice", "", 0, T0 + 5)
    recorder.poll_once()

    assert points(recorder.store) == []


def test_a_delete_between_steps_stops_the_import(analytics, tracked):
    analytics.write(
        "alice", [entry(T0 - 1000 + index, 100 + index) for index in range(6)]
    )
    analytics.record("run_start")
    mono = {"mono": 0.0}
    recorder = recorder_for(analytics, tracked, batch_size=2, mono=mono)
    real_step = recorder.backfill._step

    def step(*args):
        result = real_step(*args)
        mono["mono"] += 5.0
        return result

    recorder.backfill._step = step
    recorder.poll_once()
    assert len(points(recorder.store)) == 2

    recorder.delete_streamer("alice")
    for _ in range(3):
        recorder.poll_once()

    assert points(recorder.store) == []
    assert totals(recorder.store)["gained"] == 0


def test_a_rebuilt_database_imports_again_but_not_a_deleted_streamer(
    analytics, tracked
):
    analytics.write("alice", [entry(T0 - 300, 100), entry(T0 - 200, 110)])
    analytics.write("bob", [entry(T0 - 300, 100), entry(T0 - 200, 130)])
    analytics.record("run_start")
    tracked["names"] = ["alice", "bob"]
    recorder = recorder_for(analytics, tracked)
    recorder.poll_once()
    recorder.delete_streamer("bob")

    assert recorder.store.recover_if_corrupt(
        sqlite3.DatabaseError("database disk image is malformed")
    )
    recorder.poll_once()

    assert totals(recorder.store)["gained"] == 10
    assert points(recorder.store, "bob") == []


# -- files -------------------------------------------------------------------------
def test_an_unreadable_file_is_retried(analytics, tracked, monkeypatch, caplog):
    analytics.write("alice", [entry(T0 - 300, 100), entry(T0 - 200, 110)])
    analytics.record("run_start")
    recorder = recorder_for(analytics, tracked)
    real_open = open
    blocked = {"on": True}

    def locked_open(path, *args, **kwargs):
        if blocked["on"] and str(path).endswith("alice.json"):
            raise PermissionError(13, "Permission denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", locked_open)
    with caplog.at_level(logging.WARNING):
        recorder.poll_once()
        recorder.poll_once()
    assert points(recorder.store) == []
    assert recorder.backfill.status()["streamers_waiting"] == 1
    assert len([r for r in caplog.records if "alice.json" in r.getMessage()]) == 1

    blocked["on"] = False
    recorder.poll_once()
    assert len(points(recorder.store)) == 2


def test_a_file_that_is_not_json_is_skipped(analytics, tracked, caplog):
    with open(os.path.join(analytics.root, "alice.json"), "w") as handle:
        handle.write('{"series": [')
    analytics.record("run_start")
    recorder = recorder_for(analytics, tracked)
    with caplog.at_level(logging.WARNING):
        recorder.poll_once()

    assert recorder.backfill.status()["streamers_done"] == 1
    assert any("not valid JSON" in r.getMessage() for r in caplog.records)


def test_the_file_is_read_under_the_analytics_file_lock(analytics, tracked):
    analytics.write("alice", [entry(T0 - 300, 100)])
    analytics.record("run_start")
    recorder = recorder_for(analytics, tracked)
    held = []

    class Mutex(object):
        def acquire(self, timeout):
            held.append(("acquire", timeout))
            return True

        def release(self):
            held.append("release")

    recorder.backfill._file_mutex = Mutex()
    recorder.poll_once()

    assert held == [("acquire", 1.0), "release"]
    assert len(points(recorder.store)) == 1


def test_a_file_the_miner_is_writing_is_read_on_a_later_poll(
    analytics, tracked, caplog
):
    import threading

    analytics.write("alice", [entry(T0 - 300, 100), entry(T0 - 200, 110)])
    analytics.record("run_start")
    recorder = recorder_for(analytics, tracked)
    mutex = threading.Lock()
    recorder.backfill._file_mutex = mutex
    timeouts = []
    real_acquire = mutex.acquire

    class Watched(object):
        def acquire(self, timeout):
            timeouts.append(timeout)
            return real_acquire(timeout=0)

        def release(self):
            mutex.release()

    recorder.backfill._file_mutex = Watched()
    with caplog.at_level(logging.WARNING):
        with mutex:
            recorder.poll_once()
        assert points(recorder.store) == []
        # Waited no longer than the poll's time.
        assert timeouts and max(timeouts) <= 1.0

        recorder.poll_once()

    assert len(points(recorder.store)) == 2
    assert not [r for r in caplog.records if "alice.json" in r.getMessage()]


def test_a_file_is_parsed_once_however_many_polls_its_import_takes(
    analytics, tracked, monkeypatch
):
    analytics.write(
        "alice", [entry(T0 - 1000 + index, 100 + index) for index in range(7)]
    )
    analytics.record("run_start")
    mono = {"mono": 0.0}
    recorder = recorder_for(analytics, tracked, batch_size=2, mono=mono)
    real_step = recorder.backfill._step

    def step(*args):
        result = real_step(*args)
        mono["mono"] += 5.0
        return result

    recorder.backfill._step = step
    parsed = []
    real_read = AnalyticsBackfill._read
    monkeypatch.setattr(
        AnalyticsBackfill,
        "_read",
        lambda self, *args: parsed.append(1) or real_read(self, *args),
    )
    for _ in range(6):
        recorder.poll_once()

    assert len(points(recorder.store)) == 7
    assert len(parsed) == 1
    assert recorder.backfill._snapshots == {}


def test_a_slow_read_leaves_the_import_to_the_next_poll(
    analytics, tracked, monkeypatch
):
    analytics.write("alice", [entry(T0 - 300, 100), entry(T0 - 200, 110)])
    analytics.record("run_start")
    mono = {"mono": 0.0}
    recorder = recorder_for(analytics, tracked, mono=mono)
    real_read = AnalyticsBackfill._read

    def slow_read(self, name, files, deadline):
        mono["mono"] += 5.0
        return real_read(self, name, files, deadline)

    monkeypatch.setattr(AnalyticsBackfill, "_read", slow_read)
    recorder.poll_once()
    assert points(recorder.store) == []

    recorder.poll_once()
    assert len(points(recorder.store)) == 2


# -- retention, option, reporting --------------------------------------------------
def test_retention_runs_once_the_import_finishes(analytics, tracked):
    analytics.write(
        "alice",
        [entry(T0 - 900 * DAY, 100), entry(T0 - 800 * DAY, 150), entry(T0 - DAY, 170)],
    )
    analytics.record("run_start")
    recorder = recorder_for(analytics, tracked)
    recorder.retention_days = 730
    recorder._last_retention = 0.0

    recorder.poll_once()

    # The raw rows older than the window are gone, their totals are kept.
    assert [row["ts"] for row in points(recorder.store)] == [T0 - DAY]
    assert totals(recorder.store)["gained"] == 70


def test_the_automatic_import_can_be_turned_off(tmp_path):
    recorder = build_recorder(str(tmp_path), import_analytics=False)
    assert isinstance(recorder.backfill, AnalyticsBackfill)
    assert recorder.backfill.automatic is False

    recorder = build_recorder(
        str(tmp_path), import_analytics=True, streamers=lambda: ["alice"]
    )
    assert recorder.backfill.automatic is True


def test_with_the_automatic_import_off_only_a_request_imports(analytics, tracked):
    analytics.write("alice", [entry(T0 - 300, 100), entry(T0 - 200, 105)])
    analytics.record("run_start")
    recorder = recorder_for(analytics, tracked, automatic=False)

    recorder.poll_once()
    assert points(recorder.store) == []
    status = recorder.backfill.status()
    assert status["complete"] is False
    assert status["requested"] is False
    assert status["streamers_waiting"] == 1

    status = recorder.request_import()
    assert status["requested"] is True
    assert status["complete"] is False

    recorder.poll_once()
    assert totals(recorder.store)["gained"] == 5
    status = recorder.backfill.status()
    # Done: the request is cleared and the button hides.
    assert status["complete"] is True
    assert status["requested"] is False


def test_a_request_survives_a_restart_until_the_import_is_done(analytics, tracked):
    analytics.write("alice", [entry(T0 - 300, 100), entry(T0 - 200, 105)])
    analytics.record("run_start")
    recorder = recorder_for(analytics, tracked, automatic=False)
    tracked["names"] = None  # The miner hasn't listed its streamers yet.
    recorder.request_import()
    recorder.poll_once()
    assert recorder.backfill.status()["requested"] is True

    tracked["names"] = ["alice"]
    restarted = recorder_for(analytics, tracked, automatic=False)
    restarted.poll_once()
    assert totals(restarted.store)["gained"] == 5
    assert restarted.backfill.status()["requested"] is False


def test_a_streamer_added_after_a_requested_import_waits_for_the_next(
    analytics, tracked
):
    analytics.write("alice", [entry(T0 - 300, 100), entry(T0 - 200, 105)])
    analytics.write("bob", [entry(T0 - 300, 10), entry(T0 - 200, 30)])
    analytics.record("run_start")
    recorder = recorder_for(analytics, tracked, automatic=False)
    recorder.request_import()
    recorder.poll_once()
    assert recorder.backfill.status()["complete"] is True

    tracked["names"] = ["alice", "bob"]
    recorder.poll_once()
    assert totals(recorder.store, "bob")["gained"] == 0
    status = recorder.backfill.status()
    assert status["complete"] is False
    assert status["streamers_waiting"] == 1

    recorder.request_import()
    recorder.poll_once()
    assert totals(recorder.store, "bob")["gained"] == 20
    assert recorder.backfill.status()["complete"] is True


def test_a_request_with_the_automatic_import_on_is_cleared(analytics, tracked):
    analytics.write("alice", [entry(T0 - 300, 100), entry(T0 - 200, 105)])
    analytics.record("run_start")
    recorder = recorder_for(analytics, tracked)
    recorder.request_import()
    recorder.poll_once()
    status = recorder.backfill.status()
    assert status["complete"] is True
    assert status["requested"] is False


def test_a_request_wakes_the_history_thread(analytics, tracked):
    recorder = recorder_for(analytics, tracked, automatic=False)
    assert not recorder._wake.is_set()
    recorder.request_import()
    assert recorder._wake.is_set()


def test_the_import_routes(analytics, tracked):
    from flask import Flask

    from TwitchChannelPointsMiner.history import web as history_web

    analytics.write("alice", [entry(T0 - 300, 100), entry(T0 - 200, 105)])
    analytics.record("run_start")
    recorder = recorder_for(analytics, tracked, automatic=False)
    recorder.poll_once()
    app = Flask(__name__)
    history_web.register_history(app)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(history_web, "get_store", lambda: recorder.store)
        patch.setattr(history_web, "import_status", recorder.backfill.status)
        patch.setattr(history_web, "request_import", recorder.request_import)
        client = app.test_client()
        before = client.get("/history/import").get_json()
        started = client.post("/history/import").get_json()
        recorder.poll_once()
        after = client.get("/history/import").get_json()

    assert (before["complete"], before["requested"]) == (False, False)
    assert (started["complete"], started["requested"]) == (False, True)
    assert (after["complete"], after["requested"]) == (True, False)


def test_the_import_routes_return_503_without_history(monkeypatch):
    from flask import Flask

    from TwitchChannelPointsMiner.history import web as history_web

    monkeypatch.setattr(history_web, "get_store", lambda: None)
    app = Flask(__name__)
    history_web.register_history(app)
    client = app.test_client()
    assert client.get("/history/import").status_code == 503
    assert client.post("/history/import").status_code == 503


def test_the_overview_says_when_imported_history_ends(analytics, tracked):
    analytics.record("run_start")
    recorder = recorder_for(analytics, tracked)
    recorder.poll_once()
    assert recorder.store.overview(now=T0)["imported_before"] is None

    analytics.write("alice", [entry(T0 - 300, 100)])
    tracked["names"] = ["alice", "bob"]
    analytics.write("bob", [entry(T0 - 300, 100), entry(T0 - 200, 105)])
    recorder.poll_once()
    assert recorder.store.overview(now=T0)["imported_before"] == T0


def test_health_reports_the_import(analytics, tracked):
    from flask import Flask

    from TwitchChannelPointsMiner.history import web as history_web

    analytics.write("alice", [entry(T0 - 300, 100), entry(T0 - 200, 105)])
    analytics.record("run_start")
    recorder = recorder_for(analytics, tracked)
    recorder.poll_once()
    app = Flask(__name__)
    history_web.register_history(app)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(history_web, "get_store", lambda: recorder.store)
        patch.setattr(history_web, "import_status", recorder.backfill.status)
        body = app.test_client().get("/history/health").get_json()

    assert body["analytics_import"] == {
        "boundary": T0,
        "imported": True,
        "streamers_done": 1,
        "streamers_waiting": 0,
        "automatic": True,
        "requested": False,
        "complete": True,
    }


def test_the_reason_code_is_the_one_the_label_was_made_from():
    assert backfill_module.reason_code("Watch Streak") == "WATCH_STREAK"
    assert backfill_module.reason_code("Spent") == "SPENT"
    assert backfill_module.reason_code(None) is None


def test_the_miner_lists_the_streamers_history_records(monkeypatch):
    import importlib
    from types import SimpleNamespace

    miner_module = importlib.import_module(
        "TwitchChannelPointsMiner.TwitchChannelPointsMiner"
    )
    TwitchChannelPointsMiner = miner_module.TwitchChannelPointsMiner

    miner = object.__new__(TwitchChannelPointsMiner)
    alice = SimpleNamespace(username="alice")
    drops = SimpleNamespace(username="dropsonly")
    miner.streamers = [alice, drops]
    monkeypatch.setattr(
        miner_module.history_journal.JOURNAL,
        "eligible",
        lambda streamer: streamer is alice,
    )
    names = miner._TwitchChannelPointsMiner__history_streamer_names

    # Until run() has loaded the streamers, History waits.
    miner.history_streamers_loaded = False
    assert names() is None

    miner.history_streamers_loaded = True
    assert names() == ["alice"]


def test_history_waits_quietly_until_the_miner_lists_its_streamers(monkeypatch):
    import TwitchChannelPointsMiner.history as history

    loaded = []
    monkeypatch.setattr(
        history, "_streamer_source", lambda: list(loaded) if loaded else None
    )

    # The miner's source returns None until the streamers load.
    assert history.history_streamers() is None

    loaded.append("alice")
    assert history.history_streamers() == ["alice"]


def test_entries_in_the_journals_first_second_are_left_to_the_journal(
    analytics, tracked
):
    # The analytics data keeps whole seconds; the journal does not.
    analytics.write(
        "alice",
        [entry(T0 - 1, 100), entry(T0, 150)],
        [annotation(T0, "#ff4545", "In the same second")],
    )
    analytics.record("run_start", at=T0 + 0.4)
    recorder = recorder_for(analytics, tracked)
    recorder.poll_once()

    assert [row["ts"] for row in points(recorder.store)] == [T0 - 1]
    assert events(recorder.store) == []
    assert totals(recorder.store)["gained"] == 0


@pytest.mark.parametrize(
    "unreadable",
    [None, ["x"], "entry", 7],
    ids=["null", "list", "string", "number"],
)
def test_an_entry_that_is_not_an_object_restarts_the_change(
    analytics, tracked, unreadable
):
    analytics.write(
        "alice",
        [entry(T0 - 300, 100), unreadable, entry(T0 - 100, 500), entry(T0 - 50, 510)],
    )
    analytics.record("run_start")
    recorder = recorder_for(analytics, tracked)
    recorder.poll_once()

    assert [row["delta"] for row in points(recorder.store)] == [None, None, 10]
    assert totals(recorder.store)["gained"] == 10


def test_retention_runs_when_the_last_poll_adds_nothing(analytics, tracked):
    analytics.write(
        "alice",
        [entry(T0 - 900 * DAY, 100), entry(T0 - 800 * DAY, 150)],
        # Not imported: the streak's series entry counts it.
        [annotation(T0 - 800 * DAY, "#45c1ff", "+50 - WATCH_STREAK")],
    )
    analytics.record("run_start")
    mono = {"mono": 0.0}
    recorder = recorder_for(analytics, tracked, batch_size=2, mono=mono)
    real_step = recorder.backfill._step

    def step(*args):
        result = real_step(*args)
        mono["mono"] += 5.0
        return result

    recorder.backfill._step = step
    recorder.retention_days = 730
    # Retention ran just before the import began.
    recorder._last_retention = mono["mono"]

    recorder.poll_once()
    assert len(points(recorder.store)) == 2

    # This poll only finishes the annotations; it adds no row.
    recorder.poll_once()

    assert recorder.backfill.status()["streamers_waiting"] == 0
    assert points(recorder.store) == []
    assert totals(recorder.store)["gained"] == 50


def test_a_journal_starting_with_a_watch_starts_the_boundary_at_that_record(
    analytics, tracked
):
    # A rebuild from journal files whose first record is a minute watched:
    # its session starts a minute before the record, but no record is there.
    analytics.write(
        "alice", [entry(T0 - 100, 100), entry(T0 - 30, 110), entry(T0 + 10, 120)]
    )
    analytics.record("watch", at=T0, streamer="alice", seconds=60)
    recorder = recorder_for(analytics, tracked)
    recorder.poll_once()

    assert recorder.backfill.status()["boundary"] == T0
    assert [row["ts"] for row in points(recorder.store)] == [T0 - 100, T0 - 30]
    assert totals(recorder.store)["gained"] == 10


def test_a_legacy_database_counts_the_daily_totals_retention_kept(analytics, tracked):
    analytics.write(
        "alice",
        [entry(T0 - 3 * DAY - 100, 100), entry(T0 - 2 * DAY, 150)],
    )
    analytics.record("run_start", at=T0 - 3 * DAY)
    recorder = recorder_for(analytics, tracked)
    recorder.backfill = None
    recorder.poll_once()
    # A database from before journal_first_at was kept, whose raw rows
    # retention has pruned: only the daily totals of those days remain.
    with closing(sqlite3.connect(recorder.store.path)) as connection, connection:
        connection.execute("DELETE FROM meta")
        connection.execute("DELETE FROM runs")
        connection.execute(
            "INSERT INTO daily_stats (day, streamer, points_gained, first_seen, "
            "last_seen) VALUES (?, 'alice', 50, ?, ?)",
            (utc_day(T0 - 2 * DAY), T0 - 2 * DAY, T0 - 2 * DAY),
        )

    analytics.record("run_start", at=T0, run=2)
    recorder.backfill = recorder_for(analytics, tracked).backfill
    recorder.poll_once()

    # The journal counted the second entry's day already.
    assert recorder.backfill.status()["boundary"] == T0 - 2 * DAY
    assert [row["ts"] for row in points(recorder.store)] == [T0 - 3 * DAY - 100]
    assert totals(recorder.store)["gained"] == 50


def test_a_rebuild_with_a_later_boundary_reads_the_file_again(analytics, tracked):
    analytics.write(
        "alice", [entry(T0 - 1000 + index, 100 + index) for index in range(4)]
    )
    analytics.record("run_start", at=T0)
    mono = {"mono": 0.0}
    recorder = recorder_for(analytics, tracked, batch_size=2, mono=mono)
    real_step = recorder.backfill._step

    def step(*args):
        result = real_step(*args)
        mono["mono"] += 5.0
        return result

    recorder.backfill._step = step
    recorder.poll_once()
    assert len(points(recorder.store)) == 2

    # The miner adds an entry; the first run's journal file is deleted (the
    # size limit), and the database is rebuilt from the next run's.
    analytics.record("run_start", at=T0 + 100, run=2)
    analytics.write(
        "alice",
        [entry(T0 - 1000 + index, 100 + index) for index in range(4)]
        + [entry(T0 + 50, 200)],
    )
    os.remove(os.path.join(analytics.journal, segment_name(1, 1)))
    assert recorder.store.recover_if_corrupt(
        sqlite3.DatabaseError("database disk image is malformed")
    )
    for _ in range(5):
        recorder.poll_once()

    assert recorder.backfill.status()["boundary"] == T0 + 100
    assert [row["ts"] for row in points(recorder.store)][-1] == T0 + 50
    assert len(points(recorder.store)) == 5
