import logging
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from TwitchChannelPointsMiner.TwitchChannelPointsMiner import (
    BADGE_LOOKUP_FAILURE_LIMIT,
    TwitchChannelPointsMiner,
)
from TwitchChannelPointsMiner.classes.Chat import ChatPresence
from TwitchChannelPointsMiner.classes.entities.Streamer import (
    Streamer,
    StreamerSettings,
)
from TwitchChannelPointsMiner.classes.Settings import Settings
from TwitchChannelPointsMiner.classes.Twitch import Twitch


class FakeCatalog:
    def eligible_badge_campaigns(
        self, owned_badges, completed_campaign_signatures=None
    ):
        assert owned_badges == {"owned badge"}
        assert completed_campaign_signatures == {"a-signature"}
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
        self.unevaluated_category_slugs = set()
        self.twitch_login = SimpleNamespace(
            get_auth_token=lambda: "token",
        )
        self.completed_badge_campaign_signature_cache = None

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

    def get_category_slugs(self, categories):
        return set(categories)

    def get_drops_inventory(self):
        return self._record({"present": True, "completedRewardCampaigns": []})

    def _record(self, inventory):
        # Twitch.__get_inventory records every loaded inventory's completions
        # before returning it; the badge check only reads the cache.
        if Twitch.drops_inventory_completions_loaded(inventory):
            self.completed_badge_campaign_signature_cache = (
                self.completed_badge_campaign_signature_cache or set()
            ) | self.completed_badge_campaign_signatures(inventory)
        return inventory

    def completed_badge_campaign_signatures(self, inventory):
        assert inventory == {"present": True, "completedRewardCampaigns": []}
        return {"a-signature"}

    # The real cache logic, so these tests exercise it rather than a copy.
    drops_inventory_completions_loaded = staticmethod(
        Twitch.drops_inventory_completions_loaded
    )
    drops_inventory_completions_state = staticmethod(
        Twitch.drops_inventory_completions_state
    )
    completed_badge_campaign_signatures_or_cached = (
        Twitch.completed_badge_campaign_signatures_or_cached
    )

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

    miner.category_lookup_failure_streaks = {}
    miner.username = "testuser"
    miner.twitch = FakeTwitch()
    miner.streamers = []
    miner.original_streamers = []
    miner.ws_pool = FakeWebSocketsPool()
    miner.drop_badge_catalog = FakeCatalog()
    miner.badge_inventory_state = None
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


def _badge_check_miner(twitch):
    defaults = StreamerSettings(chat=ChatPresence.NEVER)
    defaults.default()
    defaults.bet.default()
    Settings.streamer_settings = defaults

    miner = TwitchChannelPointsMiner.__new__(TwitchChannelPointsMiner)

    miner.category_lookup_failure_streaks = {}
    miner.username = "testuser"
    miner.twitch = twitch
    miner.streamers = [
        Streamer(
            "previous-random",
            from_category=True,
            from_badge_campaign=True,
            discovery_category_slugs={"restricted-game"},
        )
    ]
    miner.original_streamers = [10]
    miner.ws_pool = FakeWebSocketsPool()
    miner.drop_badge_catalog = FakeCatalog()
    miner.badge_inventory_state = None
    miner.badge_drop_streamer_limit = 2
    miner.badge_drop_category_chat = ChatPresence.NEVER
    miner.badge_drop_category_sort = "RANDOM"
    miner.badge_drop_blacklist = set()
    miner.config_reload_lock = threading.Lock()
    miner.sync_campaigns_thread = object()
    return miner


def test_badge_check_retires_channels_it_no_longer_returns():
    # With limit 1 and a random sort, each check returned another channel and
    # none were retired until the earned badges changed.
    miner = _badge_check_miner(FakeTwitch())

    miner._TwitchChannelPointsMiner__auto_mine_badge_campaigns()

    assert [streamer.username for streamer in miner.streamers] == [
        "allchannel",
        "blacklisted",
        "allowedchannel",
    ]
    assert miner.ws_pool.removed == ["previous-random"]


