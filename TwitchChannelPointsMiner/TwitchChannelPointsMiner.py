# -*- coding: utf-8 -*-

import logging
import os
import random
import re
import signal
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from TwitchChannelPointsMiner.classes.Chat import ChatPresence, ThreadChat
from TwitchChannelPointsMiner.classes.DropBadgeCatalog import DropBadgeCatalog
from TwitchChannelPointsMiner.classes.entities.PubsubTopic import PubsubTopic
from TwitchChannelPointsMiner.classes.entities.Streamer import (
    Streamer,
    StreamerSettings,
)
from TwitchChannelPointsMiner.classes.Exceptions import StreamerDoesNotExistException
from TwitchChannelPointsMiner.classes.gql.Integration import GQLFactory
from TwitchChannelPointsMiner.classes.Settings import (
    CategoryCampaignOrder,
    Events,
    FollowersOrder,
    Priority,
    Settings,
    StreamerSource,
)
from TwitchChannelPointsMiner.classes.Twitch import Twitch
from TwitchChannelPointsMiner.classes.WebSocketsPool import WebSocketsPool
from TwitchChannelPointsMiner.data_migration import migrate_analytics_directory
from TwitchChannelPointsMiner.logger import LoggerSettings, configure_loggers
from TwitchChannelPointsMiner.utils import (
    AttemptStrategy,
    _millify,
    at_least_one_value_in_settings_is,
    check_versions,
    get_user_agent,
    internet_connection_available,
    is_newer_version,
    set_default_settings,
)
from TwitchChannelPointsMiner.WatchStreakCache import WatchStreakCache

# Suppress:
#   - chardet.charsetprober - [feed]
#   - chardet.charsetprober - [get_confidence]
#   - requests - [Starting new HTTPS connection (1)]
#   - Flask (werkzeug) logs
#   - irc.client - [process_data]
#   - irc.client - [_dispatcher]
#   - irc.client - [_handle_message]
logging.getLogger("chardet.charsetprober").setLevel(logging.ERROR)
logging.getLogger("requests").setLevel(logging.ERROR)
logging.getLogger("werkzeug").setLevel(logging.ERROR)
logging.getLogger("irc.client").setLevel(logging.ERROR)
logging.getLogger("seleniumwire").setLevel(logging.ERROR)
logging.getLogger("websocket").setLevel(logging.ERROR)

logger = logging.getLogger(__name__)


def _normalize_streams_watched(streams_watched):
    if type(streams_watched) is int and streams_watched in (1, 2):
        return streams_watched

    logger.error("streams_watched must be either 1 or 2; using the default value 2")
    return 2


def _normalize_streamer_source_priority(source_priority):
    defaults = [
        StreamerSource.STREAMERS,
        StreamerSource.FOLLOWERS,
        StreamerSource.CATEGORIES,
        StreamerSource.BADGES,
    ]
    if not isinstance(source_priority, (list, tuple)):
        logger.error(
            "streamer_source_priority must be a list of StreamerSource values; "
            "using the default order"
        )
        return defaults

    normalized = []
    for source in source_priority:
        if isinstance(source, StreamerSource) and source not in normalized:
            normalized.append(source)
    for source in defaults:
        if source not in normalized:
            normalized.append(source)
    return normalized


def _normalize_badge_drop_streamer_limit(limit):
    if type(limit) is int and limit in (1, 2):
        return limit
    logger.error(
        "badge_drop_streamer_limit must be either 1 or 2; using the default value 1"
    )
    return 1


def _unique_streamer_names(streamer_names):
    return list(dict.fromkeys(streamer_names))


def _drop_progress_report_entries(original, current):
    if original is None:
        return []

    entries = []
    for tracking_key, payload in current.items():
        previous = original.get(tracking_key, {})
        current_minutes = payload.get("current_minutes_watched", 0) or 0
        previous_minutes = previous.get("current_minutes_watched", 0) or 0
        current_status = payload.get("status") or "in_progress"
        previous_status = previous.get("status") or "in_progress"
        if current_minutes == previous_minutes and current_status == previous_status:
            continue

        entry = payload.copy()
        entry["minutes_gained"] = max(current_minutes - previous_minutes, 0)
        entries.append(entry)

    return sorted(
        entries,
        key=lambda entry: (
            str(entry.get("category") or ""),
            str(entry.get("campaign") or ""),
            str(entry.get("item_name") or ""),
        ),
    )


def _capture_drop_progress_baseline(twitch, progress_scraped=False):
    if progress_scraped is False:
        return None
    return twitch.drop_report_snapshot()


