"""Append-only journal of what the miner did, for the History tab.

The miner writes one JSON line per fact as it happens: a minute-watched
request that Twitch accepted, a points message, a spend whose amount the miner
knows, a stream going live or offline, a drop claimed.  History reads the
lines in order and adds them up (see ``importer.py``).  The miner never reads
the journal back, and History never writes it.

Layout, inside the account's analytics folder::

    history/run_counter              last run number handed out
    history/r00000042-000001.jsonl   run 42, segment 1
    history/imported                 last segment History fully imported

Each process start is a new *run* with a new number.  A run writes its own
segments, moving to the next one by size only, so the sorted segment names are
exactly the write order.  Every line carries ``run`` and ``seq`` (a per-run
counter), which together give each record a unique, ordered position.

Writing never raises into the caller: mining must not stop because History
cannot write.  A failure is logged at most once a minute and the record is
lost (History reports the gap in ``seq``).
"""

import hashlib
import json
import logging
import math
import os
import re
import threading
import time
from contextlib import contextmanager

logger = logging.getLogger(__name__)

JOURNAL_FORMAT_VERSION = 1
JOURNAL_FOLDER = "history"
RUN_COUNTER_FILE = "run_counter"
ACK_FILE = "imported"
# Delete cutoffs, kept beside the journal so a rebuilt database still skips
# what a delete removed (see the importer).
DELETES_FILE = "deleted"

# A segment is closed once it exceeds this size.
JOURNAL_SEGMENT_BYTES = 4 * 1024 * 1024
# Safety valve: past this, the oldest segments are deleted even if History has
# not imported them (it reports them as gaps).
JOURNAL_MAX_BYTES = 64 * 1024 * 1024

# The most one successful minute-watched request can credit.  The watch loop
# posts every ~20 s, so this is several normal intervals.
WATCH_CREDIT_CAP = 90.0
# How long stop() waits for the journal's locks (see stop()).
STOP_LOCK_SECONDS = 5.0
# How long stop() waits for spends still being made (see spend_in_progress).
STOP_SPEND_SECONDS = 30.0

# Drops whose channel note_drop_channel() remembers (the newest ones).
DROP_CHANNELS_KEPT = 500
# Claims record_drop() remembers having written, so a claim reported again
# in this process is not written twice (far more than a process makes).
DROP_CLAIMS_KEPT = 20000

_WARNING_INTERVAL = 60.0

_SEGMENT_PATTERN = re.compile(r"^r(\d{8})-(\d{6})\.jsonl$")


def segment_name(run, segment):
    return f"r{int(run):08d}-{int(segment):06d}.jsonl"


def parse_segment_name(name):
    """Return ``(run, segment)`` for a segment file name, else ``None``."""

    match = _SEGMENT_PATTERN.match(str(name))
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def list_segments(folder, strict=False):
    """Sorted segment names in ``folder`` (write order); [] when missing.

    Any other error listing the folder also gives [], unless ``strict``:
    then it raises, for callers that must not mistake it for an empty
    journal.
    """

    try:
        names = os.listdir(folder)
    except FileNotFoundError:
        return []
    except OSError:
        if strict:
            raise
        return []
    return sorted(name for name in names if parse_segment_name(name) is not None)


def read_ack(folder):
    """The last segment History reported fully imported, or ``None``."""

    try:
        with open(os.path.join(folder, ACK_FILE), "r", encoding="utf-8") as handle:
            value = handle.read().strip()
    except OSError:
        return None
    return value if parse_segment_name(value) is not None else None


def write_atomic(path, text):
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(temporary, path)


def journal_end(folder):
    """``(newest segment, its size)`` on disk, or ``("", 0)`` for no journal.

    Raises ``OSError`` when the folder cannot be listed or the segment's
    size read: with no known end there is no safe delete cutoff.
    """

    names = list_segments(folder, strict=True)
    if not names:
        return "", 0
    return names[-1], os.path.getsize(os.path.join(folder, names[-1]))


# Cutoff lines with this version are written before the database deletes
# the streamer's rows; the importer finishes such a delete if the database
# never committed it.  Older lines (no version) are only used to skip
# records.
CUTOFF_VERSION = 2


def read_delete_cutoffs(folder, finishable=False):
    """``streamer -> (segment, offset)``: the latest delete cutoff of each
    streamer, from ``DELETES_FILE``.  With ``finishable``, only cutoffs
    written with ``CUTOFF_VERSION`` or later count.

    The file outlives the database, so a rebuilt one still skips what was
    deleted.  Raises ``OSError`` when it exists but cannot be read:
    importing without it could bring deleted History back.
    """

    cutoffs = {}
    try:
        with open(os.path.join(folder, DELETES_FILE), "rb") as handle:
            data = handle.read()
    except FileNotFoundError:
        return cutoffs
    for raw in data.split(b"\n"):
        try:
            entry = json.loads(raw.decode("utf-8"))
            streamer = _clean(entry["streamer"])
            position = (str(entry["segment"]), int(entry["offset"]))
            version = int(entry.get("v", 1))
        except (
            UnicodeDecodeError,
            ValueError,
            KeyError,
            TypeError,
            OverflowError,
            AttributeError,
        ):
            continue
        if finishable and version < CUTOFF_VERSION:
            continue
        # A delete before anything was journalled, ("", 0), is kept too: it
        # removes no journal line, but the analytics data import
        # (backfill.py) must skip the streamer.
        if streamer and (streamer not in cutoffs or position > cutoffs[streamer]):
            cutoffs[streamer] = position
    return cutoffs


