import logging
import threading
from types import SimpleNamespace

from TwitchChannelPointsMiner.TwitchChannelPointsMiner import (
    TwitchChannelPointsMiner,
)
from TwitchChannelPointsMiner.classes.Chat import ChatPresence
from TwitchChannelPointsMiner.classes.entities.Streamer import (
    Streamer,
    StreamerSettings,
)
from TwitchChannelPointsMiner.classes.Settings import Settings


class FakeCatalog:
    def eligible_badge_campaigns(self, owned_badges):
        assert owned_badges == {"owned badge"}
        return [
            {
                "game_slug": "all-channel-game",
                "campaign": {
                    "all_channels": True,
                    "drops": [],
                },
                "eligible_drops": [{"name": "New Badge"}],
            },
            {
                "game_slug": "restricted-game",
                "campaign": {
                    "all_channels": False,
                    "channels": [" AllowedChannel ", "", None, 123],
                    "drops": [],
                },
                "eligible_drops": [{"name": "Restricted Badge"}],
            },
        ]


class FakeTwitch:
    def __init__(self):
        self.selectors = []
        self.twitch_login = SimpleNamespace(
            get_auth_token=lambda: "token",
        )

    def get_earned_badge_names(self, refresh=False):
        assert refresh is True
        return {"owned badge"}

    def get_live_streamers_for_category(self, selector, **kwargs):
        self.selectors.append((selector, kwargs))
        if kwargs.get("restricted_campaigns"):
            return ["allowedchannel"]
        return ["allchannel", "blacklisted"]

    def filter_categories_with_active_drops(self, categories, **_kwargs):
        return categories

    def get_channel_id(self, username):
        return f"id-{username}"

    def load_channel_points_context(self, streamer):
        return None

    def check_streamer_online(self, streamer):
        streamer.is_online = True


class FakeWebSocketsPool:
    def __init__(self):
        self.topics = []
        self.removed = []

    def submit(self, topic):
        self.topics.append(topic)

    def remove_streamer_topics(self, streamer):
        self.removed.append(streamer.username)


def test_auto_mine_badge_campaigns_adds_drop_streamers_and_honors_blacklist():
    defaults = StreamerSettings(chat=ChatPresence.NEVER)
    defaults.default()
    defaults.bet.default()
    Settings.streamer_settings = defaults

    miner = TwitchChannelPointsMiner.__new__(TwitchChannelPointsMiner)
    miner.username = "testuser"
    miner.twitch = FakeTwitch()
    miner.streamers = []
    miner.original_streamers = []
    miner.ws_pool = FakeWebSocketsPool()
    miner.drop_badge_catalog = FakeCatalog()
    miner.badge_drop_streamer_limit = 2
    miner.badge_drop_category_chat = ChatPresence.NEVER
    miner.badge_drop_category_sort = "VIEWERS_DESC"
    miner.badge_drop_blacklist = {"blacklisted"}
    miner.config_reload_lock = threading.Lock()
    miner.sync_campaigns_thread = object()

    miner._TwitchChannelPointsMiner__auto_mine_badge_campaigns()

    assert [streamer.username for streamer in miner.streamers] == [
        "allchannel",
        "allowedchannel",
    ]
    assert all(streamer.settings.claim_drops is True for streamer in miner.streamers)
    assert all(streamer.from_badge_campaign is True for streamer in miner.streamers)
    assert miner.original_streamers == [0, 0]
    assert miner.twitch.selectors == [
        (
            "all-channel-game",
            {
                "drops_enabled": True,
                "limit": 2,
                "sort_by": "VIEWERS_DESC",
                "respect_campaign_restrictions": False,
            },
        ),
        (
            "restricted-game",
            {
                "drops_enabled": True,
                "limit": 30,
                "sort_by": "VIEWERS_DESC",
                "restricted_campaigns": [
                    {
                        "all_channels": False,
                        "channels": ["allowedchannel"],
                        "drops": [],
                    }
                ],
            },
        ),
    ]


