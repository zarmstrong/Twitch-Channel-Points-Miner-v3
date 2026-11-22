"""Optional History dashboard tab.

The feature is intentionally self-contained: the miner calls
``start_history(username)`` once when analytics starts, and the analytics
server registers the blueprint in ``web.py``.  Both entry points are safe
no-ops when the recorder cannot start, so the rest of the miner is never
affected by a history problem.
"""

import atexit
import logging
import os
import threading

from TwitchChannelPointsMiner.history.store import DEFAULT_RETENTION_DAYS

logger = logging.getLogger(__name__)

# Reentrant: a signal handler that ends the miner can call stop_history while
# the same thread is already inside it.
_lock = threading.RLock()
_recorder = None
_store = None
_atexit_registered = False


def _retention_days(value, source):
    """Return a valid retention window, or ``None`` for an invalid value."""

    if isinstance(value, bool):
        logger.warning(
            "Ignoring invalid %s=%r; using the History retention default",
            source,
            value,
        )
        return None
    try:
        days = int(value)
    except (TypeError, ValueError):
        logger.warning(
            "Ignoring invalid %s=%r; using the History retention default",
            source,
            value,
        )
        return None
    if days < 0:
        logger.warning(
            "Ignoring negative %s=%r; using the History retention default",
            source,
            value,
        )
        return None
    return days


def _history_retention_days(configured=None):
    raw = os.environ.get("TCPM_HISTORY_RETENTION_DAYS")
    if raw not in (None, ""):
        days = _retention_days(raw, "TCPM_HISTORY_RETENTION_DAYS")
        if days is not None:
            return days
    if configured is not None:
        days = _retention_days(configured, "history_retention_days")
        if days is not None:
            return days
    return DEFAULT_RETENTION_DAYS


def _miner_version():
    try:
        from TwitchChannelPointsMiner import __version__

        return __version__
    except Exception:
        return None


def _resolve_paths(username):
    from TwitchChannelPointsMiner.classes.Settings import Settings

    analytics_path = getattr(Settings, "analytics_path", None)
    if not analytics_path:
        analytics_path = os.path.join(
            os.path.abspath(os.getcwd()), "analytics", str(username)
        )
    cache_path = os.path.join(
        os.path.abspath(os.getcwd()),
        "logs",
        ".state",
        f"watch_streak_cache.{str(username).lower()}.json",
    )
    return analytics_path, cache_path


def start_history(
    username,
    online_streamers=None,
    drop_streamers=None,
    retention_days=None,
    streamers_ready=None,
    is_drop_streamer=None,
    source_locks=None,
):
    """Start the passive recorder once for this process.

    ``online_streamers`` is an optional callable returning the miner's current
    streamers.  It lets the recorder show the live badge for every online
    streamer the History tab tracks, including across a restart.

    ``drop_streamers`` is an optional callable returning the usernames the
    miner only discovered to farm Drops (category, wildcard-category and badge
    channels).  The History tab only lists configured and follower streamers,
    so those discovery channels are never recorded.

    ``is_drop_streamer`` optionally classifies one ``online_streamers`` item
    as tracked only for Drops.  It replaces ``drop_streamers`` and reads the
    same snapshot, so a channel retired between two reads is not mistaken for
    a configured one.

    ``source_locks`` optionally maps ``"now_watching"`` and
    ``"streak_cache"`` to the locks the miner holds while it replaces those
    files, so History never has them open during a replace.

    ``streamers_ready`` is an optional callable that returns true once the
    miner has loaded its streamers; nothing is imported before then.

    ``retention_days`` is the configured raw-row retention window.  The
    ``TCPM_HISTORY_RETENTION_DAYS`` environment variable overrides it.

    Returns the recorder, or ``None`` when history is unavailable.  Callers
    must treat failure as non-fatal.
    """

    global _recorder, _store, _atexit_registered

    with _lock:
        if _recorder is not None and _recorder.is_alive():
            return _recorder
        try:
            from TwitchChannelPointsMiner.history.recorder import build_recorder

            analytics_path, cache_path = _resolve_paths(username)
            recorder = build_recorder(
                analytics_path,
                cache_path,
                username,
                version=_miner_version(),
                retention_days=_history_retention_days(retention_days),
                online_streamers=online_streamers,
                drop_streamers=drop_streamers,
                streamers_ready=streamers_ready,
                is_drop_streamer=is_drop_streamer,
                source_locks=source_locks,
            )
            _store = recorder.store
            recorder.start()
            _recorder = recorder
            if not _atexit_registered:
                atexit.register(stop_history)
                _atexit_registered = True
            logger.info(
                "History recorder started for account '%s' (database: %s)",
                username,
                recorder.store.path,
            )
            return recorder
        except Exception:
            logger.warning(
                "Unable to start the optional History dashboard recorder",
                exc_info=True,
            )
            _recorder = None
            _store = None
            return None