def save_delete_cutoff(folder, streamer, segment, offset, deleted_at, drops=()):
    """Append one delete cutoff to ``DELETES_FILE`` (fsynced); raises
    ``OSError`` when it cannot be saved.

    ``drops``: the claims the delete removes, as ``(key, drop_id, game, ts,
    source)`` (see ``read_deleted_drops``)."""

    entry = {
        "v": CUTOFF_VERSION,
        "streamer": _clean(streamer),
        "segment": str(segment),
        "offset": int(offset),
        "at": float(deleted_at),
    }
    if drops:
        entry["drops"] = [list(drop) for drop in drops]
    _append_delete_line(folder, entry)


def save_deleted_drops(folder, drops, deleted_at):
    """Append claims a delete removed that its cutoff line doesn't list
    (journalled before the cutoff, imported after it) to ``DELETES_FILE``
    (fsynced); raises ``OSError`` when they cannot be saved."""

    if drops:
        _append_delete_line(
            folder,
            {
                "v": CUTOFF_VERSION,
                "drops": [list(drop) for drop in drops],
                "at": float(deleted_at),
            },
        )


def _append_delete_line(folder, entry):
    os.makedirs(folder, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(folder, DELETES_FILE), "ab") as handle:
        # A line cut off by a crash never joins this one.
        handle.write(("\n" + line + "\n").encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())


def read_deleted_drops(folder):
    """The claims streamer deletes removed, from ``DELETES_FILE``: ``(key,
    drop_id, game, ts, source)`` tuples, oldest first.

    Like the cutoffs, they outlive the database: a rebuilt one must not
    count a deleted claim again when Twitch or the Drops data reports it.
    Raises ``OSError`` when the file exists but cannot be read."""

    found = []
    try:
        with open(os.path.join(folder, DELETES_FILE), "rb") as handle:
            data = handle.read()
    except FileNotFoundError:
        return found
    for raw in data.split(b"\n"):
        try:
            drops = json.loads(raw.decode("utf-8"))["drops"]
        except (UnicodeDecodeError, ValueError, KeyError, TypeError):
            continue
        if not isinstance(drops, list):
            continue
        for drop in drops:
            try:
                key, drop_id, game, ts, source = drop
                ts = float(ts)
            except (ValueError, TypeError, OverflowError):
                continue
            if (
                isinstance(key, str)
                and isinstance(drop_id, str)
                and (game is None or isinstance(game, str))
                and source in ("journal", "analytics")
                and math.isfinite(ts)
            ):
                found.append((key, drop_id, game, ts, source))
    return found


def _clean(username):
    return str(username or "").strip().lower()


# -- per-streamer locks ----------------------------------------------------
# Module-level, like Streamer's _chat_lifecycle_lock, so Streamer.__slots__
# and copies are unaffected.
_locks_guard = threading.Lock()
_lifecycle_locks = {}
_points_locks = {}


def _named_lock(table, username):
    username = _clean(username)
    with _locks_guard:
        lock = table.get(username)
        if lock is None:
            lock = table[username] = threading.RLock()
        return lock


def lifecycle_lock(username):
    """The lock held while a streamer's live/offline state changes and its
    record is appended, so the journal order matches the state changes.

    Take it before any journal call; the journal's own lock is innermost.
    """

    return _named_lock(_lifecycle_locks, username)


def points_lock(username):
    return _named_lock(_points_locks, username)


def history_eligible(streamer):
    """True for streamers History records: not discovered only for Drops."""

    from TwitchChannelPointsMiner.classes.entities.Streamer import is_drop_discovered

    try:
        return (
            not is_drop_discovered(streamer)
            or getattr(streamer, "explicitly_configured", False) is True
        )
    except Exception:
        return False


def _stream(streamer):
    return getattr(streamer, "stream", None)


def _broadcast_id(streamer):
    value = getattr(_stream(streamer), "broadcast_id", None)
    return str(value) if value not in (None, "") else None


def _game_name(streamer):
    game = getattr(_stream(streamer), "game", None) or {}
    if not isinstance(game, dict):
        return None
    return game.get("displayName") or game.get("name") or None


def _title(streamer):
    title = getattr(_stream(streamer), "title", None)
    return title if isinstance(title, str) else None


