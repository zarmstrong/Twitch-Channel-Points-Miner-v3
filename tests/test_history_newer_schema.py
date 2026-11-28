"""History and a database written by a newer version of the miner (#415):
left unchanged, History off for the run, the journal kept for the newer
version to import."""

import logging
import os
import shutil
import sqlite3
import time
from contextlib import closing
from types import SimpleNamespace

import pytest
from flask import Flask

import TwitchChannelPointsMiner.history as history
from TwitchChannelPointsMiner.history import HistoryUnavailableError
from TwitchChannelPointsMiner.history import store as store_module
from TwitchChannelPointsMiner.history import web as history_web
from TwitchChannelPointsMiner.history.journal import Journal, list_segments
from TwitchChannelPointsMiner.history.recorder import HistoryRecorder
from TwitchChannelPointsMiner.history.store import (
    HISTORY_SCHEMA_VERSION,
    HistorySchemaTooNewError,
    HistoryStore,
)

NEWER = HISTORY_SCHEMA_VERSION + 1
NOW = time.time()


def files(folder):
    """Every file in ``folder`` with its bytes."""

    found = {}
    for name in sorted(os.listdir(folder)):
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            with open(path, "rb") as handle:
                found[name] = handle.read()
    return found


def newer_database(path, journal_mode="wal"):
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(f"PRAGMA journal_mode={journal_mode}")
        connection.execute("CREATE TABLE future (value TEXT)")
        connection.execute("INSERT INTO future VALUES ('kept')")
        connection.execute(f"PRAGMA user_version={NEWER}")


@pytest.mark.parametrize("journal_mode", ["wal", "delete"])
def test_a_newer_database_is_left_unchanged(tmp_path, journal_mode):
    path = tmp_path / "history.sqlite3"
    newer_database(path, journal_mode)
    before = files(tmp_path)
    store = HistoryStore(str(path))
    with pytest.raises(HistorySchemaTooNewError) as error:
        store.initialize()
    assert error.value.version == NEWER
    assert store.ready is False
    # Byte for byte, with no backup and no -wal/-shm created beside it.
    assert files(tmp_path) == before


def test_a_newer_database_left_by_a_crash_is_left_unchanged(tmp_path):
    # The newer build crashed: its -wal holds the version change the file's
    # header doesn't show yet.
    live = tmp_path / "live"
    live.mkdir()
    writer = sqlite3.connect(live / "history.sqlite3")
    try:
        writer.execute("PRAGMA journal_mode=wal")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE future (value TEXT)")
        writer.commit()
        writer.execute(f"PRAGMA user_version={NEWER}")
        writer.commit()
        crashed = tmp_path / "crashed"
        crashed.mkdir()
        for name in ("history.sqlite3", "history.sqlite3-wal"):
            shutil.copy(live / name, crashed / name)
    finally:
        writer.close()
    with open(crashed / "history.sqlite3", "rb") as handle:
        assert int.from_bytes(handle.read(100)[60:64], "big") != NEWER
    before = files(crashed)
    store = HistoryStore(str(crashed / "history.sqlite3"))
    with pytest.raises(HistorySchemaTooNewError):
        store.initialize()
    after = files(crashed)
    # Only SQLite's -shm index may appear; the database and its -wal are
    # unchanged, and nothing is moved aside.
    assert {name: after[name] for name in before} == before
    assert set(after) - set(before) <= {"history.sqlite3-shm"}


def test_a_database_upgraded_after_the_check_is_left_unchanged(tmp_path, monkeypatch):
    # Another process upgrades it between the header check and the write
    # connection.
    path = tmp_path / "history.sqlite3"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("PRAGMA journal_mode=delete")
        connection.execute(f"PRAGMA user_version={HISTORY_SCHEMA_VERSION}")
    snapshot = {}

    def upgraded_meanwhile(self):
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute(f"PRAGMA user_version={NEWER}")
        snapshot.update(files(tmp_path))
        return HISTORY_SCHEMA_VERSION

    monkeypatch.setattr(HistoryStore, "_stored_version", upgraded_meanwhile)
    store = HistoryStore(str(path))
    with pytest.raises(HistorySchemaTooNewError):
        store.initialize()
    # Still in rollback-journal mode, byte for byte.
    assert files(tmp_path) == snapshot


def test_a_failed_check_opens_nothing_for_writing(tmp_path, monkeypatch):
    # The version may be in the -wal file, and the read-only connection that
    # would read it fails: nothing opens the file for writing.
    live = tmp_path / "live"
    live.mkdir()
    writer = sqlite3.connect(live / "history.sqlite3")
    try:
        writer.execute("PRAGMA journal_mode=wal")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE future (value TEXT)")
        writer.commit()
        writer.execute(f"PRAGMA user_version={NEWER}")
        writer.commit()
        crashed = tmp_path / "crashed"
        crashed.mkdir()
        for name in ("history.sqlite3", "history.sqlite3-wal"):
            shutil.copy(live / name, crashed / name)
    finally:
        writer.close()
    before = files(crashed)
    real_connect = sqlite3.connect
    writes = []

    def connect(database, *args, **kwargs):
        if kwargs.get("uri"):
            raise sqlite3.OperationalError("database is locked")
        writes.append(database)
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(store_module.sqlite3, "connect", connect)
    store = HistoryStore(str(crashed / "history.sqlite3"))
    with pytest.raises(sqlite3.OperationalError):
        store.initialize()
    assert writes == []
    assert store.ready is False
    assert files(crashed) == before


def test_an_older_database_is_still_rebuilt(tmp_path):
    path = tmp_path / "history.sqlite3"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("PRAGMA user_version=3")
    store = HistoryStore(str(path))
    store.initialize()
    assert store.ready
    assert any(name.startswith("history.sqlite3.bak-") for name in os.listdir(tmp_path))


