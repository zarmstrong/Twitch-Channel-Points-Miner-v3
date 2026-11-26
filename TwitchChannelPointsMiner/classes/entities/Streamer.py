import json
import logging
import os
import time
from datetime import datetime
from threading import Lock, RLock, Thread

from TwitchChannelPointsMiner.classes.Chat import (
    DEFAULT_GIFT_SUB_MESSAGE,
    ChatPresence,
    ThreadChat,
)
from TwitchChannelPointsMiner.classes.entities.Bet import BetSettings, DelayMode
from TwitchChannelPointsMiner.classes.entities.Stream import Stream
from TwitchChannelPointsMiner.classes.Settings import (
    ANALYTICS_FILE_MUTEX,
    Events,
    Settings,
    StreamerSource,
)
from TwitchChannelPointsMiner.constants import URL, WATCH_STREAK_CLOCK_SKEW_SECONDS
from TwitchChannelPointsMiner.data_migration import ANALYTICS_DATA_VERSION
from TwitchChannelPointsMiner.utils import _millify

logger = logging.getLogger(__name__)

# Used to rank a streamer's tiers when the caller has no user-configured
# streamer_source_priority; mirrors the miner's default order.
DEFAULT_SOURCE_PRIORITY = (
    StreamerSource.STREAMERS,
    StreamerSource.FOLLOWERS,
    StreamerSource.CATEGORIES,
    StreamerSource.BADGES,
    StreamerSource.WILDCARD_CATEGORIES,
)
CATEGORY_TIER_SOURCES = frozenset(
    (StreamerSource.CATEGORIES, StreamerSource.WILDCARD_CATEGORIES)
)
DROP_DISCOVERY_SOURCES = CATEGORY_TIER_SOURCES | {StreamerSource.BADGES}


def discovery_sources(streamer):
    """Return every StreamerSource tier a streamer belongs to.

    from_category is also set on badge-campaign and wildcard streamers, so it
    only means the regular CATEGORIES tier when neither of those is set.
    Flags are read with getattr so lightweight stand-ins missing some
    attributes classify like a real Streamer.
    """
    is_badge = getattr(streamer, "from_badge_campaign", False) is True
    is_wildcard = getattr(streamer, "from_wildcard_category", False) is True
    sources = set()
    if is_badge:
        sources.add(StreamerSource.BADGES)
    if is_wildcard:
        sources.add(StreamerSource.WILDCARD_CATEGORIES)
    if (
        getattr(streamer, "from_category", False) is True
        and not is_badge
        and not is_wildcard
    ):
        sources.add(StreamerSource.CATEGORIES)
    if getattr(streamer, "from_followers", False) is True:
        sources.add(StreamerSource.FOLLOWERS)
    if getattr(streamer, "explicitly_configured", False) is True or not sources:
        sources.add(StreamerSource.STREAMERS)
    return frozenset(sources)


def discovery_source(streamer, source_priority=None):
    """Return the single tier a streamer is ranked under.

    A streamer that belongs to several tiers (for example one the user
    configured that is also followed) is placed in whichever the user ranks
    highest in streamer_source_priority, so the ordering the user chose
    applies to overlaps too.
    """
    sources = discovery_sources(streamer)
    for source in source_priority or DEFAULT_SOURCE_PRIORITY:
        if source in sources:
            return source
    for source in DEFAULT_SOURCE_PRIORITY:
        if source in sources:
            return source
    return StreamerSource.STREAMERS


def is_category_tier(streamer):
    """True for real category and wildcard-category discovery streamers.

    Badge-campaign streamers also carry from_category=True but are excluded.
    """
    sources = discovery_sources(streamer)
    return bool(sources & CATEGORY_TIER_SOURCES) and (
        StreamerSource.BADGES not in sources
    )


def is_drop_discovered(streamer):
    """True for any streamer found to farm drops (category, wildcard or badge),
    regardless of which tier the user's priority ranks it under."""
    return bool(discovery_sources(streamer) & DROP_DISCOVERY_SOURCES)


# Serializes chat joins and leaves: online/offline changes arrive from both the
# websocket and the watch worker, and two joins must not both start the same
# ThreadChat. Module-level, so Streamer's __slots__ and copies are unaffected;
# it is reentrant because toggle_chat() calls leave_chat().
_chat_lifecycle_lock = RLock()

# How many online transitions Streamer.online_history keeps.
ONLINE_HISTORY_SIZE = 5

