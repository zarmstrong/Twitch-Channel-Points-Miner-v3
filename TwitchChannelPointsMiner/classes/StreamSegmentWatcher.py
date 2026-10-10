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
# Report a stall at most this often.
REPORT_INTERVAL_SECONDS = 300
# Bound the per-watch memory of already-requested segments.
SEEN_SEGMENT_LIMIT = 256


class StreamSegmentWatcher(object):
    """Polls one watched stream's media playlist and HEADs new segments.

    A single instance tracks a single (username, broadcast) pair. It performs
    no background work; its owner calls ``poll`` on the existing watch cadence
    and drops the instance once the stream is no longer watched.
    """

    __slots__ = [
        "username",
        "gql",
        "user_agent",
        "broadcast_id",
        "media_playlist",
        "next_resolve_attempt",
        "last_poll",
        "last_report",
        "seen_segments",
        "seen_order",
        "segments_requested",
        "segments_failed",
        "polls_failed",
        "reported_stall",
        "last_failure",
    ]

    def __init__(self, username, gql, user_agent, broadcast_id=None):
        self.username = username
        self.gql = gql
        self.user_agent = user_agent
        self.broadcast_id = broadcast_id
        self.media_playlist = None
        self.next_resolve_attempt = 0.0
        self.last_poll = 0.0
        self.last_report = 0.0
        self.seen_segments = set()
        self.seen_order = []
        self.segments_requested = 0
        self.segments_failed = 0
        self.polls_failed = 0
        self.reported_stall = False
        self.last_failure = None

    def matches_broadcast(self, broadcast_id):
        return self.broadcast_id == broadcast_id

    def poll(self, now=None):
        """Request every not-yet-seen segment from the current playlist.

        Returns True when at least one new segment was requested. Safe to call
        more often than POLL_INTERVAL_SECONDS: extra calls are skipped.
        """
        now = time.time() if now is None else now
        if now - self.last_poll < POLL_INTERVAL_SECONDS:
            return False
        self.last_poll = now

        requested = self._poll_playlist(now)

        if now - self.last_report >= REPORT_INTERVAL_SECONDS:
            self._report(now)

        return requested

    def _poll_playlist(self, now):
        if self.media_playlist is None and now >= self.next_resolve_attempt:
            # At most once a minute: a failed resolve logs nothing sensitive
            # and re-uses the cached URL until it actually expires.
            self.next_resolve_attempt = now + RESOLVE_RETRY_SECONDS
            self.media_playlist = self._resolve_media_playlist()

        if self.media_playlist is None:
            self.polls_failed += 1
            return False

        response = self._get(self.media_playlist, PLAYLIST_TIMEOUT)
        if response is None or response.status_code != 200:
            if response is not None:
                self._forget_playlist_if_expired(response.status_code)
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

        requested = False
        for segment in segments:
            if segment in self.seen_segments:
                continue

            status = self._head(segment)
            if status == 200:
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
            self._forget_playlist_if_expired(status)
            if self.media_playlist is None:
                # The signed URLs expired; the rest of this playlist would
                # fail the same way until it is re-resolved.
                break

        return requested

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
        if segment in self.seen_segments:
            return
        self.seen_segments.add(segment)
        self.seen_order.append(segment)
        while len(self.seen_order) > SEEN_SEGMENT_LIMIT:
            self.seen_segments.discard(self.seen_order.pop(0))

    def _forget_playlist_if_expired(self, status_code):
        # Signed playlist URLs expire and a restarted stream gets new ones.
        if status_code in (401, 403, 404, 410):
            self.media_playlist = None

    def _report(self, now):
        self.last_report = now
        if self.segments_requested > 0:
            if self.reported_stall:
                logger.info(
                    "Stream segment requests are working again for %s",
                    self.username,
                    extra={"emoji": ":white_check_mark:"},
                )
                self.reported_stall = False
            logger.debug(
                "Requested %s new stream segments for %s (%s failed, "
                "%s failed polls)",
                self.segments_requested,
                self.username,
                self.segments_failed,
                self.polls_failed,
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

        self.segments_requested = 0
        self.segments_failed = 0
        self.polls_failed = 0

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
            response = requests.head(
                url,
                headers={"User-Agent": self.user_agent},
                timeout=SEGMENT_TIMEOUT,
            )
            return response.status_code
        except requests.exceptions.RequestException as error:
            logger.debug("Stream segment request failed (%s)", type(error).__name__)
            return None
