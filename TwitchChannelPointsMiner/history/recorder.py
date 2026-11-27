"""The History thread: imports the journal into the database every few
seconds and prunes old raw rows once a day."""

import logging
import os
import threading
import time
from contextlib import contextmanager, nullcontext

from TwitchChannelPointsMiner.history import (
    DEFAULT_RETENTION_DAYS,
    HistoryUnavailableError,
)
from TwitchChannelPointsMiner.history import journal as journal_module
from TwitchChannelPointsMiner.history.importer import JournalImporter
from TwitchChannelPointsMiner.history.store import HistoryStore, clean_streamer

logger = logging.getLogger(__name__)

POLL_SECONDS = 10
RETENTION_INTERVAL_SECONDS = 24 * 60 * 60
_RETRY_SECONDS = 30
# A failure that repeats logs its traceback at most this often.
WARNING_INTERVAL_SECONDS = 60.0


@contextmanager
def analytics_file_lock():
    """The lock the miner holds while it rewrites an analytics JSON file."""

    try:
        from TwitchChannelPointsMiner.classes.Settings import ANALYTICS_FILE_MUTEX
    except Exception:
        ANALYTICS_FILE_MUTEX = nullcontext()
    with ANALYTICS_FILE_MUTEX:
        yield


class HistoryRecorder(threading.Thread):
    def __init__(
        self,
        store,
        journal_folder,
        interval=POLL_SECONDS,
        retention_days=DEFAULT_RETENTION_DAYS,
        journal=None,
        clock=time.time,
        monotonic=time.monotonic,
    ):
        super(HistoryRecorder, self).__init__(name="History Recorder", daemon=True)
        self.store = store
        self.journal_folder = journal_folder
        self.interval = interval
        self.retention_days = retention_days
        self._journal = journal
        self.clock = clock
        self.monotonic = monotonic
        self.importer = JournalImporter(store, journal_folder, monotonic=monotonic)
        # Serializes imports with deletes.
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._last_retention = None
        self._last_warning = {}

    # -- thread ----------------------------------------------------------------
    def run(self):
        while not self._stop_event.is_set():
            if self._initialize():
                break
            self._stop_event.wait(_RETRY_SECONDS)
        while not self._stop_event.is_set():
            self.poll_once()
            self._stop_event.wait(self.interval)
        # The miner wrote run_end before stopping History: take it in.
        self.poll_once()

    def stop(self):
        self._stop_event.set()

    def _warn(self, message):
        """Log the current exception; a repeat within the interval logs one
        DEBUG line without the traceback."""

        now = self.monotonic()
        last = self._last_warning.get(message)
        if last is None or now - last >= WARNING_INTERVAL_SECONDS:
            self._last_warning[message] = now
            logger.warning(message, exc_info=True)
        else:
            logger.debug("%s (repeated)", message)

    def _initialize(self):
        try:
            self.store.initialize()
            return True
        except Exception:
            self._warn("Unable to open the History database")
            return False

    @property
    def journal(self):
        """The miner's journal in this process (looked up when used)."""

        return journal_module.JOURNAL if self._journal is None else self._journal

    def _sync_active_run(self):
        journal = self.journal
        same_folder = journal.running and journal.folder is not None
        if same_folder:
            try:
                same_folder = os.path.samefile(journal.folder, self.journal_folder)
            except OSError:
                same_folder = False
        self.store.active_run = journal.run if same_folder else None
        return same_folder

    def poll_once(self):
        """Import what is pending; returns False when it failed."""

        with self._lock:
            if not self.store.ready and not self._initialize():
                return False
            self._sync_active_run()
            try:
                self.importer.poll()
            except Exception as error:
                try:
                    if self.store.recover_if_corrupt(error):
                        return False
                except Exception:
                    # The thread keeps running and initializes again.
                    self.store.ready = False
                    self._warn("Unable to rebuild the History database; retrying")
                    return False
                self._warn("Unable to import the History journal; retrying")
                return False
            self._maybe_apply_retention()
            return True

    def drain(self):
        """One last import (the miner stopped)."""

        return self.poll_once()

    def _maybe_apply_retention(self):
        now = self.monotonic()
        if (
            self._last_retention is not None
            and now - self._last_retention < RETENTION_INTERVAL_SECONDS
        ):
            return
        try:
            self.store.apply_retention(
                now=self.clock(), retention_days=self.retention_days
            )
            self._last_retention = now
        except Exception:
            self._warn("Unable to prune old History rows")

    # -- deleting ----------------------------------------------------------------
    def delete_streamer(self, streamer, remove_source=None):
        """Delete one streamer's History.

        Everything the miner journalled for the streamer before this moment
        stays deleted, even records not imported yet; records written after
        it are new.  ``remove_source`` (deleting the analytics JSON) runs under
        the miner's analytics file lock once History is cleared.  Only that
        step holds the lock: the miner's analytics JSON writes take it too,
        and must not wait for the import and database delete.
        """

        streamer = clean_streamer(streamer)
        with self._lock:
            if not streamer:
                _remove_source(remove_source)
                return {}
            if not self.store.ready and not self._initialize():
                raise HistoryUnavailableError("History is still starting")
            try:
                self.importer.poll()
            except Exception:
                logger.debug("Import before a History delete failed", exc_info=True)
            writing_here = self._sync_active_run()
            deleted_at = self.clock()

            # Saved first: the database is rebuilt from the journal after
            # corruption, and the cutoff must survive that.
            def save(position):
                self.importer.save_delete(
                    streamer, position[0], position[1], deleted_at
                )

            position = (
                self.journal.delete_cutoff(streamer, save) if writing_here else None
            )
            if position is None:
                position = self.importer.end_position()
                save(position)
            counts = self.store.delete_streamer(
                streamer, position[0], position[1], deleted_at=deleted_at
            )
            if writing_here:
                self.journal.forget(streamer)
            _remove_source(remove_source)
            return counts


def _remove_source(remove_source):
    if remove_source is not None:
        with analytics_file_lock():
            remove_source()


def build_recorder(analytics_path, retention_days=DEFAULT_RETENTION_DAYS):
    store = HistoryStore(os.path.join(analytics_path, "history.sqlite3"))
    return HistoryRecorder(
        store,
        os.path.join(analytics_path, "history"),
        retention_days=retention_days,
    )
