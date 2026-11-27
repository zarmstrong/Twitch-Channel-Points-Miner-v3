"""History's importer: journal records in, SQLite totals out."""

import json
import os
import sqlite3
import threading
from contextlib import closing

import pytest

from TwitchChannelPointsMiner.history import importer as importer_module
from TwitchChannelPointsMiner.history import journal as journal_module
from TwitchChannelPointsMiner.history.importer import (
    JOURNAL_STALL_SECONDS,
    JournalImporter,
)
from TwitchChannelPointsMiner.history.journal import ACK_FILE, segment_name
from TwitchChannelPointsMiner.history.recorder import HistoryRecorder
from TwitchChannelPointsMiner.history.store import HistoryStore, utc_day

T0 = 1_800_000_000.0


class Writer(object):
    """Writes journal lines by hand, so each case controls every field."""

    def __init__(self, folder):
        self.folder = folder
        os.makedirs(folder, exist_ok=True)
        self.seq = {}

    def path(self, run, segment=1):
        return os.path.join(self.folder, segment_name(run, segment))

    def raw(self, data, run=1, segment=1):
        with open(self.path(run, segment), "ab") as handle:
            handle.write(data)

    def add(self, kind, run=1, segment=1, at=T0, seq=None, **fields):
        if seq is None:
            seq = self.seq.get(run, 0) + 1
        self.seq[run] = max(seq, self.seq.get(run, 0))
        record = {"v": 1, "run": run, "seq": seq, "at": at, "kind": kind}
        record.update(fields)
        self.raw((json.dumps(record) + "\n").encode(), run, segment)


@pytest.fixture
def folder(tmp_path):
    return str(tmp_path / "history")


@pytest.fixture
def writer(folder):
    return Writer(folder)


@pytest.fixture
def store(tmp_path):
    instance = HistoryStore(str(tmp_path / "history.sqlite3"))
    instance.initialize()
    return instance


@pytest.fixture
def clock():
    return {"mono": 0.0}


@pytest.fixture
def importer(store, folder, clock):
    return JournalImporter(store, folder, monotonic=lambda: clock["mono"])


def totals(store, streamer="alice"):
    with closing(sqlite3.connect(store.path)) as connection, connection:
        row = connection.execute(
            """
            SELECT COALESCE(SUM(watch_seconds), 0), COALESCE(SUM(watch_sessions), 0),
                   COALESCE(SUM(stream_sessions), 0), COALESCE(SUM(points_gained), 0),
                   COALESCE(SUM(points_spent), 0), COALESCE(SUM(streak_count), 0)
            FROM daily_stats WHERE streamer = ?
            """,
            (streamer,),
        ).fetchone()
    keys = (
        "watch_seconds",
        "watch_sessions",
        "stream_sessions",
        "points_gained",
        "points_spent",
        "streak_count",
    )
    return dict(zip(keys, row))


def rows(store, sql, *params):
    connection = sqlite3.connect(store.path)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(sql, params)]
    finally:
        connection.close()


def watch(writer, at, seconds=20, reason="points", broadcast="b1", run=1, **kw):
    writer.add(
        "watch",
        run=run,
        at=at,
        streamer="alice",
        broadcast_id=broadcast,
        reason=reason,
        seconds=seconds,
        **kw,
    )


# -- watch sessions --------------------------------------------------------
def test_watch_records_close_together_form_one_session(writer, importer, store):
    writer.add("run_start")
    for offset in (20, 40, 60):
        watch(writer, T0 + offset)
    importer.poll()
    sessions = rows(store, "SELECT * FROM watch_sessions")
    assert len(sessions) == 1
    assert sessions[0]["started_at"] == T0
    assert sessions[0]["watch_seconds"] == 60
    assert totals(store)["watch_seconds"] == 60
    assert totals(store)["watch_sessions"] == 1


def test_a_long_gap_or_another_reason_starts_a_new_session(writer, importer, store):
    writer.add("run_start")
    watch(writer, T0 + 20)
    watch(writer, T0 + 400)
    watch(writer, T0 + 420, reason="streak")
    importer.poll()
    sessions = rows(store, "SELECT * FROM watch_sessions ORDER BY id")
    assert [session["watch_seconds"] for session in sessions] == [20, 20, 20]
    assert sessions[0]["ended_at"] == T0 + 20
    assert totals(store)["watch_sessions"] == 3


def test_a_clock_set_back_extends_the_session(writer, importer, store):
    writer.add("run_start")
    watch(writer, T0 + 20)
    watch(writer, T0 - 3600)
    importer.poll()
    assert len(rows(store, "SELECT * FROM watch_sessions")) == 1
    assert totals(store)["watch_seconds"] == 40


def test_watch_seconds_count_on_the_day_of_the_record(writer, importer, store):
    midnight = 1_800_057_600.0  # 00:00 UTC
    writer.add("run_start", at=midnight - 100)
    watch(writer, midnight - 10)
    watch(writer, midnight + 10)
    importer.poll()
    days = rows(store, "SELECT day, watch_seconds FROM daily_stats ORDER BY day")
    assert [(day["day"], day["watch_seconds"]) for day in days] == [
        (utc_day(midnight - 10), 20),
        (utc_day(midnight + 10), 20),
    ]


def test_a_session_is_active_only_while_its_records_continue(writer, importer, store):
    writer.add("run_start")
    watch(writer, T0 + 20)
    importer.poll()
    assert store.overview(now=T0 + 30)["active_watch_sessions"] == 1
    assert store.overview(now=T0 + 20 + 181)["active_watch_sessions"] == 0


