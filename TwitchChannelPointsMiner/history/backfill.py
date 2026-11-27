"""Import the per-streamer analytics JSON into History, once (#403).

History records only what the miner journals.  An account that mined
before History existed still has its points in the analytics JSON
(``analytics/<username>/<streamer>.json``): a ``series`` of balances, one
after each points message, and a few ``annotations`` (watch streaks and
predictions).  This adds what that JSON holds from *before* the journal to
the database:

- Only entries older than the *boundary* are imported: the time of the
  earliest journal record in this database (``journal_first_at``), fixed
  when the import starts.  What came later is in the journal already.
- Only streamers History records (see ``history_eligible``), as the miner
  lists them, and never a streamer that has been deleted from History.
- Each streamer's progress is saved in ``meta`` with the rows it adds, so a
  crash repeats a step with the same result and a finished streamer is not
  read again.  A rebuilt database starts over against its own boundary.
- Work is done in small steps from the History thread, so a large
  analytics folder never holds up startup, the journal import or a delete.
"""

import json
import logging
import math
import os
import time
from contextlib import nullcontext

from TwitchChannelPointsMiner.history.importer import (
    JournalImporter,
    _int,
    _number,
    _text,
)
from TwitchChannelPointsMiner.history.store import (
    _float_or_none,
    clean_streamer,
    earliest_held,
)

logger = logging.getLogger(__name__)

# Entries (series and annotations together) added in one transaction.
BACKFILL_BATCH = 2000
# How long one poll keeps importing before it leaves the rest for the next.
STEP_SECONDS = 1.0

META_JOURNAL_FIRST_AT = "journal_first_at"
META_BOUNDARY = "backfill_boundary"
META_IMPORTED = "backfill_imported"
META_STATE_PREFIX = "backfill:"

# Annotation colours (Streamer.persistent_annotations) -> event type.
# WATCH_STREAK (#45c1ff) is left out: its series entry counts the streak.
ANNOTATION_EVENTS = {
    "#ffe045": "prediction_made",
    "#36b535": "win",
    "#ff4545": "lose",
}
SPENT = "SPENT"
WATCH_STREAK = "WATCH_STREAK"


def reason_code(label):
    """The reason code a series entry's ``z`` was made from ("Watch Streak"
    -> "WATCH_STREAK", "Spent" -> "SPENT")."""

    text = _text(label)
    if text is None:
        return None
    return "_".join(text.upper().split())


class _Retry(Exception):
    """A file that cannot be read now; try again on a later poll."""