def _alice():
    return SimpleNamespace(
        username="alice",
        is_online=True,
        explicitly_configured=True,
        from_followers=False,
        from_category=False,
        from_badge_campaign=False,
        from_wildcard_category=False,
        stream=SimpleNamespace(broadcast_id="b1", game={"name": "Game"}, title="T"),
    )


def earn(journal, clock, points):
    alice = _alice()
    clock["wall"] += 60
    clock["mono"] += 60
    journal.record_points_message(
        alice,
        "points-earned",
        {
            "timestamp": f"2026-11-28T07:{int(clock['mono']) // 60 % 60:02d}:00Z",
            "point_gain": {"total_points": points, "reason_code": "WATCH"},
            "balance": {"balance": 1000 + points},
        },
    )


def points_gained(path):
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(
            "SELECT COALESCE(SUM(points_gained), 0) FROM daily_stats"
        ).fetchone()[0]


@pytest.fixture
def recorded(tmp_path):
    """A database this build made and imported 40 points into, then marked
    as written by a newer build; the miner journals 25 more points while it
    runs this (older) build."""

    folder = str(tmp_path / "history")
    clock = {"wall": NOW, "mono": 0.0}
    journal = Journal(clock=lambda: clock["wall"], monotonic=lambda: clock["mono"])
    journal.start(folder)
    alice = _alice()
    journal.record_live(alice)
    earn(journal, clock, 40)
    path = str(tmp_path / "history.sqlite3")
    recorder = HistoryRecorder(
        HistoryStore(path), folder, journal=journal, monotonic=lambda: clock["mono"]
    )
    assert recorder.poll_once()
    assert points_gained(path) == 40
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(f"PRAGMA user_version={NEWER}")
    earn(journal, clock, 25)
    yield SimpleNamespace(
        path=path, folder=folder, journal=journal, clock=clock, tmp_path=tmp_path
    )
    journal.stop()


def test_history_stays_off_for_the_run_with_one_warning(recorded, caplog):
    database = {
        name: data
        for name, data in files(recorded.tmp_path).items()
        if name.startswith("history.sqlite3")
    }
    segments = files(recorded.folder)
    recorder = HistoryRecorder(
        HistoryStore(recorded.path),
        recorded.folder,
        journal=recorded.journal,
        monotonic=lambda: recorded.clock["mono"],
    )
    waits = []

    def wait(timeout=None):
        # A retry loop would wait here: stop it so the test fails, not hangs.
        waits.append(timeout)
        recorder._stop_event.set()

    recorder._stop_event.wait = wait
    with caplog.at_level(logging.WARNING):
        # The thread's loop: it ends at once instead of retrying.
        recorder.run()
        assert waits == []
        assert recorder.poll_once() is False
        assert recorder.drain() is False
        with pytest.raises(HistoryUnavailableError) as error:
            recorder.delete_streamer("alice")
    assert "newer version" in str(error.value)
    warnings = [
        record for record in caplog.records if record.levelno >= logging.WARNING
    ]
    assert len(warnings) == 1
    assert "newer version" in warnings[0].getMessage()
    assert recorder.refused is not None
    # Nothing imported, pruned or acknowledged: the database and the journal
    # are as they were.
    assert {
        name: data
        for name, data in files(recorded.tmp_path).items()
        if name.startswith("history.sqlite3")
    } == database
    assert files(recorded.folder) == segments


def test_starting_history_again_keeps_it_off(recorded, monkeypatch, caplog):
    built = []

    def build_recorder(analytics_path, **kwargs):
        recorder = HistoryRecorder(
            HistoryStore(recorded.path), recorded.folder, journal=recorded.journal
        )
        built.append(recorder)
        return recorder

    import TwitchChannelPointsMiner.history.recorder as recorder_module

    monkeypatch.setattr(recorder_module, "build_recorder", build_recorder)
    monkeypatch.setattr(history, "_analytics_path", lambda: str(recorded.tmp_path))
    monkeypatch.setattr(history, "_recorder", None)
    monkeypatch.setattr(history, "_atexit_registered", True)
    with caplog.at_level(logging.WARNING):
        first = history.start_history()
        first.join(timeout=10)
        assert not first.is_alive()
        assert history.start_history() is first
    assert len(built) == 1
    assert history.unavailable_reason() == first.refused
    assert (
        len(
            [
                record
                for record in caplog.records
                if "newer version" in record.getMessage()
            ]
        )
        == 1
    )


def test_the_newer_version_imports_what_the_older_one_journalled(recorded, monkeypatch):
    older = HistoryRecorder(
        HistoryStore(recorded.path), recorded.folder, journal=recorded.journal
    )
    assert older.poll_once() is False
    assert list_segments(recorded.folder)
    # Back on the newer build (same tables, a higher version).
    monkeypatch.setattr(store_module, "HISTORY_SCHEMA_VERSION", NEWER)
    newer = HistoryRecorder(
        HistoryStore(recorded.path), recorded.folder, journal=recorded.journal
    )
    assert newer.poll_once()
    assert points_gained(recorded.path) == 65


def test_the_history_tab_says_why_history_is_off(monkeypatch):
    monkeypatch.setattr(
        history,
        "_recorder",
        SimpleNamespace(refused="newer", store=SimpleNamespace(ready=False)),
    )
    app = Flask(__name__)
    history_web.register_history(app)
    response = app.test_client().get("/history/overview")
    assert response.status_code == 503
    message = response.get_json()["error"]
    assert "newer version" in message
    # Not the "still starting" answer the tab retries every few seconds.
    assert "not available" not in message.lower()