def test_watch_end_and_run_end_close_the_session(writer, importer, store):
    writer.add("run_start")
    watch(writer, T0 + 20)
    writer.add("watch_end", at=T0 + 30, streamer="alice")
    watch(writer, T0 + 50)
    writer.add("run_end", at=T0 + 60)
    importer.poll()
    sessions = rows(store, "SELECT * FROM watch_sessions ORDER BY id")
    assert [(s["started_at"], s["ended_at"]) for s in sessions] == [
        (T0, T0 + 20),
        (T0 + 30, T0 + 50),
    ]


# -- streams -----------------------------------------------------------------
def test_a_broadcast_seen_live_again_is_one_stream_session(writer, importer, store):
    writer.add("run_start")
    writer.add("live", at=T0, streamer="alice", broadcast_id="b1", game="G")
    writer.add("offline", at=T0 + 60, streamer="alice", broadcast_id="b1")
    writer.add("live", at=T0 + 120, streamer="alice", broadcast_id="b1", game="G")
    importer.poll()
    (stream,) = rows(store, "SELECT * FROM streams")
    assert stream["ended_at"] is None
    assert stream["started_at"] == T0
    assert totals(store)["stream_sessions"] == 1


def test_a_new_broadcast_closes_the_previous_one(writer, importer, store):
    writer.add("run_start")
    writer.add("live", at=T0, streamer="alice", broadcast_id="b1")
    writer.add("live", at=T0 + 600, streamer="alice", broadcast_id="b2")
    importer.poll()
    streams = rows(store, "SELECT * FROM streams ORDER BY id")
    assert streams[0]["ended_at"] == T0 + 600
    assert streams[0]["end_reason"] == "replaced"
    assert streams[1]["ended_at"] is None
    assert totals(store)["stream_sessions"] == 2


def test_the_next_run_closes_what_a_crashed_run_left_open(writer, importer, store):
    writer.add("run_start", run=1)
    writer.add("live", run=1, at=T0, streamer="alice", broadcast_id="b1")
    watch(writer, T0 + 20, run=1)
    writer.add("event", run=1, at=T0 + 500, streamer="bob", type="win")
    writer.add("run_start", run=2, at=T0 + 9000)
    importer.poll()
    (stream,) = rows(store, "SELECT * FROM streams")
    assert stream["ended_at"] == T0 + 500
    assert stream["end_reason"] == "crash"
    (session,) = rows(store, "SELECT * FROM watch_sessions")
    assert session["ended_at"] == T0 + 20
    (run,) = rows(store, "SELECT * FROM runs WHERE id = 1")
    assert run["ended_at"] == T0 + 500


def test_a_crashed_runs_stream_is_not_shown_live_once_its_records_stop(
    writer, importer, store
):
    writer.add("run_start", run=1)
    writer.add("live", run=1, at=T0, streamer="alice", broadcast_id="b1")
    importer.poll()
    assert store.overview(now=T0 + 60)["live_streams"] == 1
    assert store.overview(now=T0 + 3600)["live_streams"] == 0
    store.active_run = 1  # the miner in this process is still writing run 1
    assert store.overview(now=T0 + 3600)["live_streams"] == 1


def test_an_unchanged_live_stream_counts_on_todays_heatmap(writer, importer, store):
    """Its newest record is from yesterday, but the stream is still live."""

    writer.add("run_start")
    writer.add("live", at=T0, streamer="alice", broadcast_id="b1")
    importer.poll()
    store.active_run = 1
    now = T0 + 2 * 86400 + 600
    (item,) = store.activity(days=1, now=now)["streamers"]
    (day,) = item["days"].values()
    assert day["live_seconds"] == now - (now - now % 86400)


def test_a_streak_shows_on_the_live_stream_it_was_claimed_on(writer, importer, store):
    writer.add("run_start")
    writer.add("live", at=T0, streamer="alice", broadcast_id="b1")
    writer.add(
        "points",
        at=T0 + 300,
        streamer="alice",
        broadcast_id="b1",
        balance=500,
        delta=350,
        reason_code="WATCH_STREAK",
        msg_key="k1",
        upstream_at=T0 + 299,
    )
    importer.poll()
    store.active_run = 1
    (item,) = store.streamers(now=T0 + 400)
    assert item["live_streak_at"] == T0 + 299
    assert item["streak_count"] == 1


# -- points and spends ---------------------------------------------------------
def points(writer, key, delta=10, run=1, at=T0, upstream_at=None, reason="WATCH"):
    writer.add(
        "points",
        run=run,
        at=at,
        streamer="alice",
        broadcast_id="b1",
        balance=100,
        delta=delta,
        reason_code=reason,
        msg_key=key,
        upstream_at=upstream_at,
    )


def test_a_points_message_counts_once_per_key(writer, importer, store):
    writer.add("run_start", run=1)
    points(writer, "k1", run=1)
    writer.add("run_start", run=2)
    points(writer, "k1", run=2)
    points(writer, None, run=2)
    points(writer, None, run=2)
    importer.poll()
    assert totals(store)["points_gained"] == 30


def test_keys_older_than_the_previous_run_are_pruned(writer, importer, store):
    """Accepted limitation: a replay two runs later counts again."""

    for run in (1, 2, 3):
        writer.add("run_start", run=run)
        points(writer, "k1", run=run)
    importer.poll()
    assert totals(store)["points_gained"] == 20
    assert rows(store, "SELECT run FROM points_keys") == [{"run": 3}]