def test_failed_badge_lookup_does_not_retire_badge_channels():
    class FailingTwitch(FakeTwitch):
        def get_live_streamers_for_category(self, selector, **kwargs):
            if kwargs.get("restricted_campaigns"):
                raise RuntimeError("helix unavailable")
            return super().get_live_streamers_for_category(selector, **kwargs)

    miner = _badge_check_miner(FailingTwitch())

    miner._TwitchChannelPointsMiner__auto_mine_badge_campaigns()

    assert "previous-random" in [streamer.username for streamer in miner.streamers]
    assert miner.ws_pool.removed == []


class RestrictedGameFailingTwitch(FakeTwitch):
    """The restricted badge game's lookup keeps failing; the all-channel game
    is evaluated and has nobody live."""

    def get_live_streamers_for_category(self, selector, **kwargs):
        if kwargs.get("restricted_campaigns"):
            from TwitchChannelPointsMiner.classes.Exceptions import (
                CategoryLookupFailedException,
            )

            raise CategoryLookupFailedException(
                "directory errors", game_slug="restricted-game"
            )
        return []


def _badge_streamer_in(username, game_slug):
    return Streamer(
        username,
        from_category=True,
        from_badge_campaign=True,
        discovery_category_slugs={game_slug},
    )


def test_failed_badge_game_does_not_shield_channels_of_evaluated_games():
    miner = _badge_check_miner(RestrictedGameFailingTwitch())
    evaluated = _badge_streamer_in("evaluated-stale", "all-channel-game")
    failed = _badge_streamer_in("failed-game", "restricted-game")
    miner.streamers = [evaluated, failed]
    miner.original_streamers = [10, 20]

    miner._TwitchChannelPointsMiner__auto_mine_badge_campaigns()

    assert miner.streamers == [failed]
    assert miner.original_streamers == [20]
    assert miner.ws_pool.removed == ["evaluated-stale"]


def test_badge_game_that_keeps_failing_retires_its_channels_after_the_limit(caplog):
    miner = _badge_check_miner(RestrictedGameFailingTwitch())
    failed = _badge_streamer_in("failed-game", "restricted-game")
    miner.streamers.append(failed)
    miner.original_streamers.append(20)

    for _ in range(BADGE_LOOKUP_FAILURE_LIMIT - 1):
        miner._TwitchChannelPointsMiner__auto_mine_badge_campaigns()
    assert [streamer.username for streamer in miner.streamers] == [
        "previous-random",
        "failed-game",
    ]

    with caplog.at_level(logging.WARNING):
        miner._TwitchChannelPointsMiner__auto_mine_badge_campaigns()

    assert miner.streamers == []
    assert miner.ws_pool.removed == ["previous-random", "failed-game"]
    assert (
        f"Badge game lookups for restricted-game failed "
        f"{BADGE_LOOKUP_FAILURE_LIMIT} badge Drop checks in a row"
    ) in caplog.text


def test_badge_game_lookup_success_restarts_the_failure_limit():
    class RecoveringTwitch(RestrictedGameFailingTwitch):
        recovered = False

        def get_live_streamers_for_category(self, selector, **kwargs):
            if self.recovered:
                return []
            return super().get_live_streamers_for_category(selector, **kwargs)

    twitch = RecoveringTwitch()
    miner = _badge_check_miner(twitch)
    miner.streamers = [_badge_streamer_in("failed-game", "restricted-game")]
    miner.original_streamers = [20]

    for _ in range(BADGE_LOOKUP_FAILURE_LIMIT - 1):
        miner._TwitchChannelPointsMiner__auto_mine_badge_campaigns()
    twitch.recovered = True
    miner._TwitchChannelPointsMiner__auto_mine_badge_campaigns()
    assert miner.ws_pool.removed == ["failed-game"]

    # A channel rediscovered after the recovery gets the full allowance again.
    twitch.recovered = False
    miner.streamers = [_badge_streamer_in("failed-game", "restricted-game")]
    miner.original_streamers = [20]
    for _ in range(BADGE_LOOKUP_FAILURE_LIMIT - 1):
        miner._TwitchChannelPointsMiner__auto_mine_badge_campaigns()
    assert [streamer.username for streamer in miner.streamers] == ["failed-game"]


def test_badge_channels_record_the_game_they_were_found_under():
    miner = _badge_check_miner(FakeTwitch())
    rediscovered = _badge_streamer_in("allowedchannel", "old-game")
    miner.streamers = [rediscovered]
    miner.original_streamers = [10]

    miner._TwitchChannelPointsMiner__auto_mine_badge_campaigns()

    slugs = {
        streamer.username: streamer.discovery_category_slugs
        for streamer in miner.streamers
    }
    assert slugs == {
        "allowedchannel": {"restricted-game"},
        "allchannel": {"all-channel-game"},
        "blacklisted": {"all-channel-game"},
    }


