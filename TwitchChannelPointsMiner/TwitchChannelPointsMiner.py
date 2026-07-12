# -*- coding: utf-8 -*-

import logging
import os
import random
import signal
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from TwitchChannelPointsMiner.classes.Chat import ChatPresence, ThreadChat
from TwitchChannelPointsMiner.classes.entities.PubsubTopic import PubsubTopic
from TwitchChannelPointsMiner.classes.entities.Streamer import (
    Streamer,
    StreamerSettings,
)
from TwitchChannelPointsMiner.classes.Exceptions import StreamerDoesNotExistException
from TwitchChannelPointsMiner.classes.Settings import (
    CategoryCampaignOrder,
    FollowersOrder,
    Priority,
    Settings,
)
from TwitchChannelPointsMiner.classes.Twitch import Twitch
from TwitchChannelPointsMiner.classes.WebSocketsPool import WebSocketsPool
from TwitchChannelPointsMiner.logger import LoggerSettings, configure_loggers
from TwitchChannelPointsMiner.utils import (
    _millify,
    at_least_one_value_in_settings_is,
    check_versions,
    get_user_agent,
    internet_connection_available,
    set_default_settings,
)

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


class TwitchChannelPointsMiner:
    __slots__ = [
        "username",
        "twitch",
        "claim_drops_startup",
        "enable_analytics",
        "disable_ssl_cert_verification",
        "disable_at_in_nickname",
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
        "logs_file",
        "queue_listener",
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
    ):

        # Fixes TypeError: 'NoneType' object is not subscriptable
        if not username or username == "your-twitch-username":
            logger.error("Please edit your runner file (usually run.py) and try again.")
            logger.error("No username, exiting...")
            sys.exit(0)

        # This disables certificate verification and allows the connection to proceed, but also makes it vulnerable to man-in-the-middle (MITM) attacks.
        Settings.disable_ssl_cert_verification = disable_ssl_cert_verification

        Settings.disable_at_in_nickname = disable_at_in_nickname

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

        self.username = username

        # Set as global config
        Settings.logger = logger_settings

        # Init as default all the missing values
        streamer_settings.default()
        streamer_settings.bet.default()
        Settings.streamer_settings = streamer_settings

        # user_agent = get_user_agent("FIREFOX")
        user_agent = get_user_agent("CHROME")
        self.twitch = Twitch(self.username, user_agent, password)

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

        self.logs_file, self.queue_listener = configure_loggers(
            self.username, logger_settings
        )

        # Check for the latest version of the script
        current_version, github_version = check_versions()

        logger.info(
            f"Twitch Channel Points Miner v2-{current_version} (fork by rdavydov)"
        )
        logger.info("https://github.com/rdavydov/Twitch-Channel-Points-Miner-v2")

        if github_version == "0.0.0":
            logger.error(
                "Unable to detect if you have the latest version of this script"
            )
        elif current_version != github_version:
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

            if print_open_drop_campaigns_on_load is True:
                self.twitch.log_open_drop_campaigns()

            if self.twitch.scrape_drop_progress_on_load is True:
                self.twitch.scrape_drop_progress_from_inventory(reason="run_load")

            if self.claim_drops_startup is True:
                self.twitch.claim_all_drops_from_inventory()

            streamers_name: list = []
            streamers_dict: dict = {}
            category_usernames = set()
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

            logger.info(
                f"Loading data for {len(streamers_name)} streamers. Please wait...",
                extra={"emoji": ":nerd_face:"},
            )
            for username in streamers_name:
                if username in streamers_name:
                    time.sleep(random.uniform(0.3, 0.7))
                    try:
                        is_category_streamer = username in category_usernames
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
                                from_category=is_category_streamer,
                                explicitly_configured=(
                                    username in explicitly_configured_usernames
                                ),
                            )
                        )
                        streamer.explicitly_configured = (
                            username in explicitly_configured_usernames
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
                        self.streamers.append(streamer)
                    except StreamerDoesNotExistException:
                        logger.info(
                            f"Streamer {username} does not exist",
                            extra={"emoji": ":cry:"},
                        )

            # Populate the streamers with default values.
            # 1. Load channel points and auto-claim bonus
            # 2. Check if streamers are online
            # 3. DEACTIVATED: Check if the user is a moderator. (was used before the 5th of April 2021 to deactivate predictions)
            for streamer in self.streamers:
                time.sleep(random.uniform(0.3, 0.7))
                try:
                    self.twitch.load_channel_points_context(streamer)
                    self.twitch.check_streamer_online(streamer)
                    # self.twitch.viewer_is_mod(streamer)
                except StreamerDoesNotExistException:
                    logger.info(
                        f"Streamer {streamer.username} does not exist",
                        extra={"emoji": ":cry:"},
                    )

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
            while self.running:
                time.sleep(random.uniform(20, 60))
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
        if self.ws_pool is not None:
            self.ws_pool.end()

        if self.minute_watcher_thread is not None:
            self.minute_watcher_thread.join()

        if self.sync_campaigns_thread is not None:
            self.sync_campaigns_thread.join()

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