def test_points_count_on_twitchs_day(writer, importer, store):
    writer.add("run_start")
    points(writer, "k1", at=T0, upstream_at=T0 - 2 * 86400)
    importer.poll()
    (day,) = rows(store, "SELECT day FROM daily_stats WHERE points_gained > 0")
    assert day["day"] == utc_day(T0 - 2 * 86400)


def test_a_spent_balance_counts_nothing(writer, importer, store):
    writer.add("run_start")
    points(writer, "k1", delta=None, reason=None)
    importer.poll()
    assert totals(store)["points_gained"] == 0
    assert totals(store)["points_spent"] == 0


def test_a_spend_counts_once_per_key(writer, importer, store):
    writer.add("run_start", run=1)
    for run in (1, 1):
        writer.add(
            "spend",
            run=run,
            streamer="alice",
            source="redemption",
            amount=1000,
            spend_key="redemption:r1",
            title="T",
        )
    writer.add("run_start", run=2)
    writer.add(
        "spend",
        run=2,
        streamer="alice",
        source="redemption",
        amount=1000,
        spend_key="redemption:r1",
    )
    importer.poll()
    assert totals(store)["points_spent"] == 1000
    (row,) = rows(store, "SELECT delta, kind, reason, label FROM points")
    assert row == {
        "delta": -1000,
        "kind": "spend",
        "reason": "redemption",
        "label": "T",
    }


# -- reading -------------------------------------------------------------------
def test_a_partial_last_line_waits_for_the_writer(writer, importer, store):
    writer.add("run_start")
    line = json.dumps(
        {
            "v": 1,
            "run": 1,
            "seq": 2,
            "at": T0,
            "kind": "watch",
            "streamer": "alice",
            "broadcast_id": "b1",
            "reason": "points",
            "seconds": 20,
        }
    ).encode()
    writer.raw(line[:30])
    importer.poll()
    assert totals(store)["watch_seconds"] == 0
    writer.raw(line[30:] + b"\n")
    importer.poll()
    assert totals(store)["watch_seconds"] == 20
    assert store.journal_status()["damaged_lines"] == 0


def test_a_partial_line_in_a_closed_segment_is_damaged(writer, importer, store):
    writer.add("run_start", run=1)
    writer.raw(b'{"v":1,"run":1,"seq', run=1)
    writer.add("run_start", run=2)
    watch(writer, T0 + 20, run=2)
    importer.poll()
    assert totals(store)["watch_seconds"] == 20
    assert store.journal_status()["damaged_lines"] == 1


def test_invalid_lines_are_skipped_and_gaps_counted(writer, importer, store):
    writer.add("run_start")
    writer.raw(b"not json\n")
    watch(writer, T0 + 20, seq=5)
    importer.poll()
    status = store.journal_status()
    assert status["damaged_lines"] == 1
    assert status["gaps"] == 3
    assert totals(store)["watch_seconds"] == 20


BAD_DAY = T0 + 12345


@pytest.mark.parametrize(
    "fields",
    [
        {"seq": 1e999},  # json reads it as infinity
        {"run": 2**80},
        {"upstream_at": BAD_DAY},  # a time the platform cannot convert
    ],
)
def test_a_line_that_cannot_be_applied_is_damaged_not_blocking(
    writer, importer, store, monkeypatch, fields
):
    real_utc_day = importer_module.utc_day

    def utc_day(timestamp):
        if timestamp == BAD_DAY:
            raise OSError("[Errno 22] Invalid argument")
        return real_utc_day(timestamp)

    monkeypatch.setattr(importer_module, "utc_day", utc_day)
    writer.add("run_start")
    record = {
        "v": 1,
        "run": 1,
        "seq": 2,
        "at": T0,
        "kind": "points",
        "streamer": "alice",
        "balance": 1,
        "delta": 10,
        "reason_code": "WATCH",
        "msg_key": "bad",
        "upstream_at": T0,
    }
    record.update(fields)
    writer.raw((json.dumps(record) + "\n").encode())
    writer.seq[1] = 2
    points(writer, "good", delta=7)
    importer.poll()
    importer.poll()
    assert totals(store)["points_gained"] == 7
    assert store.journal_status()["damaged_lines"] == 1
    assert rows(store, "SELECT COUNT(*) AS n FROM points")[0]["n"] == 1


def test_an_unreadable_segment_stops_the_import_until_the_stall_limit(
    writer, importer, store, clock, monkeypatch
):
    writer.add("run_start", run=1)
    watch(writer, T0 + 20, run=1)
    writer.add("run_start", run=2)
    watch(writer, T0 + 40, run=2)
    real_open = open

    def fake_open(path, *args, **kwargs):
        if os.path.basename(str(path)) == segment_name(1, 1):
            raise OSError("locked")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(importer_module, "open", fake_open, raising=False)
    importer.poll()
    assert totals(store)["watch_seconds"] == 0
    clock["mono"] += JOURNAL_STALL_SECONDS - 1
    importer.poll()
    assert totals(store)["watch_seconds"] == 0
    clock["mono"] += 2
    importer.poll()
    importer.poll()
    assert totals(store)["watch_seconds"] == 20
    assert store.journal_status()["skipped_segments"] == 1