def test_badge_ownership_change_retires_only_stale_badge_streamers():
    miner = TwitchChannelPointsMiner.__new__(TwitchChannelPointsMiner)
    miner.category_lookup_failure_streaks = {}
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
    # from_category was only ever True because this was a badge campaign;
    # retiring the badge must reset it too, or the retained streamer
    # permanently misclassifies as a real category-discovery streamer.
    assert configured.from_category is False


def test_badge_retirement_never_exposes_retiring_streamers_as_configured():
    from TwitchChannelPointsMiner.classes.entities.Streamer import (
        discovery_sources,
    )
    from TwitchChannelPointsMiner.classes.Settings import StreamerSource

    class SnapshottingPool(FakeWebSocketsPool):
        """Record how the watch thread would classify the live list mid-loop."""

        def __init__(self, miner):
            super().__init__()
            self.miner = miner
            self.snapshots = []

        def remove_streamer_topics(self, streamer):
            super().remove_streamer_topics(streamer)
            self.snapshots.append(
                {s.username: discovery_sources(s) for s in self.miner.streamers}
            )

    miner = TwitchChannelPointsMiner.__new__(TwitchChannelPointsMiner)

    miner.category_lookup_failure_streaks = {}
    miner.streamers = [
        Streamer("first-retired", from_category=True, from_badge_campaign=True),
        Streamer("second-retired", from_category=True, from_badge_campaign=True),
    ]
    miner.original_streamers = [10, 20]
    miner.ws_pool = SnapshottingPool(miner)

    miner._TwitchChannelPointsMiner__reconcile_badge_campaign_streamers([])

    assert len(miner.ws_pool.snapshots) == 2
    for snapshot in miner.ws_pool.snapshots:
        for username, sources in snapshot.items():
            assert StreamerSource.STREAMERS not in sources, username
    assert miner.streamers == []


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
        def eligible_badge_campaigns(
            self, owned_badges, completed_campaign_signatures=None
        ):
            return []

    miner = TwitchChannelPointsMiner.__new__(TwitchChannelPointsMiner)

    miner.category_lookup_failure_streaks = {}
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
    miner.badge_inventory_state = None
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


class BaselineTwitch(FakeTwitch):
    """Earned badges change on refresh, so a restored baseline is observable."""

    def __init__(self):
        super().__init__()
        self.available_badge_names = {"old badge"}

    def get_earned_badge_names(self, refresh=False):
        assert refresh is True
        self.available_badge_names = {"owned badge"}
        return self.available_badge_names


class UntouchedCatalog:
    def eligible_badge_campaigns(
        self, owned_badges, completed_campaign_signatures=None
    ):
        raise AssertionError("the badge check must be skipped this cycle")


class CompletedCatalog:
    """Every catalog campaign matches a completed-campaign signature."""

    def eligible_badge_campaigns(
        self, owned_badges, completed_campaign_signatures=None
    ):
        assert owned_badges == {"owned badge"}
        assert completed_campaign_signatures == {"a-signature"}
        return []


UNUSABLE_INVENTORIES = {
    # Twitch.get_drops_inventory() returns {} (never raises) when the request
    # or its parsing fails.
    "failed-load": {},
    # A partial response passes drops_inventory_loaded with
    # completedRewardCampaigns errored to null.
    "partial-response": {
        "dropCampaignsInProgress": [],
        "completedRewardCampaigns": None,
    },
}


class NoInventoryTwitch(BaselineTwitch):
    def __init__(self, inventory):
        super().__init__()
        self.inventory = inventory

    def get_drops_inventory(self):
        return self._record(self.inventory)

    def completed_badge_campaign_signatures(self, inventory):
        raise AssertionError("should not be called when the inventory is unusable")


def _records(caplog, fragment):
    return [record for record in caplog.records if fragment in record.getMessage()]


