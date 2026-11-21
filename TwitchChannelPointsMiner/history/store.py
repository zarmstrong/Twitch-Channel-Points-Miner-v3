"""SQLite storage for the optional History dashboard tab.

The data lives beside the existing per-streamer analytics JSON, but in its own
database: those files are read-modified-written as a whole for the chart tab,
while history needs indexed range queries and durable incremental writes.

The schema is deliberately forward-only.  ``PRAGMA user_version`` identifies
what has been applied, and ``HISTORY_SCHEMA_VERSION`` is the newest version
this build understands.  Raw event rows are kept for
``DEFAULT_RETENTION_DAYS``; the small ``daily_stats`` rollups are kept forever
so all-time totals remain available after pruning.
"""

import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from TwitchChannelPointsMiner.constants import WATCH_STREAK_STREAM_GAP_SECONDS

logger = logging.getLogger(__name__)

HISTORY_SCHEMA_VERSION = 2
DEFAULT_RETENTION_DAYS = 730
SECONDS_PER_DAY = 24 * 60 * 60

# Watch reasons written for channels the miner only discovered to farm Drops
# (category, wildcard-category and badge campaigns).  The History tab only
# lists configured and follower streamers, so these are not real watches.
DROP_FARM_REASONS = ("drops", "badge")

# Tables holding per-streamer history rows, used by the manual delete and by
# the automatic cleanup of drop-farmed channels.
_STREAMER_TABLES = ("points", "events", "watch_sessions", "streams", "daily_stats")


def utc_day(timestamp):
    """Return the UTC calendar day for ``timestamp`` as ``YYYY-MM-DD``."""

    return time.strftime("%Y-%m-%d", time.gmtime(float(timestamp)))


def day_start(day):
    """Return the UTC epoch second at which ``day`` starts."""

    return datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()


def _clean_streamer(streamer):
    return str(streamer or "").strip().lower()


def first_window_day(now, days):
    """Return the first UTC day of a ``days``-long window ending today.

    ``days=1`` is today only, so every query that takes ``days`` covers the
    same calendar days as the daily chart.
    """

    days = max(1, int(days))
    return utc_day(float(now) - ((days - 1) * SECONDS_PER_DAY))