def test_a_segment_deleted_before_it_was_imported_is_reported(writer, importer, store):
    writer.add("run_start", run=1)
    writer.add("run_start", run=2)
    importer.poll()
    writer.add("run_start", run=3)
    watch(writer, T0 + 20, run=3)
    # The cursor is on run 2's segment; the size limit deletes it.
    os.remove(writer.path(2))
    importer.poll()
    assert totals(store)["watch_seconds"] == 20
    assert store.journal_status()["missing_segments"] == 1


def test_runs_deleted_before_history_first_imported_are_reported(
    writer, importer, store
):
    """History never started while three short runs filled the journal; the
    size limit deleted runs 1 and 2 whole."""

    writer.add("run_start", run=3)
    watch(writer, T0 + 20, run=3)
    importer.poll()
    importer.poll()
    assert totals(store)["watch_seconds"] == 20
    assert store.journal_status()["missing_segments"] == 2


def test_segments_after_an_acknowledged_one_are_reported_on_a_rebuild(
    writer, importer, store, folder
):
    """A rebuilt database starts from the oldest file left: what History
    acknowledged was deleted normally, anything later by the size limit."""

    with open(os.path.join(folder, ACK_FILE), "w") as handle:
        handle.write(segment_name(1, 2) + "\n")
    writer.add("run_start", run=1, segment=4, seq=50)
    importer.poll()
    assert store.journal_status()["missing_segments"] == 1


def test_a_journal_starting_at_its_first_file_reports_nothing(writer, importer, store):
    writer.add("run_start", run=1)
    writer.add("run_start", run=2)
    importer.poll()
    importer.poll()
    assert store.journal_status()["missing_segments"] == 0


def test_whole_runs_deleted_ahead_of_the_cursor_are_reported(writer, importer, store):
    writer.add("run_start", run=1)
    writer.add("run_start", run=2)
    importer.poll()
    # Runs 3 and 4 are written and deleted unread, with run 2's file.
    writer.add("run_start", run=5)
    os.remove(writer.path(2))
    importer.poll()
    importer.poll()
    assert store.journal_status()["missing_segments"] == 3


def test_a_file_deleted_between_two_left_is_reported(writer, importer, store):
    writer.add("run_start", run=1)
    importer.poll()
    writer.add("run_start", run=2)
    writer.add("run_start", run=4)
    importer.poll()
    importer.poll()
    assert store.journal_status()["missing_segments"] == 1


def test_the_acknowledgement_names_only_fully_imported_segments(
    writer, importer, folder
):
    writer.add("run_start", run=1)
    writer.add("run_start", run=2)
    importer.poll()
    with open(os.path.join(folder, ACK_FILE)) as handle:
        assert handle.read().strip() == segment_name(1, 1)


# -- idempotence -----------------------------------------------------------------
def test_a_database_error_mid_batch_repeats_the_batch(
    writer, store, folder, monkeypatch
):
    writer.add("run_start")
    for offset in range(1, 30):
        watch(writer, T0 + 20 * offset)
    importer = JournalImporter(store, folder, batch_size=10)
    real_apply = JournalImporter._apply_line
    calls = [0]

    def flaky(self, *args, **kwargs):
        calls[0] += 1
        if calls[0] == 15:
            raise sqlite3.OperationalError("database is locked")
        return real_apply(self, *args, **kwargs)

    monkeypatch.setattr(JournalImporter, "_apply_line", flaky)
    with pytest.raises(sqlite3.OperationalError):
        importer.poll()
    assert totals(store)["watch_seconds"] == 9 * 20
    importer.poll()
    assert totals(store)["watch_seconds"] == 29 * 20


def test_a_lost_cursor_does_not_count_anything_twice(writer, importer, store):
    writer.add("run_start")
    watch(writer, T0 + 20)
    points(writer, None)
    importer.poll()
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute("DELETE FROM journal_cursor")
    JournalImporter(store, importer.folder).poll()
    assert totals(store)["watch_seconds"] == 20
    assert totals(store)["points_gained"] == 10


# -- deleting ----------------------------------------------------------------------
def test_records_written_before_a_delete_stay_deleted(writer, importer, store):
    writer.add("run_start")
    watch(writer, T0 + 20)
    importer.poll()
    watch(writer, T0 + 40)  # written but not imported yet
    HistoryRecorder(store, importer.folder).delete_streamer("alice")
    assert totals(store)["watch_seconds"] == 0
    # A later record counts, even with an earlier clock.
    watch(writer, T0 - 86400)
    points(writer, "k-after")
    importer.poll()
    assert totals(store)["watch_seconds"] == 20
    assert totals(store)["points_gained"] == 10


def test_a_delete_lets_a_key_seen_before_it_count_again(writer, importer, store):
    writer.add("run_start")
    points(writer, "k1")
    importer.poll()
    HistoryRecorder(store, importer.folder).delete_streamer("alice")
    points(writer, "k1")
    importer.poll()
    assert totals(store)["points_gained"] == 10


def test_a_delete_waits_for_records_behind_an_unreadable_segment(
    writer, importer, store, monkeypatch
):
    writer.add("run_start", run=1)
    watch(writer, T0 + 20, run=1)
    writer.add("run_start", run=2)
    real_open = open
    blocked = {segment_name(1, 1)}

    def fake_open(path, *args, **kwargs):
        if os.path.basename(str(path)) in blocked:
            raise OSError("locked")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(importer_module, "open", fake_open, raising=False)
    HistoryRecorder(store, importer.folder).delete_streamer("alice")
    blocked.clear()
    importer.poll()
    assert totals(store)["watch_seconds"] == 0


