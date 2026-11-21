"""Passive recorder for the History dashboard tab.

The recorder intentionally does not hook into the mining loop.  Everything it
needs is already written by the miner: the per-streamer chart JSON, the
``now_watching`` snapshot, and the account-scoped watch-streak cache.  Polling
those files keeps the feature independent from upstream mining code, which
makes rebasing a fork much less painful.

The optional ``online_streamers`` callable is a read-only snapshot of the
miner's streamers.  The streak cache only reports broadcasts the miner has
already seen, so it cannot tell whether a broadcast survived a restart; the
live snapshot can, and it keeps the live badge accurate for every streamer the
History tab tracks, not just the one currently being watched.

The optional ``drop_streamers`` callable names the channels the miner only
discovered to farm Drops (category, wildcard-category and badge campaigns).
Those watches are not configured streamers, so they are never recorded and
never appear in the Streamers list.

The optional ``streamers_ready`` callable reports whether the miner has
finished loading its streamers.  The analytics server, and so the recorder,
starts before that, when the Drops exclusions above are still empty, so
nothing is imported until it returns true.
"""

import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager, nullcontext

from TwitchChannelPointsMiner.history.store import (
    DEFAULT_RETENTION_DAYS,
    DROP_FARM_REASONS,
    HistoryStore,
)

logger = logging.getLogger(__name__)

ANALYTICS_POLL_SECONDS = 15.0
STALE_WATCH_SECONDS = 180.0
MAX_WATCH_TICK_SECONDS = 300.0
RETENTION_CHECK_SECONDS = 24 * 60 * 60

_ANALYTICS_EXCLUDED_FILES = {
    "now_watching.json",
    "dashboard_prefs.json",
    "drops_by_category.json",
}

_POINTS_PATTERN = re.compile(r"([+-]\d+)")


def _clean_streamer(value):
    return str(value or "").strip().lower()


def _streamer_name(value):
    """Return a normalised username from a dict or miner Streamer object."""

    if isinstance(value, dict):
        name = value.get("username") or value.get("streamer")
    else:
        name = getattr(value, "username", None)
    return _clean_streamer(name)


def _read_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


@contextmanager
def _analytics_file_lock():
    """Hold the lock the miner takes while it rewrites an analytics JSON.

    On Windows the miner's ``os.replace`` fails while another handle has the
    file open, so reading without it could break the miner's own write.
    """

    try:
        from TwitchChannelPointsMiner.classes.Settings import ANALYTICS_FILE_MUTEX
    except Exception:
        ANALYTICS_FILE_MUTEX = nullcontext()
    with ANALYTICS_FILE_MUTEX:
        yield


def _load_list(path):
    try:
        payload = _read_json(path)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError) as error:
        logger.warning("Unable to read history source %s: %s", path, error)
        return None
    if isinstance(payload, list):
        return payload
    return None


_ANNOTATION_COLORS = {
    "#45c1ff": "watch_streak",
    "#ffe045": "prediction_made",
    "#36b535": "win",
    "#ff4545": "lose",
}


def _annotation_type(text, border_color=None):
    color = str(border_color or "").strip().lower()
    if color in _ANNOTATION_COLORS:
        return _ANNOTATION_COLORS[color]
    upper = str(text or "").upper()
    if "WATCH_STREAK" in upper:
        return "watch_streak"
    if "PREDICTION" in upper:
        return "prediction_made"
    if "WIN" in upper:
        return "win"
    if "LOSE" in upper:
        return "lose"
    return "points"


def _annotation_points(text):
    match = _POINTS_PATTERN.search(str(text or ""))
    return int(match.group(1)) if match else None


def _pending_entries(entries, state):
    """Return the analytics entries not imported yet, and the new cursor.

    The per-streamer JSON lists are append-only, so the cursor is how many
    entries were consumed, plus the ``x`` of the last one to notice a file that
    was deleted and started again.  Timestamps are not the cursor: the miner
    writes wall-clock seconds, which repeat within one second and go backwards
    when the host clock is corrected.

    Returns ``(pending, consumed, last_x)`` where ``pending`` holds
    ``(x, occurrence, entry)`` in file order; ``occurrence`` numbers entries
    that share one ``x`` so identical changes within a second stay apart.
    """

    raw = entries if isinstance(entries, list) else []
    valid = []
    seen = {}
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            continue
        try:
            x = float(entry["x"])
        except (KeyError, TypeError, ValueError):
            continue
        seen[x] = seen.get(x, 0) + 1
        valid.append((index, x, seen[x], entry))

    last_x = state.get("last_x") if state else None
    consumed = state.get("consumed") if state else 0
    if state is not None and consumed is None:
        # A version 1 cursor only stored the newest x.
        pending = [item for item in valid if last_x is None or item[1] > last_x]
    else:
        consumed = int(consumed or 0)
        before = [item for item in valid if item[0] < consumed]
        if consumed > len(raw) or (before and before[-1][1] != last_x):
            # The file was deleted and started again (or rewritten); read it
            # from the start.  Dedupe keys and delete cutoffs keep already
            # recorded data from being counted twice.
            consumed = 0
        pending = [item for item in valid if item[0] >= consumed]
    if valid:
        last_x = valid[-1][1]
    return [item[1:] for item in pending], len(raw), last_x