@pytest.mark.parametrize("inventory_id", list(UNUSABLE_INVENTORIES))
def test_auto_mine_badge_campaigns_skips_without_cache_when_inventory_unusable(
    caplog, inventory_id
):
    # Both used to read as "no campaign completed" and re-add the channels of
    # finished campaigns.
    miner = _badge_check_miner(NoInventoryTwitch(UNUSABLE_INVENTORIES[inventory_id]))
    miner.drop_badge_catalog = UntouchedCatalog()
    tracked = list(miner.streamers)

    with caplog.at_level(logging.INFO):
        miner._TwitchChannelPointsMiner__auto_mine_badge_campaigns()

    assert miner.streamers == tracked
    assert miner.original_streamers == [10]
    assert miner.ws_pool.removed == []
    assert miner.twitch.selectors == []
    assert miner.twitch.available_badge_names == {"old badge"}
    assert miner.twitch.completed_badge_campaign_signature_cache is None
    # A skipped cycle warns like the earned-badge skip does.
    skip_records = _records(caplog, "skipping automatic badge Drop discovery")
    assert [record.levelno for record in skip_records] == [logging.WARNING]
    if inventory_id == "partial-response":
        assert "error for the completed Drop campaigns" in (
            skip_records[0].getMessage()
        )


@pytest.mark.parametrize("inventory_id", list(UNUSABLE_INVENTORIES))
def test_auto_mine_badge_campaigns_uses_cached_completions_when_inventory_unusable(
    caplog, inventory_id
):
    miner = _badge_check_miner(NoInventoryTwitch(UNUSABLE_INVENTORIES[inventory_id]))
    miner.drop_badge_catalog = CompletedCatalog()
    miner.twitch.completed_badge_campaign_signature_cache = {"a-signature"}

    with caplog.at_level(logging.INFO):
        miner._TwitchChannelPointsMiner__auto_mine_badge_campaigns()

    # The cycle ran against the cached result: the completed campaign stayed
    # excluded and its tracked channel was retired.
    assert miner.streamers == []
    assert miner.original_streamers == []
    assert miner.ws_pool.removed == ["previous-random"]
    assert miner.twitch.available_badge_names == {"owned badge"}
    assert miner.twitch.completed_badge_campaign_signature_cache == {"a-signature"}
    assert len(_records(caplog, "last loaded completed-campaign result")) == 1


def test_auto_mine_badge_campaigns_logs_inventory_fallback_only_on_transitions(
    caplog,
):
    twitch = NoInventoryTwitch({})
    twitch.completed_badge_campaign_signature_cache = {"a-signature"}
    miner = _badge_check_miner(twitch)
    miner.drop_badge_catalog = CompletedCatalog()

    with caplog.at_level(logging.DEBUG):
        # Two failed checks in a row: the streak is reported once at the
        # category level, then only at DEBUG.
        miner._TwitchChannelPointsMiner__auto_mine_badge_campaigns()
        miner._TwitchChannelPointsMiner__auto_mine_badge_campaigns()
        fallback_levels = [
            record.levelno
            for record in _records(caplog, "last loaded completed-campaign result")
        ]
        assert fallback_levels == [logging.INFO, logging.DEBUG]
        assert miner.badge_inventory_state == "unavailable-cached"

        # A different unusable state is a transition and is reported again.
        twitch.inventory = UNUSABLE_INVENTORIES["partial-response"]
        miner._TwitchChannelPointsMiner__auto_mine_badge_campaigns()
        assert [
            record.levelno
            for record in _records(caplog, "error for the completed Drop campaigns")
        ] == [logging.INFO]
        assert miner.badge_inventory_state == "errored-completions-cached"

        # A loaded inventory without the field will not recover by itself,
        # so it warns even with a cached result.
        twitch.inventory = {"dropCampaignsInProgress": []}
        miner._TwitchChannelPointsMiner__auto_mine_badge_campaigns()
        assert [
            record.levelno
            for record in _records(caplog, "without a completedRewardCampaigns list")
        ] == [logging.WARNING]
        assert miner.badge_inventory_state == "missing-completions-cached"

        # Recovery is reported once and clears the streak.
        twitch.inventory = {"present": True, "completedRewardCampaigns": []}
        twitch.completed_badge_campaign_signatures = lambda inventory: {"a-signature"}
        miner._TwitchChannelPointsMiner__auto_mine_badge_campaigns()
        miner._TwitchChannelPointsMiner__auto_mine_badge_campaigns()
        assert [
            record.levelno
            for record in _records(caplog, "badge Drop completion checks resumed")
        ] == [logging.INFO]
        assert miner.badge_inventory_state is None