class TwitchChannelPointsMiner:
    __slots__ = [
        "username",
        "twitch",
        "claim_drops_startup",
        "enable_analytics",
        "disable_ssl_cert_verification",
        "disable_at_in_nickname",
        "streams_watched",
        "streamer_source_priority",
        "priority",
        "streamers",
        "events_predictions",
        "minute_watcher_thread",
        "sync_campaigns_thread",
        "ws_pool",
        "session_id",
        "running",
        "start_datetime",
        "original_streamers",
        "original_drop_progress",
        "logs_file",
        "queue_listener",
        "config_reload_lock",
        "drop_badge_catalog",
        "drop_badge_catalog_thread",
        "drop_badge_catalog_stop_event",
        "auto_mine_badge_drops",
        "badge_drop_streamer_limit",
        "badge_drop_category_chat",
        "badge_drop_category_sort",
        "badge_drop_blacklist",
        "watch_streak_cache",
    ]

    def __init__(
        self,
        username: str,
        password: str = None,
        claim_drops_startup: bool = False,
        enable_analytics: bool = False,
        disable_ssl_cert_verification: bool = False,
        disable_at_in_nickname: bool = False,
        # Settings for logging and selenium as you can see.
        priority: list = [Priority.STREAK, Priority.DROPS, Priority.ORDER],
        # This settings will be global shared trought Settings class
        logger_settings: LoggerSettings = LoggerSettings(),
        # Default values for all streamers
        streamer_settings: StreamerSettings = StreamerSettings(),
        streams_watched: int = 2,
        gql: AttemptStrategy | GQLFactory | None = None,
        streamer_source_priority: list
        | tuple = (
            StreamerSource.STREAMERS,
            StreamerSource.FOLLOWERS,
            StreamerSource.CATEGORIES,
            StreamerSource.BADGES,
        ),
    ):

        # Fixes TypeError: 'NoneType' object is not subscriptable
        if not username or username == "your-twitch-username":
            logger.error("Please edit your runner file (usually run.py) and try again.")
            logger.error("No username, exiting...")
            sys.exit(0)
        if (
            not isinstance(username, str)
            or re.fullmatch(r"[A-Za-z0-9_]{1,25}", username) is None
        ):
            raise ValueError(
                "username must contain only letters, numbers, or underscores "
                "and be between 1 and 25 characters"
            )

        # This disables certificate verification and allows the connection to proceed, but also makes it vulnerable to man-in-the-middle (MITM) attacks.
        Settings.disable_ssl_cert_verification = disable_ssl_cert_verification

        Settings.disable_at_in_nickname = disable_at_in_nickname

        self.streams_watched = _normalize_streams_watched(streams_watched)
        self.streamer_source_priority = _normalize_streamer_source_priority(
            streamer_source_priority
        )

        import socket

        def is_connected():
            try:
                # resolve the IP address of the Twitch.tv domain name
                socket.gethostbyname("twitch.tv")
                return True
            except OSError:
                pass
            return False

        # check for Twitch.tv connectivity every 5 seconds
        error_printed = False
        while not is_connected():
            if not error_printed:
                logger.error("Waiting for Twitch.tv connectivity...")
                error_printed = True
            time.sleep(5)

        # Analytics switch
        Settings.enable_analytics = enable_analytics

        if enable_analytics is True:
            Settings.analytics_path = os.path.join(
                Path().absolute(), "analytics", username
            )
            Path(Settings.analytics_path).mkdir(parents=True, exist_ok=True)
            migrate_analytics_directory(Settings.analytics_path)

        self.username = username

        # Set as global config
        Settings.logger = logger_settings

        # Init as default all the missing values
        streamer_settings.default()
        streamer_settings.bet.default()
        Settings.streamer_settings = streamer_settings

        # user_agent = get_user_agent("FIREFOX")
        user_agent = get_user_agent("CHROME")
        if gql is None:
            gql_factory = GQLFactory()
        elif isinstance(gql, AttemptStrategy):
            gql_factory = GQLFactory(attempt_strategy=gql)
        elif isinstance(gql, GQLFactory):
            gql_factory = gql
        else:
            raise ValueError("gql must be None, AttemptStrategy, or GQLFactory")
        self.twitch = Twitch(
            self.username, user_agent, password, gql_factory=gql_factory
        )

        self.claim_drops_startup = claim_drops_startup
        self.priority = priority if isinstance(priority, list) else [priority]

        self.streamers: list[Streamer] = []
        self.events_predictions = {}
        self.minute_watcher_thread = None
        self.sync_campaigns_thread = None
        self.ws_pool = None

        self.session_id = str(uuid.uuid4())
        self.running = False
        self.start_datetime = None
        self.original_streamers = []
        self.original_drop_progress = None
        self.config_reload_lock = threading.Lock()
        self.drop_badge_catalog = None
        self.drop_badge_catalog_thread = None
        self.drop_badge_catalog_stop_event = threading.Event()
        self.auto_mine_badge_drops = False
        self.badge_drop_streamer_limit = 1
        self.badge_drop_category_chat = None
        self.badge_drop_category_sort = "VIEWERS_DESC"
        self.badge_drop_blacklist = set()

        if not hasattr(Settings, "config_path"):
            Settings.config_path = os.path.abspath(
                os.environ.get(
                    "TCPM_CONFIG_DIR", os.path.join(Path().absolute(), "config")
                )
            )

        self.logs_file, self.queue_listener = configure_loggers(
            self.username, logger_settings
        )
        watch_streak_cache_path = os.path.join(
            Path().absolute(),
            "logs",
            ".state",
            f"watch_streak_cache.{self.username.lower()}.json",
        )
        self.watch_streak_cache = WatchStreakCache.load(
            watch_streak_cache_path, self.username
        )

        if os.environ.pop("TCPM_LEGACY_CONFIG_NOTICE", None):
            logger.warning(
                "Docker configuration update required: mount "
                "/usr/src/app/config to enable automatic conversion from run.py. "
                "The existing run.py is still running and has not been modified.",
                extra={
                    "emoji": ":warning:",
                    "event": Events.CONFIGURATION,
                    "force_alert": True,
                },
            )

        # Check for the latest version of the script
        current_version, github_version = check_versions()

        logger.info(f"Twitch Channel Points Miner - {current_version}")
        logger.info("https://github.com/rdavydov/Twitch-Channel-Points-Miner-v2")

        if github_version == "0.0.0":
            logger.error(
                "Unable to detect if you have the latest version of this script"
            )
        elif is_newer_version(github_version, current_version):
            logger.info(f"You are running version {current_version} of this script")
            logger.info(f"The latest version on GitHub is {github_version}")

        for sign in [signal.SIGINT, signal.SIGSEGV, signal.SIGTERM]:
            signal.signal(sign, self.end)

    def analytics(
        self,
        host: str = "127.0.0.1",
        port: int = 5000,
        refresh: int = 5,
        days_ago: int = 7,
        password: str = None,
        log_poll_interval: int = 5,
    ):
        # Analytics switch
        if Settings.enable_analytics is True:
            from TwitchChannelPointsMiner.classes.AnalyticsServer import AnalyticsServer

            days_ago = days_ago if days_ago <= 365 * 15 else 365 * 15
            http_server = AnalyticsServer(
                host=host,
                port=port,
                refresh=refresh,
                days_ago=days_ago,
                username=self.username,
                password=password,
                log_poll_interval=log_poll_interval,
            )
            http_server.daemon = True
            http_server.name = "Analytics Thread"
            http_server.start()
        else:
            logger.error("Can't start analytics(), please set enable_analytics=True")

    def mine(
        self,
        streamers: list = [],
        blacklist: list = [],
        followers: bool = False,
        followers_order: FollowersOrder = FollowersOrder.ASC,
        categories: list = [],
        category_drops_enabled: bool = True,
        category_limit: int = 30,
        category_sort="VIEWERS_DESC",
        category_campaign_order: CategoryCampaignOrder = (CategoryCampaignOrder.ORDER),
        category_chat=None,
        category_log_level: int = logging.INFO,
        drop_item_art: bool = False,
        print_open_drop_campaigns_on_load: bool = False,
        scrape_drop_progress_on_load: bool = False,
        log_drop_checks: bool = False,
        track_category_streamer_points: bool = False,
        category_refresh_interval_hours: float = 6,
        drop_badge_catalog: bool = True,
        drop_badge_refresh_interval_hours: float = 1,
        auto_mine_badge_drops: bool = False,
        badge_drop_streamer_limit: int = 1,
    ):
        self.run(
            streamers=streamers,
            blacklist=blacklist,
            followers=followers,
            followers_order=followers_order,
            categories=categories,
            category_drops_enabled=category_drops_enabled,
            category_limit=category_limit,
            category_sort=category_sort,
            category_campaign_order=category_campaign_order,
            category_chat=category_chat,
            category_log_level=category_log_level,
            drop_item_art=drop_item_art,
            print_open_drop_campaigns_on_load=print_open_drop_campaigns_on_load,
            scrape_drop_progress_on_load=scrape_drop_progress_on_load,
            log_drop_checks=log_drop_checks,
            track_category_streamer_points=track_category_streamer_points,
            category_refresh_interval_hours=category_refresh_interval_hours,
            drop_badge_catalog=drop_badge_catalog,
            drop_badge_refresh_interval_hours=drop_badge_refresh_interval_hours,
            auto_mine_badge_drops=auto_mine_badge_drops,
            badge_drop_streamer_limit=badge_drop_streamer_limit,
        )

    def run(
        self,
        streamers: list = [],
        blacklist: list = [],
        followers: bool = False,
        followers_order: FollowersOrder = FollowersOrder.ASC,
        categories: list = [],
        category_drops_enabled: bool = True,
        category_limit: int = 30,
        category_sort="VIEWERS_DESC",
        category_campaign_order: CategoryCampaignOrder = (CategoryCampaignOrder.ORDER),
        category_chat=None,
        category_log_level: int = logging.INFO,
        drop_item_art: bool = False,
        print_open_drop_campaigns_on_load: bool = False,
        scrape_drop_progress_on_load: bool = False,
        log_drop_checks: bool = False,
        track_category_streamer_points: bool = False,
        category_refresh_interval_hours: float = 6,
        drop_badge_catalog: bool = True,
        drop_badge_refresh_interval_hours: float = 1,
        auto_mine_badge_drops: bool = False,
        badge_drop_streamer_limit: int = 1,
    ):
        if self.running:
            logger.error("You can't start multiple sessions of this instance!")
        else:
            logger.info(
                f"Start session: '{self.session_id}'", extra={"emoji": ":bomb:"}
            )
            self.running = True
            self.start_datetime = datetime.now()

            self.twitch.login()
            self.twitch.track_drop_item_art = drop_item_art
            self.twitch.scrape_drop_progress_on_load = scrape_drop_progress_on_load
            self.twitch.log_drop_checks = log_drop_checks
            self.twitch.category_log_level = category_log_level
            Settings.track_category_streamer_points = track_category_streamer_points
            self.auto_mine_badge_drops = auto_mine_badge_drops is True
            self.badge_drop_streamer_limit = _normalize_badge_drop_streamer_limit(
                badge_drop_streamer_limit
            )
            self.badge_drop_category_chat = category_chat
            self.badge_drop_category_sort = category_sort
            self.badge_drop_blacklist = {
                str(username).lower().strip() for username in blacklist
            }

            drop_badge_refresh_seconds = 0
            if drop_badge_catalog is True:
                self.drop_badge_catalog = DropBadgeCatalog(
                    self.twitch.twitch_login, Settings.config_path
                )
                drop_badge_refresh_seconds = (
                    max(float(drop_badge_refresh_interval_hours), 1) * 60 * 60
                    if drop_badge_refresh_interval_hours > 0
                    else 0
                )
                self.drop_badge_catalog_thread = threading.Thread(
                    target=self.__drop_badge_catalog_loop,
                    args=(drop_badge_refresh_seconds,),
                    name="Drop badge catalog",
                    daemon=True,
                )

            if print_open_drop_campaigns_on_load is True:
                self.twitch.log_open_drop_campaigns()

            if self.twitch.scrape_drop_progress_on_load is True:
                self.twitch.scrape_drop_progress_from_inventory(reason="run_load")

            if self.claim_drops_startup is True:
                self.twitch.claim_all_drops_from_inventory()

            self.original_drop_progress = _capture_drop_progress_baseline(
                self.twitch,
                progress_scraped=self.twitch.scrape_drop_progress_on_load is True,
            )

            streamers_name: list = []
            streamers_dict: dict = {}
            category_usernames = set()
            follower_usernames = set()
            explicitly_configured_usernames = set()

            for streamer in streamers:
                username = (
                    streamer.username
                    if isinstance(streamer, Streamer)
                    else streamer.lower().strip()
                )
                if username not in blacklist:
                    streamers_name.append(username)
                    streamers_dict[username] = streamer
                    explicitly_configured_usernames.add(username)

            if followers is True:
                followers_array = self.twitch.get_followers(order=followers_order)
                logger.info(
                    f"Load {len(followers_array)} followers from your profile!",
                    extra={"emoji": ":clipboard:"},
                )
                for username in followers_array:
                    if username not in streamers_dict and username not in blacklist:
                        streamers_name.append(username)
                        streamers_dict[username] = username.lower().strip()
                        follower_usernames.add(username)

            if categories:
                eligible_categories = self.twitch.filter_categories_with_active_drops(
                    categories,
                    order=category_campaign_order,
                    drops_enabled=category_drops_enabled,
                )

                if categories and eligible_categories == []:
                    logger.log(
                        category_log_level,
                        "Skipping category stream discovery: no configured categories have active incomplete campaigns",
                        extra={"emoji": ":sleeping:", "category_log": True},
                    )

                all_category_usernames = []
                for category in eligible_categories:
                    all_category_usernames.extend(
                        self.twitch.get_live_streamers_for_category(
                            category,
                            drops_enabled=category_drops_enabled,
                            limit=category_limit,
                            sort_by=category_sort,
                        )
                    )

                for username in all_category_usernames:
                    category_usernames.add(username)
                    if username not in streamers_dict and username not in blacklist:
                        streamers_name.append(username)
                        streamers_dict[username] = username.lower().strip()

            streamers_name = _unique_streamer_names(streamers_name)
            logger.info(
                f"Loading data for {len(streamers_name)} streamers. Please wait...",
                extra={"emoji": ":nerd_face:"},
            )

            def build_streamer(username):
                time.sleep(random.uniform(0.15, 0.35))
                is_follower_streamer = username in follower_usernames
                is_category_streamer = (
                    username in category_usernames
                    and username not in explicitly_configured_usernames
                    and is_follower_streamer is False
                )
                streamer = (
                    streamers_dict[username]
                    if isinstance(streamers_dict[username], Streamer) is True
                    else Streamer(
                        username,
                        settings=(
                            StreamerSettings(chat=category_chat)
                            if is_category_streamer is True
                            and category_chat is not None
                            else None
                        ),
                        from_followers=is_follower_streamer,
                        from_category=is_category_streamer,
                        explicitly_configured=(
                            username in explicitly_configured_usernames
                        ),
                    )
                )
                streamer.explicitly_configured = (
                    username in explicitly_configured_usernames
                )
                streamer.watch_streak_cache = self.watch_streak_cache
                streamer.channel_id = self.twitch.get_channel_id(username)
                streamer.settings = set_default_settings(
                    streamer.settings, Settings.streamer_settings
                )
                streamer.settings.bet = set_default_settings(
                    streamer.settings.bet, Settings.streamer_settings.bet
                )
                if streamer.settings.chat != ChatPresence.NEVER:
                    streamer.irc_chat = ThreadChat(
                        self.username,
                        self.twitch.twitch_login.get_auth_token(),
                        streamer.username,
                    )
                return streamer

            loaded_streamers = [None] * len(streamers_name)
            workers = min(10, len(streamers_name))
            if workers:
                with ThreadPoolExecutor(
                    max_workers=workers, thread_name_prefix="Streamer bootstrap"
                ) as executor:
                    futures = {
                        executor.submit(build_streamer, username): (index, username)
                        for index, username in enumerate(streamers_name)
                    }
                    for future in as_completed(futures):
                        index, username = futures[future]
                        try:
                            loaded_streamers[index] = future.result()
                        except StreamerDoesNotExistException:
                            logger.info(
                                f"Streamer {username} does not exist",
                                extra={"emoji": ":cry:"},
                            )
                        except Exception:
                            logger.error(
                                f"Failed to load streamer {username}", exc_info=True
                            )

            self.streamers = [
                streamer for streamer in loaded_streamers if streamer is not None
            ]

            # Populate the streamers with default values.
            # 1. Load channel points and auto-claim bonus
            # 2. Check if streamers are online
            # 3. DEACTIVATED: Check if the user is a moderator. (was used before the 5th of April 2021 to deactivate predictions)
            invalid_streamers = self.twitch.initialize_streamers_context(self.streamers)
            if invalid_streamers:
                self.streamers = [
                    streamer
                    for streamer in self.streamers
                    if streamer.username not in invalid_streamers
                ]

            self.original_streamers = [
                streamer.channel_points for streamer in self.streamers
            ]

            # If we have at least one streamer with settings = make_predictions True
            make_predictions = at_least_one_value_in_settings_is(
                self.streamers, "make_predictions", True
            )

            # If we have at least one streamer with settings = claim_drops True
            # Spawn a thread for sync inventory and dashboard
            if (
                at_least_one_value_in_settings_is(self.streamers, "claim_drops", True)
                is True
            ):
                self.sync_campaigns_thread = threading.Thread(
                    target=self.twitch.sync_campaigns,
                    args=(self.streamers,),
                )
                self.sync_campaigns_thread.name = "Sync campaigns/inventory"
                self.sync_campaigns_thread.start()
                time.sleep(30)

            self.minute_watcher_thread = threading.Thread(
                target=self.twitch.send_minute_watched_events,
                args=(self.streamers, self.priority),
                kwargs={
                    "streams_watched": self.streams_watched,
                    "source_priority": self.streamer_source_priority,
                },
            )
            self.minute_watcher_thread.name = "Minute watcher"
            self.minute_watcher_thread.start()

            self.ws_pool = WebSocketsPool(
                twitch=self.twitch,
                streamers=self.streamers,
                events_predictions=self.events_predictions,
            )

            # Subscribe to community-points-user. Get update for points spent or gains
            user_id = self.twitch.twitch_login.get_user_id()
            # print(f"!!!!!!!!!!!!!! USER_ID: {user_id}")

            # Fixes 'ERR_BADAUTH'
            if not user_id:
                logger.error("No user_id, exiting...")
                self.end(0, 0)

            self.ws_pool.submit(
                PubsubTopic(
                    "community-points-user-v1",
                    user_id=user_id,
                )
            )

            # Going to subscribe to predictions-user-v1. Get update when we place a new prediction (confirm)
            if make_predictions is True:
                self.ws_pool.submit(
                    PubsubTopic(
                        "predictions-user-v1",
                        user_id=user_id,
                    )
                )

            for streamer in self.streamers:
                self.ws_pool.submit(
                    PubsubTopic("video-playback-by-id", streamer=streamer)
                )

                if streamer.settings.follow_raid is True:
                    self.ws_pool.submit(PubsubTopic("raid", streamer=streamer))

                if streamer.settings.make_predictions is True:
                    self.ws_pool.submit(
                        PubsubTopic("predictions-channel-v1", streamer=streamer)
                    )

                if streamer.settings.claim_moments is True:
                    self.ws_pool.submit(
                        PubsubTopic("community-moments-channel-v1", streamer=streamer)
                    )

                if streamer.settings.community_goals is True:
                    self.ws_pool.submit(
                        PubsubTopic("community-points-channel-v1", streamer=streamer)
                    )

            if self.drop_badge_catalog_thread is not None:
                self.drop_badge_catalog_thread.start()

            refresh_context = time.time()
            category_refresh_interval_seconds = (
                max(category_refresh_interval_hours, 0.5) * 60 * 60
                if category_refresh_interval_hours > 0
                else 0
            )
            effective_category_refresh_seconds = (
                min(category_refresh_interval_seconds, 5 * 60)
                if self.twitch.twitchdrops_app_campaigns
                and category_refresh_interval_seconds > 0
                else category_refresh_interval_seconds
            )
            next_category_refresh_at = (
                time.time()
                + effective_category_refresh_seconds
                + random.randint(20, 5 * 60)
                if effective_category_refresh_seconds > 0
                else None
            )
            upcoming_drop_start = self.twitch.next_upcoming_drop_start()
            if upcoming_drop_start is not None and next_category_refresh_at is not None:
                next_category_refresh_at = min(
                    next_category_refresh_at,
                    time.time()
                    + max((upcoming_drop_start - datetime.utcnow()).total_seconds(), 0)
                    + 5,
                )
            while self.running and self.twitch.running:
                if self.twitch.restart_requested.wait(random.uniform(20, 60)):
                    break
                # Do an external control for WebSocket. Check if the thread is running
                # Check if is not None because maybe we have already created a new connection on array+1 and now index is None
                for index in range(0, len(self.ws_pool.ws)):
                    if (
                        self.ws_pool.ws[index].is_reconnecting is False
                        and self.ws_pool.ws[index].elapsed_last_ping() > 10
                        and internet_connection_available() is True
                    ):
                        logger.info(
                            f"#{index} - The last PING was sent more than 10 minutes ago. Reconnecting to the WebSocket..."
                        )
                        WebSocketsPool.handle_reconnection(self.ws_pool.ws[index])

                if ((time.time() - refresh_context) // 60) >= 30:
                    refresh_context = time.time()
                    for index in range(0, len(self.streamers)):
                        if self.streamers[index].is_online:
                            self.twitch.load_channel_points_context(
                                self.streamers[index]
                            )

                if (
                    categories
                    and next_category_refresh_at is not None
                    and time.time() >= next_category_refresh_at
                ):
                    with self.config_reload_lock:
                        self.__refresh_category_streamers(
                            categories=categories,
                            blacklist=blacklist,
                            drops_enabled=category_drops_enabled,
                            limit=category_limit,
                            sort_by=category_sort,
                            campaign_order=category_campaign_order,
                            category_chat=category_chat,
                            category_log_level=category_log_level,
                        )
                    effective_category_refresh_seconds = (
                        min(category_refresh_interval_seconds, 5 * 60)
                        if self.twitch.twitchdrops_app_campaigns
                        else category_refresh_interval_seconds
                    )
                    next_category_refresh_at = (
                        time.time()
                        + effective_category_refresh_seconds
                        + random.randint(20, 5 * 60)
                    )
                    upcoming_drop_start = self.twitch.next_upcoming_drop_start()
                    if upcoming_drop_start is not None:
                        next_category_refresh_at = min(
                            next_category_refresh_at,
                            time.time()
                            + max(
                                (
                                    upcoming_drop_start - datetime.utcnow()
                                ).total_seconds(),
                                0,
                            )
                            + 5,
                        )

    def __drop_badge_catalog_loop(self, refresh_seconds):
        logger.info(
            "Starting Drop badge catalog check in the background: "
            f"{self.drop_badge_catalog.path}",
            extra={"emoji": ":card_index_dividers:", "category_log": True},
        )
        self.__sync_drop_badge_catalog(initial=True)
        if self.auto_mine_badge_drops:
            self.__auto_mine_badge_campaigns()
        if refresh_seconds <= 0:
            return
        while self.running and self.twitch.running:
            if self.drop_badge_catalog_stop_event.wait(refresh_seconds):
                return
            if self.running and self.twitch.running:
                self.__sync_drop_badge_catalog()
                if self.auto_mine_badge_drops:
                    self.__auto_mine_badge_campaigns()

    def __sync_drop_badge_catalog(self, initial=False):
        if self.drop_badge_catalog is None:
            return
        try:
            result = self.drop_badge_catalog.sync()
        except Exception as error:
            logger.warning(
                f"Unable to refresh Drop badge catalog: {error}",
                extra={"emoji": ":warning:", "category_log": True},
            )
            return

        new_campaigns = result["new_campaigns"]
        new_badges = sum(
            drop.get("badge_classification", {}).get("status") == "BADGE"
            for record in new_campaigns
            for drop in record.get("campaign", {}).get("drops", []) or []
        )
        if initial:
            logger.info(
                "Drop badge catalog loaded: "
                f"{result['stored_campaigns']} campaigns, "
                f"{result['confirmed_badge_rewards']} confirmed badge rewards",
                extra={"emoji": ":card_index_dividers:", "category_log": True},
            )
        elif new_campaigns:
            logger.info(
                f"Drop badge catalog found {len(new_campaigns)} new campaigns "
                f"with {new_badges} confirmed badges",
                extra={"emoji": ":gift:", "category_log": True},
            )
        else:
            logger.info(
                "Drop badge catalog hourly check found no new campaigns",
                extra={"emoji": ":white_check_mark:", "category_log": True},
            )

    def __auto_mine_badge_campaigns(self):
        if self.drop_badge_catalog is None or self.ws_pool is None:
            return

        try:
            owned_badges = self.twitch.get_earned_badge_names(refresh=True)
            if owned_badges is None:
                logger.warning(
                    "Skipping automatic badge Drop discovery because Twitch did "
                    "not return the account's earned badge inventory",
                    extra={"emoji": ":warning:", "category_log": True},
                )
                return
            campaigns = self.drop_badge_catalog.eligible_badge_campaigns(owned_badges)
        except Exception as error:
            logger.warning(
                f"Unable to determine eligible badge Drop campaigns: {error}",
                extra={"emoji": ":warning:", "category_log": True},
            )
            return

        unrestricted_games = []
        restricted_campaigns_by_game = {}
        for record in campaigns:
            game_slug = str(record.get("game_slug") or "").strip()
            campaign = record.get("campaign") or {}
            if not game_slug:
                continue
            channels = [
                channel.lower().strip()
                for channel in campaign.get("channels", []) or []
                if isinstance(channel, str) and channel.strip()
            ]
            if campaign.get("all_channels") is True:
                unrestricted_games.append(game_slug)
            elif channels:
                restricted_campaigns_by_game.setdefault(game_slug, []).append(
                    {**campaign, "channels": channels}
                )

        discovered_usernames = []
        for game_slug in dict.fromkeys(unrestricted_games):
            try:
                discovered_usernames.extend(
                    self.twitch.get_live_streamers_for_category(
                        game_slug,
                        drops_enabled=True,
                        limit=self.badge_drop_streamer_limit,
                        sort_by=self.badge_drop_category_sort,
                        respect_campaign_restrictions=False,
                    )
                )
            except Exception as error:
                logger.warning(
                    f"Unable to find a live channel for badge Drop campaign "
                    f"'{game_slug}': {error}",
                    extra={"emoji": ":warning:", "category_log": True},
                )

        for game_slug, restricted_campaigns in restricted_campaigns_by_game.items():
            try:
                discovered_usernames.extend(
                    self.twitch.get_live_streamers_for_category(
                        game_slug,
                        drops_enabled=True,
                        limit=30,
                        sort_by=self.badge_drop_category_sort,
                        restricted_campaigns=restricted_campaigns,
                    )
                )
            except Exception as error:
                logger.warning(
                    f"Unable to find restricted live channels for badge Drop "
                    f"campaign '{game_slug}': {error}",
                    extra={"emoji": ":warning:", "category_log": True},
                )

        added = 0
        with self.config_reload_lock:
            existing_usernames = {streamer.username for streamer in self.streamers}
            for username in dict.fromkeys(discovered_usernames):
                username = str(username).lower().strip()
                if (
                    not username
                    or username in existing_usernames
                    or username in self.badge_drop_blacklist
                ):
                    continue

                try:
                    streamer = Streamer(
                        username,
                        settings=StreamerSettings(
                            claim_drops=True,
                            chat=self.badge_drop_category_chat,
                        ),
                        from_category=True,
                        from_badge_campaign=True,
                    )
                    streamer.channel_id = self.twitch.get_channel_id(username)
                    streamer.settings = set_default_settings(
                        streamer.settings, Settings.streamer_settings
                    )
                    streamer.settings.bet = set_default_settings(
                        streamer.settings.bet, Settings.streamer_settings.bet
                    )
                    if streamer.settings.chat != ChatPresence.NEVER:
                        streamer.irc_chat = ThreadChat(
                            self.username,
                            self.twitch.twitch_login.get_auth_token(),
                            streamer.username,
                        )

                    self.twitch.load_channel_points_context(streamer)
                    self.twitch.check_streamer_online(streamer)
                    self.streamers.append(streamer)
                    existing_usernames.add(username)
                    added += 1

                    self.ws_pool.submit(
                        PubsubTopic("video-playback-by-id", streamer=streamer)
                    )
                    if streamer.settings.follow_raid is True:
                        self.ws_pool.submit(PubsubTopic("raid", streamer=streamer))
                    if streamer.settings.make_predictions is True:
                        self.ws_pool.submit(
                            PubsubTopic("predictions-channel-v1", streamer=streamer)
                        )
                    if streamer.settings.claim_moments is True:
                        self.ws_pool.submit(
                            PubsubTopic(
                                "community-moments-channel-v1", streamer=streamer
                            )
                        )
                    if streamer.settings.community_goals is True:
                        self.ws_pool.submit(
                            PubsubTopic(
                                "community-points-channel-v1", streamer=streamer
                            )
                        )
                except StreamerDoesNotExistException:
                    logger.info(
                        f"Streamer {username} does not exist",
                        extra={"emoji": ":cry:"},
                    )
                except Exception as error:
                    logger.warning(
                        f"Unable to add badge Drop streamer {username}: {error}",
                        extra={"emoji": ":warning:", "category_log": True},
                    )

            if added > 0 and self.sync_campaigns_thread is None:
                self.sync_campaigns_thread = threading.Thread(
                    target=self.twitch.sync_campaigns,
                    args=(self.streamers,),
                    name="Sync campaigns/inventory",
                )
                self.sync_campaigns_thread.start()

        badge_count = sum(len(record.get("eligible_drops", [])) for record in campaigns)
        logger.info(
            "Automatic badge Drop check complete: "
            f"{len(campaigns)} eligible campaigns, {badge_count} unearned badges, "
            f"{added} new live streamers",
            extra={"emoji": ":gift:", "category_log": True},
        )

    def __refresh_category_streamers(
        self,
        categories,
        blacklist,
        drops_enabled,
        limit,
        sort_by,
        campaign_order,
        category_chat,
        category_log_level,
    ):
        logger.log(
            category_log_level,
            "Refreshing configured categories and drop campaigns",
            extra={
                "emoji": ":arrows_counterclockwise:",
                "category_log": True,
            },
        )

        # Force live campaign discovery to run again instead of reusing startup data.
        self.twitch.discovered_open_drop_campaigns = None
        eligible_categories = self.twitch.filter_categories_with_active_drops(
            categories,
            order=campaign_order,
            drops_enabled=drops_enabled,
        )
        discovered_usernames = []
        for category in eligible_categories:
            discovered_usernames.extend(
                self.twitch.get_live_streamers_for_category(
                    category,
                    drops_enabled=drops_enabled,
                    limit=limit,
                    sort_by=sort_by,
                )
            )

        existing_usernames = {streamer.username for streamer in self.streamers}
        blacklist_usernames = {str(username).lower().strip() for username in blacklist}
        added = 0
        for username in dict.fromkeys(discovered_usernames):
            username = username.lower().strip()
            if username in existing_usernames or username in blacklist_usernames:
                continue

            try:
                streamer = Streamer(
                    username,
                    settings=(
                        StreamerSettings(chat=category_chat)
                        if category_chat is not None
                        else None
                    ),
                    from_category=True,
                )
                streamer.channel_id = self.twitch.get_channel_id(username)
                streamer.settings = set_default_settings(
                    streamer.settings, Settings.streamer_settings
                )
                streamer.settings.bet = set_default_settings(
                    streamer.settings.bet, Settings.streamer_settings.bet
                )
                if streamer.settings.chat != ChatPresence.NEVER:
                    streamer.irc_chat = ThreadChat(
                        self.username,
                        self.twitch.twitch_login.get_auth_token(),
                        streamer.username,
                    )

                self.twitch.load_channel_points_context(streamer)
                self.twitch.check_streamer_online(streamer)
                self.streamers.append(streamer)
                existing_usernames.add(username)
                added += 1

                self.ws_pool.submit(
                    PubsubTopic("video-playback-by-id", streamer=streamer)
                )
                if streamer.settings.follow_raid is True:
                    self.ws_pool.submit(PubsubTopic("raid", streamer=streamer))
                if streamer.settings.make_predictions is True:
                    self.ws_pool.submit(
                        PubsubTopic("predictions-channel-v1", streamer=streamer)
                    )
                if streamer.settings.claim_moments is True:
                    self.ws_pool.submit(
                        PubsubTopic("community-moments-channel-v1", streamer=streamer)
                    )
                if streamer.settings.community_goals is True:
                    self.ws_pool.submit(
                        PubsubTopic("community-points-channel-v1", streamer=streamer)
                    )
            except StreamerDoesNotExistException:
                logger.info(
                    f"Streamer {username} does not exist",
                    extra={"emoji": ":cry:"},
                )

        if added > 0 and self.sync_campaigns_thread is None:
            self.sync_campaigns_thread = threading.Thread(
                target=self.twitch.sync_campaigns,
                args=(self.streamers,),
            )
            self.sync_campaigns_thread.name = "Sync campaigns/inventory"
            self.sync_campaigns_thread.start()

        logger.log(
            category_log_level,
            f"Category refresh complete: {len(eligible_categories)} active categories, {added} new streamers",
            extra={"emoji": ":white_check_mark:", "category_log": True},
        )

    def add_streamers(self, streamers):
        """Add explicitly configured streamers to a running miner."""
        if not self.running or self.ws_pool is None:
            raise RuntimeError("The miner is not ready for live configuration changes")
        with self.config_reload_lock:
            self._add_streamers(streamers)

    def _add_streamers(self, streamers):
        existing = {streamer.username for streamer in self.streamers}
        for configured in streamers:
            username = (
                configured.username
                if isinstance(configured, Streamer)
                else str(configured).lower().strip()
            )
            if username in existing:
                continue
            try:
                streamer = (
                    configured
                    if isinstance(configured, Streamer)
                    else Streamer(username)
                )
                streamer.explicitly_configured = True
                streamer.channel_id = self.twitch.get_channel_id(username)
                streamer.settings = set_default_settings(
                    streamer.settings, Settings.streamer_settings
                )
                streamer.settings.bet = set_default_settings(
                    streamer.settings.bet, Settings.streamer_settings.bet
                )
                if streamer.settings.chat != ChatPresence.NEVER:
                    streamer.irc_chat = ThreadChat(
                        self.username,
                        self.twitch.twitch_login.get_auth_token(),
                        streamer.username,
                    )
                self.twitch.load_channel_points_context(streamer)
                self.twitch.check_streamer_online(streamer)
                self.streamers.append(streamer)
                existing.add(username)
                self.ws_pool.submit(
                    PubsubTopic("video-playback-by-id", streamer=streamer)
                )
                if streamer.settings.follow_raid is True:
                    self.ws_pool.submit(PubsubTopic("raid", streamer=streamer))
                if streamer.settings.make_predictions is True:
                    self.ws_pool.submit(
                        PubsubTopic("predictions-channel-v1", streamer=streamer)
                    )
                if streamer.settings.claim_moments is True:
                    self.ws_pool.submit(
                        PubsubTopic("community-moments-channel-v1", streamer=streamer)
                    )
                if streamer.settings.community_goals is True:
                    self.ws_pool.submit(
                        PubsubTopic("community-points-channel-v1", streamer=streamer)
                    )
                logger.info(
                    f"Added {streamer.username} from the reloaded configuration",
                    extra={"emoji": ":heavy_plus_sign:"},
                )
            except StreamerDoesNotExistException:
                logger.info(
                    f"Streamer {username} does not exist",
                    extra={"emoji": ":cry:"},
                )

    def refresh_categories(self, mine_config):
        """Apply the current configured category list to a running miner."""
        with self.config_reload_lock:
            self.__refresh_category_streamers(
                categories=mine_config.get("categories", []),
                blacklist=mine_config.get("blacklist", []),
                drops_enabled=mine_config.get("category_drops_enabled", True),
                limit=mine_config.get("category_limit", 30),
                sort_by=mine_config.get("category_sort", "VIEWERS_DESC"),
                campaign_order=mine_config.get(
                    "category_campaign_order", CategoryCampaignOrder.ORDER
                ),
                category_chat=mine_config.get("category_chat"),
                category_log_level=mine_config.get("category_log_level", logging.INFO),
            )

    def end(self, signum, frame):
        if not self.running:
            return

        logger.info("CTRL+C Detected! Please wait just a moment!")

        for streamer in self.streamers:
            if (
                streamer.irc_chat is not None
                and streamer.settings.chat != ChatPresence.NEVER
            ):
                streamer.leave_chat()
                if streamer.irc_chat.is_alive() is True:
                    streamer.irc_chat.join()

        self.running = self.twitch.running = False
        self.drop_badge_catalog_stop_event.set()
        if self.ws_pool is not None:
            self.ws_pool.end()

        if self.minute_watcher_thread is not None:
            self.minute_watcher_thread.join()

        if self.sync_campaigns_thread is not None:
            self.sync_campaigns_thread.join()

        if self.drop_badge_catalog_thread is not None:
            self.drop_badge_catalog_thread.join(timeout=30)

        # Check if all the mutex are unlocked.
        # Prevent breaks of .json file
        for streamer in self.streamers:
            if streamer.mutex.locked():
                streamer.mutex.acquire()
                streamer.mutex.release()

        self.__print_report()

        # Stop the queue listener to make sure all messages have been logged
        self.queue_listener.stop()

        sys.exit(0)

    def __print_report(self):
        print("\n")
        logger.info(
            f"Ending session: '{self.session_id}'", extra={"emoji": ":stop_sign:"}
        )
        if self.logs_file is not None:
            logger.info(
                f"Logs file: {self.logs_file}", extra={"emoji": ":page_facing_up:"}
            )
        logger.info(
            f"Duration {datetime.now() - self.start_datetime}",
            extra={"emoji": ":hourglass:"},
        )

        if not Settings.logger.less and self.events_predictions != {}:
            print("")
            for event_id in self.events_predictions:
                event = self.events_predictions[event_id]
                if (
                    event.bet_confirmed is True
                    and event.streamer.settings.make_predictions is True
                ):
                    logger.info(
                        f"{event.streamer.settings.bet}",
                        extra={"emoji": ":wrench:"},
                    )
                    if event.streamer.settings.bet.filter_condition is not None:
                        logger.info(
                            f"{event.streamer.settings.bet.filter_condition}",
                            extra={"emoji": ":pushpin:"},
                        )
                    logger.info(
                        f"{event.print_recap()}",
                        extra={"emoji": ":bar_chart:"},
                    )

        print("")
        for streamer_index in range(0, len(self.streamers)):
            if self.streamers[streamer_index].history != {}:
                gained = (
                    self.streamers[streamer_index].channel_points
                    - self.original_streamers[streamer_index]
                )

                from colorama import Fore

                streamer_highlight = Fore.YELLOW

                streamer_gain = (
                    f"{streamer_highlight}{self.streamers[streamer_index]}{Fore.RESET}, Total Points Gained: {_millify(gained)}"
                    if Settings.logger.less
                    else f"{streamer_highlight}{repr(self.streamers[streamer_index])}{Fore.RESET}, Total Points Gained (after farming - before farming): {_millify(gained)}"
                )

                indent = " " * 25
                streamer_history = "\n".join(
                    f"{indent}{history}"
                    for history in self.streamers[streamer_index]
                    .print_history()
                    .split("; ")
                )

                logger.info(
                    f"{streamer_gain}\n{streamer_history}",
                    extra={"emoji": ":moneybag:"},
                )

        drop_entries = _drop_progress_report_entries(
            self.original_drop_progress,
            self.twitch.drop_report_snapshot(),
        )
        if drop_entries:
            print("")
            reward_label = "reward" if len(drop_entries) == 1 else "rewards"
            logger.info(
                f"Drop progress gained this session "
                f"({len(drop_entries)} {reward_label}):",
                extra={"emoji": ":gift:"},
            )
            for entry in drop_entries:
                category = entry.get("category") or "Unknown category"
                campaign = entry.get("campaign") or "Unknown campaign"
                item_name = entry.get("item_name") or "Unknown reward"
                watched = entry.get("current_minutes_watched", 0) or 0
                required = entry.get("minutes_required", 0) or 0
                gained = entry.get("minutes_gained", 0) or 0
                status = str(entry.get("status") or "in_progress").replace("_", " ")
                logger.info(
                    f"{category} - {campaign} - {item_name}: "
                    f"{watched}/{required}m (+{gained}m), {status}",
                    extra={"emoji": ":hourglass_flowing_sand:"},
                )