class HistoryRecorder(threading.Thread):
    """Poll existing analytics files and persist durable history rows."""

    def __init__(
        self,
        store,
        analytics_path,
        streak_cache_path,
        username,
        *,
        version=None,
        interval=ANALYTICS_POLL_SECONDS,
        retention_days=DEFAULT_RETENTION_DAYS,
        clock=time.time,
        online_streamers=None,
        drop_streamers=None,
        streamers_ready=None,
        is_drop_streamer=None,
        source_locks=None,
    ):
        super(HistoryRecorder, self).__init__()
        self.daemon = True
        self.name = "History Recorder"
        self.store = store
        self.analytics_path = os.fspath(analytics_path)
        self.streak_cache_path = os.fspath(streak_cache_path)
        self.username = _clean_streamer(username)
        self.version = version
        self.interval = max(1.0, float(interval))
        self.retention_days = retention_days
        self.clock = clock
        self.online_streamers = online_streamers
        self.drop_streamers = drop_streamers
        self.streamers_ready = streamers_ready
        self.is_drop_streamer = is_drop_streamer
        # Locks the miner holds while it replaces now_watching.json
        # ("now_watching") and the watch-streak cache ("streak_cache").  On
        # Windows its os.replace fails while History has the file open.
        self.source_locks = dict(source_locks or {})
        self._stop_event = threading.Event()
        # Held for a whole poll and for a delete, so a delete never interleaves
        # with an import that already read the deleted data.
        self._lock = threading.RLock()
        self._startup_import_done = False
        self._deleted_before = {}
        self._persisted_drop_farmed = set()
        self._retention_watermark = None
        self._prepared_once = False
        self._restore_after_rebuild = None
        self._active_watch = {}
        self._now_watching_initialized = False
        self._now_watching_mtime = 0.0
        self._run_id = None
        self._next_retention_at = 0.0
        self._streak_cache_key = None
        self._tracked_streamers = set()
        self._cached_stream_sessions = []
        self._cache_startup_closed = set()
        self._online_live_keys = set()
        self._online_details = {}
        self._drop_farmed = set()
        self._live_streamer_names = set()
        self._streamer_snapshot = None
        self._streamer_state_ready = False
        self._last_poll_error_at = 0.0

    # -- lifecycle ---------------------------------------------------------
    def stop(self):
        self._stop_event.set()

    def run(self):
        try:
            if not self._start():
                return
            while not self._stop_event.wait(self.interval):
                self._poll_guarded()
        except Exception:
            logger.warning(
                "History recorder stopped after an unexpected error",
                exc_info=True,
            )
        finally:
            self._guarded_shutdown()

    def _poll_guarded(self):
        try:
            self.poll_once()
        except Exception as error:
            if self._recover_from_corruption(error):
                return
            # A locked, corrupt or temporarily unwritable database must pause
            # History, never the miner or its existing analytics.  Keep the
            # warning rate-limited so a persistent failure does not flood the
            # miner log every poll interval.
            now = self.clock()
            if now - self._last_poll_error_at >= 5 * 60:
                self._last_poll_error_at = now
                logger.warning(
                    "History recorder poll failed; will retry",
                    exc_info=True,
                )
            else:
                logger.debug("History recorder poll still failing", exc_info=True)

    def _start(self):
        """Initialize and prepare, retrying both until they succeed.

        Startup cleanup and the first import write to the database too, so a
        lock held past the busy timeout there must be retried like one during
        initialization instead of ending the recorder.
        """

        warned = False
        while True:
            if not self._initialize_store():
                return False
            try:
                with self._lock:
                    self._prepare()
                return True
            except Exception:
                if warned:
                    logger.debug(
                        "History startup is still failing; retrying", exc_info=True
                    )
                else:
                    warned = True
                    logger.warning(
                        "Unable to prepare the History database; will retry",
                        exc_info=True,
                    )
            if self._stop_event.wait(self.interval):
                return False

    def _recover_from_corruption(self, error):
        """Rebuild a database that turned out corrupt while running."""

        if not isinstance(error, sqlite3.DatabaseError) or not (
            HistoryStore._is_corruption(error)
        ):
            return False
        try:
            with self._lock:
                # From here on the old run is gone whatever happens, so the
                # next poll prepares again if any step below fails.  What only
                # lives in memory is written back into the new file then.
                if self._restore_after_rebuild is None:
                    self._restore_after_rebuild = (
                        dict(self._deleted_before),
                        set(self._drop_farmed),
                    )
                self._active_watch.clear()
                self._online_live_keys.clear()
                self._online_details.clear()
                self._cache_startup_closed.clear()
                self._streak_cache_key = None
                self._run_id = None
                self.store.recover_if_corrupt(error)
                self._prepare()
            return True
        except Exception:
            logger.warning("Unable to rebuild the History database", exc_info=True)
            return False

    def _initialize_store(self):
        """Retry initialization so a transient lock does not disable History."""

        warned = False
        while not self._stop_event.is_set():
            try:
                self.store.initialize()
                return True
            except Exception:
                if warned:
                    logger.debug(
                        "History database is still unavailable; retrying",
                        exc_info=True,
                    )
                else:
                    warned = True
                    logger.warning(
                        "History database is unavailable; will retry",
                        exc_info=True,
                    )
                if self._stop_event.wait(self.interval):
                    return False
        return False

    def _prepare(self):
        if not self.store.ready:
            self.store.initialize()
        now = self.clock()
        # A session open in the database belongs to a previous process.  Close
        # it at its last observed time rather than letting it grow forever.
        # If the miner restarted mid-broadcast, the row is reopened once that
        # broadcast is watched again (_resume_cached_live_stream) or reported
        # online by the live snapshot (_poll_online_streamers).
        self.store.close_open_watch_sessions(now)
        self.store.close_open_streams(now)
        self.store.close_open_runs()
        # Older builds recorded live sessions for unwatched channels and every
        # drop-farmed channel, but the History tab only lists configured and
        # follower streamers.  Drop those legacy rows (once per database)
        # before the tracked set is rebuilt from what remains.
        self.store.run_legacy_cleanup()
        # Point/watch history can predate the watch-session tables, so keep
        # live state for every streamer the History tab can display.
        self._tracked_streamers = (
            self.store.watched_streamers() | self.store.known_streamers()
        )
        if self._restore_after_rebuild is not None:
            cutoffs, drop_farmed = self._restore_after_rebuild
            self.store.restore_deletion_cutoffs(cutoffs)
            self.store.update_drop_farmed_streamers(add=drop_farmed)
            self._restore_after_rebuild = None
        self._deleted_before = self.store.deletion_cutoffs()
        self._persisted_drop_farmed = self.store.drop_farmed_streamers()
        self._drop_farmed |= self._persisted_drop_farmed
        self._retention_watermark = self.store.retention_watermark()
        self._next_retention_at = now
        self._baseline_now_watching()
        self._startup_import_done = False
        if self._streamers_loaded():
            self._startup_import(now)
        # Start the run last: a failed step above is retried from the start,
        # and must not leave an extra run row behind each time.
        self._run_id = self.store.start_run(self.version, started_at=now)
        self._prepared_once = True

    def _source_lock(self, name):
        lock = self.source_locks.get(name)
        return nullcontext() if lock is None else lock

    def load_state(self):
        """Load the stored state a delete needs, without starting a run."""

        with self._lock:
            self._deleted_before = self.store.deletion_cutoffs()
            self._persisted_drop_farmed = self.store.drop_farmed_streamers()
            self._drop_farmed |= self._persisted_drop_farmed

    def _streamers_loaded(self):
        if self.streamers_ready is None:
            return True
        try:
            return bool(self.streamers_ready())
        except Exception:
            logger.debug("Unable to read miner startup state", exc_info=True)
            return False

    def _startup_import(self, now):
        # Read the Drops exclusions first, so the startup import below does
        # not record channels the miner only farms for Drops.
        self._refresh_streamer_state()
        # The cache may carry an open session from an old crash.  Import those
        # as already closed so they cannot become permanent phantom live rows.
        # Watching that streamer again proves the broadcast is live and
        # reopens it.
        self._ingest_streak_cache(now, close_open=True, force=True)
        self._startup_import_done = True

    def _guarded_shutdown(self):
        try:
            self._shutdown()
        except Exception:
            logger.warning("Unable to finish History shutdown cleanly", exc_info=True)

    def _shutdown(self):
        now = self.clock()
        if self._run_id is not None:
            try:
                # Take in the miner's last snapshot, which confirms the
                # watches up to when it was written.
                with self._lock:
                    self._poll_now_watching(now)
            except Exception:
                logger.debug("Unable to read the last watch snapshot", exc_info=True)
        for streamer in list(self._active_watch):
            try:
                self._close_watch(streamer, self._active_watch[streamer]["last_tick"])
            except Exception:
                logger.warning(
                    "Unable to close History watch session for %s",
                    streamer,
                    exc_info=True,
                )
        try:
            # Nothing observes these broadcasts any more, and the dashboard
            # can outlive the miner; end them at their last observation.
            self.store.close_open_streams(now)
        except Exception:
            logger.warning("Unable to close History live sessions", exc_info=True)
        self._online_live_keys = set()
        self._online_details = {}
        try:
            self.store.end_run(self._run_id, ended_at=now)
        except Exception:
            logger.warning("Unable to close the History run", exc_info=True)
        finally:
            self._run_id = None

    def poll_once(self):
        with self._lock:
            self._poll_once()

    def _poll_once(self):
        if self._run_id is None:
            if not self._prepared_once:
                return
            # A rebuild after corruption failed part way; prepare again.
            self._prepare()
        now = self.clock()
        self.store.heartbeat_run(self._run_id, now)
        self.store.touch_open_streams(now)
        if not self._startup_import_done:
            if not self._streamers_loaded():
                return
            self._startup_import(now)
        else:
            self._refresh_streamer_state()
            self._ingest_streak_cache(now)
        self._poll_now_watching(now)
        self._ingest_analytics(now)
        self._poll_online_streamers(now)
        if now >= self._next_retention_at:
            self._apply_retention(now)

    # -- deletion ----------------------------------------------------------
    def delete_streamer(self, streamer, remove_source=None):
        """Delete one streamer's history so it is not imported again.

        This waits for a poll in progress, so an import that already read the
        streamer's files finishes before its rows are deleted.  Afterwards
        anything older than the delete is skipped, because the analytics JSON
        and the watch-streak cache may still hold it.

        ``remove_source`` optionally deletes the streamer's analytics JSON.
        It runs while the miner's analytics file lock is held, together with
        a final import of that file, so no change the miner writes in between
        is lost or counted into the next file's first delta.
        """

        streamer = _clean_streamer(streamer)
        with self._lock, _analytics_file_lock():
            if not streamer:
                if remove_source is not None:
                    remove_source()
                return {}
            # Catch up on the streamer's JSON first, so the import cursor kept
            # below holds the last balance; otherwise the first change in a
            # new file gets a delta that includes deleted, never-imported
            # changes.
            for filename, path in self._analytics_files():
                if _clean_streamer(filename[: -len(".json")]) == streamer:
                    if not self._ingest_analytics_file(
                        filename, path, file_locked=True, force=True
                    ):
                        # Its last changes are unknown, so the kept balance
                        # may be stale.  Forget it: the next file's first
                        # entry becomes a new baseline instead of a delta
                        # that could include changes deleted here.
                        self.store.forget_import_balance(filename)
            deleted_at = float(self.clock())
            counts = self.store.delete_streamer(streamer, deleted_at=deleted_at)
            self._deleted_before[streamer] = max(
                deleted_at, self._deleted_before.get(streamer, 0.0)
            )
            # The rows behind this in-memory state are gone.  A watch still in
            # progress starts a new session on the next poll.
            self._active_watch.pop(streamer, None)
            self._tracked_streamers.discard(streamer)
            self._online_live_keys = {
                key for key in self._online_live_keys if key[0] != streamer
            }
            self._cache_startup_closed = {
                key for key in self._cache_startup_closed if key[0] != streamer
            }
            if remove_source is not None:
                remove_source()
                # The miner starts a new file with its next change.  Read
                # that from the start; the kept balance gives its first delta.
                for filename, _path in self._analytics_files_named(streamer):
                    if not os.path.exists(_path):
                        self.store.reset_import_position(filename)
            return counts

    def _deleted_since(self, streamer, timestamp, whole_seconds=False):
        """True when ``timestamp`` predates a delete of this streamer.

        The analytics JSON stores whole seconds (``whole_seconds=True``), so
        an entry in the delete's own second cannot be placed before or after
        it.  The miner also stamps an entry before it takes the file lock, so
        one stamped just before the delete can be written just after it.  The
        whole second is therefore treated as deleted: a change made later in
        that second is not recorded (the running balance still moves past
        it), but deleted data never comes back.
        """

        cutoff = self._deleted_before.get(streamer)
        if cutoff is None:
            return False
        if whole_seconds:
            return float(timestamp) < math.floor(cutoff) + 1
        return float(timestamp) < cutoff

    def _retention_cutoff(self, now):
        """Return the time before which cached data must not be imported.

        That is the current retention window, or the oldest data an earlier,
        shorter window already pruned: its rollups were kept, so importing it
        again would count it twice.
        """

        cutoffs = []
        try:
            days = float(self.retention_days or 0)
        except (TypeError, ValueError):
            days = 0
        if days > 0:
            cutoffs.append(float(now) - (days * 24 * 60 * 60))
        if self._retention_watermark is not None:
            cutoffs.append(self._retention_watermark)
        return max(cutoffs) if cutoffs else None

    def _apply_retention(self, now):
        self._next_retention_at = now + RETENTION_CHECK_SECONDS
        try:
            self.store.apply_retention(now=now, retention_days=self.retention_days)
            self._retention_watermark = self.store.retention_watermark()
        except Exception as error:
            logger.warning("Unable to apply history retention: %s", error)

    # -- miner streamer snapshot -------------------------------------------
    def _refresh_streamer_state(self):
        """Read which streamers are live and which exist only for Drops.

        Both providers are lazy because the miner starts the analytics server
        before it loads its streamers.  A failed provider keeps the previous
        state instead of clearing it, so one bad poll cannot re-enable
        recording for drop-farmed channels.
        """

        streamers = self._provider_value(self.online_streamers)
        drop_streamers = None
        if streamers is not None:
            self._streamer_snapshot = streamers
            self._live_streamer_names = {
                name for name in (_streamer_name(item) for item in streamers) if name
            }
            if self.is_drop_streamer is not None:
                # Classify the same snapshot: reading a second list later
                # could miss a channel retired in between and count it as
                # configured.
                drop_streamers = self._classify_drop_streamers(streamers)
        if drop_streamers is None and self.is_drop_streamer is None:
            drop_streamers = self._provider_value(self.drop_streamers)
        if drop_streamers is not None:
            current = {
                name
                for name in (_clean_streamer(value) for value in drop_streamers)
                if name
            }
            # A channel the miner retires drops out of the provider, but its
            # analytics JSON stays behind.  Remember it as a Drops channel
            # until the miner tracks it for another reason (configured or
            # followed), so that JSON is never imported.
            configured = (
                self._live_streamer_names - current if streamers is not None else set()
            )
            persisted = self._persisted_drop_farmed
            wanted = (persisted | current) - configured
            if wanted != persisted:
                self.store.update_drop_farmed_streamers(
                    add=wanted - persisted, remove=persisted - wanted
                )
                self._persisted_drop_farmed = wanted
            self._drop_farmed = set(wanted)
        self._streamer_state_ready = True

    def _classify_drop_streamers(self, streamers):
        try:
            return [
                _streamer_name(item)
                for item in streamers
                if self.is_drop_streamer(item)
            ]
        except Exception:
            logger.debug("Unable to classify miner streamers", exc_info=True)
            return None

    @staticmethod
    def _provider_value(provider):
        if provider is None:
            return None
        try:
            values = provider()
        except Exception:
            logger.debug("Unable to read miner streamer state", exc_info=True)
            return None
        if values is None:
            return None
        return list(values)

    def _is_drop_farmed(self, streamer, reason=None):
        """True when this watch belongs to an auto-discovered Drops channel."""

        streamer = _clean_streamer(streamer)
        if not streamer:
            return False
        if streamer in self._drop_farmed:
            return True
        if reason is None:
            return False
        if str(reason).strip().lower() not in DROP_FARM_REASONS:
            return False
        # The live snapshot proves a configured/follower streamer; without it
        # the watch reason is the only signal available.
        return streamer not in self._live_streamer_names

    # -- now_watching ------------------------------------------------------
    def _now_watching_path(self):
        return os.path.join(self.analytics_path, "now_watching.json")

    def _baseline_now_watching(self):
        """Ignore the snapshot left behind by a previous process."""

        try:
            self._now_watching_mtime = os.path.getmtime(self._now_watching_path())
        except OSError:
            self._now_watching_mtime = 0.0
        self._now_watching_initialized = True

    def _poll_now_watching(self, now=None):
        now = self.clock() if now is None else float(now)
        if not self._streamer_state_ready:
            self._refresh_streamer_state()
        path = self._now_watching_path()
        # Read the time and the contents under the miner's lock, so both
        # belong to the same snapshot.
        with self._source_lock("now_watching"):
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                mtime = 0.0
            entries = None
            if self._now_watching_initialized and mtime != self._now_watching_mtime:
                entries = _load_list(path)

        if not self._now_watching_initialized:
            self._now_watching_mtime = mtime
            self._now_watching_initialized = True
            return

        if mtime == self._now_watching_mtime:
            if self._active_watch and mtime > 0 and now - mtime > STALE_WATCH_SECONDS:
                for streamer in list(self._active_watch):
                    self._close_watch(
                        streamer, self._active_watch[streamer]["last_tick"]
                    )
            return

        if entries is None:
            entries = []
        # A snapshot written later than the last observation marks when the
        # watch selection changed; credit watch time up to it.
        changed_at = min(max(float(mtime), 0.0), now) if mtime > 0 else now

        seen = set()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            streamer = _clean_streamer(entry.get("username"))
            if not streamer:
                continue
            reason = str(entry.get("reason") or "points").strip().lower()
            if self._is_drop_farmed(streamer, reason):
                # Channels discovered only for category/badge Drops are not
                # configured streamers, so they must not enter the history.
                continue
            seen.add(streamer)
            if streamer not in self._tracked_streamers:
                # First watch: import its cached broadcasts before resuming
                # one, so the watch below is linked to the live broadcast.
                self._tracked_streamers.add(streamer)
                self._import_cached_sessions(streamer, now)
            self._resume_cached_live_stream(streamer, now)
            game = entry.get("game") or None
            broadcast_id = self.store.open_broadcast(streamer)
            active = self._active_watch.get(streamer)
            if (
                active is not None
                and active.get("broadcast_id") is None
                and broadcast_id is not None
                and active["reason"] == reason
            ):
                # The broadcast row appeared after the watch started (for
                # example right after a delete); link the session to it.
                self.store.link_watch_broadcast(active["id"], broadcast_id)
                active["broadcast_id"] = broadcast_id
            if active is not None and self._same_watch(active, reason, broadcast_id):
                self._tick_watch(streamer, now)
            if self._active_watch.get(streamer) is None or not self._same_watch(
                self._active_watch[streamer], reason, broadcast_id
            ):
                if self._active_watch.get(streamer) is not None:
                    self._close_watch(streamer, changed_at, credit=True)
                self._tracked_streamers.add(streamer)
                self._import_cached_sessions(streamer, now)
                # The watch began when the snapshot naming it was written,
                # but not before a delete of this streamer's history.
                started_at = max(
                    changed_at, self._deleted_before.get(streamer, changed_at)
                )
                broadcast_id = self.store.open_broadcast(streamer)
                session_id = self.store.start_watch_session(
                    streamer,
                    reason,
                    started_at=started_at,
                    broadcast_id=broadcast_id,
                )
                if session_id is not None:
                    self._active_watch[streamer] = {
                        "id": session_id,
                        "reason": reason,
                        "broadcast_id": broadcast_id,
                        "last_tick": started_at,
                    }
            if game:
                self.store.update_open_stream_details(
                    streamer, timestamp=now, game=game
                )

        for streamer in list(self._active_watch):
            if streamer not in seen:
                self._close_watch(streamer, changed_at, credit=True)
        # Only now is the snapshot fully recorded; a failed write above leaves
        # the old mtime so the same snapshot is processed again next poll.
        self._now_watching_mtime = mtime

    @staticmethod
    def _same_watch(active, reason, broadcast_id):
        """True while a watch continues in the same session.

        A new broadcast starts a new session even when the watch reason did
        not change.  A broadcast that is no longer open (the miner has not
        noticed the stream ending yet) keeps the session.
        """

        if active["reason"] != reason:
            return False
        current = active.get("broadcast_id")
        return broadcast_id is None or current is None or broadcast_id == current

    def _tick_watch(self, streamer, now):
        active = self._active_watch[streamer]
        delta = max(0.0, float(now) - float(active["last_tick"]))
        delta = min(delta, MAX_WATCH_TICK_SECONDS)
        if delta > 0:
            if not self.store.tick_watch_session(
                active["id"], streamer, timestamp=now, delta_seconds=delta
            ):
                # The session row is gone, so start a new one next poll.
                self._active_watch.pop(streamer, None)
                return
        active["last_tick"] = float(now)

    def _close_watch(self, streamer, ended_at, credit=False):
        active = self._active_watch.get(streamer)
        if active is None:
            return
        if credit and float(ended_at) > float(active["last_tick"]):
            # The watch lasted until the snapshot that ended it was written;
            # count that final interval too.
            self._tick_watch(streamer, ended_at)
            if self._active_watch.get(streamer) is None:
                return
        # Forget the session only once it is closed, so a failed write is
        # retried on the next poll instead of leaving it open in the database.
        self.store.end_watch_session(active["id"], ended_at=ended_at)
        self._active_watch.pop(streamer, None)

    # -- live streamers ----------------------------------------------------
    def _poll_online_streamers(self, now):
        """Mark every online streamer the miner reports as live.

        The streak cache only knows broadcasts the miner has already observed.
        After a restart its open entries are closed on purpose so a crashed run
        cannot leave phantom live rows, which meant only streamers watched
        again in the new process kept a live badge.  The live snapshot proves a
        broadcast is online, so it reopens those rows for every tracked
        streamer - not only the one currently being watched.
        """

        if self._streamer_snapshot is None:
            # No snapshot has been read yet, so absence proves nothing.
            return
        entries = self._online_stream_entries(now)
        current = set()
        previous = {}
        for streamer, broadcast_id in self._online_live_keys:
            previous[streamer] = broadcast_id
        for entry in entries:
            streamer = entry["streamer"]
            if streamer in self._drop_farmed:
                continue
            if streamer not in self._tracked_streamers:
                continue
            broadcast_id = entry["broadcast_id"]
            key = (streamer, broadcast_id)
            current.add(key)
            details = (entry["title"], entry["game"])
            if key in self._online_live_keys:
                if self._online_details.get(key) != details:
                    # Title or category changed during the broadcast.
                    self.store.update_open_stream_details(
                        streamer,
                        timestamp=now,
                        game=entry["game"],
                        title=entry["title"],
                    )
                    self._online_details[key] = details
                continue
            started_at = entry["started_at"]
            if previous.get(streamer) not in (None, broadcast_id):
                # The broadcast changed without the miner seeing the stream go
                # offline, so online_at still belongs to the previous one.
                # The new broadcast is known to exist only from now.
                started_at = float(now)
            cutoff = self._deleted_before.get(streamer)
            if cutoff is not None and started_at < cutoff:
                # History of this broadcast before the delete stays deleted.
                started_at = cutoff
            self.store.upsert_stream_session(
                streamer,
                broadcast_id,
                started_at=started_at,
                last_seen_at=now,
                title=entry["title"],
                game=entry["game"],
                source="online",
            )
            self.store.resume_stream_session(streamer, broadcast_id, timestamp=now)
            # The snapshot proves the broadcast is live, so the startup guard
            # must not close the cached session again.
            self._cache_startup_closed.discard(key)
            self._online_live_keys.add(key)
            self._online_details[key] = details
        # A broadcast this process saw online that is no longer reported, for
        # example because the streamer was removed from the config, would
        # otherwise stay live: nothing else writes its end.
        for streamer, broadcast_id in self._online_live_keys - current:
            self.store.end_stream_session(streamer, broadcast_id, ended_at=now)
        self._online_live_keys &= current
        self._online_details = {
            key: value
            for key, value in self._online_details.items()
            if key in self._online_live_keys
        }

    def _online_stream_entries(self, now):
        if not self._streamer_state_ready:
            self._refresh_streamer_state()
        streamers = self._streamer_snapshot
        if not streamers:
            return []
        entries = []
        for streamer in list(streamers):
            entry = self._online_stream_entry(streamer, now)
            if entry is not None:
                entries.append(entry)
        return entries

    @staticmethod
    def _online_stream_entry(streamer, now):
        if isinstance(streamer, dict):
            username = streamer.get("username") or streamer.get("streamer")
            broadcast_id = streamer.get("broadcast_id")
            started_at = streamer.get("online_at") or streamer.get("started_at")
            game = streamer.get("game")
            title = streamer.get("title")
            is_online = streamer.get("is_online", True)
        else:
            stream = getattr(streamer, "stream", None)
            username = getattr(streamer, "username", None)
            broadcast_id = getattr(stream, "broadcast_id", None)
            started_at = getattr(streamer, "online_at", None)
            game = getattr(stream, "game", None)
            title = getattr(stream, "title", None)
            is_online = getattr(streamer, "is_online", True)
        if is_online is not True:
            return None
        username = _clean_streamer(username)
        broadcast_id = str(broadcast_id or "").strip()
        if not username or not broadcast_id:
            return None
        try:
            started_at = float(started_at or 0)
        except (TypeError, ValueError):
            started_at = 0.0
        if started_at <= 0:
            started_at = float(now)
        if isinstance(game, dict):
            game = game.get("displayName") or game.get("name") or None
        return {
            "streamer": username,
            "broadcast_id": broadcast_id,
            "started_at": started_at,
            "game": str(game) if game else None,
            "title": str(title) if title else None,
        }

    # -- watch-streak cache ------------------------------------------------
    def _ingest_streak_cache(self, now=None, close_open=False, force=False):
        now = self.clock() if now is None else float(now)
        try:
            stat = os.stat(self.streak_cache_path)
        except OSError:
            self._streak_cache_key = None
            return
        cache_key = (stat.st_mtime_ns, stat.st_size)
        if not force and cache_key == self._streak_cache_key:
            return

        try:
            with self._source_lock("streak_cache"):
                payload = _read_json(self.streak_cache_path)
        except (OSError, ValueError, TypeError) as error:
            logger.warning(
                "Unable to read history watch-streak cache %s: %s",
                self.streak_cache_path,
                error,
            )
            return

        if not isinstance(payload, dict):
            self._streak_cache_key = cache_key
            return
        sessions = payload.get("sessions")
        if not isinstance(sessions, list):
            self._streak_cache_key = cache_key
            return

        configured = self._configured_streamers()
        parsed = []
        for item in sessions:
            if not isinstance(item, dict):
                continue
            if _clean_streamer(item.get("account_name")) != self.username:
                continue
            streamer = _clean_streamer(item.get("streamer_login"))
            broadcast_id = str(item.get("broadcast_id") or "").strip()
            try:
                started_at = float(item["started_at"])
            except (KeyError, TypeError, ValueError):
                continue
            ended_at = item.get("ended_at")
            try:
                ended_at = float(ended_at) if ended_at is not None else None
            except (TypeError, ValueError):
                ended_at = None
            if not streamer or not broadcast_id:
                continue
            parsed.append(
                {
                    "streamer": streamer,
                    "broadcast_id": broadcast_id,
                    "started_at": started_at,
                    "ended_at": ended_at,
                    "claimed": bool(item.get("claimed", False)),
                    "claimed_at": item.get("claimed_at"),
                }
            )

            if (
                parsed[-1]["claimed"]
                and streamer not in self._drop_farmed
                and (configured is None or streamer in configured)
            ):
                # A streak claim proves the streamer was watched, even if this
                # history database predates the watch-session tables.
                self._tracked_streamers.add(streamer)

        self._cached_stream_sessions = parsed
        if close_open:
            # Every broadcast open in the cache at startup may be long over,
            # including those of streamers not tracked yet: one could become
            # tracked through the analytics backfill and must then still be
            # imported as closed.
            for item in parsed:
                if item["ended_at"] is None:
                    self._cache_startup_closed.add(
                        (item["streamer"], item["broadcast_id"])
                    )
        for item in parsed:
            if item["streamer"] in self._drop_farmed:
                continue
            if item["streamer"] not in self._tracked_streamers:
                continue
            self._store_cached_session(item, now, close_open=close_open)
            self._record_cached_streak(item, now)
        # Remember the file only once it is imported, so a failed write is
        # retried on the next poll even if the cache does not change again.
        self._streak_cache_key = cache_key

    def _store_cached_session(self, item, now, close_open=False):
        started_at = item["started_at"]
        ended_at = item["ended_at"]
        key = (item["streamer"], item["broadcast_id"])
        if self._deleted_since(item["streamer"], started_at):
            return
        if close_open and ended_at is None:
            ended_at = started_at
            self._cache_startup_closed.add(key)
        elif ended_at is None and key in self._cache_startup_closed:
            ended_at = started_at
        cutoff = self._retention_cutoff(now)
        if cutoff is not None and ended_at is not None and ended_at < cutoff:
            # Retention already pruned this row (or will); importing it again
            # would count it a second time in the permanent daily rollups.
            return

        self.store.upsert_stream_session(
            item["streamer"],
            item["broadcast_id"],
            started_at=started_at,
            ended_at=ended_at,
            last_seen_at=now if ended_at is None else ended_at,
            source="streak_cache",
        )
        if ended_at is not None:
            # Let the next live snapshot reopen the row when the miner still
            # reports the broadcast as online.
            self._online_live_keys.discard(key)

    def _record_cached_streak(self, item, now):
        if not item.get("claimed") or item.get("claimed_at") is None:
            return
        try:
            claimed_at = float(item["claimed_at"])
        except (TypeError, ValueError):
            return
        if self._deleted_since(item["streamer"], claimed_at):
            return
        cutoff = self._retention_cutoff(now)
        if cutoff is not None and claimed_at < cutoff:
            # See _store_cached_session: the pruned event must not be counted
            # again in the rollups.
            return
        self.store.record_event(
            item["streamer"],
            claimed_at,
            "watch_streak",
            label="Watch streak claimed",
            metadata={
                "broadcast_id": item["broadcast_id"],
                "started_at": item["started_at"],
            },
            dedupe_key=f"streak:{item['streamer']}:{item['broadcast_id']}",
            dedupe_window=120.0,
        )

    def _import_cached_sessions(self, streamer, now):
        """Backfill live sessions once a streamer is first watched."""

        for item in self._cached_stream_sessions:
            if item["streamer"] == streamer:
                self._store_cached_session(item, now, close_open=False)
                self._record_cached_streak(item, now)

    def _resume_cached_live_stream(self, streamer, now):
        """Reopen the broadcast a restarted miner is watching again.

        Sessions still open in the streak cache are closed when the recorder
        starts, because the miner may have died long ago.  Watching the
        streamer again proves the cached broadcast is still live, so the row
        is reopened instead of staying closed until the next broadcast.
        """

        session = self._open_cached_session(streamer)
        if session is None:
            return
        key = (session["streamer"], session["broadcast_id"])
        if key not in self._cache_startup_closed:
            return
        if self.store.resume_stream_session(
            session["streamer"], session["broadcast_id"], timestamp=now
        ):
            self._cache_startup_closed.discard(key)

    def _open_cached_session(self, streamer):
        sessions = [
            item
            for item in self._cached_stream_sessions
            if item["streamer"] == streamer and item["ended_at"] is None
        ]
        if not sessions:
            return None
        return max(sessions, key=lambda item: item["started_at"])

    # -- per-streamer analytics JSON --------------------------------------
    def _analytics_files_named(self, streamer):
        """Analytics JSON names for one streamer, present or not."""

        names = {f"{streamer}.json"}
        names.update(
            name
            for name, _path in self._analytics_files()
            if _clean_streamer(name[: -len(".json")]) == streamer
        )
        return [(name, os.path.join(self.analytics_path, name)) for name in names]

    def _analytics_files(self):
        try:
            names = sorted(os.listdir(self.analytics_path))
        except OSError:
            return []
        result = []
        for name in names:
            if not name.endswith(".json") or name in _ANALYTICS_EXCLUDED_FILES:
                continue
            path = os.path.join(self.analytics_path, name)
            if os.path.isfile(path):
                result.append((name, path))
        return result

    def _configured_streamers(self):
        """Streamers the miner tracks for their own sake, or None if unknown.

        That is every streamer in the live snapshot that is not tracked only
        for Drops.  Without a snapshot provider (tests, older callers) there
        is nothing to compare with, so nothing is filtered.
        """

        if self.online_streamers is None or self._streamer_snapshot is None:
            return None
        return self._live_streamer_names - self._drop_farmed

    def _ingest_analytics(self, now=None):
        if not self._streamer_state_ready:
            self._refresh_streamer_state()
        for filename, path in self._analytics_files():
            self._ingest_analytics_file(filename, path)

    def _ingest_analytics_file(self, filename, path, file_locked=False, force=False):
        """Import one analytics JSON; False when it could not be read.

        ``force`` skips the tracking filters below; a delete uses it so the
        kept cursor has the file's last balance even for a channel History
        does not record.
        """

        streamer = _clean_streamer(filename[: -len(".json")])
        if not streamer:
            return True
        if force:
            pass
        elif streamer in self._drop_farmed:
            # Points written for a channel farmed only for Drops do not
            # belong to a configured streamer's history.
            return True
        configured = None if force else self._configured_streamers()
        if (
            configured is not None
            and streamer not in configured
            and streamer not in self._tracked_streamers
        ):
            # Leftover JSON from a channel History never tracked, for example
            # one farmed for Drops before History was enabled, would otherwise
            # be imported as a configured streamer's history.  A streamer
            # removed from the config keeps importing what it earned before.
            return True
        try:
            stat = os.stat(path)
        except OSError:
            return True

        series_source = f"analytics:{filename}:series"
        annotation_source = f"analytics:{filename}:annotations"
        # A failed read raises and the poll is retried.  Treating it as
        # "never imported" would replay the whole file, and rows that
        # retention already pruned would be counted again in the rollups.
        series_state = self.store.get_import_state(series_source)
        annotation_state = self.store.get_import_state(annotation_source)
        series_fresh = self._state_matches(series_state, stat)
        annotation_fresh = self._state_matches(annotation_state, stat)
        if series_fresh and annotation_fresh:
            return True

        try:
            with nullcontext() if file_locked else _analytics_file_lock():
                payload = _read_json(path)
        except (OSError, ValueError, TypeError) as error:
            logger.warning("Unable to import history from %s: %s", path, error)
            return False
        if not isinstance(payload, dict):
            return False
        if streamer not in self._tracked_streamers:
            self._tracked_streamers.add(streamer)
            # The cache was read before this streamer was tracked; import its
            # cached broadcasts now instead of waiting for the cache to change.
            self._import_cached_sessions(streamer, self.clock())

        if not series_fresh:
            self._ingest_series(
                streamer,
                payload.get("series"),
                series_source,
                stat,
                series_state,
            )
        if not annotation_fresh:
            self._ingest_annotations(
                streamer,
                payload.get("annotations"),
                annotation_source,
                stat,
                annotation_state,
            )
        return True

    @staticmethod
    def _state_matches(state, stat):
        return (
            state is not None
            and state.get("mtime") == stat.st_mtime
            and state.get("size") == stat.st_size
        )

    def _ingest_series(self, streamer, entries, source, stat, state):
        pending, consumed, last_x = _pending_entries(entries, state)
        last_balance = state.get("last_balance") if state else None
        if last_balance is None and pending:
            last_balance = self._baseline_balance(streamer, pending[0][0] / 1000.0)

        rows = []
        for x, occurrence, entry in pending:
            balance = entry.get("y")
            if balance is None:
                continue
            try:
                balance = int(balance)
            except (TypeError, ValueError):
                continue
            if last_balance is None:
                # First observation is the baseline; there is no delta yet.
                last_balance = balance
                continue
            delta = balance - int(last_balance)
            # Entries from before a delete still move the running balance, so
            # the first change after the delete gets the right delta.
            if delta != 0 and not self._deleted_since(
                streamer, x / 1000.0, whole_seconds=True
            ):
                rows.append(
                    (
                        streamer,
                        x / 1000.0,
                        balance,
                        delta,
                        str(entry.get("z") or ""),
                        # The occurrence keeps identical changes within one
                        # second apart (1000 -> 1050 -> 1000 -> 1050).
                        f"series:{streamer}:{x:.3f}:{occurrence}:{balance}:{delta}",
                    )
                )
            last_balance = balance

        # Rows and cursor are written together, so a failure leaves neither.
        self.store.import_batch(
            source,
            {
                "mtime": stat.st_mtime,
                "size": stat.st_size,
                "last_x": last_x,
                "last_balance": last_balance,
                "consumed": consumed,
            },
            points=rows,
        )

    def _baseline_balance(self, streamer, first_timestamp):
        """Use the stored balance only when it predates the import window."""

        latest = self.store.latest_point(streamer)
        if latest is None or latest["balance"] is None:
            return None
        if latest["ts"] <= first_timestamp:
            return latest["balance"]
        return None

    def _ingest_annotations(self, streamer, entries, source, stat, state):
        pending, consumed, last_x = _pending_entries(entries, state)
        rows = []
        for x, occurrence, entry in pending:
            if self._deleted_since(streamer, x / 1000.0, whole_seconds=True):
                continue
            label = ""
            event_type = "points"
            if isinstance(entry.get("label"), dict):
                label = str(entry["label"].get("text") or "")
                event_type = _annotation_type(label, entry.get("borderColor"))
            rows.append(
                (
                    streamer,
                    x / 1000.0,
                    event_type,
                    label,
                    _annotation_points(label),
                    f"annotation:{streamer}:{x:.3f}:{occurrence}:{label}",
                )
            )

        self.store.import_batch(
            source,
            {
                "mtime": stat.st_mtime,
                "size": stat.st_size,
                "last_x": last_x,
                "last_balance": None,
                "consumed": consumed,
            },
            events=rows,
        )


def build_recorder(
    analytics_path,
    streak_cache_path,
    username,
    *,
    version=None,
    retention_days=DEFAULT_RETENTION_DAYS,
    interval=ANALYTICS_POLL_SECONDS,
    online_streamers=None,
    drop_streamers=None,
    streamers_ready=None,
    is_drop_streamer=None,
    source_locks=None,
):
    store = HistoryStore(os.path.join(analytics_path, "history.sqlite3"))
    recorder = HistoryRecorder(
        store,
        analytics_path=analytics_path,
        streak_cache_path=streak_cache_path,
        username=username,
        version=version,
        interval=interval,
        retention_days=retention_days,
        online_streamers=online_streamers,
        drop_streamers=drop_streamers,
        streamers_ready=streamers_ready,
        is_drop_streamer=is_drop_streamer,
        source_locks=source_locks,
    )
    return recorder