def test_a_rebuilt_database_still_skips_what_was_deleted(
    writer, importer, store, tmp_path
):
    """The cutoff is saved beside the journal, which outlives the database."""

    writer.add("run_start")
    watch(writer, T0 + 20)
    importer.poll()
    HistoryRecorder(store, importer.folder).delete_streamer("alice")
    watch(writer, T0 + 40)
    rebuilt = HistoryStore(str(tmp_path / "rebuilt.sqlite3"))
    rebuilt.initialize()
    JournalImporter(rebuilt, importer.folder).poll()
    assert totals(rebuilt)["watch_seconds"] == 20


def test_an_unreadable_delete_file_stops_the_import(
    writer, importer, store, monkeypatch
):
    writer.add("run_start")
    watch(writer, T0 + 20)
    real_open = open

    def fake_open(path, *args, **kwargs):
        if os.path.basename(str(path)) == "deleted":
            raise PermissionError("locked")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(journal_module, "open", fake_open, raising=False)
    with pytest.raises(OSError):
        importer.poll()
    assert totals(store)["watch_seconds"] == 0


def test_a_delete_whose_cutoff_cannot_be_saved_changes_nothing(
    writer, importer, store, monkeypatch
):
    writer.add("run_start")
    watch(writer, T0 + 20)
    importer.poll()
    recorder = HistoryRecorder(store, importer.folder)

    def refuse(*_args):
        raise OSError("read-only")

    monkeypatch.setattr(recorder.importer, "save_delete", refuse)
    with pytest.raises(OSError):
        recorder.delete_streamer("alice")
    assert totals(store)["watch_seconds"] == 20


def test_a_failed_rebuild_leaves_history_retrying(writer, store, monkeypatch):
    writer.add("run_start")
    watch(writer, T0 + 20)
    recorder = HistoryRecorder(store, writer.folder)

    def corrupt():
        raise sqlite3.DatabaseError("database disk image is malformed")

    def cannot_rebuild(_error):
        raise PermissionError("the backup cannot be written")

    with monkeypatch.context() as patch:
        patch.setattr(recorder.importer, "poll", corrupt)
        patch.setattr(store, "recover_if_corrupt", cannot_rebuild)
        assert recorder.poll_once() is False
        assert store.ready is False
    assert recorder.poll_once() is True
    assert totals(store)["watch_seconds"] == 20


def test_a_delete_with_an_unlistable_journal_changes_nothing(
    writer, importer, store, monkeypatch
):
    writer.add("run_start")
    watch(writer, T0 + 20)
    importer.poll()
    watch(writer, T0 + 40)

    def unlistable(_folder):
        raise PermissionError("denied")

    with monkeypatch.context() as patch:
        patch.setattr(journal_module.os, "listdir", unlistable)
        with pytest.raises(OSError):
            HistoryRecorder(store, importer.folder).delete_streamer("alice")
    assert totals(store)["watch_seconds"] == 20
    assert not os.path.exists(os.path.join(importer.folder, "deleted"))


def test_a_deleted_live_streamer_is_recorded_live_again(tmp_path):
    from types import SimpleNamespace

    from TwitchChannelPointsMiner.history.journal import Journal

    folder = str(tmp_path / "history")
    journal = Journal()
    journal.start(folder)
    alice = SimpleNamespace(
        username="alice",
        is_online=True,
        explicitly_configured=True,
        from_followers=False,
        from_category=False,
        from_badge_campaign=False,
        from_wildcard_category=False,
        stream=SimpleNamespace(broadcast_id="b1", game={}, title="Title"),
    )
    journal.record_live(alice)
    store = HistoryStore(str(tmp_path / "history.sqlite3"))
    store.initialize()
    recorder = HistoryRecorder(store, folder, journal=journal)
    recorder.poll_once()
    recorder.delete_streamer("alice")
    journal.record_live(alice)  # the same broadcast, title and game
    recorder.poll_once()
    assert store.overview()["live_streams"] == 1
    journal.stop()


def test_a_delete_before_history_has_a_database_still_holds(
    writer, tmp_path, monkeypatch
):
    """History failed to start, but the journal already has records."""

    import TwitchChannelPointsMiner.history as history

    monkeypatch.setattr(history, "_recorder", None)
    writer.add("run_start")
    watch(writer, T0 + 20)
    removed = []
    database = str(tmp_path / "history.sqlite3")
    assert (
        history.delete_streamer_history(
            "alice", database_path=database, remove_source=lambda: removed.append(1)
        )
        is None
    )
    assert removed == [1]
    assert not os.path.exists(database)
    watch(writer, T0 + 40)
    store = HistoryStore(database)
    store.initialize()
    JournalImporter(store, writer.folder).poll()
    assert totals(store)["watch_seconds"] == 20


def test_a_cleared_title_and_game_are_cleared(writer, importer, store):
    writer.add("run_start")
    writer.add("live", streamer="alice", broadcast_id="b1", game="G", title="T")
    writer.add("live", streamer="alice", broadcast_id="b1", game=None, title=None)
    importer.poll()
    (stream,) = rows(store, "SELECT title, game FROM streams")
    assert stream == {"title": None, "game": None}


