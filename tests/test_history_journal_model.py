"""Randomized model test of the History journal and importer.

A real Journal writes to a temporary folder and a real importer reads it into
a real SQLite database, driven by a seeded random sequence of writes, imports,
injected database errors, clock jumps, rotation and pruning, unreadable
segments, partial lines, deletes, retention, History restarts and miner
restarts.  After every import the database totals are compared with a model
computed from the lines actually written (design section 10):

1. watch seconds = the sum of ``watch`` seconds;
2. points gained = the sum of ``points`` deltas, each ``msg_key`` once;
   points spent = the sum of ``spend`` amounts, each ``spend_key`` once;
3. streaks = distinct ``msg_key`` of ``WATCH_STREAK`` points records;
4. stream sessions = distinct broadcasts among ``live`` records;

each over the records imported so far, without a deleted streamer's records
from before its delete.  5 (idempotence) is exercised by the injected errors
and restarts, 6 (strict prefix) by comparing against everything before the
cursor.

``HISTORY_MODEL_SEEDS`` sets the number of seeds (default 20; run a few
hundred after changing the journal or the importer).
"""

import builtins
import json
import os
import random
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from TwitchChannelPointsMiner.history import importer as importer_module
from TwitchChannelPointsMiner.history import journal as journal_module
from TwitchChannelPointsMiner.history.importer import JournalImporter
from TwitchChannelPointsMiner.history.journal import Journal, list_segments
from TwitchChannelPointsMiner.history.recorder import HistoryRecorder
from TwitchChannelPointsMiner.history.store import HistoryStore

SEEDS = int(os.environ.get("HISTORY_MODEL_SEEDS", "20"))
STEPS = 260
STREAMERS = ("alice", "bob", "carol")
DAY = 24 * 60 * 60


def _streamer(name):
    return SimpleNamespace(
        username=name,
        is_online=False,
        explicitly_configured=True,
        from_followers=False,
        from_category=False,
        from_badge_campaign=False,
        from_wildcard_category=False,
        stream=SimpleNamespace(broadcast_id=None, game={}, title=None),
    )


class _Model(object):
    """Every complete line written, by position, read before each import."""

    def __init__(self, folder):
        self.folder = folder
        self.read_offsets = {}
        self.records = []  # (segment, start, record)
        self.deleted = {}  # streamer -> (segment, offset)
        self.skipped = {}  # segment -> offset from which it was skipped

    def read(self):
        for name in list_segments(self.folder):
            offset = self.read_offsets.get(name, 0)
            try:
                with open(os.path.join(self.folder, name), "rb") as handle:
                    handle.seek(offset)
                    data = handle.read()
            except OSError:
                continue
            position = 0
            while True:
                newline = data.find(b"\n", position)
                if newline < 0:
                    break
                raw = data[position:newline]
                try:
                    record = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    record = None
                if isinstance(record, dict) and "seq" in record:
                    self.records.append((name, offset + position, record))
                position = newline + 1
            self.read_offsets[name] = offset + position

    def applied(self, cursor):
        if cursor is None:
            return []
        result = []
        for segment, start, record in self.records:
            if (segment, start) >= (cursor["segment"], cursor["offset"]):
                continue
            if segment in self.skipped and start >= self.skipped[segment]:
                continue
            streamer = record.get("streamer")
            cutoff = self.deleted.get(streamer)
            if cutoff is not None and (segment, start) < cutoff:
                continue
            result.append(record)
        return result

    def expected(self, cursor):
        totals = {
            name: {
                "watch_seconds": 0.0,
                "points_gained": 0,
                "points_spent": 0,
                "streak_count": 0,
                "stream_sessions": 0,
            }
            for name in STREAMERS
        }
        msg_keys, spend_keys, streaks, broadcasts = set(), set(), set(), set()
        for record in self.applied(cursor):
            name = record.get("streamer")
            kind = record["kind"]
            if kind == "watch":
                totals[name]["watch_seconds"] += record["seconds"]
            elif kind == "points":
                key = record["msg_key"]
                if key is not None:
                    if key in msg_keys:
                        continue
                    msg_keys.add(key)
                if record["delta"] is not None:
                    totals[name]["points_gained"] += record["delta"]
                if record["reason_code"] == "WATCH_STREAK":
                    streaks.add(key)
                    totals[name]["streak_count"] += 1
            elif kind == "spend":
                if record["spend_key"] in spend_keys:
                    continue
                spend_keys.add(record["spend_key"])
                totals[name]["points_spent"] += record["amount"]
            elif kind == "live":
                key = (name, record["broadcast_id"])
                if key not in broadcasts:
                    broadcasts.add(key)
                    totals[name]["stream_sessions"] += 1
        return totals


