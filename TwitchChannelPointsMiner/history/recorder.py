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
"""

import json
import logging
import os
import re
import threading
import time

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
        self._stop_event = threading.Event()
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
        self._drop_farmed = set()
        self._live_streamer_names = set()
        self._streamer_snapshot = None
        self._streamer_state_ready = False

    # -- lifecycle ---------------------------------------------------------
    def stop(self):
        self._stop_event.set()

    def run(self):
        if not self._initialize_store():
            return
        try:
            self._prepare()
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
        except Exception:
            # A locked, corrupt or temporarily unwritable database must pause
            # History, never the miner or its existing analytics.
            logger.warning(
                "History recorder poll failed; will retry",
                exc_info=True,
            )

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
        self.store.prune_untracked_streams()
        # Older builds recorded every drop-farmed channel, but the History tab
        # only lists configured and follower streamers.  Drop those legacy rows
        # before the tracked set is rebuilt from what remains.
        self.store.prune_drop_farmed_streamers()
        # Point/watch history can predate the watch-session tables, so keep
        # live state for every streamer the History tab can display.
        self._tracked_streamers = (
            self.store.watched_streamers() | self.store.known_streamers()
        )
        self._run_id = self.store.start_run(self.version, started_at=now)
        self._next_retention_at = now
        # The cache may carry an open session from an old crash.  Import those
        # as already closed so they cannot become permanent phantom live rows.
        # Watching that streamer again proves the broadcast is live and
        # reopens it.
        self._ingest_streak_cache(now, close_open=True, force=True)
        self._baseline_now_watching()

    def _guarded_shutdown(self):
        try:
            self._shutdown()
        except Exception:
            logger.warning("Unable to finish History shutdown cleanly", exc_info=True)

    def _shutdown(self):
        now = self.clock()
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
            self.store.end_run(self._run_id, ended_at=now)
        except Exception:
            logger.warning("Unable to close the History run", exc_info=True)
        finally:
            self._run_id = None

    def poll_once(self):
        if self._run_id is None:
            return
        now = self.clock()
        self.store.heartbeat_run(self._run_id, now)
        self.store.touch_open_streams(now)
        self._refresh_streamer_state()
        self._ingest_streak_cache(now)
        self._poll_now_watching(now)
        self._ingest_analytics(now)
        self._poll_online_streamers(now)
        if now >= self._next_retention_at:
            self._apply_retention(now)

    def _apply_retention(self, now):
        self._next_retention_at = now + RETENTION_CHECK_SECONDS
        try:
            self.store.apply_retention(now=now, retention_days=self.retention_days)
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
        if streamers is not None:
            self._streamer_snapshot = streamers
            self._live_streamer_names = {
                name for name in (_streamer_name(item) for item in streamers) if name
            }
        drop_streamers = self._provider_value(self.drop_streamers)
        if drop_streamers is not None:
            self._drop_farmed = {
                name
                for name in (_clean_streamer(value) for value in drop_streamers)
                if name
            }
        self._streamer_state_ready = True

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
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0.0

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

        self._now_watching_mtime = mtime
        entries = _load_list(path)
        if entries is None:
            entries = []

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
            self._resume_cached_live_stream(streamer, now)
            game = entry.get("game") or None
            active = self._active_watch.get(streamer)
            if active is not None and active["reason"] == reason:
                self._tick_watch(streamer, now)
            else:
                if active is not None:
                    self._close_watch(streamer, now)
                self._tracked_streamers.add(streamer)
                self._import_cached_sessions(streamer, now)
                session_id = self.store.start_watch_session(
                    streamer,
                    reason,
                    started_at=now,
                    broadcast_id=self.store.open_broadcast(streamer),
                )
                if session_id is not None:
                    self._active_watch[streamer] = {
                        "id": session_id,
                        "reason": reason,
                        "last_tick": now,
                    }
            if game:
                self.store.update_open_stream_details(
                    streamer, timestamp=now, game=game
                )

        for streamer in list(self._active_watch):
            if streamer not in seen:
                self._close_watch(streamer, now)

    def _tick_watch(self, streamer, now):
        active = self._active_watch[streamer]
        delta = max(0.0, float(now) - float(active["last_tick"]))
        delta = min(delta, MAX_WATCH_TICK_SECONDS)
        if delta > 0:
            self.store.tick_watch_session(
                active["id"], streamer, timestamp=now, delta_seconds=delta
            )
        active["last_tick"] = float(now)

    def _close_watch(self, streamer, ended_at):
        active = self._active_watch.pop(streamer, None)
        if active is None:
            return
        self.store.end_watch_session(active["id"], ended_at=ended_at)

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

        entries = self._online_stream_entries(now)
        if not entries:
            return
        current = set()
        for entry in entries:
            streamer = entry["streamer"]
            if streamer in self._drop_farmed:
                continue
            if streamer not in self._tracked_streamers:
                continue
            broadcast_id = entry["broadcast_id"]
            key = (streamer, broadcast_id)
            current.add(key)
            if key in self._online_live_keys:
                continue
            self.store.upsert_stream_session(
                streamer,
                broadcast_id,
                started_at=entry["started_at"],
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
        self._online_live_keys &= current

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
            payload = _read_json(self.streak_cache_path)
        except (OSError, ValueError, TypeError) as error:
            logger.warning(
                "Unable to read history watch-streak cache %s: %s",
                self.streak_cache_path,
                error,
            )
            return
        self._streak_cache_key = cache_key

        if not isinstance(payload, dict):
            return
        sessions = payload.get("sessions")
        if not isinstance(sessions, list):
            return

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

            if parsed[-1]["claimed"] and streamer not in self._drop_farmed:
                # A streak claim proves the streamer was watched, even if this
                # history database predates the watch-session tables.
                self._tracked_streamers.add(streamer)

        self._cached_stream_sessions = parsed
        for item in parsed:
            if item["streamer"] in self._drop_farmed:
                continue
            if item["streamer"] not in self._tracked_streamers:
                continue
            self._store_cached_session(item, now, close_open=close_open)
            self._record_cached_streak(item)

    def _store_cached_session(self, item, now, close_open=False):
        started_at = item["started_at"]
        ended_at = item["ended_at"]
        key = (item["streamer"], item["broadcast_id"])
        if close_open and ended_at is None:
            ended_at = started_at
            self._cache_startup_closed.add(key)
        elif ended_at is None and key in self._cache_startup_closed:
            ended_at = started_at

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

    def _record_cached_streak(self, item):
        if not item.get("claimed") or item.get("claimed_at") is None:
            return
        try:
            claimed_at = float(item["claimed_at"])
        except (TypeError, ValueError):
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
                self._record_cached_streak(item)

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

    def _ingest_analytics(self, now=None):
        if not self._streamer_state_ready:
            self._refresh_streamer_state()
        for filename, path in self._analytics_files():
            streamer = _clean_streamer(filename[: -len(".json")])
            if not streamer:
                continue
            if streamer in self._drop_farmed:
                # Points written for a channel farmed only for Drops do not
                # belong to a configured streamer's history.
                continue
            try:
                stat = os.stat(path)
            except OSError:
                continue

            series_source = f"analytics:{filename}:series"
            annotation_source = f"analytics:{filename}:annotations"
            series_state = self._safe_import_state(series_source)
            annotation_state = self._safe_import_state(annotation_source)
            series_fresh = self._state_matches(series_state, stat)
            annotation_fresh = self._state_matches(annotation_state, stat)
            if series_fresh and annotation_fresh:
                continue

            try:
                payload = _read_json(path)
            except (OSError, ValueError, TypeError) as error:
                logger.warning("Unable to import history from %s: %s", path, error)
                continue
            if not isinstance(payload, dict):
                continue
            self._tracked_streamers.add(streamer)

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

    @staticmethod
    def _state_matches(state, stat):
        return (
            state is not None
            and state.get("mtime") == stat.st_mtime
            and state.get("size") == stat.st_size
        )

    def _safe_import_state(self, source):
        try:
            return self.store.get_import_state(source)
        except Exception:
            return None

    def _ingest_series(self, streamer, entries, source, stat, state):
        if not isinstance(entries, list):
            entries = []
        usable = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            try:
                float(entry["x"])
            except (KeyError, TypeError, ValueError):
                continue
            usable.append(entry)
        usable.sort(key=lambda entry: float(entry["x"]))

        last_x = state.get("last_x") if state else None
        last_balance = state.get("last_balance") if state else None
        if last_balance is None and usable:
            last_balance = self._baseline_balance(
                streamer, float(usable[0]["x"]) / 1000.0
            )

        for entry in usable:
            try:
                x = float(entry["x"])
            except (KeyError, TypeError, ValueError):
                continue
            if last_x is not None and x <= last_x:
                continue
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
                last_x = x
                continue
            delta = balance - int(last_balance)
            if delta != 0:
                self.store.record_points(
                    streamer,
                    timestamp=x / 1000.0,
                    balance=balance,
                    delta=delta,
                    reason=str(entry.get("z") or ""),
                    kind="spend" if delta < 0 else "earn",
                    dedupe_key=f"series:{streamer}:{x:.3f}:{balance}:{delta}",
                )
            last_balance = balance
            last_x = x

        self.store.set_import_state(
            source,
            mtime=stat.st_mtime,
            size=stat.st_size,
            last_x=last_x,
            last_balance=last_balance,
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
        if not isinstance(entries, list):
            entries = []
        usable = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            try:
                float(entry["x"])
            except (KeyError, TypeError, ValueError):
                continue
            usable.append(entry)
        usable.sort(key=lambda entry: float(entry["x"]))

        last_x = state.get("last_x") if state else None
        for entry in usable:
            try:
                x = float(entry["x"])
            except (KeyError, TypeError, ValueError):
                continue
            if last_x is not None and x <= last_x:
                continue
            label = ""
            event_type = "points"
            if isinstance(entry.get("label"), dict):
                label = str(entry["label"].get("text") or "")
                event_type = _annotation_type(label, entry.get("borderColor"))
            self.store.record_event(
                streamer,
                timestamp=x / 1000.0,
                event_type=event_type,
                label=label,
                points_delta=_annotation_points(label),
                dedupe_key=f"annotation:{streamer}:{x:.3f}:{label}",
            )
            last_x = x

        self.store.set_import_state(
            source,
            mtime=stat.st_mtime,
            size=stat.st_size,
            last_x=last_x,
            last_balance=None,
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
    )
    return recorder