# -- retention -----------------------------------------------------------------------
def test_retention_keeps_totals_and_never_recounts_a_broadcast(writer, importer, store):
    writer.add("run_start")
    writer.add("live", at=T0, streamer="alice", broadcast_id="b1")
    points(writer, "k1", upstream_at=T0)
    writer.add("offline", at=T0 + 60, streamer="alice", broadcast_id="b1")
    importer.poll()
    store.apply_retention(now=T0 + 10 * 86400, retention_days=1)
    assert rows(store, "SELECT * FROM streams") == []
    assert rows(store, "SELECT * FROM points") == []
    writer.add("live", at=T0 + 11 * 86400, streamer="alice", broadcast_id="b1")
    importer.poll()
    assert totals(store)["stream_sessions"] == 1
    assert totals(store)["points_gained"] == 10


def test_a_database_from_the_snapshot_recorder_is_rebuilt(tmp_path):
    path = tmp_path / "history.sqlite3"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE streams (id INTEGER)")
        connection.execute("PRAGMA user_version=3")
    store = HistoryStore(str(path))
    store.initialize()
    assert store.ready
    assert any(name.startswith("history.sqlite3.bak-") for name in os.listdir(tmp_path))
    with closing(sqlite3.connect(path)) as connection, connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4


def test_a_watch_answered_during_a_delete_keeps_no_time_from_before_it(tmp_path):
    """The request was sent before the delete and is answered while the
    cutoff is being saved: none of its time is kept."""

    from types import SimpleNamespace

    from TwitchChannelPointsMiner.history.journal import Journal

    clock = {"mono": 0.0}
    folder = str(tmp_path / "history")
    journal = Journal(monotonic=lambda: clock["mono"])
    journal.start(folder)
    alice = SimpleNamespace(
        username="alice",
        is_online=True,
        explicitly_configured=True,
        from_followers=False,
        from_category=False,
        from_badge_campaign=False,
        from_wildcard_category=False,
        stream=SimpleNamespace(broadcast_id="b1", game={}, title="Title"),
    )
    journal.record_live(alice)
    journal.watch_selection({"alice": (alice, "points")})
    token = journal.watch_token(alice)
    clock["mono"] = 30.0
    store = HistoryStore(str(tmp_path / "history.sqlite3"))
    store.initialize()
    recorder = HistoryRecorder(store, folder, journal=journal)
    recorder.poll_once()
    real_save = recorder.importer.save_delete

    answered = []

    def save_delete(*args):
        real_save(*args)
        clock["mono"] = 31.0
        # Twitch answers on the watcher's thread while the cutoff is saved.
        thread = threading.Thread(
            target=journal.watch_attempt,
            args=(alice, True),
            kwargs={"broadcast_id": "b1", "token": token},
            daemon=True,
        )
        thread.start()
        thread.join(0.2)
        answered.append(thread)

    recorder.importer.save_delete = save_delete
    recorder.delete_streamer("alice")
    answered[0].join(5)
    assert not answered[0].is_alive()
    recorder.poll_once()
    assert totals(store)["watch_seconds"] == 0
    journal.stop()


def test_a_failed_delete_keeps_the_watch_going(tmp_path):
    from types import SimpleNamespace

    from TwitchChannelPointsMiner.history.journal import Journal

    clock = {"mono": 0.0}
    folder = str(tmp_path / "history")
    journal = Journal(monotonic=lambda: clock["mono"])
    journal.start(folder)
    alice = SimpleNamespace(
        username="alice",
        is_online=True,
        explicitly_configured=True,
        from_followers=False,
        from_category=False,
        from_badge_campaign=False,
        from_wildcard_category=False,
        stream=SimpleNamespace(broadcast_id="b1", game={}, title="Title"),
    )
    journal.record_live(alice)
    journal.watch_selection({"alice": (alice, "points")})
    token = journal.watch_token(alice)
    clock["mono"] = 30.0
    store = HistoryStore(str(tmp_path / "history.sqlite3"))
    store.initialize()
    recorder = HistoryRecorder(store, folder, journal=journal)
    recorder.poll_once()

    def unwritable(*args):
        raise OSError("read-only")

    recorder.importer.save_delete = unwritable
    with pytest.raises(OSError):
        recorder.delete_streamer("alice")
    journal.watch_attempt(alice, True, broadcast_id="b1", token=token)
    recorder.poll_once()
    assert totals(store)["watch_seconds"] == 30
    journal.stop()


def test_a_delete_blocked_by_a_database_lock_changes_nothing(tmp_path, monkeypatch):
    """Another process holds the database's write lock: the delete fails
    before its cutoff is saved or the watch is ended, so a retry is the
    only thing that deletes anything."""

    from types import SimpleNamespace

    from TwitchChannelPointsMiner.history import store as store_module
    from TwitchChannelPointsMiner.history.journal import Journal, read_delete_cutoffs

    clock = {"mono": 0.0}
    folder = str(tmp_path / "history")
    journal = Journal(monotonic=lambda: clock["mono"])
    journal.start(folder)
    alice = SimpleNamespace(
        username="alice",
        is_online=True,
        explicitly_configured=True,
        from_followers=False,
        from_category=False,
        from_badge_campaign=False,
        from_wildcard_category=False,
        stream=SimpleNamespace(broadcast_id="b1", game={}, title="Title"),
    )
    journal.record_live(alice)
    journal.watch_selection({"alice": (alice, "points")})
    token = journal.watch_token(alice)
    clock["mono"] = 30.0
    store = HistoryStore(str(tmp_path / "history.sqlite3"))
    store.initialize()
    recorder = HistoryRecorder(store, folder, journal=journal)
    recorder.poll_once()

    class QuickConnection(sqlite3.Connection):
        # Give up on the lock at once instead of after five seconds.
        def execute(self, sql, *args):
            if sql.startswith("PRAGMA busy_timeout"):
                sql = "PRAGMA busy_timeout=50"
            return super().execute(sql, *args)

    real_connect = sqlite3.connect

    def quick_connect(*args, **kwargs):
        kwargs["timeout"] = 0.05
        kwargs["factory"] = QuickConnection
        return real_connect(*args, **kwargs)

    holder = sqlite3.connect(store.path)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(store_module.sqlite3, "connect", quick_connect)
            with pytest.raises(sqlite3.OperationalError):
                recorder.delete_streamer("alice")
    finally:
        holder.rollback()
        holder.close()

    assert read_delete_cutoffs(folder) == {}
    # The watch went on: the request answered after the failed delete
    # credits its 30 seconds, and the live session is still there.
    journal.watch_attempt(alice, True, broadcast_id="b1", token=token)
    recorder.poll_once()
    assert totals(store)["watch_seconds"] == 30
    assert totals(store)["stream_sessions"] == 1
    journal.stop()


