"""SQLite storage for the optional History dashboard tab.

The database is derived from the miner's journal (``journal.py``): the
importer (``importer.py``) adds journal records up, and the query methods here
serve the ``/history`` API.  It lives beside the per-streamer analytics JSON,
in its own file, because history needs indexed range queries.

Raw rows are kept for ``DEFAULT_RETENTION_DAYS``; the small ``daily_stats``
and ``daily_game_stats`` rollups and the claimed ``drops`` are kept forever,
so all-time totals survive pruning.

``PRAGMA user_version`` identifies the schema.  Versions 1-3 were written by
development builds of the snapshot-based recorder; such a database is moved
aside and rebuilt (it is derived data).  Version 4 is upgraded in place to 5
(drops and game totals): the journal it was imported from is gone.
"""

import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path

from TwitchChannelPointsMiner.constants import WATCH_STREAK_STREAM_GAP_SECONDS
from TwitchChannelPointsMiner.history import DEFAULT_RETENTION_DAYS

logger = logging.getLogger(__name__)

HISTORY_SCHEMA_VERSION = 5
# Older versions came from the snapshot-based development recorder.
_FIRST_JOURNAL_SCHEMA = 4
SECONDS_PER_DAY = 24 * 60 * 60

# Records further apart than this start a new watch session, and an open
# session or a crashed run's stream stops showing as active this long after
# its newest record.
SESSION_GAP_SECONDS = 180

# A claim imported from the dashboard's Drops data and a journalled claim of
# the same drop (and category) are one claim when the journal's is at most
# this much later than the Drops entry: the miner writes the journal record
# first, and the entry's time only moves later (it is refreshed whenever the
# drop is seen again).  A journalled claim later than that is a new one,
# such as the next repeat of a quest.
DROP_MATCH_SECONDS = 60

# Tables holding per-streamer rows, cleared by a delete.
_STREAMER_TABLES = (
    "points",
    "events",
    "watch_sessions",
    "streams",
    "daily_stats",
    "points_keys",
    "spend_keys",
    "counted_broadcasts",
    "daily_game_stats",
    "drops",
)


def utc_day(timestamp):
    """Return the UTC calendar day for ``timestamp`` as ``YYYY-MM-DD``."""

    return time.strftime("%Y-%m-%d", time.gmtime(float(timestamp)))


def day_start(day):
    """Return the UTC epoch second at which ``day`` starts."""

    return datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()


