"""Import the miner's journal into the History database.

Import is a strict prefix in write order: segments in name order, lines in
file order.  Each batch of records and the cursor move commit in one SQLite
transaction, so a crash or a database error repeats the same batch with the
same result.  ``journal_runs.last_seq`` is a second guard: a record at or
below its run's last applied ``seq`` is never applied again.

Nothing is kept in memory between polls except the stall timer for an
unreadable segment; everything else is in the database.
"""

import json
import logging
import math
import os
import sqlite3
import time

from TwitchChannelPointsMiner.constants import WATCH_STREAK_CLOCK_SKEW_SECONDS
from TwitchChannelPointsMiner.history.journal import (
    ACK_FILE,
    JOURNAL_FORMAT_VERSION,
    journal_end,
    list_segments,
    parse_segment_name,
    read_ack,
    read_delete_cutoffs,
    read_deleted_drops,
    save_delete_cutoff,
    save_deleted_drops,
    write_atomic,
)
from TwitchChannelPointsMiner.history.store import (
    META_DELETED_DROPS,
    SESSION_GAP_SECONDS,
    STREAM_GAPS_KEPT,
    clean_streamer,
    earliest_held,
    utc_day,
)

logger = logging.getLogger(__name__)


def _missing_between(before, after):
    """The fewest segment files that were written between ``before`` (a
    segment name, or ``None`` for the start of the journal) and ``after``
    and are no longer there.  Every run starts at segment 1 and writes at
    least one file, so a skipped run number is at least one lost file; how
    many more segments ``before``'s run had is unknown and not counted."""

    end = parse_segment_name(after)
    if end is None:
        return 0
    start = parse_segment_name(before) if before is not None else None
    if start is None:
        start = (0, 0)
        if before is not None:
            return 0
    if start[0] == end[0]:
        return max(0, end[1] - start[1] - 1)
    if end[0] < start[0]:
        return 0
    return (end[0] - start[0] - 1) + (end[1] - 1)


BATCH_SIZE = 500
# A segment that cannot be read for this long is skipped (reported).
JOURNAL_STALL_SECONDS = 60 * 60


# Larger numbers in a journal line are damage, not data.
_MAX_NUMBER = 1e15
_MAX_INT = 2**53


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    if not math.isfinite(number) or abs(number) > _MAX_NUMBER:
        return None
    return number


def _int(value):
    number = _number(value)
    if number is None or abs(number) > _MAX_INT:
        return None
    return int(number)


def _text(value):
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _offline_gaps(raw):
    """A stream's ``offline_gaps`` column as ``[(offline at, live at)]``."""

    try:
        gaps = json.loads(raw) if raw else []
    except ValueError:
        return []
    found = []
    for gap in gaps if isinstance(gaps, list) else ():
        try:
            offline_at, live_at = float(gap[0]), float(gap[1])
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if math.isfinite(offline_at) and math.isfinite(live_at):
            found.append((offline_at, live_at))
    return found


class _SaveFailed(Exception):
    """Deleted claims could not be saved beside the journal: the batch is
    imported again later, not passed over as damaged."""