def test_badge_ownership_change_retires_only_stale_badge_streamers():
    miner = TwitchChannelPointsMiner.__new__(TwitchChannelPointsMiner)
    stale = Streamer(
        "stale",
        from_category=True,
        from_badge_campaign=True,
    )
    current = Streamer(
        "current",
        from_category=True,
        from_badge_campaign=True,
    )
    configured = Streamer(
        "configured",
        from_category=True,
        from_badge_campaign=True,
        explicitly_configured=True,
    )
    miner.streamers = [stale, current, configured]
    miner.original_streamers = [10, 20, 30]
    miner.ws_pool = FakeWebSocketsPool()

    miner._TwitchChannelPointsMiner__reconcile_badge_campaign_streamers(["current"])

    assert [streamer.username for streamer in miner.streamers] == [
        "current",
        "configured",
    ]
    assert miner.original_streamers == [20, 30]
    assert miner.ws_pool.removed == ["stale"]
    assert current.from_badge_campaign is True
    assert configured.from_badge_campaign is False


def test_badge_inventory_failure_preserves_baseline_for_next_refresh():
    class RecoveringTwitch(FakeTwitch):
        def __init__(self):
            super().__init__()
            self.available_badge_names = {"old badge"}
            self.responses = iter([None, {"old badge", "new badge"}])

        def get_earned_badge_names(self, refresh=False):
            assert refresh is True
            self.available_badge_names = None
            result = next(self.responses)
            if result is not None:
                self.available_badge_names = result
            return result

    class EmptyCatalog:
        def eligible_badge_campaigns(self, owned_badges):
            return []

    miner = TwitchChannelPointsMiner.__new__(TwitchChannelPointsMiner)
    stale = Streamer(
        "stale",
        from_category=True,
        from_badge_campaign=True,
    )
    miner.twitch = RecoveringTwitch()
    miner.streamers = [stale]
    miner.original_streamers = [10]
    miner.ws_pool = FakeWebSocketsPool()
    miner.drop_badge_catalog = EmptyCatalog()
    miner.badge_drop_streamer_limit = 1
    miner.badge_drop_category_chat = ChatPresence.NEVER
    miner.badge_drop_category_sort = "VIEWERS_DESC"
    miner.badge_drop_blacklist = set()
    miner.config_reload_lock = threading.Lock()
    miner.sync_campaigns_thread = object()

    miner._TwitchChannelPointsMiner__auto_mine_badge_campaigns()

    assert miner.twitch.available_badge_names == {"old badge"}
    assert miner.streamers == [stale]

    miner._TwitchChannelPointsMiner__auto_mine_badge_campaigns()

    assert miner.streamers == []
    assert miner.original_streamers == []
    assert miner.ws_pool.removed == ["stale"]


def test_category_discovery_keeps_point_baselines_aligned():
    defaults = StreamerSettings(chat=ChatPresence.NEVER)
    defaults.default()
    defaults.bet.default()
    Settings.streamer_settings = defaults

    miner = TwitchChannelPointsMiner.__new__(TwitchChannelPointsMiner)
    miner.username = "testuser"
    miner.twitch = FakeTwitch()
    miner.streamers = []
    miner.original_streamers = []
    miner.ws_pool = FakeWebSocketsPool()
    miner.config_reload_lock = threading.Lock()
    miner.sync_campaigns_thread = object()

    miner._TwitchChannelPointsMiner__refresh_category_streamers(
        ["game"],
        [],
        True,
        2,
        "VIEWERS_DESC",
        "ORDER",
        ChatPresence.NEVER,
        logging.INFO,
    )

    assert [streamer.username for streamer in miner.streamers] == [
        "allchannel",
        "blacklisted",
    ]
    assert miner.original_streamers == [0, 0]


def test_category_refresh_retires_streamers_missing_from_latest_discovery():
    miner = TwitchChannelPointsMiner.__new__(TwitchChannelPointsMiner)
    stale = Streamer("stale", from_category=True)
    current = Streamer("allchannel", from_category=True)
    miner.streamers = [stale, current]
    miner.original_streamers = [10, 20]
    miner.ws_pool = FakeWebSocketsPool()

    miner._TwitchChannelPointsMiner__reconcile_category_streamers(
        ["allchannel", "blacklisted"]
    )

    assert miner.streamers == [current]
    assert miner.original_streamers == [20]
    assert miner.ws_pool.removed == ["stale"]