def test_auto_mine_badge_campaigns_logs_skip_streak_once_at_warning(caplog):
    twitch = NoInventoryTwitch({})
    miner = _badge_check_miner(twitch)
    miner.drop_badge_catalog = UntouchedCatalog()

    with caplog.at_level(logging.DEBUG):
        miner._TwitchChannelPointsMiner__auto_mine_badge_campaigns()
        miner._TwitchChannelPointsMiner__auto_mine_badge_campaigns()
        assert [
            record.levelno
            for record in _records(caplog, "skipping automatic badge Drop discovery")
        ] == [logging.WARNING, logging.DEBUG]
        assert miner.badge_inventory_state == "unavailable-skipped"

        # The inventory loads: the cycle runs again and recovery is logged once.
        twitch.inventory = {"present": True, "completedRewardCampaigns": []}
        twitch.completed_badge_campaign_signatures = lambda inventory: {"a-signature"}
        miner.drop_badge_catalog = CompletedCatalog()
        miner._TwitchChannelPointsMiner__auto_mine_badge_campaigns()
        assert [
            record.levelno
            for record in _records(caplog, "badge Drop completion checks resumed")
        ] == [logging.INFO]
        assert miner.badge_inventory_state is None
        assert miner.ws_pool.removed == ["previous-random"]


def test_auto_mine_badge_campaigns_retires_completed_campaigns_when_inventory_loads():
    # A loaded inventory feeds the completed-campaign signatures to the catalog
    # and refreshes the cache, and a tracked channel whose campaign is now
    # complete is retired.
    miner = _badge_check_miner(BaselineTwitch())
    miner.drop_badge_catalog = CompletedCatalog()

    miner._TwitchChannelPointsMiner__auto_mine_badge_campaigns()

    assert miner.streamers == []
    assert miner.original_streamers == []
    assert miner.ws_pool.removed == ["previous-random"]
    assert miner.twitch.selectors == []
    assert miner.twitch.available_badge_names == {"owned badge"}
    assert miner.twitch.completed_badge_campaign_signature_cache == {"a-signature"}


def test_auto_mine_badge_campaigns_restores_badge_baseline_when_check_raises():
    # An unexpected error while reading the completions aborts the cycle
    # through the outer handler.
    class BrokenTwitch(BaselineTwitch):
        def completed_badge_campaign_signatures(self, *args):
            raise RuntimeError("unexpected inventory failure")

    miner = _badge_check_miner(BrokenTwitch())
    miner.drop_badge_catalog = UntouchedCatalog()
    tracked = list(miner.streamers)

    miner._TwitchChannelPointsMiner__auto_mine_badge_campaigns()

    assert miner.streamers == tracked
    assert miner.original_streamers == [10]
    assert miner.ws_pool.removed == []
    assert miner.twitch.available_badge_names == {"old badge"}


class RaisingInventoryTwitch(BaselineTwitch):
    def get_drops_inventory(self):
        raise RuntimeError("unexpected inventory failure")


def test_auto_mine_badge_campaigns_skips_when_inventory_raises_without_cache(caplog):
    # get_drops_inventory reports failures by returning {}; one that raises
    # anyway is treated as an unavailable inventory, not a broken check.
    miner = _badge_check_miner(RaisingInventoryTwitch())
    miner.drop_badge_catalog = UntouchedCatalog()
    tracked = list(miner.streamers)

    with caplog.at_level(logging.DEBUG):
        miner._TwitchChannelPointsMiner__auto_mine_badge_campaigns()

    assert miner.streamers == tracked
    assert miner.ws_pool.removed == []
    assert miner.twitch.available_badge_names == {"old badge"}
    assert any(
        "unexpected inventory failure" in record.getMessage()
        for record in caplog.records
    )
    assert not any(
        "Unable to determine eligible badge Drop campaigns" in record.getMessage()
        for record in caplog.records
    )


def test_auto_mine_badge_campaigns_uses_cache_when_inventory_raises():
    twitch = RaisingInventoryTwitch()
    twitch.completed_badge_campaign_signature_cache = {"a-signature"}
    miner = _badge_check_miner(twitch)
    miner.drop_badge_catalog = CompletedCatalog()

    miner._TwitchChannelPointsMiner__auto_mine_badge_campaigns()

    # The cycle ran against the cached completions and retired the tracked
    # streamer of the completed campaign.
    assert miner.streamers == []
    assert miner.ws_pool.removed == ["previous-random"]