# Two reentrant locks per channel, keyed by username so a reloaded Streamer
# object for the same channel shares them; module-level for the same reason
# as the chat lock.
#
# The refresh lock is held for a whole stream-info refresh, network requests
# included. The main loop, the minute watcher and the websockets all refresh
# streamers, and overlapping refreshes could apply their replies in another
# order than Twitch answered them.
#
# The state lock is held only while a refresh applies its reply and runs the
# online/offline change it leads to, and by the websocket handlers that change
# or read that state. It is never held across a network request, so a
# websocket message never waits for Twitch. Take the refresh lock first when
# both are needed.
#
# The generation counts websocket stream-downs, requested refreshes and
# online/offline changes. A refresh that sees it move while its requests were
# out drops the reply: something newer than that reply happened meanwhile.
_streamer_refresh_locks = {}
_streamer_state_locks = {}
_streamer_state_generations = {}
_streamer_state_locks_guard = Lock()


def _keyed_lock(locks, username):
    with _streamer_state_locks_guard:
        lock = locks.get(username)
        if lock is None:
            lock = locks[username] = RLock()
        return lock


def streamer_refresh_lock(username):
    """Return the lock that serializes this channel's stream-info refreshes."""
    return _keyed_lock(_streamer_refresh_locks, username)


def streamer_state_lock(username):
    """Return the lock that guards this channel's online state."""
    return _keyed_lock(_streamer_state_locks, username)


def run_under_state_lock(username, action, name):
    """Run action() under the channel's state lock without waiting for it.

    At once when the lock is free; otherwise on a short-lived thread that
    waits for it. A refresh applying its reply holds the lock while it saves
    the watch-streak cache and joins or leaves chat, and a websocket handler
    must not wait for that: the socket's later messages would wait too.
    """
    lock = streamer_state_lock(username)
    if lock.acquire(blocking=False):
        try:
            action()
        finally:
            lock.release()
        return

    def deferred():
        with lock:
            try:
                action()
            except Exception:
                logger.error(
                    f"Exception raised in {name} for {username}", exc_info=True
                )

    Thread(target=deferred, name=f"{name}-{username}", daemon=True).start()


def streamer_state_generation(username):
    """Return this channel's state generation (see bump_streamer_state())."""
    with _streamer_state_locks_guard:
        return _streamer_state_generations.get(username, 0)


def bump_streamer_state(username):
    """Mark that something newer than any refresh reply in flight happened."""
    with _streamer_state_locks_guard:
        _streamer_state_generations[username] = (
            _streamer_state_generations.get(username, 0) + 1
        )


class StreamerSettings(object):
    __slots__ = [
        "make_predictions",
        "follow_raid",
        "claim_drops",
        "claim_moments",
        "watch_streak",
        "streak_only",
        "favorite",
        "points_limit",
        "community_goals",
        "bet",
        "chat",
        "gift_sub_thanks",
        "gift_sub_message",
    ]

    def __init__(
        self,
        make_predictions: bool = None,
        follow_raid: bool = None,
        claim_drops: bool = None,
        claim_moments: bool = None,
        watch_streak: bool = None,
        favorite: bool = None,
        points_limit: int = None,
        community_goals: bool = None,
        bet: BetSettings = None,
        chat: ChatPresence = None,
        gift_sub_thanks: bool = None,
        gift_sub_message: str = None,
        # Last, so configs that pass the earlier settings positionally keep
        # their meaning.
        streak_only: bool | int = None,
    ):
        self.make_predictions = make_predictions
        self.follow_raid = follow_raid
        self.claim_drops = claim_drops
        self.claim_moments = claim_moments
        self.watch_streak = watch_streak
        self.streak_only = streak_only
        self.favorite = favorite
        self.points_limit = points_limit
        self.community_goals = community_goals
        self.bet = bet
        self.chat = chat
        self.gift_sub_thanks = gift_sub_thanks
        self.gift_sub_message = gift_sub_message

    def default(self):
        for name in [
            "make_predictions",
            "follow_raid",
            "claim_drops",
            "claim_moments",
            "watch_streak",
        ]:
            if getattr(self, name) is None:
                setattr(self, name, True)
        if self.community_goals is None:
            self.community_goals = False
        if self.favorite is None:
            self.favorite = False
        if self.streak_only is None:
            self.streak_only = False
        if self.bet is None:
            self.bet = BetSettings()
        if self.chat is None:
            self.chat = ChatPresence.ONLINE
        if self.gift_sub_thanks is None:
            self.gift_sub_thanks = False
        if self.gift_sub_message is None:
            self.gift_sub_message = DEFAULT_GIFT_SUB_MESSAGE

    def __repr__(self):
        return f"StreamerSettings(make_predictions={self.make_predictions}, follow_raid={self.follow_raid}, claim_drops={self.claim_drops}, claim_moments={self.claim_moments}, watch_streak={self.watch_streak}, streak_only={self.streak_only}, favorite={self.favorite}, points_limit={self.points_limit}, community_goals={self.community_goals}, bet={self.bet}, chat={self.chat}, gift_sub_thanks={self.gift_sub_thanks}, gift_sub_message={self.gift_sub_message!r})"