def test_category_refresh_preserves_other_sources_when_category_is_stale():
    miner = TwitchChannelPointsMiner.__new__(TwitchChannelPointsMiner)
    configured = Streamer(
        "configured",
        from_category=True,
        explicitly_configured=True,
    )
    followed = Streamer("followed", from_category=True, from_followers=True)
    badge = Streamer("badge", from_category=True, from_badge_campaign=True)
    miner.streamers = [configured, followed, badge]
    miner.original_streamers = [10, 20, 30]
    miner.ws_pool = FakeWebSocketsPool()

    miner._TwitchChannelPointsMiner__reconcile_category_streamers([])

    assert miner.streamers == [configured, followed, badge]
    assert miner.original_streamers == [10, 20, 30]
    assert miner.ws_pool.removed == []
    assert all(streamer.from_category is False for streamer in miner.streamers)


def test_category_refresh_reorders_existing_streamers_to_latest_priority():
    defaults = StreamerSettings(chat=ChatPresence.NEVER)
    defaults.default()
    defaults.bet.default()
    Settings.streamer_settings = defaults

    miner = TwitchChannelPointsMiner.__new__(TwitchChannelPointsMiner)
    lower_priority = Streamer("blacklisted", from_category=True)
    explicit = Streamer("explicit", explicitly_configured=True)
    higher_priority = Streamer("allchannel", from_category=True)
    miner.username = "testuser"
    miner.twitch = FakeTwitch()
    miner.streamers = [lower_priority, explicit, higher_priority]
    miner.original_streamers = [10, 20, 30]
    miner.ws_pool = FakeWebSocketsPool()
    miner.config_reload_lock = threading.Lock()
    miner.sync_campaigns_thread = object()

    miner._TwitchChannelPointsMiner__refresh_category_streamers(
        ["game"],
        [],
        True,
        2,
        "VIEWERS_DESC",
        "ORDER",
        ChatPresence.NEVER,
        logging.INFO,
    )

    assert miner.streamers == [higher_priority, explicit, lower_priority]
    assert miner.original_streamers == [30, 20, 10]


def test_category_refresh_reorder_repairs_missing_baselines():
    miner = TwitchChannelPointsMiner.__new__(TwitchChannelPointsMiner)
    lower_priority = Streamer("lower", from_category=True)
    higher_priority = Streamer("higher", from_category=True)
    lower_priority.channel_points = 10
    higher_priority.channel_points = 30
    miner.streamers = [lower_priority, higher_priority]
    miner.original_streamers = [10]

    miner._TwitchChannelPointsMiner__order_category_streamers(["higher", "lower"])

    assert miner.streamers == [higher_priority, lower_priority]
    assert miner.original_streamers == [30, 10]


def test_category_refresh_adds_replacements_before_retiring_stale_streamers():
    defaults = StreamerSettings(chat=ChatPresence.NEVER)
    defaults.default()
    defaults.bet.default()
    Settings.streamer_settings = defaults

    class RecordingTwitch(FakeTwitch):
        def __init__(self, miner):
            super().__init__()
            self.miner = miner
            self.streamers_seen_during_load = []

        def load_channel_points_context(self, streamer):
            self.streamers_seen_during_load.append(
                [current.username for current in self.miner.streamers]
            )

    miner = TwitchChannelPointsMiner.__new__(TwitchChannelPointsMiner)
    stale = Streamer("stale", from_category=True)
    miner.username = "testuser"
    miner.streamers = [stale]
    miner.original_streamers = [10]
    miner.ws_pool = FakeWebSocketsPool()
    miner.config_reload_lock = threading.Lock()
    miner.sync_campaigns_thread = object()
    miner.twitch = RecordingTwitch(miner)

    miner._TwitchChannelPointsMiner__refresh_category_streamers(
        ["game"],
        [],
        True,
        2,
        "VIEWERS_DESC",
        "ORDER",
        ChatPresence.NEVER,
        logging.INFO,
    )

    assert len(miner.twitch.streamers_seen_during_load) == 2
    assert all(
        "stale" in usernames
        for usernames in miner.twitch.streamers_seen_during_load
    )
    assert [streamer.username for streamer in miner.streamers] == [
        "allchannel",
        "blacklisted",
    ]
    assert miner.ws_pool.removed == ["stale"]