def stop_history(timeout=10):
    """Stop recording, keeping the stored History readable and deletable.

    Returns False when the recorder is still finishing after ``timeout``
    seconds (a long import or a slow disk).

    The analytics server can outlive the miner (the desktop app keeps its
    window open), so the store stays available and ``recording`` reports
    false.  The stopped recorder is kept too: deletes still go through it,
    so they wait for a poll that is finishing and catch up on the JSON.
    """

    with _lock:
        recorder = _recorder
    if recorder is None:
        return True
    already_stopped = recorder.ident is not None and not recorder.is_alive()
    recorder.stop()
    if recorder.ident is not None and recorder is not threading.current_thread():
        recorder.join(timeout=timeout)
        if recorder.is_alive():
            return False
        if already_stopped:
            # Stopped earlier (the Windows shell stops it when the runner
            # returns, before the workers finish); take in what they wrote
            # since, within the same timeout.
            drainer = threading.Thread(
                target=_drain, args=(recorder,), name="HistoryDrain", daemon=True
            )
            drainer.start()
            drainer.join(timeout=timeout)
            if drainer.is_alive():
                return False
    return True


def _drain(recorder):
    try:
        recorder.drain()
    except Exception:
        logger.warning("Unable to import the last History changes", exc_info=True)


class HistoryUnavailableError(RuntimeError):
    """History is starting and cannot delete anything yet."""


def delete_streamer_history(
    streamer, store=None, database_path=None, remove_source=None
):
    """Delete one streamer's History rows; returns the per-table counts.

    Goes through the recorder so the delete is serialized with its imports and
    the deleted data is not imported again.  Raises
    :class:`HistoryUnavailableError` while the recorder is still starting, so
    callers can refuse the delete instead of leaving History behind.  When
    History is not running in this process at all, ``database_path`` (the
    account's database, if any) is cleared directly; ``None`` is returned when
    there is nothing to clear.

    ``remove_source`` optionally deletes the streamer's analytics JSON once
    its History is cleared, while the miner's analytics file lock is held.
    It is not called when clearing History fails.
    """

    from TwitchChannelPointsMiner.history.recorder import _analytics_file_lock

    recorder = _recorder
    store = get_store() if store is None else store
    if store is not None and recorder is not None and recorder.store is store:
        return recorder.delete_streamer(streamer, remove_source=remove_source)
    if store is None and recorder is not None and recorder.is_alive():
        raise HistoryUnavailableError("History is still starting")
    if store is None and database_path and os.path.isfile(database_path):
        from TwitchChannelPointsMiner.history.store import HistoryStore

        store = HistoryStore(database_path)
        store.initialize()
    if store is None:
        with _analytics_file_lock():
            if remove_source is not None:
                remove_source()
        return None
    # No recorder owns this store.  A recorder that is never started still
    # does the delete properly: it imports the JSON's pending changes first,
    # so the kept balance is right for the next file.
    from TwitchChannelPointsMiner.history.recorder import HistoryRecorder

    standalone = HistoryRecorder(
        store,
        analytics_path=os.path.dirname(os.path.abspath(store.path)),
        streak_cache_path=os.devnull,
        username="",
    )
    standalone.load_state()
    return standalone.delete_streamer(streamer, remove_source=remove_source)


def history_watching_stopped():
    """Tell History the miner stopped watching streams.

    The miner stops its watch worker first and History only after its other
    workers, so this keeps shutdown from crediting watch time in between.
    """

    recorder = _recorder
    if recorder is not None:
        recorder.watching_stopped()


def is_recording():
    """True while the recorder thread is running and has started its run."""

    recorder = _recorder
    return bool(
        recorder is not None
        and recorder.is_alive()
        and getattr(recorder, "_run_id", None) is not None
    )


def get_store():
    """Return the initialized store, or None while history is unavailable."""

    store = _store
    if store is None or not store.ready:
        return None
    return store


__all__ = [
    "DEFAULT_RETENTION_DAYS",
    "HistoryUnavailableError",
    "delete_streamer_history",
    "get_store",
    "history_watching_stopped",
    "is_recording",
    "start_history",
    "stop_history",
]