def test_auto_mine_badge_campaigns_adds_channels_a_failed_lookup_still_found():
    from TwitchChannelPointsMiner.classes.Exceptions import (
        CategoryLookupFailedException,
    )

    class PartialTwitch(FakeTwitch):
        def get_live_streamers_for_category(self, selector, **kwargs):
            self.selectors.append((selector, kwargs))
            if kwargs.get("restricted_campaigns"):
                raise CategoryLookupFailedException(
                    "second batch failed", usernames=["partialchannel"]
                )
            return ["allchannel"]

    defaults = StreamerSettings(chat=ChatPresence.NEVER)
    defaults.default()
    defaults.bet.default()
    Settings.streamer_settings = defaults
    miner = TwitchChannelPointsMiner.__new__(TwitchChannelPointsMiner)
    miner.category_lookup_failure_streaks = {}
    miner.username = "testuser"
    miner.twitch = PartialTwitch()
    miner.streamers = []
    miner.original_streamers = []
    miner.ws_pool = FakeWebSocketsPool()
    miner.drop_badge_catalog = FakeCatalog()
    miner.badge_inventory_state = None
    miner.badge_drop_streamer_limit = 2
    miner.badge_drop_category_chat = ChatPresence.NEVER
    miner.badge_drop_category_sort = "VIEWERS_DESC"
    miner.badge_drop_blacklist = set()
    miner.config_reload_lock = threading.Lock()
    miner.sync_campaigns_thread = object()

    miner._TwitchChannelPointsMiner__auto_mine_badge_campaigns()

    assert [streamer.username for streamer in miner.streamers] == [
        "allchannel",
        "partialchannel",
    ]


def test_category_discovery_keeps_point_baselines_aligned():
    defaults = StreamerSettings(chat=ChatPresence.NEVER)
    defaults.default()
    defaults.bet.default()
    Settings.streamer_settings = defaults

    miner = TwitchChannelPointsMiner.__new__(TwitchChannelPointsMiner)

    miner.category_lookup_failure_streaks = {}
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
    miner.category_lookup_failure_streaks = {}
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
    miner.category_lookup_failure_streaks = {}
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
    assert configured.from_category is False
    assert followed.from_category is False
    # The badge streamer's from_category marks it as drop-discovered and must
    # survive a category refresh, which only owns category-tier streamers.
    assert badge.from_category is True
    assert badge.from_badge_campaign is True


def test_category_refresh_leaves_badge_streamer_flags_when_category_is_stale():
    # A badge streamer is not part of either category discovery pass, so
    # neither a regular nor a wildcard refresh may strip its from_category
    # marker (which keeps it gated by drop eligibility).
    for wildcard in (False, True):
        miner = TwitchChannelPointsMiner.__new__(TwitchChannelPointsMiner)
        miner.category_lookup_failure_streaks = {}
        badge = Streamer("badge", from_category=True, from_badge_campaign=True)
        miner.streamers = [badge]
        miner.original_streamers = [30]
        miner.ws_pool = FakeWebSocketsPool()

        miner._TwitchChannelPointsMiner__reconcile_category_streamers(
            [], wildcard=wildcard
        )

        assert miner.streamers == [badge]
        assert badge.from_category is True
        assert badge.from_badge_campaign is True
        assert miner.ws_pool.removed == []


def test_category_refresh_reorders_existing_streamers_to_latest_priority():
    defaults = StreamerSettings(chat=ChatPresence.NEVER)
    defaults.default()
    defaults.bet.default()
    Settings.streamer_settings = defaults

    miner = TwitchChannelPointsMiner.__new__(TwitchChannelPointsMiner)

    miner.category_lookup_failure_streaks = {}
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
    miner.category_lookup_failure_streaks = {}
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

    miner.category_lookup_failure_streaks = {}
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
        "stale" in usernames for usernames in miner.twitch.streamers_seen_during_load
    )
    assert [streamer.username for streamer in miner.streamers] == [
        "allchannel",
        "blacklisted",
    ]
    assert miner.ws_pool.removed == ["stale"]