def _actual(store):
    totals = {
        name: {
            "watch_seconds": 0.0,
            "points_gained": 0,
            "points_spent": 0,
            "streak_count": 0,
            "stream_sessions": 0,
        }
        for name in STREAMERS
    }
    connection = sqlite3.connect(store.path)
    connection.row_factory = sqlite3.Row
    try:
        for row in connection.execute("""
            SELECT streamer, SUM(watch_seconds) AS watch_seconds,
                   SUM(points_gained) AS points_gained,
                   SUM(points_spent) AS points_spent,
                   SUM(streak_count) AS streak_count,
                   SUM(stream_sessions) AS stream_sessions
            FROM daily_stats GROUP BY streamer
            """):
            totals[row["streamer"]] = {
                "watch_seconds": float(row["watch_seconds"]),
                "points_gained": int(row["points_gained"]),
                "points_spent": int(row["points_spent"]),
                "streak_count": int(row["streak_count"]),
                "stream_sessions": int(row["stream_sessions"]),
            }
        cursor = connection.execute(
            "SELECT segment, offset FROM journal_cursor WHERE id = 1"
        ).fetchone()
    finally:
        connection.close()
    return totals, (dict(cursor) if cursor is not None else None)


def _rounded(totals):
    return {
        name: {key: round(value, 3) for key, value in values.items()}
        for name, values in totals.items()
    }


