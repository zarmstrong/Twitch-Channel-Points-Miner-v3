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

_lock = threading.Lock()
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
):
    """Start the passive recorder once for this process.

    ``online_streamers`` is an optional callable returning the miner's current
    streamers.  It lets the recorder show the live badge for every online
    streamer the History tab tracks, including across a restart.

    ``drop_streamers`` is an optional callable returning the usernames the
    miner only discovered to farm Drops (category, wildcard-category and badge
    channels).  The History tab only lists configured and follower streamers,
    so those discovery channels are never recorded.

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


def stop_history():
    global _recorder, _store

    with _lock:
        recorder = _recorder
        _recorder = None
        _store = None
    if recorder is None:
        return
    recorder.stop()
    if recorder is not threading.current_thread():
        recorder.join(timeout=10)


def delete_streamer_history(streamer, store=None):
    """Delete one streamer's History rows; returns the per-table counts.

    Goes through the recorder so the delete is serialized with its imports and
    the deleted data is not imported again.  Returns ``None`` while History is
    unavailable.
    """

    recorder = _recorder
    store = get_store() if store is None else store
    if store is None:
        return None
    if recorder is not None and recorder.store is store:
        return recorder.delete_streamer(streamer)
    return store.delete_streamer(streamer)


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
    "delete_streamer_history",
    "get_store",
    "is_recording",
    "start_history",
    "stop_history",
]