class AnalyticsBackfill(object):
    def __init__(
        self,
        store,
        analytics_path,
        importer,
        streamers,
        monotonic=time.monotonic,
        batch_size=BACKFILL_BATCH,
        step_seconds=STEP_SECONDS,
        file_lock=None,
    ):
        """``streamers`` returns the usernames History records, or None
        while the miner has not listed them yet."""

        self.store = store
        self.analytics_path = analytics_path
        self.importer = importer
        self.streamers = streamers
        self.monotonic = monotonic
        self.batch_size = max(1, int(batch_size))
        self.step_seconds = step_seconds
        self._file_lock = file_lock
        self._logged = set()

    # -- state ---------------------------------------------------------------
    @staticmethod
    def _meta(connection, key):
        row = connection.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row is not None else None

    @classmethod
    def _meta_time(cls, connection, key):
        """A time saved in ``meta`` (text), or None."""

        value = _float_or_none(cls._meta(connection, key))
        return value if value is not None and math.isfinite(value) else None

    @staticmethod
    def _set_meta(connection, key, value):
        connection.execute(
            """
            INSERT INTO meta (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )

    @staticmethod
    def _state(raw):
        try:
            state = json.loads(raw) if raw is not None else None
        except ValueError:
            state = None
        if not isinstance(state, dict):
            state = {}
        series = _int(state.get("series"))
        annotations = _int(state.get("annotations"))
        return {
            "series": max(0, series or 0),
            "annotations": max(0, annotations or 0),
            "balance": _int(state.get("balance")),
            "done": state.get("done") is True,
            "skipped": _text(state.get("skipped")),
        }

    def _states(self, connection):
        """``streamer -> progress`` of the streamers the import has seen."""

        return {
            row["key"][len(META_STATE_PREFIX) :]: self._state(row["value"])
            for row in connection.execute(
                "SELECT key, value FROM meta WHERE substr(key, 1, ?) = ?",
                (len(META_STATE_PREFIX), META_STATE_PREFIX),
            )
        }

    def _boundary(self):
        """The saved boundary, saving it first when the import starts; None
        while History holds no journal record."""

        with self.store._read_connection() as connection:
            boundary = self._meta_time(connection, META_BOUNDARY)
            if boundary is not None:
                return boundary
            if (
                connection.execute(
                    "SELECT 1 FROM journal_cursor WHERE id = 1"
                ).fetchone()
                is None
            ):
                return None
        with self.store.transaction() as connection:
            saved = self._meta_time(connection, META_BOUNDARY)
            if saved is not None:
                return saved
            boundary = self._meta_time(connection, META_JOURNAL_FIRST_AT)
            if boundary is None:
                # A database from before journal_first_at was kept: all it
                # holds came from the journal.  Saved, so the rows added
                # here never count as journal records.
                boundary = earliest_held(connection)
                if boundary is None:
                    return None
                self._set_meta(connection, META_JOURNAL_FIRST_AT, repr(float(boundary)))
            self._set_meta(connection, META_BOUNDARY, repr(float(boundary)))
        return boundary

    def _eligible(self):
        try:
            names = self.streamers() if self.streamers is not None else None
        except Exception:
            logger.debug("Unable to list the streamers History records", exc_info=True)
            return None
        if names is None:
            return None
        return sorted({clean_streamer(name) for name in names} - {""})

    def status(self):
        """For /history/health."""

        with self.store._read_connection() as connection:
            boundary = self._meta_time(connection, META_BOUNDARY)
            states = self._states(connection)
            imported = self._meta(connection, META_IMPORTED) == "1"
        eligible = self._eligible()
        return {
            "boundary": boundary,
            "imported": imported,
            "streamers_done": sum(1 for state in states.values() if state["done"]),
            # None until the miner has listed its streamers.
            "streamers_waiting": (
                None
                if eligible is None
                else sum(1 for name in eligible if not states.get(name, {}).get("done"))
            ),
        }

    # -- importing -------------------------------------------------------------
    def poll(self):
        """Import for at most ``step_seconds``.  Returns True when this call
        finished the import for the streamers it knows of and rows were
        imported (retention should then run)."""

        names = self._eligible()
        if not names:
            return False
        boundary = self._boundary()
        if boundary is None:
            return False
        # The analytics data keeps whole seconds: an entry in the boundary's
        # own second may come after the journal's first record.
        cutoff = math.floor(boundary)
        with self.store._read_connection() as connection:
            states = self._states(connection)
            imported = self._meta(connection, META_IMPORTED) == "1"
        pending = [name for name in names if not states.get(name, {}).get("done")]
        if not pending:
            return False
        # Raises when the cutoffs cannot be read: importing without them
        # could bring a deleted streamer back.
        file_deletes = self.importer._load_deletes()
        try:
            files = self._files()
        except _Retry:
            return False
        # Each file is parsed once per poll, however many steps it takes.
        payloads = {}
        deadline = self.monotonic() + self.step_seconds
        added = 0
        for name in pending:
            state = states.get(name) or self._state(None)
            try:
                while not state["done"] and self.monotonic() < deadline:
                    state, rows = self._step(
                        name, state, cutoff, file_deletes, files, payloads
                    )
                    added += rows
            except _Retry:
                pass
            states[name] = state
        # The poll that finishes runs retention, even if its own steps added
        # nothing (the rows came in earlier polls).
        return (imported or added > 0) and all(states[name]["done"] for name in pending)

    def _files(self):
        """``streamer -> file name`` of the analytics JSON files."""

        try:
            names = os.listdir(self.analytics_path)
        except OSError as error:
            raise _Retry() from error
        files = {}
        for name in sorted(names):
            if not name.endswith(".json"):
                continue
            streamer = clean_streamer(name[: -len(".json")])
            if streamer:
                files.setdefault(streamer, name)
        return files

    def _read(self, name, files):
        """The file's JSON; None when there is none, False when it is not
        valid JSON.  Raises ``_Retry`` when it cannot be read now."""

        filename = files.get(name)
        if filename is None:
            return None
        path = os.path.join(self.analytics_path, filename)
        try:
            # The miner replaces the file under this lock; on Windows the
            # replace fails while the file is open here.
            with self._file_lock() if self._file_lock is not None else nullcontext():
                with open(path, "r", encoding="utf-8") as handle:
                    return json.load(handle)
        except FileNotFoundError:
            return None
        except ValueError as error:
            if name not in self._logged:
                self._logged.add(name)
                logger.warning(
                    "Not importing %s into History: it is not valid JSON (%s)",
                    filename,
                    error,
                )
            return False
        except OSError as error:
            if name not in self._logged:
                self._logged.add(name)
                logger.warning(
                    "Unable to read %s for History; retrying: %s", filename, error
                )
            raise _Retry() from error

    def _step(self, name, state, cutoff, file_deletes, files, payloads):
        """One transaction for ``name``: the next batch of entries, or the
        streamer marked done.  Returns ``(new state, rows added)``."""

        if name in file_deletes:
            return self._finish(name, state, "deleted"), 0
        if name not in payloads:
            payloads[name] = self._read(name, files)
        payload = payloads[name]
        if payload is None or payload is False or not isinstance(payload, dict):
            reason = "invalid" if payload is not None else "no file"
            return self._finish(name, state, reason), 0
        series = payload.get("series")
        series = series if isinstance(series, list) else []
        annotations = payload.get("annotations")
        annotations = annotations if isinstance(annotations, list) else []

        state = dict(state)
        budget = self.batch_size
        series_end = min(len(series), state["series"] + budget)
        budget -= max(0, series_end - state["series"])
        annotations_end = min(len(annotations), state["annotations"] + max(0, budget))
        added = 0
        with self.store.transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM deleted_streamers WHERE streamer = ?", (name,)
            ).fetchone():
                state.update(done=True, skipped="deleted")
                self._save_state(connection, name, state)
                return state, 0
            balance = state["balance"]
            for index in range(state["series"], series_end):
                entry = series[index]
                inserted, balance = self._apply_series(
                    connection, name, index, entry, balance, cutoff
                )
                added += inserted
            for index in range(state["annotations"], annotations_end):
                added += self._apply_annotation(
                    connection, name, index, annotations[index], cutoff
                )
            state.update(
                series=series_end, annotations=annotations_end, balance=balance
            )
            if series_end >= len(series) and annotations_end >= len(annotations):
                state["done"] = True
            if added:
                self._set_meta(connection, META_IMPORTED, "1")
            self._save_state(connection, name, state)
        return state, added

    def _finish(self, name, state, reason):
        state = dict(state, done=True, skipped=reason)
        with self.store.transaction() as connection:
            self._save_state(connection, name, state)
        return state

    def _save_state(self, connection, name, state):
        self._set_meta(
            connection,
            META_STATE_PREFIX + name,
            json.dumps(
                {
                    "series": state["series"],
                    "annotations": state["annotations"],
                    "balance": state["balance"],
                    "done": state["done"],
                    "skipped": state.get("skipped"),
                },
                separators=(",", ":"),
            ),
        )

    # -- entries ---------------------------------------------------------------
    @staticmethod
    def _apply_series(connection, name, index, entry, balance, cutoff):
        """Add one series entry; returns ``(rows added, balance after it)``."""

        if not isinstance(entry, dict):
            # No readable balance: the next change can't be worked out.
            return 0, None
        y = _int(entry.get("y"))
        if y is None or y < 0:
            # The next change can't be worked out from an unknown balance.
            return 0, None
        previous, balance = balance, y
        x = _number(entry.get("x"))
        if x is None or x <= 0 or x / 1000.0 >= cutoff:
            return 0, balance
        ts = x / 1000.0
        reason = reason_code(entry.get("z"))
        change = None if previous is None else y - previous
        if reason == SPENT and change is not None and change < 0:
            kind, delta = "spend", change
        elif reason != SPENT and change is not None and change > 0:
            kind, delta = "earn", change
        else:
            kind, delta = "balance", None
        key = f"a:{name}:s:{index}"
        inserted = connection.execute(
            """
            INSERT OR IGNORE INTO points (streamer, ts, balance, delta, reason,
                                          kind, dedupe_key)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (name, ts, y, delta, reason, kind, key),
        ).rowcount
        if inserted <= 0:
            return 0, balance
        streak = reason == WATCH_STREAK
        JournalImporter._touch_daily(
            connection,
            ts,
            name,
            points_gained=delta if kind == "earn" else 0,
            points_spent=-delta if kind == "spend" else 0,
            streak_count=1 if streak else 0,
        )
        if streak:
            connection.execute(
                """
                INSERT OR IGNORE INTO events (streamer, ts, type, label,
                                              points_delta, metadata, dedupe_key)
                VALUES (?, ?, 'watch_streak', ?, ?, ?, ?)
                """,
                (
                    name,
                    ts,
                    f"+{delta} - WATCH_STREAK" if delta is not None else None,
                    delta,
                    json.dumps({"broadcast_id": None}),
                    key,
                ),
            )
        return 1, balance

    @staticmethod
    def _apply_annotation(connection, name, index, entry, cutoff):
        if not isinstance(entry, dict):
            return 0
        x = _number(entry.get("x"))
        if x is None or x <= 0 or x / 1000.0 >= cutoff:
            return 0
        event_type = ANNOTATION_EVENTS.get(
            str(entry.get("borderColor") or "").strip().lower()
        )
        if event_type is None:
            return 0
        label = entry.get("label")
        text = _text(label.get("text")) if isinstance(label, dict) else None
        return max(
            0,
            connection.execute(
                """
                INSERT OR IGNORE INTO events (streamer, ts, type, label,
                                              points_delta, dedupe_key)
                VALUES (?, ?, ?, ?, NULL, ?)
                """,
                (name, x / 1000.0, event_type, text, f"a:{name}:a:{index}"),
            ).rowcount,
        )