class JournalImporter(object):
    def __init__(
        self,
        store,
        folder,
        monotonic=time.monotonic,
        batch_size=BATCH_SIZE,
        stall_seconds=JOURNAL_STALL_SECONDS,
    ):
        self.store = store
        self.folder = folder
        self.monotonic = monotonic
        self.batch_size = max(1, int(batch_size))
        self.stall_seconds = stall_seconds
        self._stall = None
        self._last_ack = None
        self._bad_line_logged = False
        # Delete cutoffs from DELETES_FILE (loaded when first needed): all
        # of them, and those apply_saved_deletes may finish.
        self._file_deletes = None
        self._finishable_deletes = None
        # The deleted claims saved there (see restore_deleted_drops).
        self._deleted_drops = None
        # The earliest record time of the batch being applied.
        self._first_at = None

    # -- delete cutoffs --------------------------------------------------------
    def _load_deletes(self):
        """The cutoffs saved beside the journal (see
        ``journal.read_delete_cutoffs``), read once."""

        if self._file_deletes is None:
            self._file_deletes = read_delete_cutoffs(self.folder)
        return self._file_deletes

    def save_delete(self, streamer, segment, offset, deleted_at, drops=()):
        """Save a delete cutoff beside the journal before the database
        applies it, with the claims (``drops``) it removes; raises
        ``OSError`` when it cannot be saved."""

        cutoffs = self._load_deletes()
        finishable = self._load_finishable_deletes()
        deleted_drops = self._load_deleted_drops()
        position = (str(segment), int(offset))
        # Raises when the line may not be durable (even if it was written):
        # the delete then fails.  Should the line have reached the file
        # anyway, a later import honours it only while it has not passed
        # the cutoff (``apply_saved_deletes``).
        save_delete_cutoff(
            self.folder, streamer, segment, offset, deleted_at, drops=drops
        )
        for known in (cutoffs, finishable):
            if streamer not in known or position > known[streamer]:
                known[streamer] = position
        deleted_drops.extend(tuple(drop) for drop in drops)

    def _load_deleted_drops(self):
        if self._deleted_drops is None:
            self._deleted_drops = read_deleted_drops(self.folder)
        return self._deleted_drops

    def restore_deleted_drops(self):
        """Put the deleted claims saved beside the journal back into a
        database that lacks them (rebuilt after corruption), so neither the
        journal nor the Drops data counts them again.  Raises ``OSError``
        when the file cannot be read."""

        drops = self._load_deleted_drops()
        if not drops:
            return
        with self.store._read_connection() as connection:
            row = connection.execute(
                "SELECT value FROM meta WHERE key = ?", (META_DELETED_DROPS,)
            ).fetchone()
        if row is not None and row["value"] == str(len(drops)):
            return
        with self.store.transaction() as connection:
            connection.executemany(
                """
                INSERT OR IGNORE INTO deleted_drop_keys (dedupe_key, drop_id,
                                                         game, ts, source)
                VALUES (?, ?, ?, ?, ?)
                """,
                drops,
            )
            # A claim is saved as deleted before the database commits its
            # removal; if that never committed, it is removed now.
            connection.executemany(
                "DELETE FROM drops WHERE dedupe_key = ?",
                [(drop[0],) for drop in drops],
            )
            connection.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                (META_DELETED_DROPS, str(len(drops))),
            )

    def _load_finishable_deletes(self):
        if self._finishable_deletes is None:
            self._finishable_deletes = read_delete_cutoffs(self.folder, finishable=True)
        return self._finishable_deletes

    # -- reading -------------------------------------------------------------
    def _cursor(self, connection):
        row = connection.execute(
            "SELECT segment, offset FROM journal_cursor WHERE id = 1"
        ).fetchone()
        return (row["segment"], row["offset"]) if row is not None else None

    def end_position(self):
        """``(newest segment, its size)`` as found on disk, or ``("", 0)``."""

        return journal_end(self.folder)

    def apply_saved_deletes(self):
        """Finish the deletes whose cutoff was saved but whose database
        change did not commit (see ``HistoryRecorder.delete_streamer``), and
        record the cutoffs a rebuilt database has not seen yet.

        Runs before anything is imported.  The streamer's rows are deleted
        only while the import has not passed the cutoff, so they are all from
        before it; if the database already holds records from after the
        cutoff (it was rebuilt by an earlier build, which did not record
        cutoffs, or the line of a delete that failed reached the file
        anyway), only the cutoff is recorded and the activity is kept.
        Cutoffs saved before this was done (without ``CUTOFF_VERSION``) are
        left alone."""

        self.restore_deleted_drops()
        cutoffs = self._load_finishable_deletes()
        if not cutoffs:
            return
        with self.store._read_connection() as connection:
            applied = {
                row["streamer"]: (row["segment"], row["offset"])
                for row in connection.execute(
                    "SELECT streamer, segment, offset FROM deleted_streamers"
                )
            }
            cursor = self._cursor(connection) or ("", 0)
        for streamer, position in cutoffs.items():
            if position <= applied.get(streamer, ("", 0)):
                continue
            if cursor <= position:
                self.store.delete_streamer(streamer, position[0], position[1])
            else:
                logger.warning(
                    "History already holds records after %s's saved delete; "
                    "recording the delete without removing them",
                    streamer,
                )
                self.store.record_delete(streamer, position[0], position[1])

    def poll(self):
        """Import everything complete; returns the number of lines read."""

        self.apply_saved_deletes()
        names = list_segments(self.folder)
        if not names:
            return 0
        with self.store._read_connection() as connection:
            cursor = self._cursor(connection)
        if cursor is None:
            # Nothing imported yet: files before the oldest one left, past
            # what History acknowledged (none if it never did), were deleted
            # by the size limit unread.
            missing = _missing_between(read_ack(self.folder), names[0])
            if missing:
                self._move_to(names[0], count="missing_segments", amount=missing)
            cursor = (names[0], 0)
        elif cursor[0] not in names:
            later = [name for name in names if name > cursor[0]]
            if not later:
                return 0
            # The segment under the cursor is gone: deleted by the size
            # limit before it was fully imported, like any between it and
            # the next one left.
            self._move_to(
                later[0],
                count="missing_segments",
                amount=1 + _missing_between(cursor[0], later[0]),
            )
            cursor = (later[0], 0)

        newest = names[-1]
        total = 0
        for name in names:
            if name < cursor[0]:
                continue
            offset = cursor[1] if name == cursor[0] else 0
            is_newest = name == newest
            try:
                with open(os.path.join(self.folder, name), "rb") as handle:
                    handle.seek(offset)
                    data = handle.read()
            except OSError as error:
                self._read_failed(name, is_newest, names, error)
                break
            if self._stall is not None and self._stall[0] == name:
                self._stall = None
            lines, consumed = self._split(data, offset)
            for start in range(0, len(lines), self.batch_size):
                batch = lines[start : start + self.batch_size]
                end = batch[-1][2]
                self._apply_batch(name, batch, end)
                total += len(batch)
            tail = len(data) - (consumed - offset)
            if is_newest:
                # A partial last line may still be completed by the writer.
                break
            damaged = 1 if tail > 0 else 0
            following = names[names.index(name) + 1]
            missing = _missing_between(name, following)
            self._move_to(
                following,
                damaged=damaged,
                count="missing_segments" if missing else None,
                amount=missing,
            )
            cursor = (following, 0)
        self._write_ack()
        return total

    @staticmethod
    def _split(data, offset):
        """Complete lines as ``(start offset, bytes, end offset)``."""

        lines = []
        position = 0
        while True:
            newline = data.find(b"\n", position)
            if newline < 0:
                break
            lines.append(
                (offset + position, data[position:newline], offset + newline + 1)
            )
            position = newline + 1
        return lines, offset + position

    def _read_failed(self, name, is_newest, names, error):
        now = self.monotonic()
        if self._stall is None or self._stall[0] != name:
            self._stall = (name, now)
            logger.warning("Unable to read History journal %s: %s", name, error)
            return
        if is_newest or now - self._stall[1] < self.stall_seconds:
            return
        logger.warning(
            "Skipping History journal %s: unreadable for over %d minutes",
            name,
            int(self.stall_seconds // 60),
        )
        self._stall = None
        following = names[names.index(name) + 1]
        self._move_to(following, count="skipped_segments")
        missing = _missing_between(name, following)
        if missing:
            self._move_to(following, count="missing_segments", amount=missing)

    def _move_to(self, segment, damaged=0, count=None, amount=1):
        with self.store.transaction() as connection:
            self._set_cursor(connection, segment, 0)
            if damaged:
                self._count(connection, "damaged_lines", damaged)
            if count and amount:
                self._count(connection, count, amount)

    def _write_ack(self):
        """Tell the miner which segments it may delete: those before the
        cursor's segment, all fully imported."""

        try:
            with self.store._read_connection() as connection:
                cursor = self._cursor(connection)
        except Exception:
            return
        if cursor is None:
            return
        done = [name for name in list_segments(self.folder) if name < cursor[0]]
        if not done or done[-1] == self._last_ack:
            return
        try:
            write_atomic(os.path.join(self.folder, ACK_FILE), done[-1] + "\n")
            self._last_ack = done[-1]
        except OSError as error:
            logger.debug("Unable to write the History import marker: %s", error)

    # -- applying --------------------------------------------------------------
    @staticmethod
    def _set_cursor(connection, segment, offset):
        connection.execute(
            """
            INSERT INTO journal_cursor (id, segment, offset) VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                segment = excluded.segment, offset = excluded.offset
            """,
            (segment, int(offset)),
        )

    @staticmethod
    def _count(connection, key, amount):
        connection.execute(
            """
            INSERT INTO journal_health (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = value + excluded.value
            """,
            (key, int(amount)),
        )

    def _apply_batch(self, segment, batch, end):
        deleted = dict(self._load_deletes())
        with self.store.transaction() as connection:
            for row in connection.execute(
                "SELECT streamer, segment, offset FROM deleted_streamers"
            ):
                position = (row["segment"], row["offset"])
                if position > deleted.get(row["streamer"], ("", 0)):
                    deleted[row["streamer"]] = position
            runs = {}
            self._first_at = None
            # Read before this batch adds anything: rows a database already
            # held when it began keeping journal_first_at.
            legacy = (
                earliest_held(connection)
                if connection.execute(
                    "SELECT 1 FROM meta WHERE key = 'journal_first_at'"
                ).fetchone()
                is None
                else None
            )
            for start, raw, _end in batch:
                self._apply_line_safely(connection, segment, start, raw, deleted, runs)
            self._flush_runs(connection, runs)
            self._save_first_at(connection, self._first_at, legacy)
            self._set_cursor(connection, segment, end)

    @staticmethod
    def _save_first_at(connection, first_at, legacy=None):
        """Keep the time of the earliest journal record imported into this
        database: the analytics JSON import (``backfill.py``) adds only what
        came before it.  ``legacy``: the earliest time the database held
        before it kept this (everything in it came from the journal); None
        for a new or rebuilt database."""

        times = [value for value in (first_at, legacy) if value is not None]
        if not times:
            return
        connection.execute(
            """
            INSERT INTO meta (key, value) VALUES ('journal_first_at', ?)
            ON CONFLICT(key) DO UPDATE SET value = CASE
                WHEN CAST(value AS REAL) <= CAST(excluded.value AS REAL)
                THEN value ELSE excluded.value END
            """,
            (repr(float(min(times))),),
        )

    @staticmethod
    def _flush_runs(connection, runs):
        for run, (last_seq, last_at) in runs.items():
            connection.execute(
                """
                INSERT INTO journal_runs (run, last_seq, last_at) VALUES (?, ?, ?)
                ON CONFLICT(run) DO UPDATE SET
                    last_seq = excluded.last_seq,
                    last_at = COALESCE(excluded.last_at, last_at)
                """,
                (run, last_seq, last_at),
            )
            connection.execute(
                "UPDATE runs SET last_seen_at = ? WHERE id = ? AND ? IS NOT NULL",
                (last_at, run, last_at),
            )

    def _last_seq(self, connection, runs, run):
        if run in runs:
            return runs[run][0]
        row = connection.execute(
            "SELECT last_seq, last_at FROM journal_runs WHERE run = ?", (run,)
        ).fetchone()
        if row is None:
            return 0
        runs[run] = (row["last_seq"], row["last_at"])
        return row["last_seq"]

    def _apply_line_safely(self, connection, segment, start, raw, deleted, runs):
        """Apply one line; a line whose values cannot be applied (an
        impossible number or time, say) is damaged and passed over, so it
        cannot stop the import.  Database errors still fail the batch."""

        connection.execute("SAVEPOINT journal_line")
        try:
            self._apply_line(connection, segment, start, raw, deleted, runs)
        except (sqlite3.Error, _SaveFailed):
            raise
        except Exception:
            connection.execute("ROLLBACK TO journal_line")
            self._count(connection, "damaged_lines", 1)
            if not self._bad_line_logged:
                self._bad_line_logged = True
                logger.warning(
                    "Skipping a History journal line that cannot be applied "
                    "(%s at %d)",
                    segment,
                    start,
                    exc_info=True,
                )
        connection.execute("RELEASE journal_line")

    def _apply_line(self, connection, segment, start, raw, deleted, runs):
        try:
            record = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            record = None
        if (
            not isinstance(record, dict)
            or record.get("v") != JOURNAL_FORMAT_VERSION
            or _int(record.get("run")) is None
            or _int(record.get("seq")) is None
            or not isinstance(record.get("kind"), str)
        ):
            self._count(connection, "damaged_lines", 1)
            return
        run = _int(record["run"])
        seq = _int(record["seq"])
        last_seq = self._last_seq(connection, runs, run)
        if seq <= last_seq:
            return
        if seq > last_seq + 1:
            self._count(connection, "gaps", seq - last_seq - 1)
        at = _number(record.get("at"))
        if at is None:
            at = runs.get(run, (None, None))[1] or time.time()
        runs[run] = (seq, at)
        first_at = getattr(self, "_first_at", None)
        if first_at is None or at < first_at:
            self._first_at = at

        streamer = clean_streamer(record.get("streamer"))
        cutoff = deleted.get(streamer) if streamer else None
        if cutoff is not None and (segment, start) < cutoff:
            if record["kind"] == "drop":
                # Deleted before it was imported: Twitch reporting the claim
                # again later must not bring it back either.
                self._delete_drop_claim(connection, record, at)
            return
        handler = getattr(self, "_apply_" + record["kind"], None)
        if handler is None:
            return
        if record["kind"] == "run_start":
            # It closes what earlier runs left open at their newest record,
            # which may be earlier in this batch.
            self._flush_runs(connection, runs)
        handler(connection, record, run, seq, at, streamer)

    # -- rollups -----------------------------------------------------------------
    @staticmethod
    def _touch_daily(connection, timestamp, streamer, **increments):
        columns = (
            "watch_seconds",
            "watch_sessions",
            "stream_sessions",
            "points_gained",
            "points_spent",
            "streak_count",
        )
        values = [increments.get(column, 0) for column in columns]
        names = ", ".join(columns)
        placeholders = ", ".join("?" for _ in columns)
        additions = ", ".join(f"{c} = {c} + excluded.{c}" for c in columns)
        connection.execute(
            f"""
            INSERT INTO daily_stats (day, streamer, {names}, first_seen, last_seen)
            VALUES (?, ?, {placeholders}, ?, ?)
            ON CONFLICT(day, streamer) DO UPDATE SET
                {additions},
                first_seen = MIN(COALESCE(first_seen, excluded.first_seen),
                                 excluded.first_seen),
                last_seen = MAX(COALESCE(last_seen, excluded.last_seen),
                                excluded.last_seen)
            """,
            [utc_day(timestamp), streamer, *values, timestamp, timestamp],
        )

    @staticmethod
    def _stream_game(connection, streamer, broadcast_id, earned_at=None):
        """The game the stream showed at the newest record imported so far:
        what the miner was watching when it recorded the next one.

        With ``earned_at`` (when points were earned), none when the stream
        was offline by then: a points message keeps the broadcast it was
        sent after (``journal._earned_on``) even when the channel went
        offline in between, or went offline and came back.  A late message
        for points earned while it was live still counts.  Both allow for
        the two clocks."""

        if broadcast_id is None:
            return None
        row = connection.execute(
            """
            SELECT game, ended_at, end_reason, offline_gaps FROM streams
            WHERE streamer = ? AND broadcast_id = ?
            """,
            (streamer, broadcast_id),
        ).fetchone()
        if row is None:
            return None
        if earned_at is not None:
            skew = WATCH_STREAK_CLOCK_SKEW_SECONDS
            if (
                row["ended_at"] is not None
                and row["end_reason"] == "offline"
                and earned_at > row["ended_at"] + skew
            ):
                return None
            for offline_at, live_at in _offline_gaps(row["offline_gaps"]):
                if offline_at + skew < earned_at < live_at - skew:
                    return None
        return _text(row["game"])

    @staticmethod
    def _touch_game(connection, timestamp, streamer, game, **increments):
        if game is None:
            return
        connection.execute(
            """
            INSERT INTO daily_game_stats (day, game, streamer, watch_seconds,
                                          points_gained)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(day, game, streamer) DO UPDATE SET
                watch_seconds = watch_seconds + excluded.watch_seconds,
                points_gained = points_gained + excluded.points_gained
            """,
            (
                utc_day(timestamp),
                game,
                streamer,
                increments.get("watch_seconds", 0),
                increments.get("points_gained", 0),
            ),
        )

    @staticmethod
    def _open_watch(connection, streamer):
        return connection.execute(
            """
            SELECT * FROM watch_sessions WHERE streamer = ? AND ended_at IS NULL
            ORDER BY id DESC LIMIT 1
            """,
            (streamer,),
        ).fetchone()

    @staticmethod
    def _close_watches(connection, where, params):
        connection.execute(
            f"UPDATE watch_sessions SET ended_at = last_seen_at "
            f"WHERE ended_at IS NULL AND {where}",
            params,
        )

    @staticmethod
    def _close_streams(connection, ended_at, reason, where, params):
        connection.execute(
            f"""
            UPDATE streams SET ended_at = MAX(started_at, ?), end_reason = ?
            WHERE ended_at IS NULL AND {where}
            """,
            (ended_at, reason, *params),
        )

    # -- record kinds ----------------------------------------------------------
    def _apply_watch(self, connection, record, run, seq, at, streamer):
        seconds = _number(record.get("seconds"))
        if not streamer or seconds is None or seconds < 0:
            return
        broadcast_id = _text(record.get("broadcast_id"))
        reason = _text(record.get("reason")) or "points"
        self._touch_game(
            connection,
            at,
            streamer,
            self._stream_game(connection, streamer, broadcast_id),
            watch_seconds=seconds,
        )
        opened = self._open_watch(connection, streamer)
        if (
            opened is not None
            and opened["run"] == run
            and opened["broadcast_id"] == broadcast_id
            and opened["reason"] == reason
            and at - opened["last_seen_at"] <= SESSION_GAP_SECONDS
        ):
            connection.execute(
                """
                UPDATE watch_sessions
                SET last_seen_at = ?, watch_seconds = watch_seconds + ?
                WHERE id = ?
                """,
                (at, seconds, opened["id"]),
            )
            self._touch_daily(connection, at, streamer, watch_seconds=seconds)
            return
        if opened is not None:
            self._close_watches(connection, "id = ?", (opened["id"],))
        connection.execute(
            """
            INSERT INTO watch_sessions (streamer, broadcast_id, reason, run,
                started_at, last_seen_at, watch_seconds)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (streamer, broadcast_id, reason, run, at - seconds, at, seconds),
        )
        self._touch_daily(
            connection, at, streamer, watch_seconds=seconds, watch_sessions=1
        )

    def _apply_watch_end(self, connection, record, run, seq, at, streamer):
        if streamer:
            self._close_watches(connection, "streamer = ?", (streamer,))

    def _apply_points(self, connection, record, run, seq, at, streamer):
        if not streamer:
            return
        msg_key = _text(record.get("msg_key"))
        if msg_key is not None:
            if connection.execute(
                "SELECT 1 FROM points_keys WHERE msg_key = ?", (msg_key,)
            ).fetchone():
                return
            connection.execute(
                "INSERT INTO points_keys (msg_key, streamer, run) VALUES (?, ?, ?)",
                (msg_key, streamer, run),
            )
        upstream_at = _number(record.get("upstream_at"))
        ts = upstream_at if upstream_at is not None else at
        delta = _int(record.get("delta"))
        reason = _text(record.get("reason_code"))
        connection.execute(
            """
            INSERT INTO points (streamer, ts, balance, delta, reason, kind,
                                dedupe_key)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                streamer,
                ts,
                _int(record.get("balance")),
                delta,
                reason,
                "earn" if delta is not None else "balance",
                f"j:{run}:{seq}",
            ),
        )
        streak = reason == "WATCH_STREAK"
        self._touch_daily(
            connection,
            ts,
            streamer,
            points_gained=delta or 0,
            streak_count=1 if streak else 0,
        )
        if delta:
            self._touch_game(
                connection,
                ts,
                streamer,
                self._stream_game(
                    connection, streamer, _text(record.get("broadcast_id")), ts
                ),
                points_gained=delta,
            )
        if streak:
            connection.execute(
                """
                INSERT INTO events (streamer, ts, type, label, points_delta,
                                    metadata, dedupe_key)
                VALUES (?, ?, 'watch_streak', ?, ?, ?, ?)
                """,
                (
                    streamer,
                    ts,
                    f"+{delta} - WATCH_STREAK" if delta is not None else None,
                    delta,
                    json.dumps({"broadcast_id": _text(record.get("broadcast_id"))}),
                    f"j:{run}:{seq}",
                ),
            )

    def _apply_spend(self, connection, record, run, seq, at, streamer):
        spend_key = _text(record.get("spend_key"))
        amount = _int(record.get("amount"))
        if not streamer or spend_key is None or amount is None or amount < 0:
            return
        if connection.execute(
            "SELECT 1 FROM spend_keys WHERE spend_key = ?", (spend_key,)
        ).fetchone():
            return
        connection.execute(
            "INSERT INTO spend_keys (spend_key, streamer, run) VALUES (?, ?, ?)",
            (spend_key, streamer, run),
        )
        upstream_at = _number(record.get("upstream_at"))
        ts = upstream_at if upstream_at is not None else at
        connection.execute(
            """
            INSERT INTO points (streamer, ts, balance, delta, reason, kind, label,
                                dedupe_key)
            VALUES (?, ?, NULL, ?, ?, 'spend', ?, ?)
            """,
            (
                streamer,
                ts,
                -amount,
                _text(record.get("source")),
                _text(record.get("title")),
                f"j:{run}:{seq}",
            ),
        )
        self._touch_daily(connection, ts, streamer, points_spent=amount)

    def _apply_live(self, connection, record, run, seq, at, streamer):
        broadcast_id = _text(record.get("broadcast_id"))
        if not streamer or broadcast_id is None:
            return
        game = _text(record.get("game"))
        title = _text(record.get("title"))
        previous = connection.execute(
            """
            SELECT ended_at, end_reason, offline_gaps FROM streams
            WHERE streamer = ? AND broadcast_id = ?
            """,
            (streamer, broadcast_id),
        ).fetchone()
        if (
            previous is not None
            and previous["end_reason"] == "offline"
            and previous["ended_at"] is not None
            and at > previous["ended_at"]
        ):
            # Live again: the time it was offline is kept (_stream_game).
            gaps = _offline_gaps(previous["offline_gaps"])
            gaps.append((float(previous["ended_at"]), at))
            connection.execute(
                """
                UPDATE streams SET offline_gaps = ?
                WHERE streamer = ? AND broadcast_id = ?
                """,
                (
                    json.dumps([list(gap) for gap in gaps[-STREAM_GAPS_KEPT:]]),
                    streamer,
                    broadcast_id,
                ),
            )
        # Its offline was missed.
        self._close_streams(
            connection,
            at,
            "replaced",
            "streamer = ? AND broadcast_id <> ?",
            (streamer, broadcast_id),
        )
        connection.execute(
            """
            INSERT INTO streams (streamer, broadcast_id, run, started_at,
                                 last_seen_at, title, game)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(streamer, broadcast_id) DO UPDATE SET
                run = excluded.run,
                started_at = MIN(started_at, excluded.started_at),
                last_seen_at = MAX(last_seen_at, excluded.last_seen_at),
                ended_at = NULL,
                end_reason = NULL,
                -- The newest record's title and game, also when cleared.
                title = CASE WHEN ? THEN excluded.title ELSE title END,
                game = CASE WHEN ? THEN excluded.game ELSE game END
            """,
            (
                streamer,
                broadcast_id,
                run,
                at,
                at,
                title,
                game,
                "title" in record,
                "game" in record,
            ),
        )
        counted = connection.execute(
            """
            INSERT OR IGNORE INTO counted_broadcasts (streamer, broadcast_id, day)
            VALUES (?, ?, ?)
            """,
            (streamer, broadcast_id, utc_day(at)),
        )
        self._touch_daily(
            connection,
            at,
            streamer,
            stream_sessions=1 if counted.rowcount > 0 else 0,
        )

    def _apply_offline(self, connection, record, run, seq, at, streamer):
        if not streamer:
            return
        broadcast_id = _text(record.get("broadcast_id"))
        if broadcast_id is None:
            self._close_streams(connection, at, "offline", "streamer = ?", (streamer,))
        else:
            self._close_streams(
                connection,
                at,
                "offline",
                "streamer = ? AND broadcast_id = ?",
                (streamer, broadcast_id),
            )
        self._close_watches(connection, "streamer = ?", (streamer,))

    def _apply_untracked(self, connection, record, run, seq, at, streamer):
        if not streamer:
            return
        self._close_streams(connection, at, "untracked", "streamer = ?", (streamer,))
        self._close_watches(connection, "streamer = ?", (streamer,))

    def _apply_event(self, connection, record, run, seq, at, streamer):
        if not streamer:
            return
        connection.execute(
            """
            INSERT INTO events (streamer, ts, type, label, points_delta, dedupe_key)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                streamer,
                at,
                _text(record.get("type")) or "event",
                _text(record.get("label")),
                _int(record.get("points_delta")),
                f"j:{run}:{seq}",
            ),
        )

    def _apply_drop(self, connection, record, run, seq, at, streamer):
        """A claimed drop: counted once per claim, whatever run reports it."""

        drop_id = _text(record.get("drop_id"))
        claim_key = _text(record.get("claim_key"))
        if drop_id is None or claim_key is None:
            return
        game = _text(record.get("game"))
        benefits = record.get("benefits")
        benefits = [
            text
            for text in (
                _text(item) for item in (benefits if isinstance(benefits, list) else ())
            )
            if text is not None
        ]
        minutes = _int(record.get("minutes_required"))
        key = f"c:{claim_key}"
        if connection.execute(
            """
            SELECT 1 FROM deleted_drop_keys WHERE dedupe_key = ? OR drop_id = ?
            LIMIT 1
            """,
            (key, drop_id),
        ).fetchone():
            # Removed by a streamer delete (also when it was imported from
            # the Drops data: a drop is claimed once), reported again since.
            # Kept under this claim's own key too, and saved, with any copy
            # the Drops data import added since.
            new = []
            own = (key, drop_id, game, at, "journal")
            if self._tombstone(connection, own):
                new.append(own)
            new += self._remove_drops_data_copies(connection, drop_id)
            self._save_deleted(new, at)
            return
        existing = connection.execute(
            """
            SELECT dedupe_key, game, streamer FROM drops
            WHERE source = 'journal' AND (dedupe_key = ? OR drop_id = ?)
            LIMIT 1
            """,
            (key, drop_id),
        ).fetchone()
        if existing is not None:
            # Reported again.  Recorded without its game or channel before,
            # perhaps: now it is known.  A known one is kept.
            if (game is not None and existing["game"] is None) or (
                streamer and existing["streamer"] is None
            ):
                connection.execute(
                    """
                    UPDATE drops SET game = COALESCE(game, ?),
                        streamer = COALESCE(streamer, ?)
                    WHERE dedupe_key = ?
                    """,
                    (game, streamer or None, existing["dedupe_key"]),
                )
        else:
            connection.execute(
                """
                INSERT INTO drops (ts, streamer, drop_id, name, benefits,
                    campaign, campaign_id, game, quest, minutes_required, image,
                    source, dedupe_key)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'journal', ?)
                """,
                (
                    at,
                    streamer or None,
                    drop_id,
                    _text(record.get("name")),
                    json.dumps(benefits, ensure_ascii=False),
                    _text(record.get("campaign")),
                    _text(record.get("campaign_id")),
                    game,
                    1 if record.get("quest") is True else 0,
                    minutes if minutes is not None and minutes >= 0 else None,
                    _text(record.get("image")),
                    key,
                ),
            )
        # The same claim imported from the Drops data (backfill.py, which
        # skips it by the same rule, so the order of the two imports doesn't
        # matter): a drop is claimed once.
        connection.execute(
            "DELETE FROM drops WHERE source = 'analytics' AND drop_id = ?",
            (drop_id,),
        )

    def _remove_drops_data_copies(self, connection, drop_id):
        """Remove the claims of ``drop_id`` the Drops data import added
        without a channel (a delete removes only the channel's own), recorded
        as deleted.  Returns the ones not recorded as deleted yet, for
        ``_save_deleted``."""

        new = []
        for row in connection.execute(
            """
            SELECT dedupe_key, game, ts FROM drops
            WHERE source = 'analytics' AND streamer IS NULL AND drop_id = ?
            """,
            (drop_id,),
        ).fetchall():
            drop = (row["dedupe_key"], drop_id, row["game"], row["ts"], "analytics")
            if self._tombstone(connection, drop):
                new.append(drop)
        connection.execute(
            """
            DELETE FROM drops
            WHERE source = 'analytics' AND streamer IS NULL AND drop_id = ?
            """,
            (drop_id,),
        )
        return new

    @staticmethod
    def _tombstone(connection, drop):
        """Record one deleted claim, ``(key, drop_id, game, ts, source)``;
        True when it was not recorded yet."""

        return (
            connection.execute(
                """
                INSERT OR IGNORE INTO deleted_drop_keys (dedupe_key, drop_id,
                                                         game, ts, source)
                VALUES (?, ?, ?, ?, ?)
                """,
                drop,
            ).rowcount
            > 0
        )

    def _delete_drop_claim(self, connection, record, at):
        """A claim journalled before its channel's delete cutoff but imported
        after the delete: it is deleted too, with the same claim imported
        from the Drops data without a channel (it was not credited to the
        channel, so the delete did not remove it).  Saved beside the
        journal like the delete's own claims."""

        drop_id = _text(record.get("drop_id"))
        claim_key = _text(record.get("claim_key"))
        if drop_id is None or claim_key is None:
            return
        own = (f"c:{claim_key}", drop_id, _text(record.get("game")), at, "journal")
        # Only what is not recorded yet is saved: what is was saved when it
        # was recorded (or restored from the file after a rebuild).
        new = [own] if self._tombstone(connection, own) else []
        new += self._remove_drops_data_copies(connection, drop_id)
        self._save_deleted(new, at)

    def _save_deleted(self, drops, at):
        """Save deleted claims beside the journal before the import that
        recorded them commits; raises ``_SaveFailed`` when they cannot be
        saved, so the batch is rolled back and imported again later."""

        if not drops:
            return
        try:
            save_deleted_drops(self.folder, drops, at)
        except OSError as error:
            raise _SaveFailed(str(error)) from error
        self._load_deleted_drops().extend(drops)

    def _apply_run_start(self, connection, record, run, seq, at, streamer):
        # Runs before this one that never wrote run_end crashed: close what
        # they left open at their newest record.
        for row in connection.execute(
            """
            SELECT run, last_at FROM journal_runs WHERE run < ? AND ended = 0
            """,
            (run,),
        ).fetchall():
            ended_at = row["last_at"]
            if ended_at is not None:
                self._close_streams(
                    connection, ended_at, "crash", "run = ?", (row["run"],)
                )
                connection.execute(
                    "UPDATE runs SET ended_at = ? WHERE id = ? AND ended_at IS NULL",
                    (ended_at, row["run"]),
                )
            self._close_watches(connection, "run = ?", (row["run"],))
            connection.execute(
                "UPDATE journal_runs SET ended = 1 WHERE run = ?", (row["run"],)
            )
        connection.execute(
            """
            INSERT OR IGNORE INTO runs (id, started_at, last_seen_at, version)
            VALUES (?, ?, ?, ?)
            """,
            (run, at, at, _text(record.get("version"))),
        )
        # Keep the keys of this run and the previous one.
        previous = connection.execute(
            "SELECT MAX(run) FROM journal_runs WHERE run < ?", (run,)
        ).fetchone()[0]
        keep_from = previous if previous is not None else run
        for table in ("points_keys", "spend_keys"):
            connection.execute(f"DELETE FROM {table} WHERE run < ?", (keep_from,))

    def _apply_run_end(self, connection, record, run, seq, at, streamer):
        connection.execute(
            "UPDATE runs SET ended_at = ?, last_seen_at = ? WHERE id = ?",
            (at, at, run),
        )
        self._close_watches(connection, "run = ?", (run,))
        self._close_streams(connection, at, "run_end", "run = ?", (run,))
        connection.execute(
            """
            INSERT INTO journal_runs (run, last_seq, last_at, ended)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(run) DO UPDATE SET ended = 1
            """,
            (run, seq, at),
        )
