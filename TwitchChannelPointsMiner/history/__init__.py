"""Optional History dashboard tab.

Two halves:

- The miner writes a journal of what it does (``journal.py``):
  ``start_journal()`` before its workers start, ``stop_journal()`` after they
  stop.  Writing never raises into the miner.
- History (``recorder.py``) imports the journal into SQLite in its own thread:
  ``start_history()`` when the analytics server starts, ``stop_history()`` at
  shutdown.  The analytics server serves it through ``web.py``.

Both are safe no-ops when History cannot start, so the rest of the miner is
never affected by a History problem.  Loading this package imports only the
journal: the SQLite half loads when History starts, so a Python build
without ``sqlite3`` still mines.
"""

import atexit
import logging
import os
import threading
import time

from TwitchChannelPointsMiner.history import journal as _journal
from TwitchChannelPointsMiner.history.journal import JOURNAL_FOLDER

logger = logging.getLogger(__name__)

# Raw History rows are kept this many days by default (0 keeps them forever).
DEFAULT_RETENTION_DAYS = 730


class HistoryUnavailableError(RuntimeError):
    """History is starting and cannot delete anything yet."""


class HistoryRefusedError(HistoryUnavailableError):
    """History is off for this run: its database was written by a newer
    version of the miner."""


# Reentrant: a signal handler that ends the miner can call stop_history while
# the same thread is already inside it.
_lock = threading.RLock()
_recorder = None
_atexit_registered = False
# Lists the usernames History records (set by the miner once it has them);
# the analytics JSON import (backfill.py) waits for it.
_streamer_source = None


