import json
import logging
import threading
import time
from base64 import b64encode

from TwitchChannelPointsMiner.classes.Settings import Settings
from TwitchChannelPointsMiner.constants import DROP_ID

logger = logging.getLogger(__name__)

# The minute watcher posts for a watched stream about every 20 seconds. A
# longer gap between two successful posts means the stream was out of a watch
# slot (or the watcher stalled), and that time was not watched: count at most
# this much of it so minute_watched tracks real watch time.
MINUTE_WATCHED_MAX_STEP_SECONDS = 60

# Guards a stream's per-broadcast watch-streak state, so giving up on one
# broadcast's streak can't land after another thread has reset the state for
# the next broadcast.
_WATCH_STREAK_LOCK = threading.Lock()


class Stream(object):
    __slots__ = [
        "broadcast_id",
        "title",
        "game",
        "tags",
        "drops_tags",
        "campaigns",
        "campaigns_ids",
        "channel_campaign_ids",
        "viewers_count",
        "spade_url",
        "payload",
        "watch_streak_missing",
        "watch_streak_checked_at",
        "watch_streak_final_checks",
        "watch_streak_check_due",
        "watch_streak_broadcast_id",
        "watch_streak_transition_to",
        "minute_watched",
        "watch_slot_minutes",
        "live_seen_at",
        "__last_update",
        "__refresh_requested",
        "__minute_watched_timestamp",
        "__watch_slot_timestamp",
    ]

    def __init__(self):
        self.broadcast_id = None

        self.title = None
        self.game = {}
        self.tags = []

        self.drops_tags = False
        self.campaigns = []
        self.campaigns_ids = []
        # The subset of campaigns_ids Twitch advertised on this channel itself.
        self.channel_campaign_ids = []

        self.viewers_count = 0
        self.__last_update = 0
        # Set by request_refresh(): the next online check must ask Twitch.
        self.__refresh_requested = False

        self.spade_url = None
        self.payload = None
        self.live_seen_at = 0
        # The broadcast a missed-offline transition was last claimed for.
        self.watch_streak_transition_to = None

        self.init_watch_streak()

    def encode_payload(self) -> dict:
        json_event = json.dumps(self.payload, separators=(",", ":"))
        return {"data": (b64encode(json_event.encode("utf-8"))).decode("utf-8")}

    def update(
        self,
        broadcast_id,
        title,
        game,
        tags,
        viewers_count,
        seen_live_at=None,
    ):
        """Apply a stream-info response.

        Callers hold the streamer's state lock (streamer_state_lock()), so
        refreshes of one stream never overlap and replies apply in the order
        Twitch answered them.
        """
        now = time.time()
        # When Twitch last showed this stream live: the caller's request time
        # when given, since the response may arrive (and be applied) later.
        seen = now if seen_live_at is None else float(seen_live_at)
        # #343 temporary workaround
        tags = tags or []
        # ------------------------
        # Applied as one step, so live_snapshot() never pairs one broadcast's
        # ID with another's time and two responses can't interleave.
        with _WATCH_STREAK_LOCK:
            if broadcast_id != self.broadcast_id:
                # What Twitch advertised on the last broadcast proves nothing
                # about this one.
                self.channel_campaign_ids = []
            self.title = title.strip()
            self.game = game
            self.tags = tags
            self.viewers_count = viewers_count
            self.drops_tags = (
                DROP_ID in [tag["id"] for tag in self.tags] and self.game != {}
            )
            self.__last_update = now
            self.__refresh_requested = False
            self.broadcast_id = broadcast_id
            self.live_seen_at = seen

        logger.debug(f"Update: {self}")

    def __repr__(self):
        return f"Stream(title={self.title}, game={self.__str_game()}, tags={self.__str_tags()})"

    def __str__(self):
        return f"{self.title}" if Settings.logger.less else self.__repr__()

    def __str_tags(self):
        return (
            None
            if self.tags == []
            else ", ".join([tag["localizedName"] for tag in self.tags])
        )

    def __str_game(self):
        return None if self.game in [{}, None] else self.game["displayName"]

    def game_name(self):
        return None if self.game in [{}, None] else self.game["name"]

    def game_id(self):
        return None if self.game in [{}, None] else self.game["id"]

    def update_required(self):
        return (
            self.__last_update == 0
            or self.__refresh_requested
            or self.update_elapsed() >= 120
        )

    def update_elapsed(self):
        return 0 if self.__last_update == 0 else (time.time() - self.__last_update)

    def request_refresh(self):
        """Make the next online check fetch stream info again, including the
        minute watcher's, which otherwise waits for its staleness window."""
        with _WATCH_STREAK_LOCK:
            self.__refresh_requested = True

    def refresh_requested(self):
        return self.__refresh_requested

    def live_snapshot(self):
        """Return (broadcast_id, live_seen_at) as one consistent pair."""
        with _WATCH_STREAK_LOCK:
            return self.broadcast_id, self.live_seen_at

    def init_watch_streak(self):
        with _WATCH_STREAK_LOCK:
            # The broadcast this state belongs to. broadcast_id itself can
            # move to the next broadcast on a refresh before the transition
            # resets this state for it.
            self.watch_streak_broadcast_id = self.broadcast_id
            self.watch_streak_missing = True
            self.watch_streak_checked_at = 0
            # Answered milestone checks after a streak_only window closed.
            self.watch_streak_final_checks = 0
            # A milestone check queued for the minute watcher's budgeted
            # rechecks, ahead of the usual watched-minutes wait.
            self.watch_streak_check_due = False
            self.minute_watched = 0
            self.__minute_watched_timestamp = 0
            # Time holding a watch slot, whether or not Twitch accepted the
            # minute-watched posts: what watch_streak_max_minutes measures,
            # so a channel whose posts keep failing still lets its slot go.
            self.watch_slot_minutes = 0
            self.__watch_slot_timestamp = 0

    def claim_broadcast_transition(self, previous_broadcast_id, broadcast_id):
        """Claim the missed-offline move from one broadcast to the next.

        Several threads refresh a stream, and each can see the same change.
        Only the first claim wins, and only while the streak state still
        belongs to the previous broadcast, so the next broadcast's counters
        are reset once, not again by a slower check.
        """
        with _WATCH_STREAK_LOCK:
            if self.watch_streak_broadcast_id != previous_broadcast_id:
                return False
            if self.watch_streak_transition_to == broadcast_id:
                return False
            self.watch_streak_transition_to = broadcast_id
            return True

    def watch_streak_check_snapshot(self):
        """Return (broadcast the streak state belongs to, watched minutes).

        Read together, so a check never pairs one broadcast's minutes with
        the next broadcast's state.
        """
        with _WATCH_STREAK_LOCK:
            return (
                self.watch_streak_broadcast_id or self.broadcast_id,
                self.minute_watched,
            )

    def record_final_watch_streak_check(self, broadcast_id):
        """Count an answered check made after a streak_only window closed,
        unless the state has been reset for another broadcast since: a slow
        check of the old broadcast must not count for the new one.
        """
        with _WATCH_STREAK_LOCK:
            if (self.watch_streak_broadcast_id or self.broadcast_id) != broadcast_id:
                return False
            self.watch_streak_final_checks += 1
            return True

    def credit_watch_streak(self):
        """Clear the pending streak for an award; return its broadcast.

        The broadcast is the one this streak state belongs to, read in the
        same locked step as the clear, so a reset for the next broadcast
        can't land in between and receive this award.
        """
        with _WATCH_STREAK_LOCK:
            self.watch_streak_missing = False
            return self.watch_streak_broadcast_id or self.broadcast_id

    def confirm_watch_streak_for(self, broadcast_id):
        """Clear the pending streak if it still belongs to broadcast_id.

        A check started on one broadcast must not clear the next one's
        streak: the ID test and the clear happen under the lock that the
        per-broadcast reset also takes.
        """
        with _WATCH_STREAK_LOCK:
            if self.broadcast_id != broadcast_id:
                return False
            self.watch_streak_missing = False
            return True

    def stop_waiting_for_watch_streak(self, max_minutes):
        """Stop treating the streak as pending once max_minutes are watched.

        Checked and changed together under the lock, so the minutes read are
        the current broadcast's. Returns whether the streak was given up.
        """
        with _WATCH_STREAK_LOCK:
            if self.watch_streak_missing is not True or (
                self.__watch_streak_minutes_locked() < max_minutes
            ):
                return False
            self.watch_streak_missing = False
            return True

    def watch_streak_minutes(self):
        """Minutes that count toward watch_streak_max_minutes."""
        with _WATCH_STREAK_LOCK:
            return self.__watch_streak_minutes_locked()

    def __watch_streak_minutes_locked(self):
        # Includes the slot time since the last cycle, which hold_watch_slot()
        # adds only after the cap is checked: a slow cycle would otherwise
        # keep a capped channel prioritized for one more cycle.
        slot_minutes = self.watch_slot_minutes or 0
        if self.__watch_slot_timestamp != 0:
            elapsed = max(time.monotonic() - self.__watch_slot_timestamp, 0)
            slot_minutes += round(elapsed / 60, 5)
        return max(self.minute_watched or 0, slot_minutes)

    def hold_watch_slot(self):
        """Count time in a watch slot; called each cycle the stream has one.

        Elapsed time in full, unlike minute_watched: a slow cycle still held
        the slot for all of it. Measured on the monotonic clock, so a wall
        clock correction can't add or remove slot time.
        """
        with _WATCH_STREAK_LOCK:
            now = time.monotonic()
            self.__count_watch_slot_step(now)
            self.__watch_slot_timestamp = now

    def __count_watch_slot_step(self, now):
        if self.__watch_slot_timestamp != 0:
            elapsed = max(now - self.__watch_slot_timestamp, 0)
            self.watch_slot_minutes += round(elapsed / 60, 5)

    def pause_minute_watched(self):
        """Stop counting: the stream is out of a watch slot.

        The time from the last successful post (and the last cycle in the
        slot) until the slot was lost was still watched, so it is counted
        first. Otherwise a channel that only gets single-cycle slots would
        never count any time at all.
        """
        with _WATCH_STREAK_LOCK:
            self.__count_minute_watched_step(time.time())
            self.__minute_watched_timestamp = 0
            self.__count_watch_slot_step(time.monotonic())
            self.__watch_slot_timestamp = 0

    def update_minute_watched(self):
        # Under the lock the per-broadcast reset takes, so a step measured on
        # one broadcast can't be added to the next broadcast's fresh count.
        with _WATCH_STREAK_LOCK:
            now = time.time()
            self.__count_minute_watched_step(now)
            self.__minute_watched_timestamp = now

    def __count_minute_watched_step(self, now):
        if self.__minute_watched_timestamp != 0:
            self.minute_watched += self.__step_minutes(
                now, self.__minute_watched_timestamp
            )

    @staticmethod
    def __step_minutes(now, since):
        elapsed = min(max(now - since, 0), MINUTE_WATCHED_MAX_STEP_SECONDS)
        return round(elapsed / 60, 5)