def _import_source_pattern(streamer):
    """LIKE pattern for a streamer's analytics import sources.

    Usernames may contain ``_``, which LIKE treats as a wildcard, so escape it
    to keep one streamer's delete from matching another's import state.
    """

    escaped = (
        str(streamer).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    return f"analytics:{escaped}.json:%"


def _event_broadcast_id(raw_metadata):
    """Return the broadcast id stored on an event, or ``None`` when absent."""

    if not raw_metadata:
        return None
    try:
        metadata = json.loads(raw_metadata)
    except (TypeError, ValueError):
        return None
    if not isinstance(metadata, dict):
        return None
    broadcast_id = str(metadata.get("broadcast_id") or "").strip()
    return broadcast_id or None


def _logical_stream_window(live_stream, streams):
    """Return the broadcast ids and start time of the current logical stream.

    Twitch treats a broadcast that starts less than 30 minutes after the
    previous one ended as the same stream for watch-streak purposes.  Walk
    backwards through the recorded broadcasts so a streak claimed on any part
    of that chain covers the current broadcast.
    """

    broadcast_id = str(live_stream.get("broadcast_id") or "").strip()
    broadcast_ids = {broadcast_id} if broadcast_id else set()
    try:
        current_started_at = float(live_stream.get("started_at"))
    except (TypeError, ValueError):
        current_started_at = 0.0
    logical_started_at = current_started_at

    for previous in reversed(streams or []):
        previous_broadcast_id = str(previous.get("broadcast_id") or "").strip()
        if not previous_broadcast_id or previous_broadcast_id == broadcast_id:
            continue
        try:
            previous_started_at = float(previous.get("started_at"))
        except (TypeError, ValueError):
            continue
        ended_at = previous.get("ended_at")
        if ended_at is None:
            break
        try:
            previous_ended_at = float(ended_at)
        except (TypeError, ValueError):
            break
        if previous_started_at >= current_started_at:
            continue
        # An inferred end (the previous row was closed when the next broadcast
        # started) gives a zero gap. Do not use it to continue the stream:
        # only an observed offline transition proves the two parts are close.
        gap = current_started_at - previous_ended_at
        if gap <= 0 or gap >= WATCH_STREAK_STREAM_GAP_SECONDS:
            break
        broadcast_ids.add(previous_broadcast_id)
        current_started_at = previous_started_at
        logical_started_at = previous_started_at

    return broadcast_ids, logical_started_at


def _live_streak_at(claims, live_stream, broadcast_ids=None, logical_started_at=None):
    """Return the streak claim covering the current logical stream, if any.

    Streak events written from the streak cache carry the broadcast id, so a
    claim is matched against every broadcast in the current logical stream.
    Older events predating that metadata fall back to a timestamp comparison
    against the start of the logical stream.
    """

    if not claims:
        return None
    if broadcast_ids is None:
        broadcast_id = str(live_stream.get("broadcast_id") or "").strip()
        broadcast_ids = {broadcast_id} if broadcast_id else set()
    if logical_started_at is None:
        try:
            logical_started_at = float(live_stream.get("started_at"))
        except (TypeError, ValueError):
            logical_started_at = None
    matched = None
    for timestamp, claim_broadcast_id in claims:
        if claim_broadcast_id is not None:
            is_current = claim_broadcast_id in broadcast_ids
        else:
            is_current = (
                logical_started_at is not None and timestamp >= logical_started_at
            )
        if is_current:
            matched = timestamp if matched is None else max(matched, timestamp)
    return matched


_SCHEMA_V1 = (
    """
    CREATE TABLE IF NOT EXISTS streams (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        streamer TEXT NOT NULL,
        broadcast_id TEXT NOT NULL,
        started_at REAL NOT NULL,
        ended_at REAL,
        last_seen_at REAL NOT NULL,
        title TEXT,
        game TEXT,
        source TEXT NOT NULL DEFAULT 'observed',
        UNIQUE (streamer, broadcast_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_streams_streamer_started "
    "ON streams (streamer, started_at)",
    "CREATE INDEX IF NOT EXISTS idx_streams_started ON streams (started_at)",
    """
    CREATE TABLE IF NOT EXISTS watch_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        streamer TEXT NOT NULL,
        broadcast_id TEXT,
        reason TEXT NOT NULL DEFAULT 'points',
        started_at REAL NOT NULL,
        ended_at REAL,
        last_seen_at REAL NOT NULL,
        watch_seconds REAL NOT NULL DEFAULT 0,
        source TEXT NOT NULL DEFAULT 'observed'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_watch_sessions_streamer_started "
    "ON watch_sessions (streamer, started_at)",
    "CREATE INDEX IF NOT EXISTS idx_watch_sessions_started "
    "ON watch_sessions (started_at)",
    """
    CREATE TABLE IF NOT EXISTS points (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        streamer TEXT NOT NULL,
        ts REAL NOT NULL,
        balance INTEGER,
        delta INTEGER NOT NULL,
        reason TEXT,
        kind TEXT NOT NULL DEFAULT 'earn',
        dedupe_key TEXT NOT NULL UNIQUE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_points_streamer_ts ON points (streamer, ts)",
    "CREATE INDEX IF NOT EXISTS idx_points_ts ON points (ts)",
    """
    CREATE TABLE IF NOT EXISTS events (
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
    "CREATE INDEX IF NOT EXISTS idx_events_streamer_ts ON events (streamer, ts)",
    "CREATE INDEX IF NOT EXISTS idx_events_type_ts ON events (type, ts)",
    "CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts)",
    """
    CREATE TABLE IF NOT EXISTS daily_stats (
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
    "CREATE INDEX IF NOT EXISTS idx_daily_stats_day ON daily_stats (day)",
    """
    CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at REAL NOT NULL,
        ended_at REAL,
        last_seen_at REAL NOT NULL,
        version TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_runs_started ON runs (started_at)",
    """
    CREATE TABLE IF NOT EXISTS import_state (
        source TEXT PRIMARY KEY,
        mtime REAL,
        size INTEGER,
        last_x REAL,
        last_balance INTEGER
    )
    """,
)

# Version 2 stores how many entries of each append-only analytics list were
# imported (timestamps are whole wall-clock seconds, so they repeat and can go
# backwards), and when a streamer's history was deleted so data
# from before the delete is not imported again.
# It also remembers channels the miner only farmed for Drops, so they stay
# excluded after the miner retires them.
_SCHEMA_V2 = (
    "ALTER TABLE import_state ADD COLUMN consumed INTEGER",
    """
    CREATE TABLE IF NOT EXISTS deleted_streamers (
        streamer TEXT PRIMARY KEY,
        deleted_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS drop_farmed_streamers (
        streamer TEXT PRIMARY KEY,
        seen_at REAL NOT NULL
    )
    """,
)

# import_state rows that are not per-file import cursors.
_RETENTION_WATERMARK_SOURCE = "maintenance:retention_cutoff"
_LEGACY_CLEANUP_SOURCE = "maintenance:legacy_cleanup"

_SCHEMA_MIGRATIONS = {
    1: _SCHEMA_V1,
    2: _SCHEMA_V2,
}


class HistorySchemaError(RuntimeError):
    """The database was written by a newer build than this one."""


class HistoryStore(object):
    """Small, thread-safe SQLite history store.

    Write methods open a short-lived connection and commit immediately.  A
    process-local lock serializes those writes, so the recorder and the
    dashboard delete endpoint never write at the same time.  Read methods open
    a read-only connection, so Flask request threads never share a connection
    with the recorder.  WAL mode lets readers run while the recorder writes.
    """

    def __init__(self, path):
        self.path = os.fspath(path)
        self.ready = False
        self._write_lock = threading.RLock()
        self._wal_warning_logged = False

    # -- lifecycle ---------------------------------------------------------
    @contextmanager
    def _write_connection(self):
        with self._write_lock:
            connection = sqlite3.connect(self.path, timeout=5.0)
            connection.row_factory = sqlite3.Row
            try:
                # Set the busy handler before any statement that may take a
                # lock.  SQLite keeps the journal mode in the database file,
                # so this also restores WAL if the file was recreated.
                connection.execute("PRAGMA busy_timeout=5000")
                connection.execute("PRAGMA synchronous=NORMAL")
                connection.execute("PRAGMA foreign_keys=ON")
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
            except Exception:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
                raise
            finally:
                connection.close()

    @contextmanager
    def _read_connection(self):
        if not self.exists():
            raise FileNotFoundError(self.path)
        uri = Path(self.path).resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA busy_timeout=5000")
            yield connection
        finally:
            connection.close()

    def exists(self):
        return os.path.isfile(self.path)

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
            # Python's sqlite3 does not open a transaction for DDL, so each
            # ALTER/CREATE would commit on its own.  An interrupted migration
            # would then leave the new columns behind an old user_version and
            # fail on every retry.  Run the whole upgrade as one transaction.
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > HISTORY_SCHEMA_VERSION:
                raise HistorySchemaError(
                    f"History database {self.path} uses schema version {version}; "
                    f"this build supports up to {HISTORY_SCHEMA_VERSION}"
                )
            for target_version in range(version + 1, HISTORY_SCHEMA_VERSION + 1):
                for statement in _SCHEMA_MIGRATIONS[target_version]:
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version={target_version}")

    def initialize(self):
        """Create, migrate or rebuild the database, then mark it readable."""

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

        Returns True when the file was moved aside and recreated.  Startup
        handles corruption in :meth:`initialize`; this covers corruption that
        only shows up once History is running.
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

    # -- daily rollups -----------------------------------------------------
    @staticmethod
    def _touch_daily(
        connection,
        streamer,
        timestamp,
        *,
        watch_seconds=0.0,
        watch_session_delta=0,
        stream_session_delta=0,
        points_gained=0,
        points_spent=0,
        streak_delta=0,
    ):
        day = utc_day(timestamp)
        connection.execute(
            """
            INSERT INTO daily_stats (
                day, streamer, watch_seconds, watch_sessions, stream_sessions,
                points_gained, points_spent, streak_count, first_seen, last_seen
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(day, streamer) DO UPDATE SET
                watch_seconds = watch_seconds + excluded.watch_seconds,
                watch_sessions = watch_sessions + excluded.watch_sessions,
                stream_sessions = stream_sessions + excluded.stream_sessions,
                points_gained = points_gained + excluded.points_gained,
                points_spent = points_spent + excluded.points_spent,
                streak_count = streak_count + excluded.streak_count,
                first_seen = MIN(first_seen, excluded.first_seen),
                last_seen = MAX(last_seen, excluded.last_seen)
            """,
            (
                day,
                streamer,
                max(0.0, float(watch_seconds)),
                int(watch_session_delta),
                int(stream_session_delta),
                int(points_gained),
                int(points_spent),
                int(streak_delta),
                float(timestamp),
                float(timestamp),
            ),
        )

    # -- points / events ---------------------------------------------------
    def record_points(
        self,
        streamer,
        timestamp,
        balance,
        delta,
        reason="",
        kind=None,
        dedupe_key=None,
    ):
        """Insert one point balance change.  Returns False for duplicates."""

        streamer = _clean_streamer(streamer)
        if not streamer:
            return False
        timestamp = float(timestamp)
        delta = int(delta)
        balance_value = None if balance is None else int(balance)
        kind = kind or ("spend" if delta < 0 else "earn")
        dedupe_key = dedupe_key or (
            f"points:{streamer}:{timestamp:.3f}:{balance_value}:{delta}"
        )
        with self._write_connection() as connection:
            return self._insert_points(
                connection,
                streamer,
                timestamp,
                balance_value,
                delta,
                reason,
                kind,
                dedupe_key,
            )

    @classmethod
    def _insert_points(
        cls,
        connection,
        streamer,
        timestamp,
        balance_value,
        delta,
        reason,
        kind,
        dedupe_key,
    ):
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO points (
                streamer, ts, balance, delta, reason, kind, dedupe_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                streamer,
                timestamp,
                balance_value,
                delta,
                str(reason or ""),
                kind,
                dedupe_key,
            ),
        )
        if cursor.rowcount <= 0:
            return False
        cls._touch_daily(
            connection,
            streamer,
            timestamp,
            points_gained=max(0, delta),
            points_spent=max(0, -delta),
        )
        return True

    def record_event(
        self,
        streamer,
        timestamp,
        event_type,
        label="",
        points_delta=None,
        metadata=None,
        dedupe_key=None,
        dedupe_window=120.0,
    ):
        """Insert an event.  Streak events are also matched by proximity."""

        streamer = _clean_streamer(streamer)
        if not streamer:
            return False
        timestamp = float(timestamp)
        event_type = str(event_type or "event").strip().lower()
        dedupe_key = dedupe_key or (
            f"event:{streamer}:{timestamp:.3f}:{event_type}:{label}"
        )
        payload = (
            json.dumps(metadata, sort_keys=True, separators=(",", ":"))
            if metadata is not None
            else None
        )
        with self._write_connection() as connection:
            return self._insert_event(
                connection,
                streamer,
                timestamp,
                event_type,
                label,
                points_delta,
                payload,
                dedupe_key,
                dedupe_window,
            )

    @classmethod
    def _insert_event(
        cls,
        connection,
        streamer,
        timestamp,
        event_type,
        label,
        points_delta,
        payload,
        dedupe_key,
        dedupe_window,
    ):
        if event_type == "watch_streak" and dedupe_window:
            existing = connection.execute(
                """
                SELECT 1 FROM events
                WHERE streamer = ? AND type = 'watch_streak'
                  AND ABS(ts - ?) <= ?
                LIMIT 1
                """,
                (streamer, timestamp, float(dedupe_window)),
            ).fetchone()
            if existing is not None:
                return False
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO events (
                streamer, ts, type, label, points_delta, metadata, dedupe_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                streamer,
                timestamp,
                event_type,
                str(label or ""),
                None if points_delta is None else int(points_delta),
                payload,
                dedupe_key,
            ),
        )
        if cursor.rowcount <= 0:
            return False
        cls._touch_daily(
            connection,
            streamer,
            timestamp,
            streak_delta=1 if event_type == "watch_streak" else 0,
        )
        return True

    def import_batch(self, source, state, points=(), events=()):
        """Write one file's imported rows and its cursor in one transaction.

        A retry after a failure part way then starts from the old cursor with
        none of the batch written, instead of finding some rows already there
        and computing deltas against them.  ``points`` items are
        ``(streamer, timestamp, balance, delta, reason, dedupe_key)``;
        ``events`` items are ``(streamer, timestamp, type, label,
        points_delta, dedupe_key)``.  ``state`` holds the
        :meth:`set_import_state` arguments.
        """

        with self._write_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for streamer, timestamp, balance, delta, reason, dedupe_key in points:
                delta = int(delta)
                self._insert_points(
                    connection,
                    _clean_streamer(streamer),
                    float(timestamp),
                    None if balance is None else int(balance),
                    delta,
                    reason,
                    "spend" if delta < 0 else "earn",
                    dedupe_key,
                )
            for streamer, timestamp, event_type, label, delta, dedupe_key in events:
                self._insert_event(
                    connection,
                    _clean_streamer(streamer),
                    float(timestamp),
                    str(event_type or "event").strip().lower(),
                    label,
                    delta,
                    None,
                    dedupe_key,
                    120.0,
                )
            self._upsert_import_state(connection, source, **state)

    # -- streams / watch sessions -----------------------------------------
    def upsert_stream_session(
        self,
        streamer,
        broadcast_id,
        started_at,
        ended_at=None,
        last_seen_at=None,
        title=None,
        game=None,
        source="observed",
    ):
        """Create or update one observed live session.

        A cache file keeps sessions open until the miner notices the channel
        went offline, so an upsert with ``ended_at=None`` must never reopen a
        session that was closed at recorder startup.
        """

        streamer = _clean_streamer(streamer)
        broadcast_id = str(broadcast_id or "").strip()
        if not streamer or not broadcast_id:
            return None
        started_at = float(started_at)
        last_seen_at = float(last_seen_at or ended_at or started_at)
        with self._write_connection() as connection:
            existing = connection.execute(
                "SELECT id FROM streams WHERE streamer = ? AND broadcast_id = ?",
                (streamer, broadcast_id),
            ).fetchone()
            if existing is None:
                # A new broadcast supersedes anything still marked live.
                connection.execute(
                    """
                    UPDATE streams SET ended_at = ?
                    WHERE streamer = ? AND broadcast_id <> ?
                      AND ended_at IS NULL AND started_at <= ?
                    """,
                    (started_at, streamer, broadcast_id, started_at),
                )
                connection.execute(
                    """
                    INSERT INTO streams (
                        streamer, broadcast_id, started_at, ended_at,
                        last_seen_at, title, game, source
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        streamer,
                        broadcast_id,
                        started_at,
                        None if ended_at is None else float(ended_at),
                        last_seen_at,
                        title,
                        game,
                        source,
                    ),
                )
                self._touch_daily(
                    connection,
                    streamer,
                    started_at,
                    stream_session_delta=1,
                )
                return int(
                    connection.execute("SELECT last_insert_rowid()").fetchone()[0]
                )

            connection.execute(
                """
                UPDATE streams SET
                    ended_at = CASE
                        WHEN ? IS NULL THEN streams.ended_at
                        ELSE MAX(COALESCE(streams.ended_at, ?), ?)
                    END,
                    last_seen_at = MAX(streams.last_seen_at, ?),
                    title = COALESCE(?, streams.title),
                    game = COALESCE(?, streams.game),
                    source = ?
                WHERE id = ?
                """,
                (
                    None if ended_at is None else float(ended_at),
                    None if ended_at is None else float(ended_at),
                    None if ended_at is None else float(ended_at),
                    last_seen_at,
                    title,
                    game,
                    source,
                    int(existing["id"]),
                ),
            )
            return int(existing["id"])

    def update_open_stream_details(self, streamer, timestamp, game=None, title=None):
        streamer = _clean_streamer(streamer)
        if not streamer:
            return
        with self._write_connection() as connection:
            connection.execute(
                """
                UPDATE streams SET
                    last_seen_at = MAX(last_seen_at, ?),
                    game = COALESCE(?, game),
                    title = COALESCE(?, title)
                WHERE id = (
                    SELECT id FROM streams
                    WHERE streamer = ? AND ended_at IS NULL
                    ORDER BY started_at DESC LIMIT 1
                )
                """,
                (float(timestamp), game, title, streamer),
            )

    def touch_open_streams(self, timestamp):
        """Keep live sessions' last-observed time fresh for crash estimates."""

        with self._write_connection() as connection:
            connection.execute(
                "UPDATE streams SET last_seen_at = MAX(last_seen_at, ?) "
                "WHERE ended_at IS NULL",
                (float(timestamp),),
            )

    def resume_stream_session(self, streamer, broadcast_id, timestamp):
        """Reopen a broadcast that is live again after a miner restart.

        Startup cleanup closes broadcasts still open in the watch-streak cache
        so a crashed run cannot leave phantom live rows.  Watching that
        broadcast again proves it is live, so the close is reversed for the
        newest broadcast recorded for the streamer.
        """

        streamer = _clean_streamer(streamer)
        broadcast_id = str(broadcast_id or "").strip()
        if not streamer or not broadcast_id:
            return False
        timestamp = float(timestamp)
        with self._write_connection() as connection:
            cursor = connection.execute(
                """
                UPDATE streams SET
                    ended_at = NULL,
                    last_seen_at = MAX(last_seen_at, ?)
                WHERE streamer = ? AND broadcast_id = ?
                  AND ended_at IS NOT NULL
                  AND broadcast_id = (
                      SELECT latest.broadcast_id FROM streams AS latest
                      WHERE latest.streamer = streams.streamer
                      ORDER BY latest.started_at DESC, latest.id DESC
                      LIMIT 1
                  )
                """,
                (timestamp, streamer, broadcast_id),
            )
            return cursor.rowcount > 0

    def open_broadcast(self, streamer):
        streamer = _clean_streamer(streamer)
        if not streamer:
            return None
        with self._read_connection() as connection:
            row = connection.execute(
                """
                SELECT broadcast_id FROM streams
                WHERE streamer = ? AND ended_at IS NULL
                ORDER BY started_at DESC LIMIT 1
                """,
                (streamer,),
            ).fetchone()
        if row is None:
            return None
        return row["broadcast_id"]

    def start_watch_session(
        self,
        streamer,
        reason,
        started_at,
        broadcast_id=None,
        source="observed",
    ):
        streamer = _clean_streamer(streamer)
        if not streamer:
            return None
        started_at = float(started_at)
        with self._write_connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO watch_sessions (
                    streamer, broadcast_id, reason, started_at,
                    last_seen_at, watch_seconds, source
                ) VALUES (?, ?, ?, ?, ?, 0, ?)
                """,
                (
                    streamer,
                    None if broadcast_id in (None, "") else str(broadcast_id),
                    str(reason or "points"),
                    started_at,
                    started_at,
                    source,
                ),
            )
            self._touch_daily(
                connection,
                streamer,
                started_at,
                watch_session_delta=1,
            )
            return int(cursor.lastrowid)

    def tick_watch_session(self, session_id, streamer, timestamp, delta_seconds):
        """Advance one active watch session and its daily rollup."""

        timestamp = float(timestamp)
        delta = max(0.0, float(delta_seconds))
        with self._write_connection() as connection:
            cursor = connection.execute(
                """
                UPDATE watch_sessions SET
                    watch_seconds = watch_seconds + ?,
                    last_seen_at = MAX(last_seen_at, ?)
                WHERE id = ? AND ended_at IS NULL
                """,
                (delta, timestamp, int(session_id)),
            )
            if cursor.rowcount <= 0:
                # The session was deleted or closed; do not count watch time
                # that no session row accounts for.
                return False
            # Split the interval at UTC midnight so each day gets its part.
            cursor = timestamp - delta
            while cursor < timestamp:
                next_midnight = day_start(utc_day(cursor)) + SECONDS_PER_DAY
                chunk_end = min(timestamp, next_midnight)
                self._touch_daily(
                    connection,
                    _clean_streamer(streamer),
                    # The latest moment of this chunk that is still its day.
                    chunk_end if chunk_end < next_midnight else cursor,
                    watch_seconds=chunk_end - cursor,
                )
                cursor = chunk_end
            return True

    def end_watch_session(self, session_id, ended_at):
        with self._write_connection() as connection:
            connection.execute(
                """
                UPDATE watch_sessions SET ended_at = COALESCE(ended_at, ?),
                    last_seen_at = MAX(last_seen_at, ?)
                WHERE id = ?
                """,
                (float(ended_at), float(ended_at), int(session_id)),
            )

    def end_stream_session(self, streamer, broadcast_id, ended_at):
        """Close one live session that is no longer reported online."""

        streamer = _clean_streamer(streamer)
        broadcast_id = str(broadcast_id or "").strip()
        if not streamer or not broadcast_id:
            return False
        with self._write_connection() as connection:
            cursor = connection.execute(
                """
                UPDATE streams SET ended_at = ?,
                    last_seen_at = MAX(last_seen_at, ?)
                WHERE streamer = ? AND broadcast_id = ? AND ended_at IS NULL
                """,
                (float(ended_at), float(ended_at), streamer, broadcast_id),
            )
            return cursor.rowcount > 0

    def close_open_watch_sessions(self, ended_at):
        with self._write_connection() as connection:
            connection.execute("""
                UPDATE watch_sessions
                SET ended_at = COALESCE(last_seen_at, started_at),
                    last_seen_at = COALESCE(last_seen_at, started_at)
                WHERE ended_at IS NULL
                """)

    def close_open_streams(self, ended_at=None):
        """Close sessions left open by a stopped miner.

        ``last_seen_at`` is the last observation the recorder actually made, so
        it is a better end estimate than process startup time.
        """

        with self._write_connection() as connection:
            connection.execute(
                """
                UPDATE streams
                SET ended_at = COALESCE(ended_at, last_seen_at,
                                        started_at, ?)
                WHERE ended_at IS NULL
                """,
                (float(ended_at or time.time()),),
            )

    # -- runs --------------------------------------------------------------
    def start_run(self, version=None, started_at=None):
        started_at = float(started_at or time.time())
        with self._write_connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO runs (started_at, last_seen_at, version)
                VALUES (?, ?, ?)
                """,
                (started_at, started_at, None if version is None else str(version)),
            )
            return int(cursor.lastrowid)

    def heartbeat_run(self, run_id, timestamp=None):
        if run_id is None:
            return
        with self._write_connection() as connection:
            connection.execute(
                "UPDATE runs SET last_seen_at = MAX(last_seen_at, ?) WHERE id = ?",
                (float(timestamp or time.time()), int(run_id)),
            )

    def end_run(self, run_id, ended_at=None):
        if run_id is None:
            return
        with self._write_connection() as connection:
            connection.execute(
                """
                UPDATE runs SET ended_at = COALESCE(ended_at, ?),
                    last_seen_at = MAX(last_seen_at, ?)
                WHERE id = ?
                """,
                (
                    float(ended_at or time.time()),
                    float(ended_at or time.time()),
                    int(run_id),
                ),
            )

    def close_open_runs(self):
        with self._write_connection() as connection:
            connection.execute("""
                UPDATE runs
                SET ended_at = COALESCE(last_seen_at, started_at),
                    last_seen_at = COALESCE(last_seen_at, started_at)
                WHERE ended_at IS NULL
                """)

    def latest_point(self, streamer):
        streamer = _clean_streamer(streamer)
        with self._read_connection() as connection:
            row = connection.execute(
                """
                SELECT ts, balance FROM points WHERE streamer = ?
                ORDER BY ts DESC LIMIT 1
                """,
                (streamer,),
            ).fetchone()
        if row is None:
            return None
        return {
            "ts": float(row["ts"]),
            "balance": None if row["balance"] is None else int(row["balance"]),
        }

    # -- import state ------------------------------------------------------
    def get_import_state(self, source):
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM import_state WHERE source = ?", (source,)
            ).fetchone()
        return dict(row) if row is not None else None

    def watched_streamers(self):
        """Return every streamer the miner has actually watched."""

        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT DISTINCT streamer FROM watch_sessions"
            ).fetchall()
        return {row["streamer"] for row in rows}

    def known_streamers(self):
        """Return every streamer with recorded history.

        Point and watch history can predate the watch-session tables, so this
        is broader than :meth:`watched_streamers`.  Live state for these
        streamers is legitimate: recording points only happens while watching.
        """

        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT DISTINCT streamer FROM daily_stats"
            ).fetchall()
        return {row["streamer"] for row in rows}

    def prune_drop_farmed_streamers(self):
        """Delete history recorded only for Drop-farmed channels.

        Older history builds recorded every category, wildcard and badge
        discovery channel as a watched streamer, which cluttered the Streamers
        list with channels the user never configured.  A streamer is dropped
        only when all of its watch sessions belong to those discovery tiers;
        configured and follower streamers always have a points/streak session,
        so their history is never touched.
        """

        placeholders = ",".join("?" for _ in DROP_FARM_REASONS)
        counts = {table: 0 for table in _STREAMER_TABLES + ("import_state",)}
        with self._write_connection() as connection:
            rows = connection.execute(
                f"""
                SELECT streamer FROM watch_sessions
                GROUP BY streamer
                HAVING SUM(CASE WHEN reason IN ({placeholders}) THEN 0 ELSE 1 END) = 0
                """,
                DROP_FARM_REASONS,
            ).fetchall()
            for row in rows:
                streamer = row["streamer"]
                for table in _STREAMER_TABLES:
                    cursor = connection.execute(
                        f"DELETE FROM {table} WHERE streamer = ?", (streamer,)
                    )
                    counts[table] += max(0, cursor.rowcount)
                cursor = connection.execute(
                    "DELETE FROM import_state WHERE source LIKE ? ESCAPE '\\'",
                    (_import_source_pattern(streamer),),
                )
                counts["import_state"] += max(0, cursor.rowcount)
        return counts

    def run_legacy_cleanup(self):
        """Remove rows older History builds wrote, once per database.

        The checks below look at raw watch sessions, which retention prunes.
        Running them again later would treat a streamer whose old sessions
        were pruned as never watched and drop its kept rollups.
        """

        with self._read_connection() as connection:
            done = connection.execute(
                "SELECT 1 FROM import_state WHERE source = ?",
                (_LEGACY_CLEANUP_SOURCE,),
            ).fetchone()
        if done is not None:
            return False
        self.prune_untracked_streams()
        self.prune_drop_farmed_streamers()
        self.set_import_state(_LEGACY_CLEANUP_SOURCE, mtime=time.time())
        return True

    def prune_untracked_streams(self):
        """Drop live-session data for streamers that were never watched.

        Older history builds recorded live sessions for every monitored
        category/badge channel.  Keep point/watch/streak history intact, but
        remove live-only rows that would otherwise clutter the dashboard.
        """

        with self._write_connection() as connection:
            connection.execute("""
                DELETE FROM streams
                WHERE streamer NOT IN (SELECT streamer FROM watch_sessions)
                """)
            connection.execute("""
                UPDATE daily_stats SET stream_sessions = 0
                WHERE streamer NOT IN (SELECT streamer FROM watch_sessions)
                """)
            connection.execute("""
                DELETE FROM daily_stats
                WHERE streamer NOT IN (SELECT streamer FROM watch_sessions)
                  AND points_gained = 0 AND points_spent = 0
                  AND watch_seconds = 0 AND streak_count = 0
                """)

    def set_import_state(
        self,
        source,
        mtime=None,
        size=None,
        last_x=None,
        last_balance=None,
        consumed=None,
    ):
        with self._write_connection() as connection:
            self._upsert_import_state(
                connection, source, mtime, size, last_x, last_balance, consumed
            )

    @staticmethod
    def _upsert_import_state(
        connection,
        source,
        mtime=None,
        size=None,
        last_x=None,
        last_balance=None,
        consumed=None,
    ):
        connection.execute(
            """
            INSERT INTO import_state (
                source, mtime, size, last_x, last_balance, consumed
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(source) DO UPDATE SET
                mtime = excluded.mtime,
                size = excluded.size,
                last_x = excluded.last_x,
                last_balance = excluded.last_balance,
                consumed = excluded.consumed
            """,
            (
                source,
                None if mtime is None else float(mtime),
                None if size is None else int(size),
                None if last_x is None else float(last_x),
                None if last_balance is None else int(last_balance),
                None if consumed is None else int(consumed),
            ),
        )

    def reset_import_position(self, filename):
        """Read a recreated analytics JSON from the start, keeping balances."""

        with self._write_connection() as connection:
            connection.execute(
                """
                UPDATE import_state SET consumed = 0, last_x = NULL,
                    mtime = NULL, size = NULL
                WHERE source IN (?, ?)
                """,
                (f"analytics:{filename}:series", f"analytics:{filename}:annotations"),
            )

    def deletion_cutoffs(self):
        """Return ``{streamer: deleted_at}`` for streamers deleted on request."""

        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT streamer, deleted_at FROM deleted_streamers"
            ).fetchall()
        return {row["streamer"]: float(row["deleted_at"]) for row in rows}

    # -- maintenance -------------------------------------------------------
    def apply_retention(self, now=None, retention_days=DEFAULT_RETENTION_DAYS):
        """Prune raw rows while keeping the daily rollups forever."""

        if not retention_days or retention_days <= 0:
            return {}
        cutoff = float(now or time.time()) - (float(retention_days) * SECONDS_PER_DAY)
        counts = {}
        with self._write_connection() as connection:
            for name, statement in (
                ("points", "DELETE FROM points WHERE ts < ?"),
                ("events", "DELETE FROM events WHERE ts < ?"),
                (
                    "watch_sessions",
                    """
                    DELETE FROM watch_sessions
                    WHERE ended_at IS NOT NULL AND ended_at < ?
                    """,
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
            # Remember the oldest data ever pruned, so a later, longer
            # retention window does not import it again from the cache and
            # count it twice in the kept rollups.
            connection.execute(
                """
                INSERT INTO import_state (source, last_x) VALUES (?, ?)
                ON CONFLICT(source) DO UPDATE SET
                    last_x = MAX(COALESCE(last_x, excluded.last_x), excluded.last_x)
                """,
                (_RETENTION_WATERMARK_SOURCE, cutoff),
            )
            connection.execute("PRAGMA optimize")
        return counts

    def retention_watermark(self):
        """Return the newest retention cutoff ever applied, or None."""

        state = self.get_import_state(_RETENTION_WATERMARK_SOURCE)
        if state is None or state.get("last_x") is None:
            return None
        return float(state["last_x"])

    def restore_deletion_cutoffs(self, cutoffs):
        """Write deletion cutoffs back, for example after a rebuild."""

        if not cutoffs:
            return
        with self._write_connection() as connection:
            for streamer, deleted_at in cutoffs.items():
                connection.execute(
                    """
                    INSERT INTO deleted_streamers (streamer, deleted_at)
                    VALUES (?, ?)
                    ON CONFLICT(streamer) DO UPDATE SET
                        deleted_at = MAX(deleted_at, excluded.deleted_at)
                    """,
                    (_clean_streamer(streamer), float(deleted_at)),
                )

    def drop_farmed_streamers(self):
        """Return the channels remembered as farmed only for Drops."""

        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT streamer FROM drop_farmed_streamers"
            ).fetchall()
        return {row["streamer"] for row in rows}

    def update_drop_farmed_streamers(self, add=(), remove=(), seen_at=None):
        add = {_clean_streamer(name) for name in add} - {""}
        remove = {_clean_streamer(name) for name in remove} - {""}
        if not add and not remove:
            return
        seen_at = float(seen_at or time.time())
        with self._write_connection() as connection:
            for streamer in add:
                connection.execute(
                    """
                    INSERT INTO drop_farmed_streamers (streamer, seen_at)
                    VALUES (?, ?)
                    ON CONFLICT(streamer) DO UPDATE SET seen_at = excluded.seen_at
                    """,
                    (streamer, seen_at),
                )
            for streamer in remove:
                connection.execute(
                    "DELETE FROM drop_farmed_streamers WHERE streamer = ?",
                    (streamer,),
                )

    # -- queries -----------------------------------------------------------
    def overview(self, days=30, now=None):
        now = float(now or time.time())
        today = utc_day(now)
        cutoff_day = first_window_day(now, days)
        with self._read_connection() as connection:
            window = connection.execute(
                """
                SELECT COALESCE(SUM(points_gained), 0) AS points_gained,
                       COALESCE(SUM(points_spent), 0) AS points_spent,
                       COALESCE(SUM(watch_seconds), 0) AS watch_seconds,
                       COALESCE(SUM(streak_count), 0) AS streak_count,
                       COUNT(DISTINCT streamer) AS streamers
                FROM daily_stats WHERE day >= ?
                """,
                (cutoff_day,),
            ).fetchone()
            today_row = connection.execute(
                """
                SELECT COALESCE(SUM(points_gained), 0) AS points_gained,
                       COALESCE(SUM(points_spent), 0) AS points_spent,
                       COALESCE(SUM(watch_seconds), 0) AS watch_seconds,
                       COALESCE(SUM(streak_count), 0) AS streak_count,
                       COUNT(DISTINCT streamer) AS streamers
                FROM daily_stats WHERE day = ?
                """,
                (today,),
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
            active_watch = connection.execute(
                "SELECT COUNT(*) FROM watch_sessions WHERE ended_at IS NULL"
            ).fetchone()[0]
            live_streams = connection.execute(
                "SELECT COUNT(*) FROM streams WHERE ended_at IS NULL"
            ).fetchone()[0]
            last_point = connection.execute("SELECT MAX(ts) FROM points").fetchone()[0]
            last_watch = connection.execute(
                "SELECT MAX(last_seen_at) FROM watch_sessions"
            ).fetchone()[0]
            current_run = connection.execute("""
                SELECT * FROM runs WHERE ended_at IS NULL
                ORDER BY started_at DESC LIMIT 1
                """).fetchone()
        return {
            "generated_at": now,
            "days": int(days),
            "since_day": cutoff_day,
            "today": dict(today_row),
            "window": dict(window),
            "lifetime": dict(lifetime),
            "active_watch_sessions": int(active_watch or 0),
            "live_streams": int(live_streams or 0),
            "last_point_at": last_point,
            "last_watch_at": last_watch,
            "current_run": dict(current_run) if current_run is not None else None,
        }

    def streamers(self, days=30, now=None):
        now = float(now or time.time())
        cutoff_day = first_window_day(now, days)
        with self._read_connection() as connection:
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
                FROM daily_stats WHERE day >= ?
                GROUP BY streamer ORDER BY last_seen DESC
                """,
                (cutoff_day,),
            ).fetchall()
            lifetime = {row["streamer"]: row for row in connection.execute("""
                    SELECT streamer,
                           COALESCE(SUM(points_gained), 0) AS points_gained,
                           COALESCE(SUM(points_spent), 0) AS points_spent,
                           COALESCE(SUM(watch_seconds), 0) AS watch_seconds,
                           COALESCE(SUM(streak_count), 0) AS streak_count
                    FROM daily_stats GROUP BY streamer
                    """).fetchall()}
            active_watch = {row["streamer"]: dict(row) for row in connection.execute("""
                    SELECT streamer, id, reason, started_at, last_seen_at,
                           watch_seconds, broadcast_id
                    FROM watch_sessions WHERE ended_at IS NULL
                    """).fetchall()}
            live_streams = {row["streamer"]: dict(row) for row in connection.execute("""
                    SELECT streamer, id, broadcast_id, started_at, last_seen_at,
                           title, game
                    FROM streams WHERE ended_at IS NULL
                    """).fetchall()}
            stream_history = {}
            last_streak = {row["streamer"]: row["ts"] for row in connection.execute("""
                    SELECT streamer, MAX(ts) AS ts FROM events
                    WHERE type = 'watch_streak' GROUP BY streamer
                    """).fetchall()}
            streak_claims = {}
            if live_streams:
                placeholders = ",".join("?" for _ in live_streams)
                for row in connection.execute(
                    f"""
                    SELECT streamer, broadcast_id, started_at, ended_at
                    FROM streams
                    WHERE streamer IN ({placeholders})
                    ORDER BY streamer, started_at, id
                    """,
                    tuple(live_streams),
                ).fetchall():
                    stream_history.setdefault(row["streamer"], []).append(dict(row))
                for row in connection.execute(
                    f"""
                    SELECT streamer, ts, metadata FROM events
                    WHERE type = 'watch_streak'
                      AND streamer IN ({placeholders})
                    """,
                    tuple(live_streams),
                ).fetchall():
                    streak_claims.setdefault(row["streamer"], []).append(
                        (float(row["ts"]), _event_broadcast_id(row["metadata"]))
                    )
        result = []
        for row in rows:
            item = dict(row)
            streamer = item["streamer"]
            item["lifetime"] = dict(lifetime.get(streamer, {}))
            item["active_watch"] = active_watch.get(streamer)
            item["live_stream"] = live_streams.get(streamer)
            item["last_streak_at"] = last_streak.get(streamer)
            if item["live_stream"] is not None:
                broadcast_ids, logical_started_at = _logical_stream_window(
                    item["live_stream"], stream_history.get(streamer)
                )
                item["live_streak_at"] = _live_streak_at(
                    streak_claims.get(streamer),
                    item["live_stream"],
                    broadcast_ids=broadcast_ids,
                    logical_started_at=logical_started_at,
                )
            else:
                item["live_streak_at"] = None
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

    def streamer_detail(self, streamer, days=90, now=None):
        streamer = _clean_streamer(streamer)
        now = float(now or time.time())
        cutoff_day = first_window_day(now, days)
        cutoff = day_start(cutoff_day)
        with self._read_connection() as connection:
            daily_rows = connection.execute(
                """
                SELECT * FROM daily_stats
                WHERE streamer = ? AND day >= ? ORDER BY day
                """,
                (streamer, cutoff_day),
            ).fetchall()
            # The newest 100 of each within the same window as the totals.
            watch_rows = connection.execute(
                """
                SELECT * FROM watch_sessions WHERE streamer = ? AND started_at >= ?
                ORDER BY started_at DESC LIMIT 100
                """,
                (streamer, cutoff),
            ).fetchall()
            stream_rows = connection.execute(
                """
                SELECT * FROM streams WHERE streamer = ? AND started_at >= ?
                ORDER BY started_at DESC LIMIT 100
                """,
                (streamer, cutoff),
            ).fetchall()
            event_rows = connection.execute(
                """
                SELECT * FROM events WHERE streamer = ? AND ts >= ?
                ORDER BY ts DESC LIMIT 100
                """,
                (streamer, cutoff),
            ).fetchall()
            point_rows = connection.execute(
                """
                SELECT * FROM points WHERE streamer = ? AND ts >= ?
                ORDER BY ts
                """,
                (streamer, cutoff),
            ).fetchall()
        summary = next(
            (
                item
                for item in self.streamers(days=days, now=now)
                if item["streamer"] == streamer
            ),
            None,
        )
        return {
            "streamer": streamer,
            "days": int(days),
            "summary": summary,
            "daily": [dict(row) for row in daily_rows],
            "watch_sessions": [dict(row) for row in watch_rows],
            "streams": [dict(row) for row in stream_rows],
            "events": [dict(row) for row in event_rows],
            "points": [dict(row) for row in point_rows],
        }

    def daily(self, days=30, streamer=None, now=None):
        now = float(now or time.time())
        days = max(1, int(days))
        first_day = first_window_day(now, days)
        streamer = _clean_streamer(streamer) if streamer else None
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
            item = by_day.get(day) or {
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
            result.append(item)
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
        streamer = _clean_streamer(streamer) if streamer else None
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        with self._read_connection() as connection:
            if kind == "stream":
                where = "started_at >= ? AND (? IS NULL OR streamer = ?)"
                params = (cutoff, streamer, streamer)
                total = connection.execute(
                    f"SELECT COUNT(*) FROM streams WHERE {where}", params
                ).fetchone()[0]
                rows = connection.execute(
                    f"""
                    SELECT id, streamer, broadcast_id, started_at, ended_at,
                           last_seen_at, title, game, source
                    FROM streams WHERE {where}
                    ORDER BY started_at DESC LIMIT ? OFFSET ?
                    """,
                    params + (limit, offset),
                ).fetchall()
                items = []
                for row in rows:
                    item = dict(row)
                    end = item["ended_at"] or now
                    item["duration_seconds"] = max(0.0, end - item["started_at"])
                    item["active"] = item["ended_at"] is None
                    items.append(item)
            elif kind == "run":
                total = connection.execute(
                    "SELECT COUNT(*) FROM runs WHERE started_at >= ?", (cutoff,)
                ).fetchone()[0]
                rows = connection.execute(
                    """
                    SELECT * FROM runs WHERE started_at >= ?
                    ORDER BY started_at DESC LIMIT ? OFFSET ?
                    """,
                    (cutoff, limit, offset),
                ).fetchall()
                items = []
                for row in rows:
                    item = dict(row)
                    end = item["ended_at"] or now
                    item["duration_seconds"] = max(0.0, end - item["started_at"])
                    item["active"] = item["ended_at"] is None
                    items.append(item)
            else:
                where = "started_at >= ? AND (? IS NULL OR streamer = ?)"
                params = (cutoff, streamer, streamer)
                total = connection.execute(
                    f"SELECT COUNT(*) FROM watch_sessions WHERE {where}", params
                ).fetchone()[0]
                rows = connection.execute(
                    f"""
                    SELECT id, streamer, broadcast_id, reason, started_at,
                           ended_at, last_seen_at, watch_seconds, source
                    FROM watch_sessions WHERE {where}
                    ORDER BY started_at DESC LIMIT ? OFFSET ?
                    """,
                    params + (limit, offset),
                ).fetchall()
                items = []
                for row in rows:
                    item = dict(row)
                    item["duration_seconds"] = max(
                        0.0, float(item["watch_seconds"] or 0.0)
                    )
                    item["active"] = item["ended_at"] is None
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
        streamer = _clean_streamer(streamer) if streamer else None
        event_type = str(event_type).strip().lower() if event_type else None
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        where = "ts >= ? AND (? IS NULL OR streamer = ?) AND (? IS NULL OR type = ?)"
        params = (cutoff, streamer, streamer, event_type, event_type)
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
        streamer = _clean_streamer(streamer) if streamer else None
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
            if streamer:
                daily_rows = connection.execute(
                    """
                    SELECT * FROM daily_stats
                    WHERE day >= ? AND streamer = ?
                    """,
                    (day_list[0], streamer),
                ).fetchall()
            else:
                daily_rows = connection.execute(
                    """
                    SELECT day, streamer,
                           COALESCE(SUM(watch_seconds), 0) AS watch_seconds,
                           COALESCE(SUM(points_gained), 0) AS points_gained,
                           COALESCE(SUM(streak_count), 0) AS streak_count
                    FROM daily_stats WHERE day >= ? GROUP BY day, streamer
                    """,
                    (day_list[0],),
                ).fetchall()
            for row in daily_rows:
                item = cell(row["streamer"], row["day"])
                item["watch_seconds"] = float(row["watch_seconds"] or 0.0)
                item["points_gained"] = int(row["points_gained"] or 0)
                item["streak_count"] = int(row["streak_count"] or 0)

            if streamer:
                stream_rows = connection.execute(
                    """
                    SELECT streamer, started_at, ended_at, last_seen_at
                    FROM streams
                    WHERE streamer = ? AND started_at <= ?
                      AND COALESCE(ended_at, last_seen_at, started_at) >= ?
                    """,
                    (streamer, now, cutoff),
                ).fetchall()
            else:
                stream_rows = connection.execute(
                    """
                    SELECT streamer, started_at, ended_at, last_seen_at
                    FROM streams
                    WHERE started_at <= ?
                      AND COALESCE(ended_at, last_seen_at, started_at) >= ?
                    """,
                    (now, cutoff),
                ).fetchall()

        for row in stream_rows:
            start = max(float(row["started_at"]), cutoff)
            end = min(float(row["ended_at"] or now), now)
            while start < end:
                day = utc_day(start)
                next_day = day_start(day) + SECONDS_PER_DAY
                chunk_end = min(end, next_day)
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

    def delete_streamer(self, streamer, deleted_at=None):
        """Delete one streamer's rows and remember when it happened.

        The per-streamer JSON and the watch-streak cache still hold the
        deleted data, so the recorder skips anything older than
        ``deleted_at`` instead of importing it again.  The import cursors are
        kept: they say how far the JSON was read and its last balance, so the
        first change written after the delete still gets the right delta, even
        when the dashboard deleted the JSON and the miner starts a new one.
        """

        streamer = _clean_streamer(streamer)
        if not streamer:
            return {}
        deleted_at = float(deleted_at or time.time())
        counts = {}
        with self._write_connection() as connection:
            for table in _STREAMER_TABLES:
                cursor = connection.execute(
                    f"DELETE FROM {table} WHERE streamer = ?", (streamer,)
                )
                counts[table] = max(0, cursor.rowcount)
            connection.execute(
                """
                INSERT INTO deleted_streamers (streamer, deleted_at)
                VALUES (?, ?)
                ON CONFLICT(streamer) DO UPDATE SET
                    deleted_at = MAX(deleted_at, excluded.deleted_at)
                """,
                (streamer, deleted_at),
            )
        return counts
