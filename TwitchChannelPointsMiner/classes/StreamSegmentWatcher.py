"""Request HLS media segments so Twitch credits Drop watch time.

Since 2026-10-07 Twitch only advances time-based Drop progress while a
stream's media segments are being requested the way a player does; the
minute-watched spade event on its own no longer earns watch time. This polls
the stream's HLS media playlist and sends a HEAD request for every segment it
has not requested yet, so no audio or video body is downloaded.

The approach mirrors the current fixes in Alorf/TwitchDropsBot#48 and
rangermix/TwitchDropsMiner#164.
"""

import logging
import time
from collections import OrderedDict
from threading import Thread
from urllib.parse import quote, urljoin

import requests

logger = logging.getLogger(__name__)

# The CDN URL that serves the signed master playlist for a live channel.
MASTER_PLAYLIST_URL = "https://usher.ttvnw.net/api/channel/hls/{login}.m3u8"

# Poll at most this often so the watch loop can call poll() every cycle
# without hammering Twitch.
POLL_INTERVAL_SECONDS = 10
PLAYLIST_TIMEOUT = (5, 15)
SEGMENT_TIMEOUT = (3, 10)
# A signed playlist URL expires and a restarted stream gets new ones; only
# re-resolve at most this often after a failed attempt.
RESOLVE_RETRY_SECONDS = 60
# Responses meaning the signed playlist (or segment) URL is no longer valid.
EXPIRED_STATUS_CODES = (401, 403, 404, 410)
# Report a stall at most this often.
REPORT_INTERVAL_SECONDS = 300
# Being watched again after this long starts a fresh report window instead
# of reporting a stall on the first poll back.
RESUME_GAP_SECONDS = 60
# A watcher not polled for this long can be dropped by its owner; shorter
# gaps (rotation, slot swaps) keep it so stall reporting stays deduplicated.
IDLE_EXPIRY_SECONDS = 600
# Bound the per-watch memory of already-requested segments.
SEEN_SEGMENT_LIMIT = 256