def _parse_upstream(raw):
    """Epoch seconds of a Twitch timestamp (a date string, or epoch seconds),
    or ``None``."""

    if raw in (None, "") or isinstance(raw, bool):
        return None
    try:
        # server_time is epoch seconds (see utils.Utils.server_time).
        number = float(raw)
    except (TypeError, ValueError):
        number = None
    if number is not None:
        return number if math.isfinite(number) else None
    try:
        from datetime import timezone

        from dateutil import parser

        parsed = parser.parse(str(raw))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _earned_on(streamer, upstream_at):
    """The broadcast a points message belongs to.

    Decided from one read of the streamer's ``online_history`` (``(broadcast
    id, marked online at)`` pairs, newest last), the record ``Streamer`` uses
    to credit a streak.  Its newest entry is the broadcast the miner last
    marked online, which stays consistent while a refresh has already moved
    ``stream.broadcast_id`` on but not yet run the offline/online change.

    A message Twitch sent after that broadcast went online (allowing for the
    two clocks) belongs to it.  One sent earlier can be a late one from an
    earlier broadcast, such as a delayed watch-streak award: it gets the
    broadcast online when it was sent, or none when that is unknown.
    """

    current = _broadcast_id(streamer)
    history = getattr(streamer, "online_history", None)
    if upstream_at is None or not isinstance(history, tuple) or not history:
        return current
    from TwitchChannelPointsMiner.constants import WATCH_STREAK_CLOCK_SKEW_SECONDS

    try:
        latest_id, latest_at = history[-1]
        if upstream_at >= float(latest_at) + WATCH_STREAK_CLOCK_SKEW_SECONDS:
            # Marked online without a known id: it is the stream's now.
            return str(latest_id) if latest_id not in (None, "") else current

        def online_when(sent_at):
            for broadcast_id, online_at in reversed(history):
                if float(online_at) <= sent_at:
                    return broadcast_id
            return None

        earned_on = online_when(
            upstream_at - WATCH_STREAK_CLOCK_SKEW_SECONDS
        ) or online_when(upstream_at)
    except (TypeError, ValueError):
        return None
    return str(earned_on) if earned_on not in (None, "") else None


def _int_or_none(value):
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# watch_attempt() without a token (callers that do not take one).
_NO_TOKEN = object()


class _WatchState(object):
    __slots__ = ("boundary", "broadcast_id", "live", "previous", "reason", "streamer")

    def __init__(self, boundary, broadcast_id, reason, streamer, live=True):
        self.boundary = boundary
        self.broadcast_id = broadcast_id
        # False between an offline and the next live: nothing is credited.
        self.live = live
        # Broadcasts this streamer moved on from: a request still in flight
        # for one of them credits nothing and leaves the boundary alone.
        self.previous = ()
        self.reason = reason
        self.streamer = streamer

    def switch(self, broadcast_id, now):
        """The streamer is on ``broadcast_id`` from ``now``."""

        self.previous = (self.previous + (self.broadcast_id,))[-4:]
        self.broadcast_id = broadcast_id
        self.boundary = now