def _float_or_none(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def earliest_held(connection):
    """The earliest time of a journal record the database holds, or None
    when it holds none.  Only times recorded as they happened count: a
    watch session or a stream starts before the record that reports it.
    The daily totals count too: retention keeps them after it prunes the
    rows they were added from."""

    earliest = None
    for statement in (
        "SELECT MIN(started_at) FROM runs",
        "SELECT MIN(ts) FROM points",
        "SELECT MIN(ts) FROM events",
        "SELECT MIN(first_seen) FROM daily_stats",
    ):
        value = connection.execute(statement).fetchone()[0]
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if earliest is None or value < earliest:
                earliest = float(value)
    return earliest


def clean_streamer(streamer):
    return str(streamer or "").strip().lower()


def first_window_day(now, days):
    """Return the first UTC day of a ``days``-long window ending today.

    ``days=1`` is today only, so every query that takes ``days`` covers the
    same calendar days as the daily chart.
    """

    days = max(1, int(days))
    return utc_day(float(now) - ((days - 1) * SECONDS_PER_DAY))


def _logical_stream_window(live_stream, streams):
    """Return the broadcast ids and start of the current logical stream.

    Twitch treats a broadcast that starts less than 30 minutes after the
    previous one ended as the same stream for watch-streak purposes.  Only an
    end the miner observed (an ``offline`` record) proves such a gap.
    """

    broadcast_id = str(live_stream.get("broadcast_id") or "").strip()
    broadcast_ids = {broadcast_id} if broadcast_id else set()
    current_started_at = float(live_stream.get("started_at") or 0.0)
    logical_started_at = current_started_at
    for previous in reversed(streams or []):
        previous_id = str(previous.get("broadcast_id") or "").strip()
        if not previous_id or previous_id == broadcast_id:
            continue
        previous_started_at = float(previous.get("started_at") or 0.0)
        if previous_started_at >= current_started_at:
            continue
        if previous.get("ended_at") is None or previous.get("end_reason") != "offline":
            break
        gap = current_started_at - float(previous["ended_at"])
        if gap <= 0 or gap >= WATCH_STREAK_STREAM_GAP_SECONDS:
            break
        broadcast_ids.add(previous_id)
        current_started_at = previous_started_at
        logical_started_at = previous_started_at
    return broadcast_ids, logical_started_at


def _metadata_broadcast_id(raw):
    try:
        metadata = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None
    if not isinstance(metadata, dict):
        return None
    value = metadata.get("broadcast_id")
    return str(value) if value not in (None, "") else None


_SCHEMA = (
    """
    CREATE TABLE streams (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        streamer TEXT NOT NULL,
        broadcast_id TEXT NOT NULL,
        run INTEGER,
        started_at REAL NOT NULL,
        ended_at REAL,
        end_reason TEXT,
        last_seen_at REAL NOT NULL,
        title TEXT,
        game TEXT,
        UNIQUE (streamer, broadcast_id)
    )
    """,
    "CREATE INDEX idx_streams_streamer_started ON streams (streamer, started_at)",
    "CREATE INDEX idx_streams_started ON streams (started_at)",
    "CREATE INDEX idx_streams_open ON streams (streamer) WHERE ended_at IS NULL",
    """
    CREATE TABLE watch_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        streamer TEXT NOT NULL,
        broadcast_id TEXT,
        reason TEXT NOT NULL DEFAULT 'points',
        run INTEGER NOT NULL,
        started_at REAL NOT NULL,
        ended_at REAL,
        last_seen_at REAL NOT NULL,
        watch_seconds REAL NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX idx_watch_sessions_streamer_started "
    "ON watch_sessions (streamer, started_at)",
    "CREATE INDEX idx_watch_sessions_started ON watch_sessions (started_at)",
    "CREATE INDEX idx_watch_sessions_open ON watch_sessions (streamer) "
    "WHERE ended_at IS NULL",
    """
    CREATE TABLE points (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        streamer TEXT NOT NULL,
        ts REAL NOT NULL,
        balance INTEGER,
        delta INTEGER,
        reason TEXT,
        kind TEXT NOT NULL DEFAULT 'earn',
        label TEXT,
        dedupe_key TEXT NOT NULL UNIQUE
    )
    """,
    "CREATE INDEX idx_points_streamer_ts ON points (streamer, ts)",
    "CREATE INDEX idx_points_ts ON points (ts)",
    """
    CREATE TABLE events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        streamer TEXT NOT NULL,
        ts REAL NOT NULL,
        type TEXT NOT NULL,
        label TEXT,
        points_delta INTEGER,
        metadata TEXT,
        dedupe_key TEXT NOT NULL UNIQUE
    )
    """,
    "CREATE INDEX idx_events_streamer_ts ON events (streamer, ts)",
    "CREATE INDEX idx_events_type_ts ON events (type, ts)",
    "CREATE INDEX idx_events_ts ON events (ts)",
    """
    CREATE TABLE daily_stats (
        day TEXT NOT NULL,
        streamer TEXT NOT NULL,
        watch_seconds REAL NOT NULL DEFAULT 0,
        watch_sessions INTEGER NOT NULL DEFAULT 0,
        stream_sessions INTEGER NOT NULL DEFAULT 0,
        points_gained INTEGER NOT NULL DEFAULT 0,
        points_spent INTEGER NOT NULL DEFAULT 0,
        streak_count INTEGER NOT NULL DEFAULT 0,
        first_seen REAL,
        last_seen REAL,
        PRIMARY KEY (day, streamer)
    )
    """,
    "CREATE INDEX idx_daily_stats_day ON daily_stats (day)",
    """
    CREATE TABLE runs (
        id INTEGER PRIMARY KEY,
        started_at REAL NOT NULL,
        ended_at REAL,
        last_seen_at REAL NOT NULL,
        version TEXT
    )
    """,
    "CREATE INDEX idx_runs_started ON runs (started_at)",
    # Import position: the next byte of ``segment`` to read.
    """
    CREATE TABLE journal_cursor (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        segment TEXT NOT NULL,
        offset INTEGER NOT NULL
    )
    """,
    # Last applied seq per run: a second guard against applying twice.
    """
    CREATE TABLE journal_runs (
        run INTEGER PRIMARY KEY,
        last_seq INTEGER NOT NULL,
        last_at REAL,
        ended INTEGER NOT NULL DEFAULT 0
    )
    """,
    # Counters for /history/health: gaps, damaged lines, skipped segments.
    """
    CREATE TABLE journal_health (
        key TEXT PRIMARY KEY,
        value INTEGER NOT NULL DEFAULT 0
    )
    """,
    # Points messages and spends already counted, pruned by run number.
    """
    CREATE TABLE points_keys (
        msg_key TEXT PRIMARY KEY,
        streamer TEXT NOT NULL,
        run INTEGER NOT NULL
    )
    """,
    "CREATE INDEX idx_points_keys_run ON points_keys (run)",
    """
    CREATE TABLE spend_keys (
        spend_key TEXT PRIMARY KEY,
        streamer TEXT NOT NULL,
        run INTEGER NOT NULL
    )
    """,
    "CREATE INDEX idx_spend_keys_run ON spend_keys (run)",
    # Broadcasts already counted as a stream session.  Never pruned by
    # retention, so a broadcast seen again later is not counted twice.
    """
    CREATE TABLE counted_broadcasts (
        streamer TEXT NOT NULL,
        broadcast_id TEXT NOT NULL,
        day TEXT NOT NULL,
        PRIMARY KEY (streamer, broadcast_id)
    )
    """,
    # A delete's journal write position: that streamer's records before it
    # are ignored.
    """
    CREATE TABLE deleted_streamers (
        streamer TEXT PRIMARY KEY,
        segment TEXT NOT NULL,
        offset INTEGER NOT NULL,
        deleted_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE meta (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """,
)

# Added by schema 5; a schema 4 database gains them in place (see
# HistoryStore._initialize_schema), keeping everything it holds.
_SCHEMA_5 = (
    # Claimed drops and quest rewards, one row per claim.  Kept forever,
    # like the daily rollups: there are few, and they are the account's.
    # ``streamer`` is the channel Twitch credited, when known.  ``source``
    # is 'journal', or 'analytics' for a claim imported from the Drops data
    # (see backfill.py), which a journal row of the same claim replaces.
    """
    CREATE TABLE drops (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        streamer TEXT,
        drop_id TEXT NOT NULL,
        name TEXT,
        benefits TEXT,
        campaign TEXT,
        campaign_id TEXT,
        game TEXT,
        quest INTEGER NOT NULL DEFAULT 0,
        minutes_required INTEGER,
        image TEXT,
        source TEXT NOT NULL DEFAULT 'journal',
        dedupe_key TEXT NOT NULL UNIQUE
    )
    """,
    "CREATE INDEX idx_drops_ts ON drops (ts)",
    "CREATE INDEX idx_drops_streamer_ts ON drops (streamer, ts)",
    "CREATE INDEX idx_drops_game_ts ON drops (game, ts)",
    "CREATE INDEX idx_drops_drop_id ON drops (drop_id, game)",
    # Watch time and points per game, credited to the game the stream
    # showed when the miner recorded them.  Kept forever, like daily_stats.
    """
    CREATE TABLE daily_game_stats (
        day TEXT NOT NULL,
        game TEXT NOT NULL,
        streamer TEXT NOT NULL,
        watch_seconds REAL NOT NULL DEFAULT 0,
        points_gained INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (day, game, streamer)
    )
    """,
    "CREATE INDEX idx_daily_game_stats_game ON daily_game_stats (game, day)",
    "CREATE INDEX idx_daily_game_stats_streamer " "ON daily_game_stats (streamer, day)",
)

# meta key: when a schema 4 database gained the game totals.  Watch time and
# points before it have no game.
META_GAMES_SINCE = "games_since"


class HistorySchemaError(RuntimeError):
    """The database was written by a different build than this one."""


# A session listed for a window started in it or was still going in it, so a
# watch or broadcast that began before midnight is listed for today too.  Takes
# the window end (see _window_end) and then the window start twice.
_OVERLAPS_WINDOW = (
    "(started_at < ? AND (started_at >= ? OR ended_at IS NULL OR ended_at >= ?))"
)


def _window_end(now):
    """End of today: rows dated later (the clock went back) are left out,
    as the daily chart leaves out their days."""

    return day_start(utc_day(now)) + SECONDS_PER_DAY


class HistoryStore(object):
    """Small, thread-safe SQLite history store.

    Writes go through :meth:`transaction`, serialized by a process-local
    lock.  Read methods open a read-only connection, so Flask request threads
    never share a connection with the importer.  WAL mode lets readers run
    while the importer writes.

    ``active_run`` is the journal run the miner in this process is writing,
    if any: its open streams show as live however old their newest record.
    """

    def __init__(self, path):
        self.path = os.fspath(path)
        self.ready = False
        self.active_run = None
        self._write_lock = threading.RLock()
        self._wal_warning_logged = False

    # -- connections -------------------------------------------------------
    @contextmanager
    def _write_connection(self):
        with self._write_lock:
            connection = sqlite3.connect(self.path, timeout=5.0)
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("PRAGMA busy_timeout=5000")
                connection.execute("PRAGMA synchronous=NORMAL")
                mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
                if (
                    mode is not None
                    and str(mode[0]).lower() != "wal"
                    and not self._wal_warning_logged
                ):
                    self._wal_warning_logged = True
                    logger.warning(
                        "History database %s could not enable SQLite WAL mode "
                        "(got %r); history will continue with SQLite locking",
                        self.path,
                        mode[0],
                    )
                yield connection
                connection.commit()
            except BaseException:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
                raise
            finally:
                connection.close()

    @contextmanager
    def transaction(self):
        """One write transaction: everything in it commits or nothing does."""

        with self._write_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            yield connection

    @contextmanager
    def _read_connection(self):
        if not self.exists():
            raise FileNotFoundError(self.path)
        uri = Path(self.path).resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA busy_timeout=5000")
            # One read transaction, so every query of a request sees the same
            # snapshot even while the importer commits.
            connection.execute("BEGIN")
            yield connection
        finally:
            connection.close()

    def exists(self):
        return os.path.isfile(self.path)

    # -- lifecycle ---------------------------------------------------------
    @staticmethod
    def _is_corruption(error):
        message = str(error).lower()
        return any(
            marker in message
            for marker in (
                "file is not a database",
                "database disk image is malformed",
                "file is encrypted",
            )
        )

    def _backup_database(self):
        """Move an unusable database aside so it can be rebuilt safely."""

        base = f"{self.path}.bak-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}"
        backup = base
        suffix = 1
        while any(
            os.path.exists(backup + extension)
            for extension in ("", "-wal", "-shm", "-journal")
        ):
            backup = f"{base}-{suffix}"
            suffix += 1
        for extension in ("", "-wal", "-shm", "-journal"):
            source = self.path + extension
            if os.path.exists(source):
                os.replace(source, backup + extension)
        logger.warning(
            "History database %s could not be used; moved it to %s and rebuilding",
            self.path,
            backup,
        )
        return backup

    def _initialize_schema(self):
        with self._write_connection() as connection:
            # One transaction, so an interrupted creation leaves nothing
            # half-made behind (sqlite3 does not open one for DDL itself).
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == HISTORY_SCHEMA_VERSION:
                return
            if version == 4:
                # The journal segments History imported are gone, so a
                # rebuild would lose what this database holds: add the new
                # tables instead.
                for statement in _SCHEMA_5:
                    connection.execute(statement)
                connection.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                    (META_GAMES_SINCE, repr(float(time.time()))),
                )
                connection.execute(f"PRAGMA user_version={HISTORY_SCHEMA_VERSION}")
                return
            if version != 0:
                raise HistorySchemaError(
                    f"History database {self.path} uses schema version {version}; "
                    f"this build uses {HISTORY_SCHEMA_VERSION}"
                )
            for statement in _SCHEMA + _SCHEMA_5:
                connection.execute(statement)
            connection.execute(f"PRAGMA user_version={HISTORY_SCHEMA_VERSION}")

    def initialize(self):
        """Create or rebuild the database, then mark it readable."""

        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._write_lock:
            try:
                self._initialize_schema()
            except HistorySchemaError as error:
                logger.warning("%s; rebuilding the derived database", error)
                self._backup_database()
                self._initialize_schema()
            except sqlite3.DatabaseError as error:
                if not self._is_corruption(error):
                    raise
                logger.warning(
                    "History database %s is corrupt (%s); rebuilding the derived database",
                    self.path,
                    error,
                )
                self._backup_database()
                self._initialize_schema()
            self.ready = True

    def recover_if_corrupt(self, error):
        """Rebuild the database after a corruption error seen while running.

        Returns True when the file was moved aside and recreated; the importer
        then reads the journal again from the start.
        """

        if not isinstance(error, sqlite3.DatabaseError) or not self._is_corruption(
            error
        ):
            return False
        with self._write_lock:
            logger.warning(
                "History database %s is corrupt (%s); rebuilding the derived database",
                self.path,
                error,
            )
            self.ready = False
            self._backup_database()
            self._initialize_schema()
            self.ready = True
        return True

    # -- maintenance ---------------------------------------------------------
    def apply_retention(self, now=None, retention_days=DEFAULT_RETENTION_DAYS):
        """Prune raw rows while keeping the daily rollups forever.

        Deduplication keys are pruned by run number (importer), and
        ``counted_broadcasts`` is never pruned, so retention cannot make
        anything count twice.
        """

        if not retention_days or retention_days <= 0:
            return {}
        cutoff = float(now or time.time()) - (float(retention_days) * SECONDS_PER_DAY)
        counts = {}
        with self.transaction() as connection:
            for name, statement in (
                ("points", "DELETE FROM points WHERE ts < ?"),
                ("events", "DELETE FROM events WHERE ts < ?"),
                (
                    "watch_sessions",
                    "DELETE FROM watch_sessions "
                    "WHERE ended_at IS NOT NULL AND ended_at < ?",
                ),
                (
                    "streams",
                    "DELETE FROM streams WHERE ended_at IS NOT NULL AND ended_at < ?",
                ),
                (
                    "runs",
                    "DELETE FROM runs WHERE ended_at IS NOT NULL AND ended_at < ?",
                ),
            ):
                cursor = connection.execute(statement, (cutoff,))
                counts[name] = max(0, cursor.rowcount)
        return counts

    def delete_streamer(
        self, streamer, segment, offset, deleted_at=None, connection=None
    ):
        """Delete one streamer's rows; its journal records before
        ``(segment, offset)`` (the journal's end at the time) stay ignored.

        ``connection`` is a write transaction the caller already holds
        (see ``transaction``); without one, a transaction is opened here."""

        streamer = clean_streamer(streamer)
        if not streamer:
            return {}
        if connection is None:
            with self.transaction() as connection:
                return self._delete_streamer_rows(
                    connection, streamer, segment, offset, deleted_at
                )
        return self._delete_streamer_rows(
            connection, streamer, segment, offset, deleted_at
        )

    @staticmethod
    def _delete_streamer_rows(connection, streamer, segment, offset, deleted_at):
        counts = {}
        for table in _STREAMER_TABLES:
            cursor = connection.execute(
                f"DELETE FROM {table} WHERE streamer = ?", (streamer,)
            )
            counts[table] = max(0, cursor.rowcount)
        HistoryStore._record_delete(connection, streamer, segment, offset, deleted_at)
        return {table: count for table, count in counts.items() if count}

    def record_delete(self, streamer, segment, offset, deleted_at=None):
        """Record a delete cutoff without deleting rows (see
        ``JournalImporter.apply_saved_deletes``)."""

        streamer = clean_streamer(streamer)
        if not streamer:
            return
        with self.transaction() as connection:
            self._record_delete(connection, streamer, segment, offset, deleted_at)

    @staticmethod
    def _record_delete(connection, streamer, segment, offset, deleted_at):
        existing = connection.execute(
            "SELECT segment, offset FROM deleted_streamers WHERE streamer = ?",
            (streamer,),
        ).fetchone()
        position = (str(segment or ""), int(offset or 0))
        if existing is not None:
            position = max(position, (existing["segment"], existing["offset"]))
        connection.execute(
            """
            INSERT INTO deleted_streamers (streamer, segment, offset, deleted_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(streamer) DO UPDATE SET
                segment = excluded.segment,
                offset = excluded.offset,
                deleted_at = excluded.deleted_at
            """,
            (streamer, position[0], position[1], float(deleted_at or time.time())),
        )

    def journal_status(self):
        with self._read_connection() as connection:
            cursor = connection.execute(
                "SELECT segment, offset FROM journal_cursor WHERE id = 1"
            ).fetchone()
            health = {
                row["key"]: row["value"]
                for row in connection.execute("SELECT key, value FROM journal_health")
            }
        return {
            "cursor": dict(cursor) if cursor is not None else None,
            "gaps": int(health.get("gaps", 0)),
            "damaged_lines": int(health.get("damaged_lines", 0)),
            "skipped_segments": int(health.get("skipped_segments", 0)),
            "missing_segments": int(health.get("missing_segments", 0)),
        }

    # -- activity ------------------------------------------------------------
    def _live_runs(self, connection, now):
        """Runs whose open rows count as current: this process's run, and
        any other unended run whose newest record is recent."""

        runs = {
            row["run"]
            for row in connection.execute(
                "SELECT run FROM journal_runs WHERE ended = 0 AND last_at >= ?",
                (float(now) - SESSION_GAP_SECONDS,),
            )
        }
        if self.active_run is not None:
            runs.add(int(self.active_run))
        return runs

    @staticmethod
    def _active_watches(connection, now):
        return {
            row["streamer"]: dict(row)
            for row in connection.execute(
                """
                SELECT streamer, id, reason, started_at, last_seen_at,
                       watch_seconds, broadcast_id
                FROM watch_sessions
                WHERE ended_at IS NULL AND last_seen_at >= ?
                """,
                (float(now) - SESSION_GAP_SECONDS,),
            )
        }

    def _live_streams(self, connection, now):
        live_runs = self._live_runs(connection, now)
        return {row["streamer"]: dict(row) for row in connection.execute("""
                SELECT streamer, id, run, broadcast_id, started_at, last_seen_at,
                       title, game
                FROM streams WHERE ended_at IS NULL
                """) if row["run"] in live_runs}

    # -- queries ---------------------------------------------------------------
    def overview(self, days=30, now=None):
        now = float(now or time.time())
        today = utc_day(now)
        cutoff_day = first_window_day(now, days)
        totals = """
            SELECT COALESCE(SUM(points_gained), 0) AS points_gained,
                   COALESCE(SUM(points_spent), 0) AS points_spent,
                   COALESCE(SUM(watch_seconds), 0) AS watch_seconds,
                   COALESCE(SUM(streak_count), 0) AS streak_count,
                   COUNT(DISTINCT streamer) AS streamers
            FROM daily_stats
        """
        with self._read_connection() as connection:
            window = connection.execute(
                totals + " WHERE day >= ? AND day <= ?", (cutoff_day, today)
            ).fetchone()
            today_row = connection.execute(
                totals + " WHERE day = ?", (today,)
            ).fetchone()
            lifetime = connection.execute("""
                SELECT COALESCE(SUM(points_gained), 0) AS points_gained,
                       COALESCE(SUM(points_spent), 0) AS points_spent,
                       COALESCE(SUM(watch_seconds), 0) AS watch_seconds,
                       COALESCE(SUM(streak_count), 0) AS streak_count,
                       COUNT(DISTINCT streamer) AS streamers,
                       MIN(day) AS first_day,
                       MAX(day) AS last_day
                FROM daily_stats
                """).fetchone()
            active_watch = len(self._active_watches(connection, now))
            live_streams = len(self._live_streams(connection, now))
            last_point = connection.execute("SELECT MAX(ts) FROM points").fetchone()[0]
            last_watch = connection.execute(
                "SELECT MAX(last_seen_at) FROM watch_sessions"
            ).fetchone()[0]
            window_start = day_start(cutoff_day)
            window_end = _window_end(now)
            drop_counts = {
                "window": self._drop_count(connection, window_start, window_end),
                "today": self._drop_count(connection, day_start(today), window_end),
                "lifetime": self._drop_count(connection, None, None),
            }
            games_since = connection.execute(
                "SELECT value FROM meta WHERE key = ?", (META_GAMES_SINCE,)
            ).fetchone()
            imported_before = connection.execute("""
                SELECT boundary.value FROM meta AS boundary
                JOIN meta AS imported ON imported.key = 'backfill_imported'
                WHERE boundary.key = 'backfill_boundary' AND imported.value = '1'
                """).fetchone()
            live_runs = self._live_runs(connection, now)
            current_run = None
            if live_runs:
                placeholders = ",".join("?" for _ in live_runs)
                current_run = connection.execute(
                    f"""
                    SELECT * FROM runs WHERE ended_at IS NULL
                      AND id IN ({placeholders})
                    ORDER BY id DESC LIMIT 1
                    """,
                    tuple(live_runs),
                ).fetchone()
        return {
            "generated_at": now,
            "days": int(days),
            "since_day": cutoff_day,
            "today": dict(today_row, drops=drop_counts["today"]),
            "window": dict(window, drops=drop_counts["window"]),
            "lifetime": dict(lifetime, drops=drop_counts["lifetime"]),
            # Watch time and points before this have no game (the database
            # was made before History recorded games); None when it never
            # lacked them.
            "games_since": (
                _float_or_none(games_since[0]) if games_since is not None else None
            ),
            "active_watch_sessions": int(active_watch),
            "live_streams": int(live_streams),
            "last_point_at": last_point,
            "last_watch_at": last_watch,
            "current_run": dict(current_run) if current_run is not None else None,
            # Days before this were imported from the analytics JSON:
            # points and predictions only.
            "imported_before": (
                _float_or_none(imported_before[0])
                if imported_before is not None
                else None
            ),
        }

    @staticmethod
    def _drop_count(connection, start, end, streamer=None):
        """Claims from ``start`` until ``end`` (None: no limit)."""

        return int(
            connection.execute(
                """
                SELECT COUNT(*) FROM drops
                WHERE (? IS NULL OR ts >= ?) AND (? IS NULL OR ts < ?)
                  AND (? IS NULL OR streamer = ?)
                """,
                (start, start, end, end, streamer, streamer),
            ).fetchone()[0]
            or 0
        )

    def streamers(self, days=30, now=None, connection=None):
        """Per-streamer totals; ``connection`` reads within a caller's snapshot."""

        now = float(now or time.time())
        cutoff_day = first_window_day(now, days)
        reading = (
            nullcontext(connection)
            if connection is not None
            else self._read_connection()
        )
        with reading as connection:
            rows = connection.execute(
                """
                SELECT streamer,
                       COALESCE(SUM(points_gained), 0) AS points_gained,
                       COALESCE(SUM(points_spent), 0) AS points_spent,
                       COALESCE(SUM(watch_seconds), 0) AS watch_seconds,
                       COALESCE(SUM(watch_sessions), 0) AS watch_sessions,
                       COALESCE(SUM(stream_sessions), 0) AS stream_sessions,
                       COALESCE(SUM(streak_count), 0) AS streak_count,
                       MIN(first_seen) AS first_seen,
                       MAX(last_seen) AS last_seen
                FROM daily_stats WHERE day >= ? AND day <= ?
                GROUP BY streamer ORDER BY last_seen DESC
                """,
                # Days after today (the clock went back) are not in the
                # daily chart either.
                (cutoff_day, utc_day(now)),
            ).fetchall()
            lifetime = {row["streamer"]: row for row in connection.execute("""
                    SELECT streamer,
                           COALESCE(SUM(points_gained), 0) AS points_gained,
                           COALESCE(SUM(points_spent), 0) AS points_spent,
                           COALESCE(SUM(watch_seconds), 0) AS watch_seconds,
                           COALESCE(SUM(streak_count), 0) AS streak_count
                    FROM daily_stats GROUP BY streamer
                    """).fetchall()}
            active_watch = self._active_watches(connection, now)
            live_streams = self._live_streams(connection, now)
            last_streak = {row["streamer"]: row["ts"] for row in connection.execute("""
                    SELECT streamer, MAX(ts) AS ts FROM events
                    WHERE type = 'watch_streak' GROUP BY streamer
                    """).fetchall()}
            stream_history = {}
            streak_claims = {}
            if live_streams:
                placeholders = ",".join("?" for _ in live_streams)
                for row in connection.execute(
                    f"""
                    SELECT streamer, broadcast_id, started_at, ended_at, end_reason
                    FROM streams WHERE streamer IN ({placeholders})
                    ORDER BY streamer, started_at, id
                    """,
                    tuple(live_streams),
                ).fetchall():
                    stream_history.setdefault(row["streamer"], []).append(dict(row))
                for row in connection.execute(
                    f"""
                    SELECT streamer, ts, metadata FROM events
                    WHERE type = 'watch_streak' AND streamer IN ({placeholders})
                    """,
                    tuple(live_streams),
                ).fetchall():
                    streak_claims.setdefault(row["streamer"], []).append(
                        (float(row["ts"]), _metadata_broadcast_id(row["metadata"]))
                    )
        rows = [dict(row) for row in rows]
        # A streamer live or watched now belongs in the list even without
        # activity in the window, such as a broadcast that started yesterday.
        listed = {row["streamer"] for row in rows}
        for streamer in sorted((set(live_streams) | set(active_watch)) - listed):
            rows.append(
                {
                    "streamer": streamer,
                    "points_gained": 0,
                    "points_spent": 0,
                    "watch_seconds": 0.0,
                    "watch_sessions": 0,
                    "stream_sessions": 0,
                    "streak_count": 0,
                    "first_seen": None,
                    "last_seen": None,
                }
            )
        result = []
        for item in rows:
            streamer = item["streamer"]
            item["lifetime"] = dict(lifetime.get(streamer, {}))
            item["active_watch"] = active_watch.get(streamer)
            item["live_stream"] = live_streams.get(streamer)
            item["last_streak_at"] = last_streak.get(streamer)
            item["live_streak_at"] = None
            if item["live_stream"] is not None:
                broadcast_ids, _started = _logical_stream_window(
                    item["live_stream"], stream_history.get(streamer)
                )
                claimed = [
                    ts
                    for ts, broadcast_id in streak_claims.get(streamer, ())
                    if broadcast_id in broadcast_ids
                ]
                item["live_streak_at"] = max(claimed) if claimed else None
            observed = [item.get("last_seen")]
            if item["active_watch"] is not None:
                observed.append(item["active_watch"].get("last_seen_at"))
            if item["live_stream"] is not None:
                observed.append(item["live_stream"].get("last_seen_at"))
            observed = [value for value in observed if value is not None]
            if observed:
                item["last_seen"] = max(observed)
            result.append(item)
        return result

    @staticmethod
    def _idle_streamer_summary(connection, streamer, active, live):
        lifetime = connection.execute(
            """
            SELECT ? AS streamer,
                   COALESCE(SUM(points_gained), 0) AS points_gained,
                   COALESCE(SUM(points_spent), 0) AS points_spent,
                   COALESCE(SUM(watch_seconds), 0) AS watch_seconds,
                   COALESCE(SUM(streak_count), 0) AS streak_count
            FROM daily_stats WHERE streamer = ?
            """,
            (streamer, streamer),
        ).fetchone()
        last_streak = connection.execute(
            """
            SELECT MAX(ts) FROM events
            WHERE type = 'watch_streak' AND streamer = ?
            """,
            (streamer,),
        ).fetchone()[0]
        return {
            "streamer": streamer,
            "points_gained": 0,
            "points_spent": 0,
            "watch_seconds": 0.0,
            "watch_sessions": 0,
            "stream_sessions": 0,
            "streak_count": 0,
            "first_seen": None,
            "last_seen": None,
            "lifetime": dict(lifetime),
            "active_watch": active,
            "live_stream": live,
            "last_streak_at": last_streak,
            "live_streak_at": None,
        }

    def streamer_detail(self, streamer, days=90, now=None):
        streamer = clean_streamer(streamer)
        now = float(now or time.time())
        cutoff_day = first_window_day(now, days)
        cutoff = day_start(cutoff_day)
        end = _window_end(now)
        with self._read_connection() as connection:
            daily_rows = connection.execute(
                """
                SELECT * FROM daily_stats
                WHERE streamer = ? AND day >= ? AND day <= ? ORDER BY day
                """,
                (streamer, cutoff_day, utc_day(now)),
            ).fetchall()
            # The newest 100 of each within the same window as the totals
            # (the daily rows carry the full points series).
            watch_rows = connection.execute(
                f"""
                SELECT * FROM watch_sessions
                WHERE streamer = ? AND {_OVERLAPS_WINDOW}
                ORDER BY started_at DESC LIMIT 100
                """,
                (streamer, end, cutoff, cutoff),
            ).fetchall()
            stream_rows = connection.execute(
                f"""
                SELECT * FROM streams WHERE streamer = ? AND {_OVERLAPS_WINDOW}
                ORDER BY started_at DESC LIMIT 100
                """,
                (streamer, end, cutoff, cutoff),
            ).fetchall()
            event_rows = connection.execute(
                """
                SELECT * FROM events WHERE streamer = ? AND ts >= ? AND ts < ?
                ORDER BY ts DESC LIMIT 100
                """,
                (streamer, cutoff, end),
            ).fetchall()
            point_rows = connection.execute(
                """
                SELECT * FROM points WHERE streamer = ? AND ts >= ? AND ts < ?
                ORDER BY ts DESC, id DESC LIMIT 100
                """,
                (streamer, cutoff, end),
            ).fetchall()
            drop_rows = connection.execute(
                """
                SELECT * FROM drops WHERE streamer = ? AND ts >= ? AND ts < ?
                ORDER BY ts DESC, id DESC LIMIT 100
                """,
                (streamer, cutoff, end),
            ).fetchall()
            games = self._game_rows(
                connection, cutoff_day, utc_day(now), cutoff, end, streamer
            )
            # The summary comes from the same snapshot as the rows above.
            summary = next(
                (
                    item
                    for item in self.streamers(
                        days=days, now=now, connection=connection
                    )
                    if item["streamer"] == streamer
                ),
                None,
            )
            active = self._active_watches(connection, now).get(streamer)
            live = self._live_streams(connection, now).get(streamer)
            if summary is None:
                # No activity in this window: zero window totals, but the
                # lifetime totals and last streak still describe the streamer.
                summary = self._idle_streamer_summary(
                    connection, streamer, active, live
                )
        watch_sessions = []
        for row in watch_rows:
            item = dict(row)
            item["active"] = active is not None and active["id"] == item["id"]
            watch_sessions.append(item)
        streams = []
        for row in stream_rows:
            item = dict(row)
            item["active"] = live is not None and live["id"] == item["id"]
            # Measured on the server's clock, as in sessions().
            finished = item["ended_at"]
            if finished is None:
                finished = now if item["active"] else item["last_seen_at"]
            item["duration_seconds"] = max(0.0, finished - item["started_at"])
            streams.append(item)
        return {
            "streamer": streamer,
            "days": int(days),
            "generated_at": now,
            "summary": summary,
            "daily": [dict(row) for row in daily_rows],
            "watch_sessions": watch_sessions,
            "streams": streams,
            "events": [dict(row) for row in event_rows],
            "points": [dict(row) for row in point_rows],
            "drops": [_drop_item(row) for row in drop_rows],
            "games": games,
        }

    def daily(self, days=30, streamer=None, now=None):
        now = float(now or time.time())
        days = max(1, int(days))
        first_day = first_window_day(now, days)
        streamer = clean_streamer(streamer) if streamer else None
        with self._read_connection() as connection:
            if streamer:
                rows = connection.execute(
                    """
                    SELECT * FROM daily_stats
                    WHERE day >= ? AND streamer = ? ORDER BY day
                    """,
                    (first_day, streamer),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT day,
                           COALESCE(SUM(points_gained), 0) AS points_gained,
                           COALESCE(SUM(points_spent), 0) AS points_spent,
                           COALESCE(SUM(watch_seconds), 0) AS watch_seconds,
                           COALESCE(SUM(watch_sessions), 0) AS watch_sessions,
                           COALESCE(SUM(stream_sessions), 0) AS stream_sessions,
                           COALESCE(SUM(streak_count), 0) AS streak_count,
                           MIN(first_seen) AS first_seen,
                           MAX(last_seen) AS last_seen
                    FROM daily_stats WHERE day >= ?
                    GROUP BY day ORDER BY day
                    """,
                    (first_day,),
                ).fetchall()
        by_day = {row["day"]: dict(row) for row in rows}
        result = []
        start = day_start(first_day)
        for offset in range(days):
            day = utc_day(start + (offset * SECONDS_PER_DAY))
            result.append(
                by_day.get(day)
                or {
                    "day": day,
                    "points_gained": 0,
                    "points_spent": 0,
                    "watch_seconds": 0.0,
                    "watch_sessions": 0,
                    "stream_sessions": 0,
                    "streak_count": 0,
                    "first_seen": None,
                    "last_seen": None,
                }
            )
        return result

    def sessions(
        self,
        kind="watch",
        days=30,
        streamer=None,
        limit=100,
        offset=0,
        now=None,
    ):
        now = float(now or time.time())
        cutoff = day_start(first_window_day(now, days))
        end = _window_end(now)
        streamer = clean_streamer(streamer) if streamer else None
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        with self._read_connection() as connection:
            if kind == "stream":
                where = f"{_OVERLAPS_WINDOW} AND (? IS NULL OR streamer = ?)"
                params = (end, cutoff, cutoff, streamer, streamer)
                total = connection.execute(
                    f"SELECT COUNT(*) FROM streams WHERE {where}", params
                ).fetchone()[0]
                rows = connection.execute(
                    f"""
                    SELECT id, streamer, broadcast_id, started_at, ended_at,
                           last_seen_at, title, game
                    FROM streams WHERE {where}
                    ORDER BY started_at DESC LIMIT ? OFFSET ?
                    """,
                    params + (limit, offset),
                ).fetchall()
                live_ids = {
                    item["id"] for item in self._live_streams(connection, now).values()
                }
                items = []
                for row in rows:
                    item = dict(row)
                    item["active"] = item["id"] in live_ids
                    finished = item["ended_at"]
                    if finished is None:
                        finished = now if item["active"] else item["last_seen_at"]
                    item["duration_seconds"] = max(0.0, finished - item["started_at"])
                    items.append(item)
            elif kind == "run":
                total = connection.execute(
                    f"SELECT COUNT(*) FROM runs WHERE {_OVERLAPS_WINDOW}",
                    (end, cutoff, cutoff),
                ).fetchone()[0]
                rows = connection.execute(
                    f"""
                    SELECT * FROM runs WHERE {_OVERLAPS_WINDOW}
                    ORDER BY started_at DESC LIMIT ? OFFSET ?
                    """,
                    (end, cutoff, cutoff, limit, offset),
                ).fetchall()
                live_runs = self._live_runs(connection, now)
                items = []
                for row in rows:
                    item = dict(row)
                    item["active"] = (
                        item["ended_at"] is None and item["id"] in live_runs
                    )
                    finished = item["ended_at"]
                    if finished is None:
                        finished = now if item["active"] else item["last_seen_at"]
                    item["duration_seconds"] = max(0.0, finished - item["started_at"])
                    items.append(item)
            else:
                where = f"{_OVERLAPS_WINDOW} AND (? IS NULL OR streamer = ?)"
                params = (end, cutoff, cutoff, streamer, streamer)
                total = connection.execute(
                    f"SELECT COUNT(*) FROM watch_sessions WHERE {where}", params
                ).fetchone()[0]
                rows = connection.execute(
                    f"""
                    SELECT id, streamer, broadcast_id, reason, started_at,
                           ended_at, last_seen_at, watch_seconds
                    FROM watch_sessions WHERE {where}
                    ORDER BY started_at DESC LIMIT ? OFFSET ?
                    """,
                    params + (limit, offset),
                ).fetchall()
                active_ids = {
                    item["id"]
                    for item in self._active_watches(connection, now).values()
                }
                items = []
                for row in rows:
                    item = dict(row)
                    item["duration_seconds"] = max(
                        0.0, float(item["watch_seconds"] or 0.0)
                    )
                    item["active"] = item["id"] in active_ids
                    items.append(item)
        return {
            "kind": kind,
            "days": int(days),
            "limit": limit,
            "offset": offset,
            "total": int(total or 0),
            "sessions": items,
        }

    def events(
        self,
        event_type=None,
        days=30,
        streamer=None,
        limit=100,
        offset=0,
        now=None,
    ):
        now = float(now or time.time())
        cutoff = day_start(first_window_day(now, days))
        streamer = clean_streamer(streamer) if streamer else None
        event_type = str(event_type).strip().lower() if event_type else None
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        where = (
            "ts >= ? AND ts < ? AND (? IS NULL OR streamer = ?) "
            "AND (? IS NULL OR type = ?)"
        )
        params = (cutoff, _window_end(now), streamer, streamer, event_type, event_type)
        with self._read_connection() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) FROM events WHERE {where}", params
            ).fetchone()[0]
            rows = connection.execute(
                f"""
                SELECT * FROM events WHERE {where}
                ORDER BY ts DESC LIMIT ? OFFSET ?
                """,
                params + (limit, offset),
            ).fetchall()
        return {
            "days": int(days),
            "limit": limit,
            "offset": offset,
            "total": int(total or 0),
            "events": [dict(row) for row in rows],
        }

    def activity(self, days=30, streamer=None, now=None):
        """Return per-streamer, per-day watch and live time for a heatmap."""

        now = float(now or time.time())
        days = max(1, int(days))
        first_day = first_window_day(now, days)
        cutoff = day_start(first_day)
        streamer = clean_streamer(streamer) if streamer else None
        day_list = [
            utc_day(cutoff + (offset * SECONDS_PER_DAY)) for offset in range(days)
        ]
        cells = {}

        def cell(streamer_name, day):
            key = (streamer_name, day)
            if key not in cells:
                cells[key] = {
                    "watch_seconds": 0.0,
                    "live_seconds": 0.0,
                    "points_gained": 0,
                    "streak_count": 0,
                }
            return cells[key]

        with self._read_connection() as connection:
            daily_rows = connection.execute(
                """
                SELECT day, streamer,
                       COALESCE(SUM(watch_seconds), 0) AS watch_seconds,
                       COALESCE(SUM(points_gained), 0) AS points_gained,
                       COALESCE(SUM(streak_count), 0) AS streak_count
                FROM daily_stats
                WHERE day >= ? AND day <= ? AND (? IS NULL OR streamer = ?)
                GROUP BY day, streamer
                """,
                (day_list[0], day_list[-1], streamer, streamer),
            ).fetchall()
            for row in daily_rows:
                item = cell(row["streamer"], row["day"])
                item["watch_seconds"] = float(row["watch_seconds"] or 0.0)
                item["points_gained"] = int(row["points_gained"] or 0)
                item["streak_count"] = int(row["streak_count"] or 0)
            stream_rows = connection.execute(
                """
                SELECT id, streamer, started_at, ended_at, last_seen_at
                FROM streams
                WHERE started_at <= ?
                  -- An open stream may still be live, however old its
                  -- newest record; its end is decided below.
                  AND (ended_at IS NULL OR ended_at >= ?)
                  AND (? IS NULL OR streamer = ?)
                """,
                (now, cutoff, streamer, streamer),
            ).fetchall()
            live_ids = {
                item["id"] for item in self._live_streams(connection, now).values()
            }

        for row in stream_rows:
            start = max(float(row["started_at"]), cutoff)
            finished = row["ended_at"]
            if finished is None:
                finished = now if row["id"] in live_ids else row["last_seen_at"]
            end = min(float(finished), now)
            while start < end:
                day = utc_day(start)
                chunk_end = min(end, day_start(day) + SECONDS_PER_DAY)
                if day in day_list:
                    cell(row["streamer"], day)["live_seconds"] += max(
                        0.0, chunk_end - start
                    )
                start = chunk_end

        names = sorted({key[0] for key in cells})
        return {
            "days": days,
            "day_list": day_list,
            "streamers": [
                {
                    "streamer": name,
                    "days": {
                        day: cells[(name, day)]
                        for day in day_list
                        if (name, day) in cells
                    },
                }
                for name in names
            ],
        }

    # -- drops and games -------------------------------------------------------
    @staticmethod
    def _game_rows(connection, first_day, last_day, start, end, streamer=None):
        """Per-game totals: watch time and points from ``first_day`` to
        ``last_day``, and the claims from ``start`` until ``end``.  Drops
        without a game (most quests) are one row with ``game`` None."""

        games = {}

        def game(name):
            if name not in games:
                games[name] = {
                    "game": name,
                    "watch_seconds": 0.0,
                    "points_gained": 0,
                    "drops": 0,
                    "quests": 0,
                    "streamers": 0,
                    "last_drop_at": None,
                }
            return games[name]

        for row in connection.execute(
            """
            SELECT game,
                   COALESCE(SUM(watch_seconds), 0) AS watch_seconds,
                   COALESCE(SUM(points_gained), 0) AS points_gained,
                   COUNT(DISTINCT CASE WHEN watch_seconds > 0 OR points_gained <> 0
                                       THEN streamer END) AS streamers
            FROM daily_game_stats
            WHERE day >= ? AND day <= ? AND (? IS NULL OR streamer = ?)
            GROUP BY game
            """,
            (first_day, last_day, streamer, streamer),
        ):
            item = game(row["game"])
            item["watch_seconds"] = float(row["watch_seconds"] or 0.0)
            item["points_gained"] = int(row["points_gained"] or 0)
            item["streamers"] = int(row["streamers"] or 0)
        for row in connection.execute(
            """
            SELECT game, COUNT(*) AS drops, COALESCE(SUM(quest), 0) AS quests,
                   MAX(ts) AS last_drop_at
            FROM drops
            WHERE ts >= ? AND ts < ? AND (? IS NULL OR streamer = ?)
            GROUP BY game
            """,
            (start, end, streamer, streamer),
        ):
            item = game(row["game"])
            item["drops"] = int(row["drops"] or 0)
            item["quests"] = int(row["quests"] or 0)
            item["last_drop_at"] = row["last_drop_at"]
        return sorted(
            (
                item
                for item in games.values()
                if item["watch_seconds"] > 0
                or item["points_gained"] != 0
                or item["drops"] > 0
            ),
            key=lambda item: (
                -item["watch_seconds"],
                -item["drops"],
                item["game"] is None,
                (item["game"] or "").lower(),
            ),
        )

    def games(self, days=30, streamer=None, now=None):
        """Watch time, points and drops per game in the window."""

        now = float(now or time.time())
        first_day = first_window_day(now, days)
        streamer = clean_streamer(streamer) if streamer else None
        with self._read_connection() as connection:
            games = self._game_rows(
                connection,
                first_day,
                utc_day(now),
                day_start(first_day),
                _window_end(now),
                streamer,
            )
            since = connection.execute(
                "SELECT value FROM meta WHERE key = ?", (META_GAMES_SINCE,)
            ).fetchone()
        return {
            "days": int(days),
            "games": games,
            "games_since": _float_or_none(since[0]) if since is not None else None,
        }

    def drops(
        self,
        days=30,
        streamer=None,
        game=None,
        limit=100,
        offset=0,
        now=None,
    ):
        """Claimed drops in the window, newest first."""

        now = float(now or time.time())
        cutoff = day_start(first_window_day(now, days))
        streamer = clean_streamer(streamer) if streamer else None
        game = str(game).strip() if game else None
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        where = (
            "ts >= ? AND ts < ? AND (? IS NULL OR streamer = ?) "
            "AND (? IS NULL OR game = ?)"
        )
        params = (cutoff, _window_end(now), streamer, streamer, game, game)
        with self._read_connection() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) FROM drops WHERE {where}", params
            ).fetchone()[0]
            rows = connection.execute(
                f"""
                SELECT * FROM drops WHERE {where}
                ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?
                """,
                params + (limit, offset),
            ).fetchall()
        return {
            "days": int(days),
            "limit": limit,
            "offset": offset,
            "total": int(total or 0),
            "drops": [_drop_item(row) for row in rows],
        }


def _drop_item(row):
    item = dict(row)
    try:
        benefits = json.loads(item.get("benefits") or "[]")
    except (TypeError, ValueError):
        benefits = []
    item["benefits"] = (
        [str(value) for value in benefits if isinstance(value, str) and value]
        if isinstance(benefits, list)
        else []
    )
    item["quest"] = bool(item.get("quest"))
    item.pop("dedupe_key", None)
    return item