def _watching_alice(tmp_path):
    from types import SimpleNamespace

    from TwitchChannelPointsMiner.history.journal import Journal

    clock = {"mono": 0.0}
    folder = str(tmp_path / "history")
    journal = Journal(monotonic=lambda: clock["mono"])
    journal.start(folder)
    alice = SimpleNamespace(
        username="alice",
        is_online=True,
        explicitly_configured=True,
        from_followers=False,
        from_category=False,
        from_badge_campaign=False,
        from_wildcard_category=False,
        stream=SimpleNamespace(broadcast_id="b1", game={}, title="Title"),
    )
    journal.record_live(alice)
    journal.watch_selection({"alice": (alice, "points")})
    token = journal.watch_token(alice)
    clock["mono"] = 30.0
    store = HistoryStore(str(tmp_path / "history.sqlite3"))
    store.initialize()
    recorder = HistoryRecorder(store, folder, journal=journal)
    recorder.poll_once()
    return SimpleNamespace(
        journal=journal,
        alice=alice,
        token=token,
        clock=clock,
        folder=folder,
        store=store,
        recorder=recorder,
    )


def test_a_delete_whose_rows_cannot_be_deleted_changes_nothing(tmp_path, monkeypatch):
    from TwitchChannelPointsMiner.history.journal import read_delete_cutoffs

    setup = _watching_alice(tmp_path)

    def failing(*_args, **_kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(setup.store, "_delete_streamer_rows", failing)
    with pytest.raises(sqlite3.OperationalError):
        setup.recorder.delete_streamer("alice")
    monkeypatch.undo()

    assert read_delete_cutoffs(setup.folder) == {}
    setup.journal.watch_attempt(setup.alice, True, broadcast_id="b1", token=setup.token)
    setup.recorder.poll_once()
    assert totals(setup.store)["watch_seconds"] == 30
    assert totals(setup.store)["stream_sessions"] == 1
    setup.journal.stop()


def test_a_delete_whose_commit_fails_is_finished_by_the_next_import(
    tmp_path, monkeypatch
):
    """The cutoff was saved (the delete happened) but the database did not
    commit: History deletes the rows before it imports anything else."""

    from TwitchChannelPointsMiner.history import store as store_module
    from TwitchChannelPointsMiner.history.journal import read_delete_cutoffs

    setup = _watching_alice(tmp_path)
    setup.journal.watch_attempt(setup.alice, True, broadcast_id="b1", token=setup.token)
    setup.recorder.poll_once()
    assert totals(setup.store)["watch_seconds"] == 30
    failing = {"commit": False}

    class FailingCommit(sqlite3.Connection):
        def commit(self):
            if failing["commit"]:
                failing["commit"] = False
                raise sqlite3.OperationalError("database or disk is full")
            return super().commit()

    real_connect = sqlite3.connect

    def connect(*args, **kwargs):
        kwargs["factory"] = FailingCommit
        return real_connect(*args, **kwargs)

    real_save = setup.recorder.importer.save_delete

    def save_then_fail_commit(*args):
        real_save(*args)
        failing["commit"] = True

    monkeypatch.setattr(store_module.sqlite3, "connect", connect)
    monkeypatch.setattr(setup.recorder.importer, "save_delete", save_then_fail_commit)
    with pytest.raises(sqlite3.OperationalError):
        setup.recorder.delete_streamer("alice")
    monkeypatch.undo()

    assert "alice" in read_delete_cutoffs(setup.folder)
    assert totals(setup.store)["watch_seconds"] == 30  # not committed yet
    assert setup.recorder.poll_once() is True
    assert totals(setup.store)["watch_seconds"] == 0
    assert totals(setup.store)["stream_sessions"] == 0
    # The same broadcast, title and game: alice is recorded live again.
    setup.journal.record_live(setup.alice)
    setup.recorder.poll_once()
    assert totals(setup.store)["stream_sessions"] == 1
    setup.journal.stop()


def test_cutoffs_saved_before_finishing_existed_never_delete_later_activity(
    writer, importer, store
):
    """A database rebuilt by an earlier build holds activity after a delete
    without the delete's row; its cutoff line (no version) only skips."""

    writer.add("run_start")
    watch(writer, T0 + 20)
    cutoff = os.path.getsize(writer.path(1))
    with open(os.path.join(writer.folder, journal_module.DELETES_FILE), "a") as f:
        f.write(
            json.dumps(
                {
                    "streamer": "alice",
                    "segment": segment_name(1, 1),
                    "offset": cutoff,
                    "at": T0 + 30,
                }
            )
            + "\n"
        )
    watch(writer, T0 + 40)
    # The earlier build imported without recording the cutoff in the database.
    importer.apply_saved_deletes = lambda: None
    importer.poll()
    assert totals(store)["watch_seconds"] == 20
    del importer.apply_saved_deletes

    importer.poll()
    assert totals(store)["watch_seconds"] == 20


def test_a_delete_whose_cutoff_was_written_but_not_synced_fails_and_keeps_later_activity(
    writer, importer, store, monkeypatch
):
    """The cutoff line reached the file but the fsync raised: the delete
    fails and changes nothing, and once activity after the cutoff has been
    imported, a restart that finds the line keeps that activity."""

    writer.add("run_start")
    watch(writer, T0 + 20)
    importer.poll()
    recorder = HistoryRecorder(store, importer.folder)

    def failing_fsync(_fd):
        raise OSError("fsync failed")

    with monkeypatch.context() as patch:
        patch.setattr(journal_module.os, "fsync", failing_fsync)
        with pytest.raises(OSError):
            recorder.delete_streamer("alice")
    assert totals(store)["watch_seconds"] == 20

    watch(writer, T0 + 40)
    recorder.poll_once()
    assert totals(store)["watch_seconds"] == 40
    # A restart reads the line: the activity after it stays.
    HistoryRecorder(store, importer.folder).poll_once()
    assert totals(store)["watch_seconds"] == 40


def test_a_saved_cutoff_the_import_has_passed_keeps_later_activity(
    writer, importer, store
):
    writer.add("run_start")
    watch(writer, T0 + 20)
    cutoff = os.path.getsize(writer.path(1))
    with open(os.path.join(writer.folder, journal_module.DELETES_FILE), "a") as f:
        f.write(
            json.dumps(
                {
                    "v": journal_module.CUTOFF_VERSION,
                    "streamer": "alice",
                    "segment": segment_name(1, 1),
                    "offset": cutoff,
                    "at": T0 + 30,
                }
            )
            + "\n"
        )
    watch(writer, T0 + 40)
    # Imported past the cutoff without the database recording it.
    importer.apply_saved_deletes = lambda: None
    importer.poll()
    del importer.apply_saved_deletes
    assert totals(store)["watch_seconds"] == 20

    importer.poll()
    assert totals(store)["watch_seconds"] == 20


def test_the_drill_down_lists_only_the_newest_100_points(writer, importer, store):
    writer.add("run_start")
    for index in range(150):
        points(writer, f"k{index}", at=T0 + index)
    importer.poll()
    detail = store.streamer_detail("alice", days=30, now=T0 + 1000)
    assert [row["ts"] for row in detail["points"]] == [
        T0 + index for index in range(149, 49, -1)
    ]
    # The daily totals still count every points message.
    assert sum(day["points_gained"] for day in detail["daily"]) == 1500


def test_a_delete_holds_the_analytics_file_lock_only_to_remove_the_source(
    writer, importer, store
):
    from TwitchChannelPointsMiner.classes.Settings import ANALYTICS_FILE_MUTEX

    writer.add("run_start")
    watch(writer, T0 + 20)
    recorder = HistoryRecorder(store, importer.folder)
    held = []
    real_poll = recorder.importer.poll
    real_delete = store.delete_streamer

    def poll():
        held.append(("import", ANALYTICS_FILE_MUTEX.locked()))
        return real_poll()

    def delete(*args, **kwargs):
        held.append(("delete", ANALYTICS_FILE_MUTEX.locked()))
        return real_delete(*args, **kwargs)

    recorder.importer.poll = poll
    store.delete_streamer = delete
    recorder.delete_streamer(
        "alice",
        remove_source=lambda: held.append(("json", ANALYTICS_FILE_MUTEX.locked())),
    )
    recorder.delete_streamer(
        "", remove_source=lambda: held.append(("blank", ANALYTICS_FILE_MUTEX.locked()))
    )
    assert held == [
        ("import", False),
        ("delete", False),
        ("json", True),
        ("blank", True),
    ]
    assert totals(store)["watch_seconds"] == 0


def test_a_repeating_failure_logs_its_traceback_at_most_once_a_minute(
    store, folder, caplog
):
    import logging

    clock = {"mono": 0.0}
    recorder = HistoryRecorder(store, folder, monotonic=lambda: clock["mono"])

    def fail():
        raise OSError("read-only")

    recorder.importer.poll = fail
    caplog.set_level(logging.DEBUG, logger="TwitchChannelPointsMiner.history.recorder")
    for now in (0.0, 10.0, 59.0, 60.0, 70.0):
        clock["mono"] = now
        assert recorder.poll_once() is False
    records = [
        (record.levelno, record.exc_info is not None)
        for record in caplog.records
        if "Unable to import the History journal" in record.getMessage()
    ]
    assert records == [
        (logging.WARNING, True),
        (logging.DEBUG, False),
        (logging.DEBUG, False),
        (logging.WARNING, True),
        (logging.DEBUG, False),
    ]

    # Another failure has its own interval.
    store.ready = False
    store.initialize = fail
    caplog.clear()
    recorder.poll_once()
    assert [
        (record.levelno, record.exc_info is not None) for record in caplog.records
    ] == [(logging.WARNING, True)]