def test_rediscovered_category_streamer_is_checked_before_it_can_be_watched():
    # A channel retired for missing one category refresh loses its stored
    # channel check verdict. When discovery finds it again it must be
    # re-checked before the watch loop can see it, so a channel Twitch
    # already ruled out cannot take the Drops slot on discovery's seed.
    defaults = StreamerSettings(chat=ChatPresence.NEVER)
    defaults.default()
    defaults.bet.default()
    Settings.streamer_settings = defaults

    miner = TwitchChannelPointsMiner.__new__(TwitchChannelPointsMiner)

    miner.category_lookup_failure_streaks = {}
    miner.username = "testuser"
    miner.twitch = FakeTwitch()
    miner.streamers = []
    miner.original_streamers = []
    miner.ws_pool = FakeWebSocketsPool()
    checked_while_listed = []

    def check_streamer_online(streamer):
        checked_while_listed.append(streamer in miner.streamers)
        streamer.is_online = True

    miner.twitch.check_streamer_online = check_streamer_online

    added = miner._TwitchChannelPointsMiner__add_discovered_category_streamers(
        ["returning"], [], ChatPresence.NEVER
    )

    assert added == 1
    assert checked_while_listed == [False]
    assert [streamer.username for streamer in miner.streamers] == ["returning"]


def test_auto_mine_badge_campaigns_runs_without_completions_field(caplog):
    """An inventory that loads without completedRewardCampaigns (and without
    an error for it) means the response no longer carries the field. Badge
    discovery keeps running instead of standing down indefinitely."""

    class EmptyCompletionsCatalog:
        def eligible_badge_campaigns(
            self, owned_badges, completed_campaign_signatures=None
        ):
            assert completed_campaign_signatures == set()
            return []

    miner = _badge_check_miner(NoInventoryTwitch({"dropCampaignsInProgress": []}))
    miner.drop_badge_catalog = EmptyCompletionsCatalog()

    with caplog.at_level(logging.DEBUG):
        miner._TwitchChannelPointsMiner__auto_mine_badge_campaigns()
        miner._TwitchChannelPointsMiner__auto_mine_badge_campaigns()

    assert miner.ws_pool.removed == ["previous-random"]
    assert miner.badge_inventory_state == "missing-completions-unmatched"
    assert [
        record.levelno
        for record in _records(caplog, "without a completedRewardCampaigns list")
    ] == [logging.WARNING, logging.DEBUG]


def test_auto_mine_badge_campaigns_repeats_persistent_state_hourly(caplog, monkeypatch):
    clock = [1000.0]
    miner_module = sys.modules[TwitchChannelPointsMiner.__module__]
    monkeypatch.setattr(
        miner_module,
        "time",
        SimpleNamespace(
            **{
                **{name: getattr(time, name) for name in dir(time)},
                "monotonic": lambda: clock[0],
            }
        ),
    )
    miner = _badge_check_miner(NoInventoryTwitch({}))
    miner.drop_badge_catalog = UntouchedCatalog()

    with caplog.at_level(logging.DEBUG):
        miner._TwitchChannelPointsMiner__auto_mine_badge_campaigns()
        clock[0] += TwitchChannelPointsMiner.BADGE_INVENTORY_REMINDER_SECONDS - 1
        miner._TwitchChannelPointsMiner__auto_mine_badge_campaigns()
        clock[0] += 1
        miner._TwitchChannelPointsMiner__auto_mine_badge_campaigns()
        miner._TwitchChannelPointsMiner__auto_mine_badge_campaigns()

    assert [
        record.levelno
        for record in _records(caplog, "skipping automatic badge Drop discovery")
    ] == [logging.WARNING, logging.DEBUG, logging.WARNING, logging.DEBUG]


def test_auto_mine_badge_campaigns_reports_raising_inventory_once(caplog):
    miner = _badge_check_miner(RaisingInventoryTwitch())
    miner.drop_badge_catalog = UntouchedCatalog()

    with caplog.at_level(logging.DEBUG):
        miner._TwitchChannelPointsMiner__auto_mine_badge_campaigns()
        miner._TwitchChannelPointsMiner__auto_mine_badge_campaigns()

    warnings = [
        record
        for record in caplog.records
        if record.levelno >= logging.WARNING
        and "unexpected inventory failure" in record.getMessage()
    ]
    assert len(warnings) == 1