class StreamSegmentWatcher(object):
    """Polls one watched stream's media playlist and HEADs new segments.

    A single instance tracks a single (username, broadcast) pair. Its owner
    calls ``start_poll`` on the existing watch cadence, which runs ``poll`` on
    a short-lived background thread so slow Twitch or CDN responses never hold
    up the watch loop, and drops the instance once ``is_idle``.
    """

    __slots__ = [
        "username",
        "gql",
        "user_agent",
        "broadcast_id",
        "media_playlist",
        "next_resolve_attempt",
        "last_poll",
        "last_used",
        "last_report",
        "seen_segments",
        "segments_requested",
        "segments_failed",
        "polls_failed",
        "reported_stall",
        "last_failure",
        "last_sequence",
        "segments_missed",
        "playlist_lengths",
        "worker",
    ]

    def __init__(self, username, gql, user_agent, broadcast_id=None):
        self.username = username
        self.gql = gql
        self.user_agent = user_agent
        self.broadcast_id = broadcast_id
        self.media_playlist = None
        self.next_resolve_attempt = 0.0
        self.last_poll = 0.0
        self.last_used = time.time()
        # None until the first poll opens a report window.
        self.last_report = None
        # Insertion-ordered so the oldest segment is evicted first.
        self.seen_segments = OrderedDict()
        self.segments_requested = 0
        self.segments_failed = 0
        self.polls_failed = 0
        self.reported_stall = False
        self.last_failure = None
        # Media sequence of the newest segment seen, used to measure segments
        # that left the playlist window between polls.
        self.last_sequence = None
        self.segments_missed = 0
        self.playlist_lengths = []
        self.worker = None

    def matches_broadcast(self, broadcast_id):
        return self.broadcast_id == broadcast_id

    def is_idle(self, now=None):
        now = time.time() if now is None else now
        return now - self.last_used > IDLE_EXPIRY_SECONDS

    def start_poll(self, now=None):
        """Run ``poll`` on a background thread unless one is still running.

        Returns the started thread, or None when the previous poll is still in
        flight (a slow resolve or CDN) and this cycle is skipped.
        """
        requested_at = time.time() if now is None else now
        resumed = requested_at - self.last_used > RESUME_GAP_SECONDS
        self.last_used = requested_at
        if self.worker is not None and self.worker.is_alive():
            return None
        if resumed:
            # Watched again after a break: no poll is running, so it is safe
            # to have the next poll open a fresh report window.
            self.last_report = None
        self.worker = Thread(
            target=self._poll_safely,
            args=(now,),
            name=f"StreamSegmentWatcher-{self.username}",
            daemon=True,
        )
        self.worker.start()
        return self.worker

    def _poll_safely(self, now=None):
        try:
            self.poll(now)
        except Exception:
            logger.warning(
                "Unable to request stream segments for %s",
                self.username,
                exc_info=True,
                extra={"emoji": ":warning:"},
            )

    def poll(self, now=None):
        """Request every not-yet-seen segment from the current playlist.

        Returns True when at least one new segment was requested. Safe to call
        more often than POLL_INTERVAL_SECONDS: extra calls are skipped.
        """
        now = time.time() if now is None else now
        if now - self.last_poll < POLL_INTERVAL_SECONDS:
            return False
        if self.last_report is None:
            # First poll, or the stream is being watched again: give it a
            # full report window before a failure is reported as a stall.
            self._start_report_window(now)
        self.last_poll = now

        requested = self._poll_playlist(now)

        if now - self.last_report >= REPORT_INTERVAL_SECONDS:
            self._report(now)

        return requested

    def _poll_playlist(self, now):
        resolved_now = False
        if self.media_playlist is None:
            if now < self.next_resolve_attempt or not self._resolve(now):
                self.polls_failed += 1
                return False
            resolved_now = True

        response = self._get(self.media_playlist, PLAYLIST_TIMEOUT)
        if (
            response is not None
            and response.status_code in EXPIRED_STATUS_CODES
            and not resolved_now
        ):
            # The cached signed URL expired: resolve a fresh one straight away
            # rather than losing this poll's segments.
            self.media_playlist = None
            if not self._resolve(now):
                self.polls_failed += 1
                return False
            resolved_now = True
            response = self._get(self.media_playlist, PLAYLIST_TIMEOUT)

        if response is None or response.status_code != 200:
            if response is not None:
                self._forget_playlist_if_expired(
                    response.status_code, now, resolved_now
                )
                self.last_failure = f"media playlist HTTP {response.status_code}"
            else:
                self.last_failure = "media playlist request failed"
            self.polls_failed += 1
            return False

        segments = self.parse_playlist(response.text, self.media_playlist, master=False)
        if not segments:
            self.last_failure = "media playlist had no segments"
            self.polls_failed += 1
            return False
        self._measure_window(response.text, len(segments))

        requested = False
        for segment in segments:
            if segment in self.seen_segments:
                continue

            status = self._head(segment)
            if status is not None and 200 <= status < 300:
                self._remember(segment)
                self.segments_requested += 1
                self.last_failure = None
                requested = True
                continue

            # Leave a failed segment unseen so it is retried while the
            # rolling playlist window still lists it.
            self.segments_failed += 1
            if status is None:
                # A timeout or connection error will most likely repeat for
                # the remaining segments; stop so the serial watch loop is not
                # held up for one timeout per segment.
                self.last_failure = "segment request failed"
                break

            self.last_failure = f"segment HTTP {status}"
            # A 404 only means this segment already left the edge cache; the
            # playlist itself is still valid, so move on to the next one.
            if status != 404 and status in EXPIRED_STATUS_CODES:
                # The signed URLs expired; the rest of this playlist would
                # fail the same way until it is re-resolved.
                self._forget_playlist_if_expired(status, now, resolved_now)
                break

        return requested

    def _resolve(self, now):
        """Resolve a fresh media playlist, throttling only failed attempts.

        A successful resolve leaves no cooldown, so a later expiry can be
        recovered from immediately; a failed one waits RESOLVE_RETRY_SECONDS.
        """
        self.media_playlist = self._resolve_media_playlist()
        if self.media_playlist is None:
            self.next_resolve_attempt = now + RESOLVE_RETRY_SECONDS
            return False
        self.next_resolve_attempt = now
        return True

    def _resolve_media_playlist(self):
        try:
            token = self.gql.get_playback_access_token(self.username)
        except Exception as error:  # RetryError and friends
            self.last_failure = f"playback token error ({type(error).__name__})"
            logger.debug(
                "Unable to resolve the playback token for %s (%s)",
                self.username,
                type(error).__name__,
            )
            return None

        if token is None or not token.value or not token.signature:
            self.last_failure = "playback token missing"
            return None

        authorization = getattr(token, "authorization", None)
        if authorization is not None and getattr(authorization, "is_forbidden", False):
            self.last_failure = "playback access forbidden"
            logger.debug(
                "Twitch forbids playback access for %s; skipping segment requests",
                self.username,
            )
            return None

        master_url = (
            MASTER_PLAYLIST_URL.format(login=quote(self.username, safe=""))
            + f"?sig={quote(token.signature, safe='')}"
            + f"&token={quote(token.value, safe='')}"
        )

        response = self._get(master_url, PLAYLIST_TIMEOUT)
        if response is None or response.status_code != 200:
            if response is not None:
                self.last_failure = f"master playlist HTTP {response.status_code}"
            else:
                self.last_failure = "master playlist request failed"
            return None

        variants = self.parse_playlist(response.text, master_url, master=True)
        if not variants:
            self.last_failure = "master playlist had no variants"
            return None
        # The last variant is normally audio-only or the lowest bitrate; only
        # HEAD requests follow it, so the exact choice barely matters.
        return variants[-1]

    def _measure_window(self, text, length):
        """Count segments that left the playlist window between two polls."""
        self.playlist_lengths.append(length)
        first = self.parse_media_sequence(text)
        if first is None:
            return
        if self.last_sequence is not None and first > self.last_sequence + 1:
            self.segments_missed += first - self.last_sequence - 1
        self.last_sequence = max(first + length - 1, self.last_sequence or 0)

    @staticmethod
    def parse_media_sequence(text):
        """Return the playlist's ``#EXT-X-MEDIA-SEQUENCE`` value, if any."""
        for raw_line in (text or "").splitlines():
            line = raw_line.strip()
            if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                try:
                    return int(line.split(":", 1)[1])
                except ValueError:
                    return None
        return None

    @staticmethod
    def parse_playlist(text, base_url, master):
        """Return the absolute URI lines of a master or media playlist.

        Only URIs introduced by ``#EXT-X-STREAM-INF`` (master) or ``#EXTINF``
        (media) are returned; anything malformed yields an empty list.
        """
        urls = []
        if not text or not text.lstrip().startswith("#EXTM3U"):
            return urls

        tag = "#EXT-X-STREAM-INF:" if master else "#EXTINF:"
        pending = False

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("#"):
                if line.startswith(tag):
                    pending = True
                continue
            if not pending:
                continue

            pending = False
            url = urljoin(base_url, line)
            if not url.startswith(("http://", "https://")):
                continue
            if master and not url.split("?", 1)[0].lower().endswith(".m3u8"):
                continue
            urls.append(url)

        return urls

    def _remember(self, segment):
        self.seen_segments[segment] = None
        while len(self.seen_segments) > SEEN_SEGMENT_LIMIT:
            self.seen_segments.popitem(last=False)

    def _start_report_window(self, now):
        self.last_report = now
        self.segments_requested = 0
        self.segments_failed = 0
        self.polls_failed = 0
        self.segments_missed = 0
        self.playlist_lengths = []
        # A gap since the last poll is not a miss between consecutive polls.
        self.last_sequence = None

    def _forget_playlist_if_expired(self, status_code, now, resolved_now):
        # Signed playlist URLs expire and a restarted stream gets new ones.
        if status_code not in EXPIRED_STATUS_CODES:
            return
        self.media_playlist = None
        if resolved_now:
            # Even a freshly resolved URL was rejected; back off instead of
            # re-resolving on every poll.
            self.next_resolve_attempt = now + RESOLVE_RETRY_SECONDS

    def _report(self, now):
        if self.segments_requested > 0:
            if self.reported_stall:
                logger.info(
                    "Stream segment requests are working again for %s",
                    self.username,
                    extra={"emoji": ":white_check_mark:"},
                )
                self.reported_stall = False
            lengths = self.playlist_lengths
            logger.debug(
                "Requested %s new stream segments for %s (%s failed, "
                "%s failed polls; playlist window %s-%s segments, %s missed "
                "between polls)",
                self.segments_requested,
                self.username,
                self.segments_failed,
                self.polls_failed,
                min(lengths) if lengths else 0,
                max(lengths) if lengths else 0,
                self.segments_missed,
            )
        elif not self.reported_stall:
            logger.warning(
                "No stream segment could be requested for %s (%s failed, "
                "%s failed polls; last failure: %s), so Twitch will not "
                "count this watch time",
                self.username,
                self.segments_failed,
                self.polls_failed,
                self.last_failure or "unknown",
                extra={"emoji": ":warning:"},
            )
            self.reported_stall = True

        # Consecutive polls continue, so keep last_sequence for miss counting.
        last_sequence = self.last_sequence
        self._start_report_window(now)
        self.last_sequence = last_sequence

    def _get(self, url, timeout):
        try:
            return requests.get(
                url,
                headers={"User-Agent": self.user_agent},
                timeout=timeout,
            )
        except requests.exceptions.RequestException as error:
            # Only the type: the message can carry the signed URL.
            logger.debug("Stream playlist request failed (%s)", type(error).__name__)
            return None

    def _head(self, url):
        try:
            # HEAD does not follow redirects by default; follow them so a CDN
            # redirect to an edge node still reaches (and counts) the segment.
            response = requests.head(
                url,
                headers={"User-Agent": self.user_agent},
                timeout=SEGMENT_TIMEOUT,
                allow_redirects=True,
            )
            return response.status_code
        except requests.exceptions.RequestException as error:
            logger.debug("Stream segment request failed (%s)", type(error).__name__)
            return None