def _retention_days(value, source):
    """Return a valid retention window, or ``None`` for an invalid value."""

    try:
        if isinstance(value, bool):
            raise ValueError(value)
        days = int(value)
        if days < 0:
            raise ValueError(value)
    except (TypeError, ValueError):
        logger.warning(
            "Ignoring invalid %s=%r; using the History retention default",
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


def _analytics_path():
    from TwitchChannelPointsMiner.classes.Settings import Settings

    if getattr(Settings, "enable_analytics", False) is not True:
        return None
    return getattr(Settings, "analytics_path", None) or None


def start_journal():
    """Start this process's journal run; call before the workers start."""

    try:
        _journal.JOURNAL.allow_spends()
        analytics_path = _analytics_path()
        if analytics_path is None:
            return False
        return _journal.JOURNAL.start(
            os.path.join(analytics_path, JOURNAL_FOLDER), version=_miner_version()
        )
    except Exception:
        logger.warning("Unable to start the History journal", exc_info=True)
        return False


def stop_journal():
    """End the journal run (``run_end``); call after the workers stopped."""

    try:
        _journal.JOURNAL.stop()
    except Exception:
        logger.warning("Unable to stop the History journal", exc_info=True)


def set_history_streamers(source):
    """Tell History which streamers it records: ``source()`` returns their
    usernames.  The analytics JSON import imports only those."""

    global _streamer_source
    _streamer_source = source


def history_streamers():
    """The usernames History records, or None until the miner listed them."""

    source = _streamer_source
    if source is None:
        return None
    names = source()
    return list(names) if names is not None else None


def start_history(retention_days=None, import_analytics=True):
    """Start the History importer once for this process.

    ``retention_days`` is the configured raw-row retention window; the
    ``TCPM_HISTORY_RETENTION_DAYS`` environment variable overrides it.
    ``import_analytics`` imports the analytics JSON from before the journal
    once (``backfill.py``).  Returns the recorder, or ``None`` when History
    is unavailable.
    """

    global _recorder, _atexit_registered

    with _lock:
        if _recorder is not None and (
            _recorder.is_alive() or getattr(_recorder, "refused", None)
        ):
            # A recorder that refused a database written by a newer version
            # of the miner keeps History off for the rest of this process.
            return _recorder
        try:
            from TwitchChannelPointsMiner.history.recorder import build_recorder

            analytics_path = _analytics_path()
            if analytics_path is None:
                return None
            recorder = build_recorder(
                analytics_path,
                retention_days=_history_retention_days(retention_days),
                import_analytics=import_analytics is not False,
                streamers=history_streamers,
            )
            recorder.start()
            _recorder = recorder
            if not _atexit_registered:
                atexit.register(stop_history)
                _atexit_registered = True
            logger.info(
                "History started (database: %s)",
                recorder.store.path,
            )
            return recorder
        except Exception:
            logger.warning("Unable to start the optional History tab", exc_info=True)
            _recorder = None
            return None


def stop_history(timeout=10):
    """Stop importing after one last import, keeping History readable.

    Returns False when the recorder is still finishing after ``timeout``
    seconds.  The analytics server can outlive the miner (the desktop app
    keeps its window open), so the store stays available and deletes still
    go through the stopped recorder.
    """

    with _lock:
        recorder = _recorder
    if recorder is None or recorder is threading.current_thread():
        return True
    if recorder.ident is not None and not recorder.is_alive():
        # Already stopped: take in anything written since.
        drainer = threading.Thread(
            target=recorder.drain, name="HistoryDrain", daemon=True
        )
        drainer.start()
        drainer.join(timeout=timeout)
        return not drainer.is_alive()
    recorder.stop()
    if recorder.ident is not None:
        recorder.join(timeout=timeout)
        if recorder.is_alive():
            return False
    return True


def delete_streamer_history(
    streamer, store=None, database_path=None, remove_source=None
):
    """Delete one streamer's History rows; returns the per-table counts.

    Goes through the recorder so the delete is serialized with its imports.
    When History is not running in this process, ``database_path`` (the
    account's database, if any) is cleared directly; ``None`` is returned
    when there is nothing to clear.  ``remove_source`` optionally deletes the
    streamer's analytics JSON once its History is cleared, under the miner's
    analytics file lock; it is not called when clearing History fails.
    """

    recorder = _recorder
    if recorder is not None and (store is None or recorder.store is store):
        return recorder.delete_streamer(streamer, remove_source=remove_source)
    if store is None and not (database_path and os.path.isfile(database_path)):
        # No database to clear, but the journal may already hold records
        # for this streamer, which a later import must skip.
        folder = None
        if database_path:
            folder = os.path.join(
                os.path.dirname(os.path.abspath(database_path)), JOURNAL_FOLDER
            )
        with _analytics_file_lock():
            if folder is not None and os.path.isdir(folder):
                _save_cutoff_without_database(streamer, folder)
            if remove_source is not None:
                remove_source()
        return None

    from TwitchChannelPointsMiner.history.recorder import HistoryRecorder
    from TwitchChannelPointsMiner.history.store import HistoryStore

    if store is None:
        store = HistoryStore(database_path)
    standalone = HistoryRecorder(
        store,
        os.path.join(os.path.dirname(os.path.abspath(store.path)), JOURNAL_FOLDER),
    )
    return standalone.delete_streamer(streamer, remove_source=remove_source)


def _analytics_file_lock():
    try:
        from TwitchChannelPointsMiner.classes.Settings import ANALYTICS_FILE_MUTEX
    except Exception:
        from contextlib import nullcontext

        return nullcontext()
    return ANALYTICS_FILE_MUTEX


def _save_cutoff_without_database(streamer, folder):
    """Save a delete cutoff for a journal History has no database for yet;
    raises ``OSError`` when the cutoff is unknown or cannot be saved."""

    streamer = _journal._clean(streamer)
    if not streamer:
        return
    journal = _journal.JOURNAL
    writing_here = False
    if journal.running and journal.folder is not None:
        try:
            writing_here = os.path.samefile(journal.folder, folder)
        except OSError:
            writing_here = False
    deleted_at = time.time()

    def save(position):
        _journal.save_delete_cutoff(
            folder, streamer, position[0], position[1], deleted_at
        )

    position = journal.delete_cutoff(streamer, save) if writing_here else None
    if position is None:
        save(_journal.journal_end(folder))
    if writing_here:
        journal.forget(streamer)


def is_recording():
    """True while the History thread is running with its database open."""

    recorder = _recorder
    return bool(recorder is not None and recorder.is_alive() and recorder.store.ready)


def import_status():
    """The analytics JSON import's state for /history/health, or None."""

    recorder = _recorder
    backfill = getattr(recorder, "backfill", None)
    if backfill is None:
        return None
    return backfill.status()


def unavailable_reason():
    """Why History is off for this run (its database was written by a newer
    version of the miner), or None."""

    recorder = _recorder
    return getattr(recorder, "refused", None) if recorder is not None else None


def get_store():
    """Return the initialized store, or None while History is unavailable."""

    recorder = _recorder
    if recorder is None or not recorder.store.ready:
        return None
    return recorder.store


__all__ = [
    "DEFAULT_RETENTION_DAYS",
    "HistoryRefusedError",
    "HistoryUnavailableError",
    "delete_streamer_history",
    "get_store",
    "history_streamers",
    "import_status",
    "is_recording",
    "set_history_streamers",
    "start_history",
    "start_journal",
    "stop_history",
    "stop_journal",
    "unavailable_reason",
]
