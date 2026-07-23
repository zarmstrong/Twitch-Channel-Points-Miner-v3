import logging
import threading
from types import SimpleNamespace

from TwitchChannelPointsMiner.TwitchChannelPointsMiner import (
    TwitchChannelPointsMiner,
)
from TwitchChannelPointsMiner.classes.Chat import ChatPresence
from TwitchChannelPointsMiner.classes.entities.Streamer import StreamerSettings
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

    def submit(self, topic):
        self.topics.append(topic)


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