class Streamer(object):
    __slots__ = [
        "username",
        "from_followers",
        "from_category",
        "from_badge_campaign",
        "from_wildcard_category",
        "discovery_category_slugs",
        "explicitly_configured",
        "channel_id",
        "settings",
        "is_online",
        "is_watching",
        "stream_up",
        "online_at",
        "online_history",
        "offline_at",
        "channel_points",
        "community_goals",
        "minute_watched_requests",
        "viewer_is_mod",
        "activeMultipliers",
        "irc_chat",
        "stream",
        "raid",
        "history",
        "streamer_url",
        "mutex",
        "last_drop_watch_signature",
        "watch_streak_cache",
    ]

    def __init__(
        self,
        username,
        settings=None,
        from_category=False,
        explicitly_configured=False,
        from_badge_campaign=False,
        from_followers=False,
        from_wildcard_category=False,
        discovery_category_slugs=None,
    ):
        self.username: str = username.lower().strip()
        self.from_followers = from_followers
        self.from_category = from_category
        self.from_badge_campaign = from_badge_campaign
        self.from_wildcard_category = from_wildcard_category
        # Configured category slug(s) this channel was discovered under, so a
        # category refresh can map it back to its category before its first
        # stream update reports a game. None for channels not found that way.
        self.discovery_category_slugs = (
            set(discovery_category_slugs) if discovery_category_slugs else None
        )
        self.explicitly_configured = explicitly_configured
        self.channel_id: str = ""
        self.settings = settings
        self.is_online = False
        self.is_watching = False
        self.stream_up = 0
        self.online_at = 0
        # (broadcast ID, online_at) for the last few times the miner marked
        # this streamer online, oldest first: which broadcast was online when
        # Twitch sent a late message.
        self.online_history = ()
        self.offline_at = 0
        self.channel_points = 0
        self.community_goals = {}
        self.minute_watched_requests = None
        self.viewer_is_mod = False
        self.activeMultipliers = None
        self.irc_chat = None

        self.stream = Stream()

        self.raid = None
        self.history = {}
        self.last_drop_watch_signature = None
        self.watch_streak_cache = None

        self.streamer_url = f"{URL}/{self.username}"

        self.mutex = Lock()

    def __repr__(self):
        return f"Streamer(username={self.username}, channel_id={self.channel_id}, channel_points={_millify(self.channel_points)})"

    def __str__(self):
        return (
            f"{self.username} ({_millify(self.channel_points)} points)"
            if Settings.logger.less
            else self.__repr__()
        )

    def discovery_source(self):
        return discovery_source(self)

    def set_offline(self, broadcast_id=None, seen_live_at=None):
        """Mark the streamer offline.

        broadcast_id and seen_live_at describe the broadcast that ended when
        the stream already shows a newer one (an offline the miner missed).
        """
        if broadcast_id is None:
            live_snapshot = getattr(self.stream, "live_snapshot", None)
            if callable(live_snapshot):
                # One read of both: a refresh on another thread landing
                # between two reads would pair this broadcast's ID with the
                # next broadcast's last-seen time and record a late end.
                broadcast_id, snapshot_seen_live_at = live_snapshot()
                if seen_live_at is None:
                    # 0 (never seen live) still means "unknown" downstream.
                    seen_live_at = snapshot_seen_live_at or 0
            else:
                broadcast_id = self.stream.broadcast_id
        if self.is_online is True:
            self.offline_at = time.time()
            self.is_online = False
            bump_streamer_state(self.username)
            # Stop the watch timers now rather than at the watcher's next
            # cycle, and keep them stopped until the stream is back online:
            # if the same broadcast comes back, its streak state carries on,
            # and the offline time must not count.
            stop = getattr(self.stream, "stop_watch_timers", None)
            if callable(stop):
                stop()

            if self.watch_streak_cache is not None:
                ended_at, seen_live = self._last_seen_live_at(seen_live_at)
                self.watch_streak_cache.mark_ended(
                    self.username,
                    broadcast_id,
                    ended_at=ended_at,
                    seen_live=seen_live,
                )

        self.toggle_chat()

        logger.info(
            f"{self} is Offline!",
            extra={
                "emoji": ":sleeping:",
                "event": Events.STREAMER_OFFLINE,
            },
        )

    def set_online(self, drops_description=None):
        if self.is_online is False:
            self.online_at = time.time()
            self.online_history = (getattr(self, "online_history", ()) or ()) + (
                (self.stream.broadcast_id, self.online_at),
            )
            self.online_history = self.online_history[-ONLINE_HISTORY_SIZE:]
            self.is_online = True
            bump_streamer_state(self.username)
            resume = getattr(self.stream, "resume_watch_streak", None)
            if callable(resume):
                resumed = resume() is True
            else:
                self.stream.init_watch_streak()
                resumed = False
            if resumed:
                # Back online on the broadcast the streak state belongs to (a
                # brief stream-down, or a refresh that failed): the same
                # broadcast, so its watched minutes, cap and claim carry on,
                # and its cache session is live again.
                if self.watch_streak_cache is not None:
                    self.watch_streak_cache.reopen(
                        self.username, self.stream.broadcast_id
                    )
            elif self.watch_streak_cache is not None:
                session = self.watch_streak_cache.ensure(
                    self.username,
                    self.stream.broadcast_id,
                    started_at=self.online_at,
                )
                if session is not None and session.ended_at is not None:
                    # A broadcast the cache had seen end is live again (marked
                    # offline before the miner restarted): reopen it, so its
                    # real end is recorded when it comes.
                    self.watch_streak_cache.reopen(
                        self.username, self.stream.broadcast_id
                    )
                # An award processed before this transition is not carried
                # over: receipt order can't tell this broadcast's award from a
                # late one for the previous broadcast. The milestone check
                # that follows going online confirms it from Twitch's data.
                if session is not None and session.claimed is True:
                    self.stream.watch_streak_missing = False
                elif (
                    self.watch_streak_cache.claimed_at_for_logical_stream(
                        self.username,
                        self.stream.broadcast_id,
                        started_at=self.online_at,
                    )
                    is not None
                ):
                    # A broadcast that started less than 30 minutes after the
                    # previous one ended is the same Twitch stream for streak
                    # purposes. If that stream was already claimed, do not farm
                    # this continuation again.
                    self.stream.watch_streak_missing = False

        self.toggle_chat()

        online_message = (
            f"{self} is Online for {drops_description}"
            if drops_description
            else f"{self} is Online!"
        )
        logger.info(
            online_message,
            extra={
                "emoji": ":partying_face:",
                "event": Events.STREAMER_ONLINE,
            },
        )

    def _last_seen_live_at(self, seen_live_at=None):
        """The earliest the broadcast that just went offline can have ended.

        offline_at is when the miner noticed, which can be any amount later
        (a missed stream-down, a network outage, a suspended machine). The
        last stream-info refresh proves the broadcast was still live then,
        so a gap measured from it is never shorter than the real one and a
        claim is never carried into a broadcast that started 30 or more
        minutes after the previous one ended.

        Returns (time, seen_live): seen_live is False when no live refresh is
        known and offline_at, which may be late, is the only time available.
        """
        last_seen = (
            getattr(self.stream, "live_seen_at", 0)
            if seen_live_at is None
            else seen_live_at
        ) or 0
        if 0 < last_seen <= self.offline_at:
            return last_seen, True
        return self.offline_at, False

    def print_history(self):
        return "; ".join(
            [
                f"{key} ({self.history[key]['counter']} times, {_millify(self.history[key]['amount'])} gained)"
                for key in sorted(self.history)
                if self.history[key]["counter"] != 0
            ]
        )

    def sent_before_online(self, sent_at):
        """Whether Twitch sent something at sent_at (epoch seconds) before the
        miner last marked this streamer online.

        Before that moment the miner may not have known about the current
        broadcast yet, so such a message can be about an earlier one. Compares
        Twitch's clock with the local one, so anything sent up to
        WATCH_STREAK_CLOCK_SKEW_SECONDS after counts too: a stream-down that
        may be stale is rechecked rather than applied, and an award that may
        be late is not credited to a broadcast the miner had only just found.
        """
        since = getattr(self, "online_at", 0) or 0
        return (
            sent_at is not None
            and since > 0
            and sent_at < since + WATCH_STREAK_CLOCK_SKEW_SECONDS
        )

    def broadcast_online_at(self, sent_at):
        """The broadcast the miner had marked online when Twitch sent
        something at sent_at, or None if that is older than the history."""
        for broadcast_id, online_at in reversed(
            getattr(self, "online_history", ()) or ()
        ):
            if online_at <= sent_at:
                return broadcast_id
        return None

    def update_history(self, reason_code, earned, counter=1, awarded_at=None):
        if reason_code not in self.history:
            self.history[reason_code] = {"counter": 0, "amount": 0}
        self.history[reason_code]["counter"] += counter
        self.history[reason_code]["amount"] += earned

        if reason_code == "WATCH_STREAK":
            # The channel lock keeps a broadcast transition from landing
            # between deciding which broadcast earned this and crediting it.
            run_under_state_lock(
                self.username,
                lambda: self.__credit_watch_streak(awarded_at),
                "watch-streak-award",
            )

    def __credit_watch_streak(self, awarded_at):
        if self.sent_before_online(awarded_at):
            # Delivered after the miner marked the current broadcast online,
            # but sent before (or too close to tell, on two clocks): earned on
            # whichever broadcast was online then, taking the earlier one when
            # it is too close to tell and the miner knew one. Credit it only if the cache tracks that
            # one (creating a session now would close the current
            # broadcast's), and leave the current broadcast's streak pending:
            # if the award was its own after all, the milestone check finds
            # it in Twitch's data.
            earned_on = self.broadcast_online_at(
                awarded_at - WATCH_STREAK_CLOCK_SKEW_SECONDS
            ) or self.broadcast_online_at(awarded_at)
            current = self.stream.watch_streak_broadcast_id or self.stream.broadcast_id
            if (
                earned_on not in (None, "", current)
                and self.watch_streak_cache is not None
                and self.watch_streak_cache.get(self.username, earned_on)
            ):
                self.watch_streak_cache.mark_claimed(self.username, earned_on)
            if earned_on != current:
                logger.info(
                    "[watch-streak] "
                    f"Twitch confirmed streak for streamer={self.username} on "
                    f"an earlier broadcast ({earned_on}); still pending for "
                    "this one"
                )
                return
        # Credited to the broadcast the streak state belongs to. A refresh
        # that found the next broadcast runs its transition in the same hold
        # of the state lock this runs under, so the two are never split. An
        # award processed just before such a refresh can still be the next
        # broadcast's (Twitch started it before the miner noticed): the
        # transition queues a milestone check, which confirms it from
        # Twitch's data instead of farming that broadcast up to the cap.
        credit = getattr(self.stream, "credit_watch_streak", None)
        if callable(credit):
            broadcast_id = credit()
        else:
            self.stream.watch_streak_missing = False
            broadcast_id = self.stream.broadcast_id
        if self.watch_streak_cache is not None:
            self.watch_streak_cache.mark_claimed(self.username, broadcast_id)
        logger.info(
            "[watch-streak] "
            f"Twitch confirmed streak for streamer={self.username}; "
            f"local watched={self.stream.minute_watched:.2f}m"
        )

    def stream_up_elapsed(self):
        return self.stream_up == 0 or ((time.time() - self.stream_up) > 120)

    def drops_condition(self):
        has_watchable_campaign_drops = any(
            campaign.has_watchable_drops() for campaign in self.stream.campaigns
        )
        return (
            self.settings.claim_drops is True
            and self.is_online is True
            # and self.stream.drops_tags is True
            and self.stream.campaigns_ids != []
            and has_watchable_campaign_drops is True
        )

    def viewer_has_points_multiplier(self):
        return self.activeMultipliers is not None and len(self.activeMultipliers) > 0

    def total_points_multiplier(self):
        return (
            sum(
                map(
                    lambda x: x["factor"],
                    self.activeMultipliers,
                ),
            )
            if self.activeMultipliers is not None
            else 0
        )

    def get_prediction_window(self, prediction_window_seconds):
        delay_mode = self.settings.bet.delay_mode
        delay = self.settings.bet.delay
        if delay_mode == DelayMode.FROM_START:
            return min(delay, prediction_window_seconds)
        elif delay_mode == DelayMode.FROM_END:
            return max(prediction_window_seconds - delay, 0)
        elif delay_mode == DelayMode.PERCENTAGE:
            return prediction_window_seconds * delay
        else:
            return prediction_window_seconds

    # === ANALYTICS === #
    def persistent_annotations(self, event_type, event_text):
        event_type = event_type.upper()
        if event_type in ["WATCH_STREAK", "WIN", "PREDICTION_MADE", "LOSE"]:
            primary_color = (
                "#45c1ff"  # blue #45c1ff yellow #ffe045 green #36b535 red #ff4545
                if event_type == "WATCH_STREAK"
                else (
                    "#ffe045"
                    if event_type == "PREDICTION_MADE"
                    else ("#36b535" if event_type == "WIN" else "#ff4545")
                )
            )
            data = {
                "borderColor": primary_color,
                "label": {
                    "style": {"color": "#000", "background": primary_color},
                    "text": event_text,
                },
            }
            self.__save_json("annotations", data)

    def persistent_series(self, event_type="Watch"):
        self.__save_json("series", event_type=event_type)

    def __save_json(self, key, data=None, event_type="Watch"):
        # https://stackoverflow.com/questions/4676195/why-do-i-need-to-multiply-unix-timestamps-by-1000-in-javascript
        data = {} if data is None else data.copy()
        now = datetime.now().replace(microsecond=0)
        data.update({"x": round(datetime.timestamp(now) * 1000)})

        if key == "series":
            data.update({"y": self.channel_points})
            if event_type is not None:
                data.update({"z": event_type.replace("_", " ").title()})

        fname = os.path.join(Settings.analytics_path, f"{self.username}.json")
        temp_fname = fname + ".temp"  # Temporary file name

        with ANALYTICS_FILE_MUTEX, self.mutex:
            # Create and write to the temporary file
            with open(temp_fname, "w", encoding="utf-8") as temp_file:
                if os.path.isfile(fname):
                    with open(fname, "r", encoding="utf-8") as analytics_file:
                        json_data = json.load(analytics_file)
                else:
                    json_data = {}
                json_data["version"] = ANALYTICS_DATA_VERSION
                if key not in json_data:
                    json_data[key] = []
                json_data[key].append(data)
                json.dump(json_data, temp_file, indent=4)

            # Replace the original file with the temporary file
            os.replace(temp_fname, fname)

    def leave_chat(self):
        with _chat_lifecycle_lock:
            self.__leave_chat()

    def __leave_chat(self):
        if self.irc_chat is not None:
            self.irc_chat.stop()
            if self.__chat_retired():
                return

            # Recreate a new thread to start again
            # raise RuntimeError("threads can only be started once")
            self.irc_chat = ThreadChat(
                self.irc_chat.username,
                self.irc_chat.token,
                self.username,
            )

    def __chat_retired(self):
        return getattr(self.irc_chat, "retired", False) is True

    def retire_chat(self, timeout=5):
        """Stop the chat of a streamer that is leaving the miner, and keep
        online/offline updates that are still in flight from joining it again.
        A streamer added back later gets a new ThreadChat."""
        with _chat_lifecycle_lock:
            irc_chat = self.irc_chat
            if irc_chat is None:
                return
            irc_chat.retired = True
        if irc_chat.is_alive():
            irc_chat.stop()
            irc_chat.join(timeout=timeout)

    def __join_chat(self):
        if self.irc_chat is not None and not self.__chat_retired():
            if self.irc_chat.is_alive() is False:
                if self.irc_chat.ident is not None:
                    # This thread already ran and ended (IRC died, or chat
                    # was stopped while waiting for TLS). A thread can only
                    # be started once, so start a fresh one.
                    self.irc_chat = ThreadChat(
                        self.irc_chat.username,
                        self.irc_chat.token,
                        self.username,
                    )
                self.irc_chat.settings = self.settings
                self.irc_chat.start()

    def toggle_chat(self):
        with _chat_lifecycle_lock:
            self.__toggle_chat()

    def __toggle_chat(self):
        if self.settings.chat == ChatPresence.ALWAYS:
            self.__join_chat()
        elif self.settings.chat != ChatPresence.NEVER:
            if self.is_online is True:
                if self.settings.chat == ChatPresence.ONLINE:
                    self.__join_chat()
                elif self.settings.chat == ChatPresence.OFFLINE:
                    self.leave_chat()
            else:
                if self.settings.chat == ChatPresence.ONLINE:
                    self.leave_chat()
                elif self.settings.chat == ChatPresence.OFFLINE:
                    self.__join_chat()

    def update_community_goal(self, community_goal):
        self.community_goals[community_goal.goal_id] = community_goal

    def delete_community_goal(self, goal_id):
        self.community_goals.pop(goal_id)
