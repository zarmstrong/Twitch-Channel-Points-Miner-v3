import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

WATCH_STREAK_CACHE_VERSION = 1
STALE_SESSION_TTL_SECONDS = 7 * 24 * 60 * 60


@dataclass
class WatchStreakSession:
    account_name: str
    streamer_login: str
    broadcast_id: str
    started_at: float
    claimed: bool = False
    claimed_at: Optional[float] = None
    ended_at: Optional[float] = None

    @property
    def key(self):
        return ":".join((self.account_name, self.streamer_login, self.broadcast_id))


class WatchStreakCache:
    """Durable, account-scoped state for watch-streak broadcast sessions."""

    def __init__(self, path, account_name, sessions=None):
        self.path = os.fspath(path)
        self.account_name = account_name.lower().strip()
        self._sessions = sessions or {}
        self._lock = threading.RLock()

    @classmethod
    def load(cls, path, account_name):
        normalized_account = account_name.lower().strip()
        sessions = {}
        try:
            with open(path, "r", encoding="utf-8") as cache_file:
                payload = json.load(cache_file)
        except FileNotFoundError:
            payload = {}
        except (OSError, ValueError, TypeError) as error:
            logger.warning(
                "Unable to read watch-streak state from %s; starting with an "
                "empty cache: %s",
                path,
                error,
            )
            payload = {}

        if isinstance(payload, dict) and payload.get("version") not in (
            None,
            WATCH_STREAK_CACHE_VERSION,
        ):
            logger.warning(
                "Unsupported watch-streak cache version %s in %s; starting "
                "with an empty cache",
                payload.get("version"),
                path,
            )
            payload = {}

        if isinstance(payload, dict) and isinstance(payload.get("sessions"), list):
            for item in payload["sessions"]:
                if not isinstance(item, dict):
                    continue
                try:
                    session = WatchStreakSession(
                        account_name=str(item["account_name"]).lower().strip(),
                        streamer_login=str(item["streamer_login"]).lower().strip(),
                        broadcast_id=str(item["broadcast_id"]),
                        started_at=float(item["started_at"]),
                        claimed=bool(item.get("claimed", False)),
                        claimed_at=(
                            float(item["claimed_at"])
                            if item.get("claimed_at") is not None
                            else None
                        ),
                        ended_at=(
                            float(item["ended_at"])
                            if item.get("ended_at") is not None
                            else None
                        ),
                    )
                except (KeyError, TypeError, ValueError):
                    continue
                if (
                    session.account_name == normalized_account
                    and session.streamer_login
                    and session.broadcast_id
                ):
                    sessions[session.key] = session

        cache = cls(path, normalized_account, sessions)
        cache.prune()
        return cache

    def get(self, streamer_login, broadcast_id):
        with self._lock:
            return self._sessions.get(self._key(streamer_login, broadcast_id))

    def ensure(self, streamer_login, broadcast_id, started_at=None):
        if broadcast_id in [None, ""]:
            return None
        now = time.time() if started_at is None else float(started_at)
        key = self._key(streamer_login, broadcast_id)
        with self._lock:
            session = self._sessions.get(key)
            if session is not None:
                return session

            login = streamer_login.lower().strip()
            for previous in self._sessions.values():
                if (
                    previous.account_name == self.account_name
                    and previous.streamer_login == login
                    and previous.broadcast_id != str(broadcast_id)
                    and previous.ended_at is None
                ):
                    previous.ended_at = now

            session = WatchStreakSession(
                account_name=self.account_name,
                streamer_login=login,
                broadcast_id=str(broadcast_id),
                started_at=now,
            )
            self._sessions[key] = session
            self._save_locked()
            return session

    def mark_claimed(self, streamer_login, broadcast_id, claimed_at=None):
        now = time.time() if claimed_at is None else float(claimed_at)
        with self._lock:
            session = self.ensure(streamer_login, broadcast_id, started_at=now)
            if session is None:
                return None
            session.claimed = True
            session.claimed_at = session.claimed_at or now
            self._save_locked()
            return session

    def mark_ended(self, streamer_login, broadcast_id, ended_at=None):
        if broadcast_id in [None, ""]:
            return None
        now = time.time() if ended_at is None else float(ended_at)
        with self._lock:
            session = self._sessions.get(self._key(streamer_login, broadcast_id))
            if session is None:
                return None
            if session.ended_at is None:
                session.ended_at = now
                self._save_locked()
            return session

    def prune(self, now=None, ttl_seconds=STALE_SESSION_TTL_SECONDS):
        current_time = time.time() if now is None else float(now)
        with self._lock:
            stale_keys = [
                key
                for key, session in self._sessions.items()
                if session.ended_at is not None
                and current_time - session.ended_at > ttl_seconds
            ]
            for key in stale_keys:
                del self._sessions[key]
            if stale_keys:
                self._save_locked()

    def _key(self, streamer_login, broadcast_id):
        return ":".join(
            (self.account_name, streamer_login.lower().strip(), str(broadcast_id))
        )

    def _save_locked(self):
        target = Path(self.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": WATCH_STREAK_CACHE_VERSION,
            "sessions": [asdict(session) for session in self._sessions.values()],
        }
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".tmp",
                delete=False,
            ) as temp_file:
                temp_path = temp_file.name
                json.dump(payload, temp_file, indent=2, sort_keys=True)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.replace(temp_path, target)
        except OSError as error:
            logger.warning("Unable to save watch-streak state to %s: %s", target, error)
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