class Journal(object):
    """The miner's writer.  One per process (see ``JOURNAL``)."""

    def __init__(self, clock=time.time, monotonic=time.monotonic):
        self.clock = clock
        self.monotonic = monotonic
        self._lock = threading.Lock()
        self._folder = None
        self._handle = None
        self._run = None
        self._segment = 0
        self._seq = 0
        self._size = 0
        self._last_warning = None
        self._valve_warned = False
        self._points_keys = set()
        self._spend_keys = set()
        # username -> the last lifecycle record written ("live", ...),
        # ("offline",) or ("untracked",).  Read and updated under the
        # streamer's lifecycle lock.
        self._lifecycle = {}
        # id() -> each Streamer object removed from the miner (see
        # retire()); kept so the ids stay unique.  Nothing more is recorded
        # from those objects.
        self._retired = {}
        # Set by stop() before it waits for the lock: no more records.
        self._stopping = False
        # Spends being made (token -> thread id); see spend_in_progress().
        self._spends = threading.Condition(threading.Lock())
        self._spending = {}
        # Set by stop(): no new spend may start (see spend_in_progress).
        self._spends_closed = False
        self._watch_lock = threading.Lock()
        self._watch = {}
        # drop id -> the channel Twitch last reported progress for it on
        # (see note_drop_channel), newest last.  Kept across runs of this
        # process; lost at a restart.
        self._drop_lock = threading.Lock()
        self._drop_channels = {}
        # Claim keys record_drop wrote in this process -> (game, channel) written,
        # newest last; and claims it could not write -> whether Twitch first
        # said "already claimed".  The lock is held across the append so a
        # claim reported twice at once is written once; outer to the
        # journal's own lock.
        self._claims_lock = threading.Lock()
        self._drop_claims = {}
        self._unwritten_claims = {}
        # Claims written without a channel: drop id -> {claim key: the
        # record written}, newest last, so a channel noted for the drop
        # later is written to them (note_drop_channel).
        self._channelless_claims = {}

    def _eligible(self, streamer):
        """``history_eligible`` and not removed from the miner."""

        if self._retired.get(id(streamer)) is streamer:
            return False
        return history_eligible(streamer)

    def eligible(self, streamer):
        """Whether History records ``streamer`` now."""

        return self._eligible(streamer)

    # -- run lifecycle ---------------------------------------------------
    @property
    def running(self):
        return self._run is not None and not self._stopping

    @property
    def folder(self):
        return self._folder

    @property
    def run(self):
        return self._run

    def start(self, folder, version=None):
        """Begin a new run in ``folder``; returns True when writing.

        Must be called before any worker that writes records starts.
        """

        with self._lock:
            if self._run is not None and not self._stopping:
                return True
            # A stop() that could not take the lock left the run open; end
            # it without run_end (the next run_start closes it like a
            # crashed one).
            self._close_locked()
            self._run = None
            self._stopping = False
            try:
                os.makedirs(folder, exist_ok=True)
                run = self._next_run_number(folder)
                if os.path.exists(os.path.join(folder, segment_name(run, 1))):
                    # The folder listing missed it: the run number is not
                    # safe to use.
                    raise OSError(f"{segment_name(run, 1)} already exists")
            except OSError as error:
                logger.warning("History journal unavailable in %s: %s", folder, error)
                return False
            self._folder = folder
            self._run = run
            self._segment = 1
            self._seq = 0
            self._size = 0
            self._points_keys = set()
            self._spend_keys = set()
            self._lifecycle = {}
            if not self._open_locked():
                self._folder = None
                self._run = None
                return False
            self._prune_locked()
        with self._watch_lock:
            self._watch = {}
        with self._spends:
            self._spends_closed = False
        self.append("run_start", version=version)
        return True

    def stop(self):
        """Write ``run_end`` and close the run; later appends are ignored.

        ``run_end`` and the end of writing are one step, so no record can
        follow it.  The waits for the locks are bounded: Ctrl+C runs the
        miner's shutdown in a signal handler, which can interrupt this
        thread while it holds one of them.  The run is then left without
        ``run_end`` (the next run closes it like a crashed one), but it
        still takes no more records and is no longer reported as running.
        """

        if not self.running:
            return
        if not self._close_spends(STOP_SPEND_SECONDS):
            logger.warning(
                "A bet or community-goal contribution was still in progress "
                "when the miner stopped; History will not count it"
            )
        # Before waiting: from here on every record is refused.
        self._stopping = True
        if not self._lock.acquire(timeout=STOP_LOCK_SECONDS):
            logger.warning("History journal busy; ending the run without run_end")
            return
        try:
            if self._run is None:
                return
            self._append_locked("run_end", {}, final=True)
            self._close_locked()
            self._run = None
            self._stopping = False
        finally:
            self._lock.release()
        if self._watch_lock.acquire(timeout=STOP_LOCK_SECONDS):
            try:
                self._watch = {}
            finally:
                self._watch_lock.release()

    @contextmanager
    def spend_in_progress(self):
        """Wrap a request that spends points and records the spend (a bet,
        a community-goal contribution); yields whether it may go ahead.

        stop() waits for the ones in progress, so a spend Twitch accepts
        while the miner is stopping is still recorded, and from then on
        refuses new ones (False): the stopped miner makes no spend it could
        not record.  With no journal run ever started (History off), every
        spend goes ahead.
        """

        token = object()
        with self._spends:
            admitted = not self._spends_closed
            if admitted:
                self._spending[token] = threading.get_ident()
        try:
            yield admitted
        finally:
            if admitted:
                with self._spends:
                    self._spending.pop(token, None)
                    self._spends.notify_all()

    def allow_spends(self):
        """A new miner session starts: spends go ahead again, with or
        without a journal run."""

        with self._spends:
            self._spends_closed = False

    def _close_spends(self, timeout):
        """Refuse new spends, then wait up to ``timeout`` seconds for other
        threads' spends in progress; False when one is still running.  A
        spend on this thread (Ctrl+C interrupted it) is not waited for."""

        current = threading.get_ident()
        deadline = time.monotonic() + timeout
        if not self._spends.acquire(timeout=STOP_LOCK_SECONDS):
            self._spends_closed = True
            return False
        try:
            self._spends_closed = True
            while any(owner != current for owner in self._spending.values()):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._spends.wait(remaining)
            return True
        finally:
            self._spends.release()

    def position(self):
        """``(segment name, size)`` of the end of the journal right now.

        Raises ``OSError`` when the end is unknown (the journal cannot be
        reopened after a failed write).
        """

        with self._lock:
            if self._run is None:
                return None
            if self._handle is None and not self._open_locked():
                # A failed write may have left bytes past the size we know
                # of; without the file's real end there is no safe cutoff.
                raise OSError("the History journal is not writable")
            return segment_name(self._run, self._segment), self._size

    def _next_run_number(self, folder):
        """One more than any run on disk.  Raises ``OSError`` when the folder
        cannot be listed: a reused run number would be lost to History."""

        highest = 0
        for name in list_segments(folder, strict=True):
            highest = max(highest, parse_segment_name(name)[0])
        counter_path = os.path.join(folder, RUN_COUNTER_FILE)
        try:
            with open(counter_path, "r", encoding="utf-8") as handle:
                highest = max(highest, int(handle.read().strip()))
        except (OSError, ValueError):
            pass
        run = highest + 1
        try:
            write_atomic(counter_path, f"{run}\n")
        except OSError as error:
            # The segment names still give the next run its number.
            logger.warning("Unable to save the History run counter: %s", error)
        return run

    # -- writing ---------------------------------------------------------
    def append(self, kind, **fields):
        """Append one record; returns True when it was written."""

        try:
            with self._lock:
                return self._append_locked(kind, fields)
        except Exception:
            logger.debug("History journal append failed", exc_info=True)
            return False

    def _append_locked(self, kind, fields, final=False):
        """``append`` with the journal lock held; never raises.  Once stop()
        began, only its own ``run_end`` (``final``) is written."""

        try:
            if self._run is None or (self._stopping and not final):
                return False
            self._seq += 1
            record = {
                "v": JOURNAL_FORMAT_VERSION,
                "run": self._run,
                "seq": self._seq,
                "at": float(self.clock()),
                "kind": kind,
            }
            record.update(fields)
            line = (
                json.dumps(
                    record, ensure_ascii=False, separators=(",", ":"), default=str
                )
                + "\n"
            ).encode("utf-8")
            if self._handle is None and not self._open_locked():
                return False
            try:
                self._handle.write(line)
                self._handle.flush()
            except (OSError, ValueError) as error:
                self._warn_locked("Unable to write the History journal: %s", error)
                self._close_locked()
                return False
            self._size += len(line)
            if self._size >= JOURNAL_SEGMENT_BYTES:
                self._close_locked()
                self._segment += 1
                self._size = 0
                self._open_locked()
                self._prune_locked()
            return True
        except Exception:
            logger.debug("History journal append failed", exc_info=True)
            return False

    def _path_locked(self):
        return os.path.join(self._folder, segment_name(self._run, self._segment))

    def _open_locked(self):
        """Open the current segment for append, ending a partial last line."""

        try:
            handle = open(self._path_locked(), "ab")
            try:
                size = handle.seek(0, os.SEEK_END)
                if size > 0:
                    with open(self._path_locked(), "rb") as reader:
                        reader.seek(size - 1)
                        last = reader.read(1)
                    if last != b"\n":
                        handle.write(b"\n")
                        handle.flush()
                        size += 1
            except Exception:
                handle.close()
                raise
        except (OSError, ValueError) as error:
            self._warn_locked("Unable to open the History journal: %s", error)
            return False
        self._handle = handle
        self._size = size
        return True

    def _close_locked(self):
        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                handle.close()
            except (OSError, ValueError):
                pass

    def _prune_locked(self):
        """Delete segments History acknowledged; enforce the size limit."""

        folder = self._folder
        current = segment_name(self._run, self._segment)
        ack = read_ack(folder)
        remaining = []
        for name in list_segments(folder):
            if name == current:
                continue
            if ack is not None and name <= ack:
                try:
                    os.remove(os.path.join(folder, name))
                    continue
                except OSError:
                    # Windows may have it open; retried at the next rotation.
                    pass
            remaining.append(name)

        sizes = {}
        for name in remaining + [current]:
            try:
                sizes[name] = os.path.getsize(os.path.join(folder, name))
            except OSError:
                sizes[name] = 0
        total = sum(sizes.values())
        for name in remaining:
            if total <= JOURNAL_MAX_BYTES:
                break
            try:
                os.remove(os.path.join(folder, name))
            except OSError:
                continue
            total -= sizes[name]
            if not self._valve_warned:
                self._valve_warned = True
                logger.warning(
                    "The History journal exceeded %d MB without being imported; "
                    "deleted its oldest records",
                    JOURNAL_MAX_BYTES // (1024 * 1024),
                )

    def _warn_locked(self, message, *args):
        now = self.monotonic()
        if self._last_warning is None or now - self._last_warning >= _WARNING_INTERVAL:
            self._last_warning = now
            logger.warning(message, *args)
        else:
            logger.debug(message, *args)

    # -- lifecycle records -----------------------------------------------
    def record_live(self, streamer):
        """Write ``live`` for an online, eligible streamer unless the last
        lifecycle record already says exactly this."""

        username = _clean(getattr(streamer, "username", None))
        if not self.running or not username:
            return
        with lifecycle_lock(username):
            if getattr(streamer, "is_online", False) is not True:
                return
            if not self._eligible(streamer):
                return
            broadcast_id = _broadcast_id(streamer)
            if broadcast_id is None:
                return
            self._watch_broadcast(username, broadcast_id)
            state = ("live", broadcast_id, _game_name(streamer), _title(streamer))
            if self._lifecycle.get(username) == state:
                return
            if self.append(
                "live",
                streamer=username,
                broadcast_id=broadcast_id,
                game=state[2],
                title=state[3],
            ):
                self._lifecycle[username] = state

    def record_offline(self, streamer, broadcast_id=None):
        username = _clean(getattr(streamer, "username", None))
        if not self.running or not username:
            return
        with lifecycle_lock(username):
            if not self._eligible(streamer):
                return
            if broadcast_id in (None, ""):
                broadcast_id = _broadcast_id(streamer)
            # Offline ends the watch (the importer closes the session): a
            # request still pending credits nothing, even if the stream is
            # back before it is answered (a new state is a new token, see
            # watch_token).  The channel stays selected; live starts it again.
            with self._watch_lock:
                state = self._watch.get(username)
                if state is not None:
                    self._watch[username] = _WatchState(
                        self.monotonic(), None, state.reason, state.streamer, live=False
                    )
            self.append(
                "offline",
                streamer=username,
                broadcast_id=str(broadcast_id) if broadcast_id else None,
            )
            self._lifecycle[username] = ("offline",)

    def record_untracked(self, streamer, force=False):
        """History stops tracking ``streamer`` (removed, or no longer
        eligible when ``force`` is true)."""

        username = _clean(getattr(streamer, "username", None))
        if not self.running or not username:
            return
        with lifecycle_lock(username):
            if not force and not self._eligible(streamer):
                return
            self._end_watch(username)
            self.append("untracked", streamer=username)
            self._lifecycle[username] = ("untracked",)

    def retire(self, streamer):
        """``streamer`` was removed from the miner: write ``untracked`` and
        record nothing more from this object, even from a request or
        refresh still in flight, until it is tracked again (``reinstate``).
        """

        username = _clean(getattr(streamer, "username", None))
        if not username:
            return
        with lifecycle_lock(username):
            eligible = self._eligible(streamer)
            self._retired[id(streamer)] = streamer
            self._end_watch(username, streamer)
            if eligible and self.running:
                self.append("untracked", streamer=username)
                self._lifecycle[username] = ("untracked",)

    def _end_watch(self, username, streamer=None):
        """Tracking ended: requests already sent credit nothing, and the
        channel's watch time starts again when it is next selected.  With
        ``streamer``, only that object's watch ends (not a replacement's)."""

        with self._watch_lock:
            state = self._watch.get(username)
            if state is not None and (streamer is None or state.streamer is streamer):
                del self._watch[username]

    def reinstate(self, streamer):
        """The miner tracks ``streamer`` again (a removed object added back):
        its records count again."""

        username = _clean(getattr(streamer, "username", None))
        with lifecycle_lock(username):
            self._retired.pop(id(streamer), None)

    def delete_cutoff(self, username, save):
        """Take and save the cutoff for deleting ``username``'s History.

        Under the streamer's lifecycle lock: the journal's end (see
        ``position``) is passed to ``save``, which must store it durably,
        and only once it is saved does the streamer's watch accounting end.
        A watch request answered before this is journalled before the cutoff
        (and deleted); one answered after it credits nothing, so no watch
        time from before the delete survives.  If ``save`` raises, the
        delete has not happened and the watch goes on untouched.

        Returns the position, or None when not writing (nothing is saved).
        Raises ``OSError`` when the journal's end is unknown.
        """

        username = _clean(username)
        with lifecycle_lock(username):
            position = self.position()
            if position is None:
                return None
            save(position)
            self._end_watch(username)
            return position

    def forget(self, username):
        """``username``'s History was deleted: its next stream update writes
        ``live`` again, even for an unchanged broadcast, so the stream it is
        on is recorded from then on."""

        username = _clean(username)
        with lifecycle_lock(username):
            self._lifecycle.pop(username, None)
            # Watch accounting was ended at the cutoff (delete_cutoff); a
            # watch started since then is after the delete and is kept.

    @contextmanager
    def eligibility_change(self, streamer):
        """Wrap code that may change whether History records ``streamer``.

        Becoming ineligible writes ``untracked``; becoming eligible while
        online writes ``live``.  Both under the streamer's lifecycle lock, so
        no record of the streamer is written between the change and this.
        """

        username = _clean(getattr(streamer, "username", None))
        with lifecycle_lock(username):
            before = self._eligible(streamer)
            try:
                yield
            finally:
                after = self._eligible(streamer)
                if before and not after:
                    self.record_untracked(streamer, force=True)
                elif after and not before:
                    self.record_live(streamer)

    # -- watch time --------------------------------------------------------
    def watch_selection(self, selected):
        """The streamers the watch loop selected this cycle.

        ``selected`` maps username -> ``(streamer, reason)``.  A streamer
        entering the selection starts its accounting boundary now; one
        leaving it gets ``watch_end``.
        """

        if not self.running:
            return
        now = self.monotonic()
        selected = {_clean(name): value for name, value in dict(selected).items()}
        left = []
        with self._watch_lock:
            for username in list(self._watch):
                if username not in selected:
                    left.append((username, self._watch.pop(username).streamer))
            for username, (streamer, reason) in selected.items():
                state = self._watch.get(username)
                # Read under the watch lock: set_online / set_offline change
                # it before their record takes this lock, so a transition
                # racing this selection still updates the state after it.
                online = getattr(streamer, "is_online", False) is True
                if state is None or (not state.live and online):
                    # New to the selection, or selected again after an
                    # offline: the watch starts now, unless the channel
                    # went offline since it was selected (then it starts at
                    # the next live).
                    self._watch[username] = _WatchState(
                        now, _broadcast_id(streamer), reason, streamer, live=online
                    )
                else:
                    state.reason = reason
                    state.streamer = streamer
        # Lifecycle lock before the watch lock (see watch_attempt); a channel
        # that left the selection gets no more watch records.
        for username, streamer in left:
            with lifecycle_lock(username):
                if self._eligible(streamer):
                    self.append("watch_end", streamer=username)

    def _watch_broadcast(self, username, broadcast_id):
        """A stream update saw ``broadcast_id``: a new broadcast starts its
        watch time now, not at the next accepted request."""

        now = self.monotonic()
        with self._watch_lock:
            state = self._watch.get(username)
            if state is None:
                return
            if not state.live:
                # Back live while still selected: watching starts now.
                self._watch[username] = _WatchState(
                    now, broadcast_id, state.reason, state.streamer
                )
            elif state.broadcast_id != broadcast_id:
                state.switch(broadcast_id, now)

    def watch_token(self, streamer):
        """Taken when a minute-watched request is sent and passed back to
        watch_attempt(): the request belongs to this stretch of watching,
        which an offline or leaving the selection ends."""

        username = _clean(getattr(streamer, "username", None))
        with self._watch_lock:
            return self._watch.get(username)

    def watch_attempt(self, streamer, success, broadcast_id=None, token=_NO_TOKEN):
        """One minute-watched attempt for a selected streamer finished.

        Every outcome (success, failure, exception, skip) moves the
        boundary; only a success credits the time since the previous attempt,
        capped at ``WATCH_CREDIT_CAP``.
        """

        if not self.running:
            return
        username = _clean(getattr(streamer, "username", None))
        now = self.monotonic()
        if broadcast_id in (None, ""):
            broadcast_id = _broadcast_id(streamer)
        else:
            broadcast_id = str(broadcast_id)
        # Under the lifecycle lock, so the channel cannot become ineligible
        # (writing ``untracked``) between the check and the record.
        with lifecycle_lock(username), self._watch_lock:
            state = self._watch.get(username)
            if (
                state is None
                or not state.live
                or (token is not _NO_TOKEN and token is not state)
            ):
                # Offline, or sent before an offline or leaving the selection.
                return
            if broadcast_id != state.broadcast_id:
                if broadcast_id in state.previous:
                    # Sent for a broadcast that has since ended.
                    return
                if token is _NO_TOKEN or state.broadcast_id is None:
                    # Time before the change belongs to the earlier broadcast.
                    state.switch(broadcast_id, now)
                # With a token, a stream update (record_live) moves the state
                # to a new broadcast, never a request: one built from an
                # older payload must not move it back.  Its time goes to the
                # broadcast it was sent for.
            seconds = min(max(0.0, now - state.boundary), WATCH_CREDIT_CAP)
            state.boundary = now
            if success is not True or seconds <= 0:
                return
            if not self._eligible(streamer):
                return
            self.append(
                "watch",
                streamer=username,
                broadcast_id=broadcast_id,
                reason=state.reason,
                seconds=round(seconds, 3),
            )

    # -- points and spends -------------------------------------------------
    def record_points_message(self, streamer, message_type, data):
        """A ``points-earned`` or ``points-spent`` PubSub message."""

        username = _clean(getattr(streamer, "username", None))
        if not self.running or not username or not isinstance(data, dict):
            return
        earned = message_type == "points-earned"
        upstream = data.get("timestamp")
        if upstream in (None, ""):
            upstream = data.get("server_time")
        balance = _int_or_none((data.get("balance") or {}).get("balance"))
        gain = (data.get("point_gain") or {}) if earned else {}
        reason_code = gain.get("reason_code") if earned else None
        total_points = _int_or_none(gain.get("total_points")) if earned else None
        if upstream in (None, ""):
            msg_key = None
        else:
            identity = [username, message_type, str(upstream), balance]
            if earned:
                identity += [reason_code, total_points]
            msg_key = hashlib.sha256(
                json.dumps(identity, separators=(",", ":")).encode("utf-8")
            ).hexdigest()[:32]
        upstream_at = _parse_upstream(upstream)
        with lifecycle_lock(username), points_lock(username):
            if not self._eligible(streamer):
                return
            if msg_key is not None:
                if msg_key in self._points_keys:
                    return
                self._points_keys.add(msg_key)
            self.append(
                "points",
                streamer=username,
                broadcast_id=_earned_on(streamer, upstream_at),
                balance=balance,
                delta=total_points,
                reason_code=reason_code,
                msg_key=msg_key,
                upstream_at=upstream_at,
            )

    def record_redemption(self, streamer, data):
        """A ``reward-redeemed`` message: a spend with a known cost."""

        if not isinstance(data, dict):
            return
        redemption = data.get("redemption") or {}
        reward = redemption.get("reward") or {}
        redemption_id = redemption.get("id")
        cost = _int_or_none(reward.get("cost"))
        if not redemption_id or cost is None:
            return
        upstream = data.get("timestamp") or redemption.get("redeemed_at")
        self.record_spend(
            streamer,
            "redemption",
            cost,
            f"redemption:{redemption_id}",
            title=reward.get("title"),
            upstream_at=_parse_upstream(upstream),
        )

    def record_spend(
        self, streamer, source, amount, spend_key, title=None, upstream_at=None
    ):
        username = _clean(getattr(streamer, "username", None))
        amount = _int_or_none(amount)
        if not self.running or not username or amount is None or not spend_key:
            return
        with lifecycle_lock(username), points_lock(username):
            if not self._eligible(streamer):
                return
            if spend_key in self._spend_keys:
                return
            self._spend_keys.add(spend_key)
            self.append(
                "spend",
                streamer=username,
                source=source,
                amount=amount,
                spend_key=str(spend_key),
                title=title if isinstance(title, str) else None,
                upstream_at=upstream_at,
            )

    def record_event(self, streamer, event_type, label=None, points_delta=None):
        username = _clean(getattr(streamer, "username", None))
        if not self.running or not username:
            return
        with lifecycle_lock(username):
            if not self._eligible(streamer):
                return
            self.append(
                "event",
                streamer=username,
                type=str(event_type or "").strip().lower() or "event",
                label=label if isinstance(label, str) else None,
                points_delta=_int_or_none(points_delta),
            )

    # -- drops -------------------------------------------------------------
    def note_drop_channel(self, streamer, drop_id):
        """Twitch reported progress on ``drop_id`` for this channel (a
        PubSub ``drop-progress`` message, or the channel's current drop
        session): a claim of the drop is credited to it.

        Twitch counts a drop's progress on one channel at a time, so the
        newest report names the channel that earned it.  ``streamer`` is a
        Streamer or a username.  Drops are the account's, so this is kept
        whether or not History records the channel itself."""

        if isinstance(streamer, str):
            username = _clean(streamer)
        else:
            username = _clean(getattr(streamer, "username", None))
        drop_id = str(drop_id or "").strip()
        if not username or not drop_id:
            return
        with self._drop_lock:
            self._drop_channels.pop(drop_id, None)
            self._drop_channels[drop_id] = username
            while len(self._drop_channels) > DROP_CHANNELS_KEPT:
                del self._drop_channels[next(iter(self._drop_channels))]
        # A claim of the drop written before its channel was known (for
        # example claimed right after a restart, then named by a session
        # still on it): written again with it.
        with self._claims_lock:
            pending = self._channelless_claims.get(drop_id)
            for claim_key, record in list((pending or {}).items()):
                self._write_claim(claim_key, record, username)

    def drop_channel(self, drop_id):
        """The channel last noted for ``drop_id``, or None."""

        with self._drop_lock:
            return self._drop_channels.get(str(drop_id or "").strip())

    def record_drop(
        self,
        drop_id,
        claim_key,
        name=None,
        benefits=None,
        campaign=None,
        campaign_id=None,
        game=None,
        quest=False,
        minutes_required=None,
        image=None,
        already_claimed=False,
    ):
        """A drop (or quest reward) Twitch accepted the claim of.

        ``claim_key`` is unique per claim (the drop instance, and the drop
        for a reward group that repeats under one instance), so History
        counts a claim once however often it is reported.  ``already_claimed``:
        Twitch said the drop was claimed before (by hand, or by an earlier
        run); History then counts it only if it has not already."""

        drop_id = str(drop_id or "").strip()
        claim_key = str(claim_key or "").strip()
        if not self.running or not drop_id or not claim_key:
            return
        names = []
        for benefit in benefits or ():
            text = benefit.strip() if isinstance(benefit, str) else ""
            if text and text not in names:
                names.append(text)

        def text_or_none(value):
            value = value.strip() if isinstance(value, str) else ""
            return value or None

        game = text_or_none(game)
        if game is not None and game.lower() == "unknown":
            game = None
        with self._claims_lock:
            # Under the lock, so a channel noted meanwhile is either seen here
            # or written to this claim by note_drop_channel once it is
            # written (the drop lock is taken inside this one, never around
            # it).
            streamer = self.drop_channel(drop_id)
            written_game, written_streamer = self._drop_claims.get(
                claim_key, (None, None)
            )
            if claim_key in self._drop_claims:
                if (game is None or written_game is not None) and (
                    streamer is None or written_streamer is not None
                ):
                    # Written, and this adds nothing (a claim written without
                    # its game or channel is written again once it is known).
                    return
                # Written again with what was known already, too.
                game = written_game or game
                streamer = written_streamer or streamer
            if claim_key in self._unwritten_claims:
                # Not written when Twitch accepted it: what Twitch said then.
                already_claimed = self._unwritten_claims[claim_key]
            record = {
                "drop_id": drop_id,
                "claim_key": claim_key,
                "name": text_or_none(name),
                "benefits": names,
                "campaign": text_or_none(campaign),
                "campaign_id": text_or_none(campaign_id),
                "game": game,
                "quest": quest is True,
                "minutes_required": _int_or_none(minutes_required),
                "image": text_or_none(image),
                "already_claimed": already_claimed is True,
            }
            if not self._write_claim(claim_key, record, streamer):
                self._unwritten_claims.setdefault(claim_key, already_claimed is True)
                while len(self._unwritten_claims) > DROP_CLAIMS_KEPT:
                    del self._unwritten_claims[next(iter(self._unwritten_claims))]

    def _write_claim(self, claim_key, record, streamer):
        """Write a claim's record credited to ``streamer`` (or None), and
        remember what was written.  Called with ``_claims_lock`` held.
        Returns whether it was written."""

        drop_id = record["drop_id"]
        if not self.append("drop", streamer=streamer, **record):
            return False
        self._unwritten_claims.pop(claim_key, None)
        self._drop_claims.pop(claim_key, None)
        self._drop_claims[claim_key] = (record["game"], streamer)
        while len(self._drop_claims) > DROP_CLAIMS_KEPT:
            del self._drop_claims[next(iter(self._drop_claims))]
        pending = self._channelless_claims.pop(drop_id, {})
        pending.pop(claim_key, None)
        if streamer is None:
            pending[claim_key] = record
        if pending:
            self._channelless_claims[drop_id] = pending
            while len(self._channelless_claims) > DROP_CHANNELS_KEPT:
                del self._channelless_claims[next(iter(self._channelless_claims))]
        return True


# The process-wide journal.  Inert until ``start()``; every call is a no-op
# while it is not running, so call sites need no checks of their own.
JOURNAL = Journal()