class _Scenario(object):
    def __init__(self, seed, tmp_path, monkeypatch):
        self.rng = random.Random(seed)
        self.seed = seed
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        self.folder = str(tmp_path / "history")
        self.wall = [1_800_000_000.0]
        self.mono = [1000.0]
        self.streamers = {name: _streamer(name) for name in STREAMERS}
        self.journal = None
        self.model = _Model(self.folder)
        self.store = HistoryStore(str(tmp_path / "history.sqlite3"))
        self.store.initialize()
        self.importer = self._importer()
        self.unreadable = set()
        self.fail_after = None
        self.messages = {}  # run -> [(streamer, type, data)]
        self.redemptions = {}  # run -> [(streamer, data)]
        self.counter = 0
        self.watch_selected = set()
        self._start_miner()

    # -- clocks and components -------------------------------------------
    def _importer(self):
        return JournalImporter(
            self.store, self.folder, monotonic=lambda: self.mono[0], batch_size=7
        )

    def _start_miner(self):
        self.journal = Journal(
            clock=lambda: self.wall[0], monotonic=lambda: self.mono[0]
        )
        assert self.journal.start(self.folder, version="test")
        self.messages.setdefault(self.journal.run, [])
        self.redemptions.setdefault(self.journal.run, [])
        self.watch_selected = set()
        for streamer in self.streamers.values():
            if streamer.is_online:
                self.journal.record_live(streamer)

    def tick(self, seconds):
        self.wall[0] += seconds
        self.mono[0] += seconds

    def unique(self):
        self.counter += 1
        return self.counter

    # -- writes ----------------------------------------------------------
    def watch_cycle(self):
        online = [s for s in self.streamers.values() if s.is_online]
        chosen = self.rng.sample(online, min(len(online), self.rng.randint(0, 2)))
        self.journal.watch_selection(
            {s.username: (s, self.rng.choice(("points", "streak"))) for s in chosen}
        )
        for streamer in chosen:
            self.tick(self.rng.choice((5, 10, 20, 20, 30, 200)))
            self.journal.watch_attempt(streamer, self.rng.random() < 0.8)

    def points(self):
        streamer = self.rng.choice(list(self.streamers.values()))
        earned = self.rng.random() < 0.85
        data = {
            "timestamp": f"2026-11-23T07:{self.unique() % 60:02d}:{self.unique()}Z",
            "balance": {"balance": self.rng.randint(0, 10000)},
        }
        if self.rng.random() < 0.05:
            del data["timestamp"]
        message_type = "points-earned" if earned else "points-spent"
        if earned:
            reason = self.rng.choice(("WATCH", "WATCH", "CLAIM", "WATCH_STREAK"))
            data["point_gain"] = {
                "total_points": self.rng.choice((10, 50, 350)),
                "reason_code": reason,
            }
        self.journal.record_points_message(streamer, message_type, data)
        self.messages[self.journal.run].append((streamer, message_type, data))

    def replay(self):
        run = self.journal.run
        pool = list(self.messages.get(run, ())) + list(self.messages.get(run - 1, ()))
        if pool:
            streamer, message_type, data = self.rng.choice(pool)
            self.journal.record_points_message(streamer, message_type, data)
        pool = list(self.redemptions.get(run, ())) + list(
            self.redemptions.get(run - 1, ())
        )
        if pool:
            streamer, data = self.rng.choice(pool)
            self.journal.record_redemption(streamer, data)

    def spend(self):
        streamer = self.rng.choice(list(self.streamers.values()))
        if self.rng.random() < 0.6:
            data = {
                "timestamp": "2026-11-23T08:00:00Z",
                "redemption": {
                    "id": f"r{self.unique()}",
                    "reward": {"cost": self.rng.choice((100, 1000)), "title": "T"},
                },
            }
            self.journal.record_redemption(streamer, data)
            self.redemptions[self.journal.run].append((streamer, data))
        else:
            source = self.rng.choice(("prediction", "goal"))
            self.journal.record_spend(
                streamer, source, self.rng.randint(10, 500), f"{source}:{self.unique()}"
            )

    def lifecycle(self):
        streamer = self.rng.choice(list(self.streamers.values()))
        if streamer.is_online and self.rng.random() < 0.5:
            streamer.is_online = False
            self.journal.record_offline(streamer)
        else:
            if not streamer.is_online or self.rng.random() < 0.3:
                streamer.stream.broadcast_id = self.rng.choice(
                    (streamer.stream.broadcast_id or "b0", f"b{self.unique()}")
                )
            streamer.stream.title = self.rng.choice(("a", "b"))
            streamer.is_online = True
            self.journal.record_live(streamer)

    def event(self):
        streamer = self.rng.choice(list(self.streamers.values()))
        self.journal.record_event(streamer, "WIN", "label", 10)

    def burst(self):
        """Several threads write at once (distinct streamers)."""

        barrier = threading.Barrier(len(STREAMERS))
        # Unique per burst: a message repeated two runs later is counted
        # again by design, which the model does not mimic.
        burst = self.unique()

        def write(streamer, index):
            barrier.wait(timeout=5)
            for number in range(15):
                self.journal.record_points_message(
                    streamer,
                    "points-earned",
                    {
                        "timestamp": f"burst-{self.seed}-{burst}-{index}-{number}",
                        "balance": {"balance": number},
                        "point_gain": {"total_points": 10, "reason_code": "WATCH"},
                    },
                )

        threads = [
            threading.Thread(target=write, args=(streamer, index))
            for index, streamer in enumerate(self.streamers.values())
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            assert not thread.is_alive()

    def partial_line(self):
        """A write that failed halfway; the writer ends the line on reopen."""

        with self.journal._lock:
            path = self.journal._path_locked()
            self.journal._close_locked()
        with open(path, "ab") as handle:
            handle.write(b'{"v":1,"run":')

    # -- restarts and faults -----------------------------------------------
    def miner_restart(self, crash):
        if crash:
            with self.journal._lock:
                path = self.journal._path_locked()
                self.journal._close_locked()
                self.journal._run = None
            if self.rng.random() < 0.5:
                with open(path, "ab") as handle:
                    handle.write(b'{"v":1,"run":99,"seq"')
        else:
            self.journal.stop()
        for streamer in self.streamers.values():
            if self.rng.random() < 0.3:
                streamer.is_online = False
        self.tick(self.rng.choice((1, 60, 3600)))
        self._start_miner()

    def history_restart(self):
        self.store = HistoryStore(self.store.path)
        self.store.initialize()
        self.importer = self._importer()

    def unreadable_segment(self):
        names = list_segments(self.folder)
        if names:
            self.unreadable.add(self.rng.choice(names))

    def heal(self):
        self.unreadable.clear()

    def clock_jump(self):
        self.wall[0] += self.rng.choice((-2 * DAY, -3600, -30, 30, 3600, DAY + 7))

    def delete(self):
        name = self.rng.choice(STREAMERS)
        self.model.read()
        recorder = HistoryRecorder(self.store, self.folder, journal=self.journal)
        recorder.importer = self.importer
        before = _actual(self.store)[1]
        with self._faults():
            try:
                recorder.delete_streamer(name)
            except sqlite3.OperationalError:
                return
        self._note_skips(before, _actual(self.store)[1])
        position = self.journal.position()
        previous = self.model.deleted.get(name)
        self.model.deleted[name] = max(position, previous or position)
        self._check_after_import()

    def retention(self):
        self.store.apply_retention(now=self.wall[0], retention_days=1)

    # -- importing ---------------------------------------------------------
    def _faults(self):
        scenario = self
        real_open = builtins.open
        real_apply = JournalImporter._apply_line

        def fake_open(path, *args, **kwargs):
            if os.path.basename(str(path)) in scenario.unreadable:
                raise OSError("unreadable for the test")
            return real_open(path, *args, **kwargs)

        calls = [0]

        def failing_apply(self, *args, **kwargs):
            calls[0] += 1
            if scenario.fail_after is not None and calls[0] > scenario.fail_after:
                raise sqlite3.OperationalError("database is locked (test)")
            return real_apply(self, *args, **kwargs)

        class _Context(object):
            def __enter__(self_inner):
                scenario.monkeypatch.setattr(
                    importer_module, "open", fake_open, raising=False
                )
                scenario.monkeypatch.setattr(
                    JournalImporter, "_apply_line", failing_apply
                )

            def __exit__(self_inner, *exc):
                scenario.monkeypatch.setattr(JournalImporter, "_apply_line", real_apply)
                scenario.monkeypatch.delattr(importer_module, "open", raising=False)
                return False

        return _Context()

    def poll(self, faults=True):
        self.model.read()
        self.fail_after = None
        if faults and self.rng.random() < 0.25:
            self.fail_after = self.rng.randint(0, 20)
        before = _actual(self.store)[1]
        if faults and self.rng.random() < 0.3:
            self.mono[0] += importer_module.JOURNAL_STALL_SECONDS + 1
        with self._faults():
            try:
                self.importer.poll()
            except sqlite3.OperationalError:
                pass
        self.fail_after = None
        after = _actual(self.store)[1]
        self._note_skips(before, after)
        self._check_after_import()

    def _note_skips(self, before, after):
        """Record the part of an unreadable segment the importer skipped."""

        if after is None:
            return
        if before is None:
            # No cursor yet: the importer starts at the oldest segment.
            names = sorted(name for name, _start, _record in self.model.records)
            if not names:
                return
            before = {"segment": names[0], "offset": 0}
        if (
            before["segment"] in self.unreadable
            and after["segment"] > before["segment"]
            and before["segment"] not in self.model.skipped
        ):
            self.model.skipped[before["segment"]] = before["offset"]

    def _check_after_import(self):
        actual, cursor = _actual(self.store)
        expected = self.model.expected(cursor)
        assert _rounded(actual) == _rounded(
            expected
        ), f"seed {self.seed}: totals differ from the model at cursor {cursor}"

    def finish(self):
        self.heal()
        self.journal.stop()
        self.poll(faults=False)
        _actual_totals, cursor = _actual(self.store)
        names = list_segments(self.folder)
        assert cursor["segment"] == names[-1]
        assert cursor["offset"] == os.path.getsize(os.path.join(self.folder, names[-1]))

    def step(self):
        action = self.rng.choices(
            (
                self.watch_cycle,
                self.points,
                self.replay,
                self.spend,
                self.lifecycle,
                self.event,
                self.poll,
                self.clock_jump,
                self.burst,
                self.partial_line,
                lambda: self.miner_restart(crash=False),
                lambda: self.miner_restart(crash=True),
                self.history_restart,
                self.delete,
                self.retention,
                self.unreadable_segment,
                self.heal,
            ),
            weights=(25, 20, 6, 6, 10, 3, 15, 3, 1, 1, 2, 2, 2, 2, 1, 1, 2),
        )[0]
        action()


@pytest.mark.parametrize("seed", range(SEEDS))
def test_history_matches_the_model(seed, tmp_path, monkeypatch):
    monkeypatch.setattr(journal_module, "JOURNAL_SEGMENT_BYTES", 1500)
    monkeypatch.setattr(journal_module, "JOURNAL_MAX_BYTES", 10**12)
    scenario = _Scenario(seed, tmp_path, monkeypatch)
    for _ in range(STEPS):
        scenario.step()
    scenario.finish()
