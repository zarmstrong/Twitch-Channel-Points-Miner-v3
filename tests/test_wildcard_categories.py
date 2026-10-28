import logging
import threading
from datetime import datetime
from types import SimpleNamespace

import pytest

from TwitchChannelPointsMiner.TwitchChannelPointsMiner import TwitchChannelPointsMiner
from TwitchChannelPointsMiner.classes.Chat import ChatPresence
from TwitchChannelPointsMiner.classes.Exceptions import CategoryLookupFailedException
from TwitchChannelPointsMiner.classes.Settings import Settings
from TwitchChannelPointsMiner.classes.Twitch import Twitch, _drops_dashboard_state
from TwitchChannelPointsMiner.classes.entities.Streamer import (
    Streamer,
    StreamerSettings,
)


def _bare_twitch(monkeypatch, deadlines, requested_slugs_seen=None):
    """A Twitch instance with the campaign-fetch internals stubbed out so
    get_wildcard_categories_with_active_drops's own exclude/limit/pin/sort
    logic can be exercised directly, independent of campaign JSON parsing
    (already covered for the shared helper by test_twitch_drop_claim.py).
    """
    twitch = object.__new__(Twitch)
    twitch.campaign_lookup_unavailable = False
    twitch.unevaluated_category_slugs = set()
    twitch.category_campaign_deadlines = {}
    twitch.category_log_level = logging.DEBUG
    twitch.twitchdrops_app_deadlines = {}
    twitch.twitchdrops_app_catalog_complete = True

    def fake_active_drop_category_slugs_from_campaigns(
        self, inventory, requested_category_slugs
    ):
        if requested_slugs_seen is not None:
            requested_slugs_seen.append(requested_category_slugs)
        return dict(deadlines), set(deadlines)

    monkeypatch.setattr(
        Twitch, "_Twitch__get_inventory", lambda self: {"present": True}
    )
    monkeypatch.setattr(
        Twitch,
        "_Twitch__active_drop_category_slugs_from_campaigns",
        fake_active_drop_category_slugs_from_campaigns,
    )
    monkeypatch.setattr(
        Twitch,
        "_Twitch__twitchdrops_app_fallback",
        lambda self, categories, known_slugs, inventory=None: {},
    )
    return twitch


def test_get_category_slugs_treats_none_as_empty():
    twitch = object.__new__(Twitch)
    twitch.campaign_lookup_unavailable = False
    twitch.unevaluated_category_slugs = set()

    assert twitch.get_category_slugs(None) == set()


def test_get_wildcard_categories_returns_full_set_sorted_by_expiration(monkeypatch):
    deadlines = {
        "slow-game": datetime(2099, 1, 1),
        "urgent-game": datetime(2020, 1, 1),
        "mid-game": datetime(2050, 1, 1),
    }
    twitch = _bare_twitch(monkeypatch, deadlines)

    result = twitch.get_wildcard_categories_with_active_drops()

    assert result == ["urgent-game", "mid-game", "slow-game"]


def test_get_wildcard_categories_unfiltered_request_is_passed_through(monkeypatch):
    seen = []
    twitch = _bare_twitch(monkeypatch, {"game": datetime(2099, 1, 1)}, seen)

    twitch.get_wildcard_categories_with_active_drops()

    assert seen == [None]


def test_preferred_filter_keeps_external_catalog_scoped(monkeypatch):
    twitch = _bare_twitch(monkeypatch, {"preferred-game": datetime(2099, 1, 1)})
    fallback_requests = []

    def fallback(self, categories, known_slugs, inventory=None):
        fallback_requests.append(categories)
        return {}

    monkeypatch.setattr(Twitch, "_Twitch__twitchdrops_app_fallback", fallback)

    result = twitch.filter_categories_with_active_drops(
        ["preferred-game"], inventory={"present": True}
    )

    assert result == ["preferred-game"]
    assert fallback_requests == [["preferred-game"]]


def test_get_wildcard_categories_excludes_configured_category_slugs(monkeypatch):
    deadlines = {
        "excluded-game": datetime(2020, 1, 1),
        "other-game": datetime(2050, 1, 1),
    }
    twitch = _bare_twitch(monkeypatch, deadlines)

    result = twitch.get_wildcard_categories_with_active_drops(
        exclude_category_slugs={"excluded-game"}
    )

    assert result == ["other-game"]


def test_get_wildcard_categories_drops_enabled_false_returns_empty(monkeypatch):
    twitch = _bare_twitch(monkeypatch, {"game": datetime(2099, 1, 1)})

    assert twitch.get_wildcard_categories_with_active_drops(drops_enabled=False) == []


def test_get_wildcard_categories_limit_throttles_new_additions(monkeypatch):
    deadlines = {
        "first": datetime(2020, 1, 1),
        "second": datetime(2021, 1, 1),
        "third": datetime(2022, 1, 1),
        "fourth": datetime(2023, 1, 1),
    }
    twitch = _bare_twitch(monkeypatch, deadlines)

    result = twitch.get_wildcard_categories_with_active_drops(limit=2, pin_active=False)

    assert result == ["first", "second"]


def test_get_wildcard_categories_pin_keeps_tracked_slug_past_top_n(monkeypatch):
    # "pinned" would fall outside the top 2 soonest-expiring slugs on a
    # straight re-sort, but pinning must keep it because it's still eligible.
    # Room for one new addition remains (limit=2, one pinned slug), and
    # "first" is the soonest-expiring of the unpinned candidates.
    deadlines = {
        "first": datetime(2020, 1, 1),
        "second": datetime(2021, 1, 1),
        "pinned": datetime(2099, 1, 1),
    }
    twitch = _bare_twitch(monkeypatch, deadlines)

    result = twitch.get_wildcard_categories_with_active_drops(
        limit=2, pinned_category_slugs={"pinned"}, pin_active=True
    )

    assert set(result) == {"pinned", "first"}


def test_get_wildcard_categories_pin_can_exceed_limit(monkeypatch):
    # Three already-tracked (pinned) categories against a limit of 2: pinning
    # only throttles *new* additions, so the returned count intentionally
    # exceeds `limit` here rather than evicting a pinned category.
    deadlines = {
        "pin-a": datetime(2030, 1, 1),
        "pin-b": datetime(2040, 1, 1),
        "pin-c": datetime(2050, 1, 1),
        "brand-new": datetime(2020, 1, 1),
    }
    twitch = _bare_twitch(monkeypatch, deadlines)

    result = twitch.get_wildcard_categories_with_active_drops(
        limit=2,
        pinned_category_slugs={"pin-a", "pin-b", "pin-c"},
        pin_active=True,
    )

    assert set(result) == {"pin-a", "pin-b", "pin-c"}
    assert len(result) > 2


def test_get_wildcard_categories_pin_inactive_drops_slug_outside_top_n(monkeypatch):
    deadlines = {
        "first": datetime(2020, 1, 1),
        "second": datetime(2021, 1, 1),
        "would-be-pinned": datetime(2099, 1, 1),
    }
    twitch = _bare_twitch(monkeypatch, deadlines)

    result = twitch.get_wildcard_categories_with_active_drops(
        limit=2, pinned_category_slugs={"would-be-pinned"}, pin_active=False
    )

    assert result == ["first", "second"]


def test_get_wildcard_categories_pinned_slug_no_longer_eligible_is_dropped(monkeypatch):
    # The pinned category's campaign genuinely closed -- it's no longer in
    # the eligible set at all, so pinning cannot resurrect it.
    deadlines = {"still-open": datetime(2020, 1, 1)}
    twitch = _bare_twitch(monkeypatch, deadlines)

    result = twitch.get_wildcard_categories_with_active_drops(
        pinned_category_slugs={"closed-campaign"}, pin_active=True
    )

    assert result == ["still-open"]


def test_get_wildcard_categories_replaces_rather_than_merges_deadlines(monkeypatch):
    # The unfiltered fetch is always a superset of what a same-cycle preferred
    # pass could have found (wildcard only triggers once that pass found
    # nothing), so it must replace category_campaign_deadlines rather than
    # merge into it -- a merge would let a closed campaign's stale deadline
    # linger forever whenever `categories` is empty and this is the only
    # per-cycle source.
    twitch = _bare_twitch(monkeypatch, {"still-open": datetime(2099, 1, 1)})
    twitch.category_campaign_deadlines = {"closed-campaign": datetime(2020, 1, 1)}

    twitch.get_wildcard_categories_with_active_drops()

    assert twitch.category_campaign_deadlines == {"still-open": datetime(2099, 1, 1)}


def test_wildcard_ranks_external_missing_campaign_ahead_of_later_twitch_campaigns(
    monkeypatch,
):
    twitch = _bare_twitch(
        monkeypatch,
        {
            "dead-by-daylight": datetime(2099, 1, 1),
            "special-events": datetime(2099, 2, 1),
        },
    )
    twitch.twitchdrops_app_catalog_complete = False
    monkeypatch.setattr(
        Twitch,
        "_Twitch__twitchdrops_app_fallback",
        lambda self, categories, known_slugs, inventory=None: {
            "urgent-external-game": datetime(2050, 1, 1)
        },
    )

    result = twitch.get_wildcard_categories_with_active_drops()

    assert result == [
        "urgent-external-game",
        "dead-by-daylight",
        "special-events",
    ]
    assert twitch.category_campaign_deadlines == {
        "urgent-external-game": datetime(2050, 1, 1),
        "dead-by-daylight": datetime(2099, 1, 1),
        "special-events": datetime(2099, 2, 1),
    }


def test_wildcard_does_not_override_twitch_authoritative_deadline(monkeypatch):
    twitch = _bare_twitch(monkeypatch, {"dead-by-daylight": datetime(2099, 1, 1)})
    twitch.twitchdrops_app_catalog_complete = False
    monkeypatch.setattr(
        Twitch,
        "_Twitch__twitchdrops_app_fallback",
        lambda self, categories, known_slugs, inventory=None: {
            "dead-by-daylight": datetime(2020, 1, 1),
            "external-game": datetime(2050, 1, 1),
        },
    )

    result = twitch.get_wildcard_categories_with_active_drops()

    assert result == ["external-game", "dead-by-daylight"]
    assert twitch.category_campaign_deadlines["dead-by-daylight"] == datetime(
        2099, 1, 1
    )


def test_wildcard_external_addition_survives_fallback_mutating_known_slugs(
    monkeypatch,
):
    # Regression test: the real __twitchdrops_app_fallback records every
    # front-page match (not just Twitch-authoritative ones) by mutating the
    # `known_category_slugs` set it's given, in place -- it does this for its
    # own internal "skip if already Twitch-authoritative" bookkeeping. The
    # wildcard pass must diff fallback_deadlines against a snapshot taken
    # *before* that call, not the (now mutated) live set, or every external
    # addition the fallback just found would appear to already be a "Twitch"
    # slug and get silently dropped -- exactly what happened in production
    # (see the coffeedicee bug report: "0 external additions" forever, even
    # with dozens of genuinely open external campaigns logged moments
    # earlier).
    twitch = _bare_twitch(monkeypatch, {"dead-by-daylight": datetime(2099, 1, 1)})
    twitch.twitchdrops_app_catalog_complete = False

    def mutating_fallback(self, categories, known_slugs, inventory=None):
        known_slugs.add("urgent-external-game")
        return {"urgent-external-game": datetime(2050, 1, 1)}

    monkeypatch.setattr(Twitch, "_Twitch__twitchdrops_app_fallback", mutating_fallback)

    result = twitch.get_wildcard_categories_with_active_drops()

    assert result == ["urgent-external-game", "dead-by-daylight"]


def test_wildcard_reuses_preloaded_external_catalog(monkeypatch):
    twitch = _bare_twitch(monkeypatch, {"dead-by-daylight": datetime(2099, 1, 1)})
    twitch.twitchdrops_app_deadlines = {"urgent-external-game": datetime(2050, 1, 1)}
    monkeypatch.setattr(
        Twitch,
        "_Twitch__twitchdrops_app_fallback",
        lambda self, categories, known_slugs, inventory=None: pytest.fail(
            "the preloaded full catalog should be reused"
        ),
    )

    result = twitch.get_wildcard_categories_with_active_drops(
        refresh_external_catalog=False
    )

    assert result == ["urgent-external-game", "dead-by-daylight"]


class FakeWebSocketsPool:
    def __init__(self):
        self.topics = []
        self.removed = []

    def submit(self, topic):
        self.topics.append(topic)

    def remove_streamer_topics(self, streamer):
        self.removed.append(streamer.username)


class FakeIrcChat:
    def __init__(self):
        self.stopped = False
        self.joined = False

    def is_alive(self):
        return True

    def stop(self):
        self.stopped = True

    def join(self, timeout=None):
        self.joined = True


class FakeTwitch:
    def __init__(self, eligible_categories, wildcard_categories, empty_categories=()):
        self.eligible_categories = eligible_categories
        self.wildcard_categories = wildcard_categories
        # Categories that are eligible (an active campaign exists) but have
        # zero live matching channels right now -- lets tests reproduce "a
        # preferred category is technically eligible but produced no
        # streamer this cycle" without needing real campaign/stream data.
        self.empty_categories = set(empty_categories)
        self.wildcard_calls = []
        self.filter_calls = []
        self.inventory_fetch_count = 0
        self.selectors = []
        self.twitch_login = SimpleNamespace(get_auth_token=lambda: "token")
        self.discovered_open_drop_campaigns = None
        self.unevaluated_category_slugs = set()

    def get_drops_inventory(self):
        self.inventory_fetch_count += 1
        return {"fetched": self.inventory_fetch_count}

    def filter_categories_with_active_drops(self, categories, **kwargs):
        self.filter_calls.append(kwargs)
        return self.eligible_categories

    def get_category_slugs(self, categories):
        return {category for category in categories}

    def get_game_name_slug(self, game_name):
        return (game_name or "").lower().replace(" ", "-")

    def get_wildcard_categories_with_active_drops(self, **kwargs):
        self.wildcard_calls.append(kwargs)
        return self.wildcard_categories

    def get_live_streamers_for_category(self, selector, **kwargs):
        self.selectors.append((selector, kwargs))
        if selector in self.empty_categories:
            return []
        return [f"{selector}-streamer"]

    def get_channel_id(self, username):
        return f"id-{username}"

    def load_channel_points_context(self, streamer):
        return None

    def check_streamer_online(self, streamer):
        streamer.is_online = True


DISCOVERY_OUTAGE_EXPIRED = (
    "category discovery (drops inventory or campaign lookup) failed 3 refreshes "
    "in a row"
)


def _bare_miner(twitch):
    defaults = StreamerSettings(chat=ChatPresence.NEVER)
    defaults.default()
    defaults.bet.default()
    Settings.streamer_settings = defaults

    miner = TwitchChannelPointsMiner.__new__(TwitchChannelPointsMiner)

    miner.category_lookup_failure_streaks = {}
    miner.username = "testuser"
    miner.twitch = twitch
    miner.streamers = []
    miner.original_streamers = []
    miner.ws_pool = FakeWebSocketsPool()
    miner.config_reload_lock = threading.Lock()
    miner.sync_campaigns_thread = object()
    return miner


def test_wildcard_discovery_only_runs_when_preferred_pass_finds_a_streamer():
    # The preferred pass here has an eligible category AND finds a live
    # streamer for it -- capacity isn't idle, so wildcard must stand down.
    twitch = FakeTwitch(eligible_categories=["preferred-game"], wildcard_categories=[])
    miner = _bare_miner(twitch)

    miner._TwitchChannelPointsMiner__refresh_category_streamers(
        ["preferred-game"],
        [],
        True,
        2,
        "VIEWERS_DESC",
        "ORDER",
        ChatPresence.NEVER,
        logging.INFO,
        wildcard_categories=True,
        wildcard_category_limit=10,
        wildcard_category_streamer_limit=1,
        wildcard_category_pin_active=True,
    )

    assert twitch.wildcard_calls == []
    assert [streamer.username for streamer in miner.streamers] == [
        "preferred-game-streamer"
    ]
    assert all(streamer.from_wildcard_category is False for streamer in miner.streamers)


def test_wildcard_discovery_runs_when_eligible_category_has_no_live_streamer():
    # Reproduces the real-world case: a preferred category has an active
    # campaign (so eligible_categories is non-empty) but zero live matching
    # channels this cycle -- capacity is genuinely idle, so wildcard must
    # still trigger even though no preferred category was "exhausted" in the
    # old (campaign-eligibility-only) sense.
    twitch = FakeTwitch(
        eligible_categories=["pokemon-go"],
        wildcard_categories=["some-other-game"],
        empty_categories={"pokemon-go"},
    )
    miner = _bare_miner(twitch)

    miner._TwitchChannelPointsMiner__refresh_category_streamers(
        ["pokemon-go"],
        [],
        True,
        2,
        "VIEWERS_DESC",
        "ORDER",
        ChatPresence.NEVER,
        logging.INFO,
        wildcard_categories=True,
        wildcard_category_limit=10,
        wildcard_category_streamer_limit=1,
        wildcard_category_pin_active=True,
    )

    assert len(twitch.wildcard_calls) == 1
    assert [streamer.username for streamer in miner.streamers] == [
        "some-other-game-streamer"
    ]
    assert miner.streamers[0].from_wildcard_category is True


def test_standdown_does_not_retire_existing_wildcard_streamer_in_unrelated_game():
    # Regression test for the review-identified bug: a preferred category
    # (gameA) becoming eligible must not retire a wildcard streamer tracked
    # in a completely unrelated game (gameX) whose own campaign was never
    # re-evaluated this cycle.
    twitch = FakeTwitch(eligible_categories=["gameA"], wildcard_categories=[])
    miner = _bare_miner(twitch)
    gamex = Streamer("gamex-streamer", from_category=True, from_wildcard_category=True)
    gamex.is_online = True
    gamex.stream.game = {"name": "GameX", "displayName": "GameX"}
    gamex.irc_chat = FakeIrcChat()
    miner.streamers = [gamex]
    miner.original_streamers = [10]

    miner._TwitchChannelPointsMiner__refresh_category_streamers(
        ["gameA", "gameB"],
        [],
        True,
        2,
        "VIEWERS_DESC",
        "ORDER",
        ChatPresence.NEVER,
        logging.INFO,
        wildcard_categories=True,
        wildcard_category_limit=10,
        wildcard_category_streamer_limit=1,
        wildcard_category_pin_active=True,
    )

    # Discovery correctly stood down (the traffic safeguard, unchanged)...
    assert twitch.wildcard_calls == []
    # ...but the previously-tracked wildcard streamer must survive untouched
    # (gameA legitimately gained its own preferred-category streamer too --
    # that's expected and irrelevant to gameX's fate).
    assert gamex in miner.streamers
    assert gamex.from_wildcard_category is True
    assert gamex.from_category is True
    assert "gamex-streamer" not in miner.ws_pool.removed
    assert gamex.irc_chat.stopped is False


def test_real_wildcard_discovery_still_retires_genuinely_closed_streamer():
    # Regression guard: confirm the stand-down fix didn't disable legitimate
    # cleanup. This cycle preferred categories are genuinely exhausted, a
    # real wildcard discovery pass runs, and it no longer includes gameX at
    # all -- gameX must still be retired in this case.
    twitch = FakeTwitch(eligible_categories=[], wildcard_categories=["still-open-game"])
    miner = _bare_miner(twitch)
    gamex = Streamer("gamex-streamer", from_category=True, from_wildcard_category=True)
    gamex.is_online = True
    gamex.stream.game = {"name": "GameX", "displayName": "GameX"}
    gamex.irc_chat = FakeIrcChat()
    miner.streamers = [gamex]
    miner.original_streamers = [10]

    miner._TwitchChannelPointsMiner__refresh_category_streamers(
        ["gameA"],
        [],
        True,
        2,
        "VIEWERS_DESC",
        "ORDER",
        ChatPresence.NEVER,
        logging.INFO,
        wildcard_categories=True,
        wildcard_category_limit=10,
        wildcard_category_streamer_limit=1,
        wildcard_category_pin_active=True,
    )

    assert len(twitch.wildcard_calls) == 1
    assert "gamex-streamer" not in [streamer.username for streamer in miner.streamers]
    assert miner.ws_pool.removed == ["gamex-streamer"]
    assert gamex.irc_chat.stopped is True


def test_wildcard_streamer_survives_transient_standdown_then_resumes():
    # Full scenario from the review narrative: (a) wildcard discovers and
    # tracks gameX while preferred categories are idle; (b) a preferred
    # category becomes eligible for one cycle -- gameX must survive
    # untouched, not rediscovered; (c) the preferred category closes again
    # -- wildcard discovery resumes and gameX is still the same tracked
    # instance, confirming it was never actually dropped in step (b).
    twitch = FakeTwitch(eligible_categories=[], wildcard_categories=["gamex"])
    miner = _bare_miner(twitch)
    refresh_kwargs = dict(
        wildcard_categories=True,
        wildcard_category_limit=10,
        wildcard_category_streamer_limit=1,
        wildcard_category_pin_active=True,
    )

    # (a) idle preferred categories -> wildcard discovers and tracks gameX.
    miner._TwitchChannelPointsMiner__refresh_category_streamers(
        ["gameA"],
        [],
        True,
        2,
        "VIEWERS_DESC",
        "ORDER",
        ChatPresence.NEVER,
        logging.INFO,
        **refresh_kwargs,
    )
    assert [streamer.username for streamer in miner.streamers] == ["gamex-streamer"]
    tracked = miner.streamers[0]
    assert tracked.from_wildcard_category is True

    # (b) gameA becomes eligible for one cycle -> wildcard stands down, but
    # gameX must not be retired.
    twitch.eligible_categories = ["gameA"]
    twitch.wildcard_calls = []
    miner._TwitchChannelPointsMiner__refresh_category_streamers(
        ["gameA"],
        [],
        True,
        2,
        "VIEWERS_DESC",
        "ORDER",
        ChatPresence.NEVER,
        logging.INFO,
        **refresh_kwargs,
    )
    # gameA being eligible legitimately adds its own preferred streamer, but
    # gameX (still the same tracked instance) must survive alongside it,
    # untouched, rather than being retired or replaced.
    assert twitch.wildcard_calls == []
    assert tracked in miner.streamers
    assert tracked.from_wildcard_category is True
    assert "gamex-streamer" not in miner.ws_pool.removed

    # (c) gameA closes again -> wildcard discovery resumes and finds gameX
    # still tracked, without having rediscovered it from scratch.
    twitch.eligible_categories = []
    miner._TwitchChannelPointsMiner__refresh_category_streamers(
        ["gameA"],
        [],
        True,
        2,
        "VIEWERS_DESC",
        "ORDER",
        ChatPresence.NEVER,
        logging.INFO,
        **refresh_kwargs,
    )
    assert len(twitch.wildcard_calls) == 1
    assert tracked in miner.streamers
    assert "gamex-streamer" not in miner.ws_pool.removed


def test_wildcard_discovery_runs_and_tags_streamers_once_preferred_is_exhausted():
    twitch = FakeTwitch(eligible_categories=[], wildcard_categories=["wild-game"])
    miner = _bare_miner(twitch)

    miner._TwitchChannelPointsMiner__refresh_category_streamers(
        ["preferred-game"],
        [],
        True,
        2,
        "VIEWERS_DESC",
        "ORDER",
        ChatPresence.NEVER,
        logging.INFO,
        wildcard_categories=True,
        wildcard_category_limit=5,
        wildcard_category_streamer_limit=1,
        wildcard_category_pin_active=True,
    )

    assert len(twitch.wildcard_calls) == 1
    assert twitch.wildcard_calls[0]["limit"] == 5
    assert twitch.wildcard_calls[0]["exclude_category_slugs"] == {"preferred-game"}
    assert [streamer.username for streamer in miner.streamers] == ["wild-game-streamer"]
    assert miner.streamers[0].from_category is True
    assert miner.streamers[0].from_wildcard_category is True


def test_refresh_fetches_drops_inventory_once_and_shares_it_with_wildcard_pass():
    # Both filter_categories_with_active_drops and
    # get_wildcard_categories_with_active_drops need the drops inventory;
    # the miner should fetch it once per refresh and pass the same object to
    # both instead of each fetching it independently over GraphQL.
    twitch = FakeTwitch(eligible_categories=[], wildcard_categories=["wild-game"])
    miner = _bare_miner(twitch)

    miner._TwitchChannelPointsMiner__refresh_category_streamers(
        ["preferred-game"],
        [],
        True,
        2,
        "VIEWERS_DESC",
        "ORDER",
        ChatPresence.NEVER,
        logging.INFO,
        wildcard_categories=True,
    )

    assert twitch.inventory_fetch_count == 1
    assert twitch.filter_calls[0]["inventory"] == {"fetched": 1}
    assert "include_all_fallback" not in twitch.filter_calls[0]
    assert twitch.wildcard_calls[0]["inventory"] == {"fetched": 1}
    assert twitch.wildcard_calls[0]["refresh_external_catalog"] is True


def test_hot_reload_updates_periodic_wildcard_settings():
    twitch = FakeTwitch(eligible_categories=[], wildcard_categories=[])
    miner = _bare_miner(twitch)

    miner.refresh_categories(
        {
            "categories": [],
            "wildcard_categories": True,
            "wildcard_category_limit": 4,
            "wildcard_category_streamer_limit": 2,
            "wildcard_category_pin_active": False,
        }
    )

    assert miner.wildcard_categories is True
    assert miner.wildcard_category_limit == 4
    assert miner.wildcard_category_streamer_limit == 2
    assert miner.wildcard_category_pin_active is False
    assert twitch.wildcard_calls[0]["limit"] == 4
    assert twitch.wildcard_calls[0]["pin_active"] is False


def test_hot_reload_normalizes_invalid_wildcard_limits():
    twitch = FakeTwitch(eligible_categories=[], wildcard_categories=[])
    miner = _bare_miner(twitch)

    miner.refresh_categories(
        {
            "categories": [],
            "wildcard_categories": True,
            "wildcard_category_limit": None,
            "wildcard_category_streamer_limit": 0,
        }
    )

    assert miner.wildcard_category_limit == 10
    assert miner.wildcard_category_streamer_limit == 1


def test_refresh_skips_inventory_fetch_when_drops_disabled():
    twitch = FakeTwitch(eligible_categories=[], wildcard_categories=[])
    miner = _bare_miner(twitch)

    miner._TwitchChannelPointsMiner__refresh_category_streamers(
        ["preferred-game"],
        [],
        False,
        2,
        "VIEWERS_DESC",
        "ORDER",
        ChatPresence.NEVER,
        logging.INFO,
        wildcard_categories=True,
    )

    assert twitch.inventory_fetch_count == 0


def test_wildcard_disabled_retires_previously_discovered_wildcard_streamers():
    twitch = FakeTwitch(eligible_categories=["preferred-game"], wildcard_categories=[])
    miner = _bare_miner(twitch)
    stale_wildcard = Streamer(
        "stale-wild", from_category=True, from_wildcard_category=True
    )
    miner.streamers = [stale_wildcard]
    miner.original_streamers = [10]

    miner._TwitchChannelPointsMiner__refresh_category_streamers(
        ["preferred-game"],
        [],
        True,
        2,
        "VIEWERS_DESC",
        "ORDER",
        ChatPresence.NEVER,
        logging.INFO,
        wildcard_categories=False,
    )

    assert "stale-wild" not in [s.username for s in miner.streamers]
    assert "stale-wild" in miner.ws_pool.removed


def test_reconcile_scoped_to_wildcard_does_not_evict_preferred_streamers():
    miner = TwitchChannelPointsMiner.__new__(TwitchChannelPointsMiner)
    miner.category_lookup_failure_streaks = {}
    preferred = Streamer("preferred", from_category=True)
    wildcard = Streamer("wild", from_category=True, from_wildcard_category=True)
    miner.streamers = [preferred, wildcard]
    miner.original_streamers = [10, 20]
    miner.ws_pool = FakeWebSocketsPool()

    # A wildcard-scoped reconcile with an empty discovery list must only
    # retire the wildcard streamer, not the preferred-category one.
    miner._TwitchChannelPointsMiner__reconcile_category_streamers([], wildcard=True)

    assert [s.username for s in miner.streamers] == ["preferred"]
    assert miner.ws_pool.removed == ["wild"]


def test_reconcile_never_exposes_retiring_streamers_as_configured():
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
    first = Streamer("first-retired", from_category=True)
    second = Streamer("second-retired", from_category=True)
    configured = Streamer("configured", from_category=True)
    configured.explicitly_configured = True
    miner.streamers = [first, second, configured]
    miner.original_streamers = [10, 20, 30]
    miner.ws_pool = SnapshottingPool(miner)

    miner._TwitchChannelPointsMiner__reconcile_category_streamers([], wildcard=False)

    # While each streamer was being retired, none of the not-yet-removed
    # category-only streamers fell back to the configured STREAMERS tier.
    assert miner.ws_pool.snapshots
    for snapshot in miner.ws_pool.snapshots:
        for username in ("first-retired", "second-retired"):
            assert StreamerSource.STREAMERS not in snapshot[username]
    assert [s.username for s in miner.streamers] == ["configured"]
    assert configured.from_category is False


def test_reconcile_scoped_to_preferred_does_not_evict_wildcard_streamers():
    miner = TwitchChannelPointsMiner.__new__(TwitchChannelPointsMiner)
    miner.category_lookup_failure_streaks = {}
    preferred = Streamer("preferred", from_category=True)
    wildcard = Streamer("wild", from_category=True, from_wildcard_category=True)
    miner.streamers = [preferred, wildcard]
    miner.original_streamers = [10, 20]
    miner.ws_pool = FakeWebSocketsPool()

    miner._TwitchChannelPointsMiner__reconcile_category_streamers([], wildcard=False)

    assert [s.username for s in miner.streamers] == ["wild"]
    assert miner.ws_pool.removed == ["preferred"]


class InventoryFailureTwitch(FakeTwitch):
    """Mirrors Twitch.__get_inventory's `{}` on a transient GQL failure: both
    discovery filters then skip and report no eligible categories."""

    def get_drops_inventory(self):
        self.inventory_fetch_count += 1
        return {}

    def filter_categories_with_active_drops(self, categories, **kwargs):
        self.filter_calls.append(kwargs)
        return []

    def get_wildcard_categories_with_active_drops(self, **kwargs):
        self.wildcard_calls.append(kwargs)
        return []


def _refresh(miner, categories, wildcard_categories=False, count_failures=True):
    miner._TwitchChannelPointsMiner__refresh_category_streamers(
        categories,
        [],
        True,
        2,
        "VIEWERS_DESC",
        "ORDER",
        ChatPresence.NEVER,
        logging.INFO,
        wildcard_categories=wildcard_categories,
        wildcard_category_limit=10,
        wildcard_category_streamer_limit=1,
        wildcard_category_pin_active=True,
        count_failures=count_failures,
    )


def _tracked(username, wildcard=False):
    streamer = Streamer(username, from_category=True, from_wildcard_category=wildcard)
    streamer.irc_chat = FakeIrcChat()
    return streamer


def test_failed_inventory_load_keeps_preferred_and_wildcard_streamers():
    # Issue #124: a transient Inventory failure made discovery come back
    # empty and the reconcile pass retired every live category streamer.
    twitch = InventoryFailureTwitch(eligible_categories=[], wildcard_categories=[])
    miner = _bare_miner(twitch)
    preferred = _tracked("ow_esports")
    wildcard = _tracked("wild", wildcard=True)
    miner.streamers = [preferred, wildcard]
    miner.original_streamers = [10, 20]

    _refresh(miner, ["overwatch-2"], wildcard_categories=True)

    assert miner.streamers == [preferred, wildcard]
    assert miner.original_streamers == [10, 20]
    assert miner.ws_pool.removed == []
    assert preferred.irc_chat.stopped is False
    assert wildcard.irc_chat.stopped is False
    assert wildcard.from_wildcard_category is True
    # Wildcard discovery can't succeed without the inventory either.
    assert twitch.wildcard_calls == []


def test_inventory_that_never_loads_retires_streamers_after_the_limit(caplog):
    # An expired or revoked token breaks the inventory along with every other
    # lookup. Kept forever, the tracked streamers would never be retired, so
    # the inventory outage is bounded like the campaign and channel lookups.
    twitch = InventoryFailureTwitch(eligible_categories=[], wildcard_categories=[])
    miner = _bare_miner(twitch)
    preferred = _tracked("ow_esports")
    wildcard = _tracked("wild", wildcard=True)
    miner.streamers = [preferred, wildcard]
    miner.original_streamers = [10, 20]

    for _ in range(2):
        _refresh(miner, ["overwatch-2"], wildcard_categories=True)
    assert miner.streamers == [preferred, wildcard]
    assert twitch.wildcard_calls == []

    with caplog.at_level(logging.WARNING):
        _refresh(miner, ["overwatch-2"], wildcard_categories=True)

    assert miner.streamers == []
    assert miner.original_streamers == []
    assert miner.ws_pool.removed == ["ow_esports", "wild"]
    assert DISCOVERY_OUTAGE_EXPIRED in caplog.text


class AlternatingOutageTwitch(FakeTwitch):
    """The drops inventory and the campaign lookup fail in turn, one per
    refresh, so each cause alone is never three refreshes in a row."""

    inventory_down = True

    def get_drops_inventory(self):
        if self.inventory_down:
            self.inventory_fetch_count += 1
            return {}
        return super().get_drops_inventory()

    def filter_categories_with_active_drops(self, categories, **kwargs):
        self.filter_calls.append(kwargs)
        self.unevaluated_category_slugs = set()
        if self.inventory_down:
            return []
        raise CategoryLookupFailedException("drop campaign lookup failed")

    def get_wildcard_categories_with_active_drops(self, **kwargs):
        self.wildcard_calls.append(kwargs)
        return []


def test_alternating_inventory_and_campaign_outages_share_the_limit(caplog):
    twitch = AlternatingOutageTwitch(eligible_categories=[], wildcard_categories=[])
    miner = _bare_miner(twitch)
    preferred = _tracked("ow_esports")
    wildcard = _tracked("wild", wildcard=True)
    miner.streamers = [preferred, wildcard]
    miner.original_streamers = [10, 20]

    for inventory_down in (True, False):
        twitch.inventory_down = inventory_down
        _refresh(miner, ["overwatch-2"], wildcard_categories=True)
    assert miner.streamers == [preferred, wildcard]
    assert twitch.wildcard_calls == []

    # The third unavailable refresh in a row, whichever cause it has.
    twitch.inventory_down = True
    with caplog.at_level(logging.WARNING):
        _refresh(miner, ["overwatch-2"], wildcard_categories=True)

    assert miner.streamers == []
    assert miner.original_streamers == []
    assert miner.ws_pool.removed == ["ow_esports", "wild"]
    assert len(twitch.wildcard_calls) == 1
    assert DISCOVERY_OUTAGE_EXPIRED in caplog.text


def test_config_change_refreshes_do_not_use_up_the_outage_limit():
    # Each config edit refreshes categories at once; during an outage, a few
    # quick edits must not retire every tracked streamer within minutes.
    twitch = CampaignLookupFailureTwitch(eligible_categories=[], wildcard_categories=[])
    miner = _bare_miner(twitch)
    preferred = _tracked("ow_esports")
    miner.streamers = [preferred]
    miner.original_streamers = [10]

    _refresh(miner, ["overwatch-2"])
    for _ in range(3):
        miner.refresh_categories({"categories": ["overwatch-2"], "category_limit": 2})
    _refresh(miner, ["overwatch-2"])
    assert len(twitch.filter_calls) == 5
    assert miner.streamers == [preferred]
    assert miner.ws_pool.removed == []

    _refresh(miner, ["overwatch-2"])
    assert miner.streamers == []
    assert miner.ws_pool.removed == ["ow_esports"]


def test_config_change_refresh_still_resets_the_outage_limit_on_success():
    class RecoveringTwitch(CampaignLookupFailureTwitch):
        down = True

        def filter_categories_with_active_drops(self, categories, **kwargs):
            if self.down:
                return super().filter_categories_with_active_drops(categories, **kwargs)
            return FakeTwitch.filter_categories_with_active_drops(
                self, categories, **kwargs
            )

    twitch = RecoveringTwitch(
        eligible_categories=["overwatch-2"], wildcard_categories=[]
    )
    miner = _bare_miner(twitch)
    preferred = _tracked("overwatch-2-streamer")
    miner.streamers = [preferred]
    miner.original_streamers = [10]

    for _ in range(2):
        _refresh(miner, ["overwatch-2"])
    twitch.down = False
    miner.refresh_categories({"categories": ["overwatch-2"], "category_limit": 2})
    twitch.down = True
    for _ in range(2):
        _refresh(miner, ["overwatch-2"])

    assert miner.streamers == [preferred]
    assert miner.ws_pool.removed == []


def test_inventory_recovery_resets_its_failure_limit():
    class RecoveringInventoryTwitch(InventoryFailureTwitch):
        unavailable = True

        def get_drops_inventory(self):
            if self.unavailable:
                return super().get_drops_inventory()
            return FakeTwitch.get_drops_inventory(self)

        def filter_categories_with_active_drops(self, categories, **kwargs):
            if self.unavailable:
                return super().filter_categories_with_active_drops(categories, **kwargs)
            return FakeTwitch.filter_categories_with_active_drops(
                self, categories, **kwargs
            )

    twitch = RecoveringInventoryTwitch(
        eligible_categories=["overwatch-2"], wildcard_categories=[]
    )
    miner = _bare_miner(twitch)
    preferred = _tracked("overwatch-2-streamer")
    miner.streamers = [preferred]
    miner.original_streamers = [10]

    for _ in range(2):
        _refresh(miner, ["overwatch-2"])
    twitch.unavailable = False
    _refresh(miner, ["overwatch-2"])
    twitch.unavailable = True
    for _ in range(2):
        _refresh(miner, ["overwatch-2"])

    # Two failures, a success, two more failures: never three in a row.
    assert miner.streamers == [preferred]
    assert miner.ws_pool.removed == []


def test_failed_inventory_load_still_retires_streamers_of_disabled_categories():
    # Turning `categories` off still means off, even while the inventory is
    # unavailable for the wildcard pass.
    twitch = InventoryFailureTwitch(eligible_categories=[], wildcard_categories=[])
    miner = _bare_miner(twitch)
    preferred = _tracked("ow_esports")
    wildcard = _tracked("wild", wildcard=True)
    miner.streamers = [preferred, wildcard]
    miner.original_streamers = [10, 20]

    _refresh(miner, [], wildcard_categories=True)

    assert miner.streamers == [wildcard]
    assert miner.ws_pool.removed == ["ow_esports"]


def test_successful_empty_discovery_still_retires_stale_streamers():
    twitch = FakeTwitch(eligible_categories=[], wildcard_categories=[])
    miner = _bare_miner(twitch)
    miner.streamers = [_tracked("ow_esports")]
    miner.original_streamers = [10]

    _refresh(miner, ["overwatch-2"])

    assert miner.streamers == []
    assert miner.ws_pool.removed == ["ow_esports"]


def test_failed_inventory_load_still_retires_wildcard_streamers_when_disabled():
    # Same "off means off" rule for wildcard_categories: turning it off
    # retires wildcard streamers even while the inventory is unavailable,
    # while preferred streamers are kept.
    twitch = InventoryFailureTwitch(eligible_categories=[], wildcard_categories=[])
    miner = _bare_miner(twitch)
    preferred = _tracked("ow_esports")
    wildcard = _tracked("wild", wildcard=True)
    miner.streamers = [preferred, wildcard]
    miner.original_streamers = [10, 20]

    _refresh(miner, ["overwatch-2"], wildcard_categories=False)

    assert miner.streamers == [preferred]
    assert miner.ws_pool.removed == ["wild"]


def test_failed_inventory_load_logs_skip_instead_of_empty_summary(caplog):
    twitch = InventoryFailureTwitch(eligible_categories=[], wildcard_categories=[])
    miner = _bare_miner(twitch)
    miner.streamers = [_tracked("ow_esports")]
    miner.original_streamers = [10]

    with caplog.at_level(logging.INFO):
        _refresh(miner, ["overwatch-2"], wildcard_categories=True)

    messages = [record.getMessage() for record in caplog.records]
    assert any("Category discovery skipped" in message for message in messages)
    assert not any("Category refresh complete" in message for message in messages)


def test_drops_inventory_loaded_rejects_failed_loads():
    assert Twitch.drops_inventory_loaded({"dropCampaignsInProgress": []}) is True
    assert Twitch.drops_inventory_loaded({}) is False
    assert Twitch.drops_inventory_loaded(None) is False


def test_real_discovery_filters_skip_on_failed_inventory(monkeypatch):
    # The refresh tests stub both filters; this pins the real ones to the
    # same "{} means not loaded" rule the refresh relies on.
    twitch = _bare_twitch(monkeypatch, {"overwatch-2": datetime(2099, 1, 1)})
    monkeypatch.setattr(Twitch, "_Twitch__get_inventory", lambda self: {})

    assert twitch.filter_categories_with_active_drops(["overwatch-2"]) == []
    assert (
        twitch.filter_categories_with_active_drops(["overwatch-2"], inventory={}) == []
    )
    assert twitch.get_wildcard_categories_with_active_drops() == []
    assert twitch.get_wildcard_categories_with_active_drops(inventory={}) == []


# --- Issue #175: a failed category lookup must not retire its streamers ---


class LookupFailureTwitch(FakeTwitch):
    """Mirrors Twitch.get_live_streamers_for_category raising when a Helix or
    GQL request fails for one of `failing_categories`; other categories are
    looked up normally."""

    def __init__(self, failing_categories, partial_usernames=(), **kwargs):
        super().__init__(**kwargs)
        self.failing_categories = set(failing_categories)
        self.partial_usernames = list(partial_usernames)

    def get_live_streamers_for_category(self, selector, **kwargs):
        if selector in self.failing_categories:
            self.selectors.append((selector, kwargs))
            raise CategoryLookupFailedException(
                f"lookup for '{selector}' failed", usernames=self.partial_usernames
            )
        return super().get_live_streamers_for_category(selector, **kwargs)


class CampaignLookupFailureTwitch(FakeTwitch):
    """Both discovery filters raise, as they do when the drops dashboard
    request failed and no other source produced a campaign."""

    def filter_categories_with_active_drops(self, categories, **kwargs):
        self.filter_calls.append(kwargs)
        raise CategoryLookupFailedException("drop campaign lookup failed")

    def get_wildcard_categories_with_active_drops(self, **kwargs):
        self.wildcard_calls.append(kwargs)
        raise CategoryLookupFailedException("drop campaign lookup failed")


def _tracked_in_game(username, game_name, wildcard=False):
    streamer = _tracked(username, wildcard=wildcard)
    streamer.is_online = True
    streamer.stream.game = {"id": f"{game_name}-id", "name": game_name}
    return streamer


def test_failed_category_lookup_keeps_its_streamers_but_retires_others(caplog):
    # overwatch-2's live-channel lookup fails while rust is evaluated and
    # found genuinely empty: only rust's streamer is stale.
    twitch = LookupFailureTwitch(
        ["overwatch-2"],
        eligible_categories=["overwatch-2", "rust"],
        wildcard_categories=[],
        empty_categories=["rust"],
    )
    miner = _bare_miner(twitch)
    ow_esports = _tracked_in_game("ow_esports", "Overwatch 2")
    rust_streamer = _tracked_in_game("rust_streamer", "Rust")
    miner.streamers = [ow_esports, rust_streamer]
    miner.original_streamers = [10, 20]

    with caplog.at_level(logging.INFO):
        _refresh(miner, ["overwatch-2", "rust"])

    assert miner.streamers == [ow_esports]
    assert miner.original_streamers == [10]
    assert miner.ws_pool.removed == ["rust_streamer"]
    assert ow_esports.irc_chat.stopped is False
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "Unable to look up live channels for category 'overwatch-2'" in message
        for message in messages
    )
    assert any("Category refresh complete" in message for message in messages)


def test_stale_streamer_is_not_kept_by_the_failed_game_it_now_streams():
    # Discovered under rust, which refreshed without it, and now live in
    # Overwatch 2, whose lookup failed. Its recorded category decides: it is
    # stale for rust, and overwatch-2 never discovered it.
    twitch = LookupFailureTwitch(
        ["overwatch-2"],
        eligible_categories=["overwatch-2", "rust"],
        wildcard_categories=[],
        empty_categories=["rust"],
    )
    miner = _bare_miner(twitch)
    moved = _tracked_in_game("moved", "Overwatch 2")
    moved.discovery_category_slugs = {"rust"}
    miner.streamers = [moved]
    miner.original_streamers = [10]

    _refresh(miner, ["overwatch-2", "rust"])

    assert miner.streamers == []
    assert miner.ws_pool.removed == ["moved"]


def test_every_category_lookup_failing_keeps_every_streamer():
    twitch = LookupFailureTwitch(
        ["overwatch-2", "rust"],
        eligible_categories=["overwatch-2", "rust"],
        wildcard_categories=[],
    )
    miner = _bare_miner(twitch)
    ow_esports = _tracked_in_game("ow_esports", "Overwatch 2")
    rust_streamer = _tracked_in_game("rust_streamer", "Rust")
    miner.streamers = [ow_esports, rust_streamer]
    miner.original_streamers = [10, 20]

    _refresh(miner, ["overwatch-2", "rust"])

    assert miner.streamers == [ow_esports, rust_streamer]
    assert miner.ws_pool.removed == []


def test_successful_empty_lookup_still_retires_streamer_in_that_category():
    # The protection is keyed on a failed lookup, not on the streamer's game:
    # an evaluated category with no live channel still retires its streamer.
    twitch = FakeTwitch(
        eligible_categories=["overwatch-2"],
        wildcard_categories=[],
        empty_categories=["overwatch-2"],
    )
    miner = _bare_miner(twitch)
    ow_esports = _tracked_in_game("ow_esports", "Overwatch 2")
    miner.streamers = [ow_esports]
    miner.original_streamers = [10]

    _refresh(miner, ["overwatch-2"])

    assert miner.streamers == []
    assert miner.ws_pool.removed == ["ow_esports"]
    assert ow_esports.irc_chat.stopped is True


def test_failed_lookup_keeps_streamer_under_resolved_game_slug():
    # Aliased categories resolve to a differently slugged Twitch game; the
    # exception carries that slug so the streamer still maps to the failure.
    class AliasLookupFailureTwitch(FakeTwitch):
        def get_live_streamers_for_category(self, selector, **kwargs):
            raise CategoryLookupFailedException(
                "streams failed", game_slug="bitcraft-online"
            )

    twitch = AliasLookupFailureTwitch(
        eligible_categories=["bitcraft"], wildcard_categories=[]
    )
    miner = _bare_miner(twitch)
    bitcrafter = _tracked_in_game("bitcrafter", "BitCraft Online")
    miner.streamers = [bitcrafter]
    miner.original_streamers = [10]

    usernames, unevaluated, slugs_by_username = (
        miner._TwitchChannelPointsMiner__discover_live_category_streamers(
            ["bitcraft"], drops_enabled=True, limit=2, sort_by="VIEWERS_DESC"
        )
    )
    assert usernames == []
    assert unevaluated == {"bitcraft", "bitcraft-online"}
    assert slugs_by_username == {}

    _refresh(miner, ["bitcraft"])

    assert miner.streamers == [bitcrafter]
    assert miner.ws_pool.removed == []


def test_failed_wildcard_category_lookup_keeps_its_streamers():
    twitch = LookupFailureTwitch(
        ["gamex"], eligible_categories=[], wildcard_categories=["gamex", "gamey"]
    )
    miner = _bare_miner(twitch)
    gamex = _tracked_in_game("gamex-streamer", "GameX", wildcard=True)
    closed = _tracked_in_game("closed-streamer", "ClosedGame", wildcard=True)
    miner.streamers = [gamex, closed]
    miner.original_streamers = [10, 20]

    _refresh(miner, ["gameA"], wildcard_categories=True)

    assert [streamer.username for streamer in miner.streamers] == [
        "gamex-streamer",
        "gamey-streamer",
    ]
    assert miner.ws_pool.removed == ["closed-streamer"]
    assert gamex.irc_chat.stopped is False


def test_failed_campaign_lookup_keeps_preferred_and_wildcard_streamers(caplog):
    twitch = CampaignLookupFailureTwitch(eligible_categories=[], wildcard_categories=[])
    miner = _bare_miner(twitch)
    preferred = _tracked("ow_esports")
    wildcard = _tracked("wild", wildcard=True)
    miner.streamers = [preferred, wildcard]
    miner.original_streamers = [10, 20]

    with caplog.at_level(logging.INFO):
        _refresh(miner, ["overwatch-2"], wildcard_categories=True)

    assert miner.streamers == [preferred, wildcard]
    assert miner.ws_pool.removed == []
    # Wildcard discovery stands down rather than repeating the failed lookup.
    assert twitch.wildcard_calls == []
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "Category discovery skipped: drop campaign lookup failed" in message
        for message in messages
    )
    assert not any("Category refresh complete" in message for message in messages)


def test_failed_wildcard_campaign_lookup_only_spares_wildcard_streamers():
    # The preferred pass evaluated fine and found nothing, so its streamer is
    # stale; only the wildcard pass failed and keeps its own.
    class WildcardCampaignLookupFailureTwitch(FakeTwitch):
        def get_wildcard_categories_with_active_drops(self, **kwargs):
            self.wildcard_calls.append(kwargs)
            raise CategoryLookupFailedException("drop campaign lookup failed")

    twitch = WildcardCampaignLookupFailureTwitch(
        eligible_categories=[], wildcard_categories=[]
    )
    miner = _bare_miner(twitch)
    preferred = _tracked("ow_esports")
    wildcard = _tracked("wild", wildcard=True)
    miner.streamers = [preferred, wildcard]
    miner.original_streamers = [10, 20]

    _refresh(miner, ["overwatch-2"], wildcard_categories=True)

    assert miner.streamers == [wildcard]
    assert miner.ws_pool.removed == ["ow_esports"]
    assert len(twitch.wildcard_calls) == 1


def test_total_campaign_outage_expires_each_pass_after_three_failures(caplog):
    twitch = CampaignLookupFailureTwitch(eligible_categories=[], wildcard_categories=[])
    miner = _bare_miner(twitch)
    preferred = _tracked("ow_esports")
    wildcard = _tracked("wild", wildcard=True)
    miner.streamers = [preferred, wildcard]
    miner.original_streamers = [10, 20]

    for _ in range(2):
        _refresh(miner, ["overwatch-2"], wildcard_categories=True)
    assert miner.streamers == [preferred, wildcard]
    assert twitch.wildcard_calls == []

    with caplog.at_level(logging.WARNING):
        _refresh(miner, ["overwatch-2"], wildcard_categories=True)

    assert miner.streamers == [wildcard]
    assert miner.original_streamers == [20]
    assert miner.ws_pool.removed == ["ow_esports"]
    assert len(twitch.wildcard_calls) == 1
    assert DISCOVERY_OUTAGE_EXPIRED in caplog.text

    # Wildcard failures have their own count, starting when that pass resumes.
    _refresh(miner, ["overwatch-2"], wildcard_categories=True)
    assert miner.streamers == [wildcard]
    _refresh(miner, ["overwatch-2"], wildcard_categories=True)
    assert miner.streamers == []
    assert miner.original_streamers == []
    assert miner.ws_pool.removed == ["ow_esports", "wild"]
    assert len(twitch.wildcard_calls) == 3
    assert "all wildcard categories failed 3 refreshes in a row" in caplog.text


@pytest.mark.parametrize("wildcard", [False, True])
def test_campaign_lookup_recovery_resets_the_failure_limit(wildcard):
    class RecoveringTwitch(FakeTwitch):
        unavailable = True

        def filter_categories_with_active_drops(self, categories, **kwargs):
            if self.unavailable and not wildcard:
                raise CategoryLookupFailedException("drop campaign lookup failed")
            return super().filter_categories_with_active_drops(categories, **kwargs)

        def get_wildcard_categories_with_active_drops(self, **kwargs):
            if self.unavailable and wildcard:
                raise CategoryLookupFailedException("drop campaign lookup failed")
            return super().get_wildcard_categories_with_active_drops(**kwargs)

    twitch = RecoveringTwitch(
        eligible_categories=[] if wildcard else ["overwatch-2"],
        wildcard_categories=["gamex"],
    )
    miner = _bare_miner(twitch)
    tracked = _tracked(
        "gamex-streamer" if wildcard else "overwatch-2-streamer", wildcard=wildcard
    )
    miner.streamers = [tracked]
    miner.original_streamers = [10]

    for _ in range(2):
        _refresh(miner, ["overwatch-2"], wildcard_categories=wildcard)
    assert miner.streamers == [tracked]

    twitch.unavailable = False
    _refresh(miner, ["overwatch-2"], wildcard_categories=wildcard)
    assert miner.streamers == [tracked]

    twitch.unavailable = True
    for _ in range(2):
        _refresh(miner, ["overwatch-2"], wildcard_categories=wildcard)
    assert miner.streamers == [tracked]
    assert miner.ws_pool.removed == []

    _refresh(miner, ["overwatch-2"], wildcard_categories=wildcard)
    assert tracked not in miner.streamers
    assert miner.ws_pool.removed == [tracked.username]


def test_real_discovery_filters_raise_when_dashboard_failed_and_nothing_found(
    monkeypatch,
):
    twitch = _bare_twitch(monkeypatch, {})
    twitch.category_campaign_eligibility = {}
    twitch.campaign_lookup_unavailable = True

    with pytest.raises(CategoryLookupFailedException):
        twitch.filter_categories_with_active_drops(
            ["overwatch-2"], inventory={"present": True}
        )
    with pytest.raises(CategoryLookupFailedException):
        twitch.get_wildcard_categories_with_active_drops(inventory={"present": True})


def test_real_discovery_filters_keep_external_catalog_when_dashboard_failed(
    monkeypatch,
):
    # The external catalog still counts as an evaluation: a failed dashboard
    # only turns an otherwise empty result into "not evaluated".
    twitch = _bare_twitch(monkeypatch, {})
    twitch.category_campaign_eligibility = {}
    twitch.campaign_lookup_unavailable = True
    monkeypatch.setattr(
        Twitch,
        "_Twitch__twitchdrops_app_fallback",
        lambda self, categories, known_slugs, inventory=None: {
            "overwatch-2": datetime(2099, 1, 1)
        },
    )

    assert twitch.filter_categories_with_active_drops(
        ["overwatch-2"], inventory={"present": True}
    ) == ["overwatch-2"]
    assert twitch.unevaluated_category_slugs == set()
    assert twitch.get_wildcard_categories_with_active_drops(
        inventory={"present": True}
    ) == ["overwatch-2"]
    assert twitch.unevaluated_category_slugs == set()


def test_real_discovery_filters_empty_result_is_genuine_when_dashboard_loaded(
    monkeypatch,
):
    twitch = _bare_twitch(monkeypatch, {})
    twitch.category_campaign_eligibility = {}
    twitch.campaign_lookup_unavailable = False
    # The raw dashboard outcome alone must not steer a discovery pass; only
    # the snapshot the pass took after its own dashboard call does.
    monkeypatch.setattr(_drops_dashboard_state, "unavailable", True, raising=False)

    assert (
        twitch.filter_categories_with_active_drops(
            ["overwatch-2"], inventory={"present": True}
        )
        == []
    )
    assert (
        twitch.get_wildcard_categories_with_active_drops(inventory={"present": True})
        == []
    )


def test_filter_reports_uncovered_categories_as_unevaluated_when_lookup_failed(
    monkeypatch,
):
    # The dashboard failed but a secondary source still vouched for rust:
    # rust is evaluated normally, overwatch-2 was never evaluated.
    twitch = _bare_twitch(monkeypatch, {"rust": datetime(2099, 1, 1)})
    twitch.category_campaign_eligibility = {}
    twitch.campaign_lookup_unavailable = True
    twitch.twitchdrops_app_game_names = {"bitcraft": "BitCraft Online"}
    twitch.category_campaign_deadlines = {
        "overwatch-2": datetime(2098, 1, 1),
        "ended-game": datetime(2098, 1, 1),
    }

    result = twitch.filter_categories_with_active_drops(
        ["overwatch-2", "bitcraft", "rust"], inventory={"present": True}
    )

    assert result == ["rust"]
    assert twitch.unevaluated_category_slugs == {
        "overwatch-2",
        "bitcraft",
        "bitcraft-online",
    }
    # Unevaluated deadlines survive; a covered pass still prunes the rest.
    assert twitch.category_campaign_deadlines == {
        "overwatch-2": datetime(2098, 1, 1),
        "ended-game": datetime(2098, 1, 1),
        "rust": datetime(2099, 1, 1),
    }
    # Only a genuinely evaluated miss clears a category's eligibility.
    assert twitch.category_campaign_eligibility == {}


def test_filter_without_lookup_failure_treats_uncovered_categories_as_inactive(
    monkeypatch,
):
    twitch = _bare_twitch(monkeypatch, {"rust": datetime(2099, 1, 1)})
    twitch.category_campaign_eligibility = {}
    twitch.category_campaign_deadlines = {"overwatch-2": datetime(2098, 1, 1)}

    result = twitch.filter_categories_with_active_drops(
        ["overwatch-2", "rust"], inventory={"present": True}
    )

    assert result == ["rust"]
    assert twitch.unevaluated_category_slugs == set()
    assert twitch.category_campaign_deadlines == {"rust": datetime(2099, 1, 1)}


def test_wildcard_reports_uncovered_pinned_games_as_unevaluated_when_lookup_failed(
    monkeypatch,
):
    twitch = _bare_twitch(monkeypatch, {"rust": datetime(2099, 1, 1)})
    twitch.campaign_lookup_unavailable = True
    twitch.category_campaign_deadlines = {
        "gamex": datetime(2098, 1, 1),
        "gone": datetime(2098, 1, 1),
    }

    result = twitch.get_wildcard_categories_with_active_drops(
        pinned_category_slugs={"gamex"}, inventory={"present": True}
    )

    assert result == ["rust"]
    assert twitch.unevaluated_category_slugs == {"gamex"}
    assert twitch.category_campaign_deadlines == {
        "gamex": datetime(2098, 1, 1),
        "rust": datetime(2099, 1, 1),
    }


class PartialCampaignLookupTwitch(FakeTwitch):
    """The filter's Twitch lookup failed but a secondary source covered rust;
    it reports overwatch-2 as not evaluated, as the real filter does."""

    def filter_categories_with_active_drops(self, categories, **kwargs):
        self.filter_calls.append(kwargs)
        self.unevaluated_category_slugs = {"overwatch-2"}
        return ["rust"]


def test_partially_covered_campaign_lookup_keeps_uncovered_category_streamers():
    twitch = PartialCampaignLookupTwitch(
        eligible_categories=[], wildcard_categories=[], empty_categories=["rust"]
    )
    miner = _bare_miner(twitch)
    ow_esports = _tracked_in_game("ow_esports", "Overwatch 2")
    rust_streamer = _tracked_in_game("rust_streamer", "Rust")
    miner.streamers = [ow_esports, rust_streamer]
    miner.original_streamers = [10, 20]

    _refresh(miner, ["overwatch-2", "rust"])

    # rust was evaluated (and found empty); overwatch-2 was not.
    assert miner.streamers == [ow_esports]
    assert miner.ws_pool.removed == ["rust_streamer"]
    assert [selector for selector, _ in twitch.selectors] == ["rust"]


def test_failed_lookup_with_partial_page_still_adds_found_channels():
    twitch = LookupFailureTwitch(
        ["overwatch-2"],
        partial_usernames=["page_one_streamer"],
        eligible_categories=["overwatch-2"],
        wildcard_categories=[],
    )
    miner = _bare_miner(twitch)
    ow_esports = _tracked_in_game("ow_esports", "Overwatch 2")
    miner.streamers = [ow_esports]
    miner.original_streamers = [10]

    _refresh(miner, ["overwatch-2"])

    assert [streamer.username for streamer in miner.streamers] == [
        "ow_esports",
        "page_one_streamer",
    ]
    assert miner.ws_pool.removed == []
    added = miner.streamers[1]
    assert added.from_category is True
    assert added.discovery_category_slugs == {"overwatch-2"}


def test_freshly_added_streamer_without_game_is_kept_on_failed_lookup():
    # A streamer added last pass has no stream game yet; the category it was
    # discovered under is what maps it to the failed lookup.
    twitch = LookupFailureTwitch(
        ["overwatch-2"], eligible_categories=["overwatch-2"], wildcard_categories=[]
    )
    miner = _bare_miner(twitch)
    fresh = _tracked("fresh")
    fresh.discovery_category_slugs = {"overwatch-2"}
    assert fresh.stream.game_name() is None
    unknown = _tracked("unknown")
    miner.streamers = [fresh, unknown]
    miner.original_streamers = [10, 20]

    _refresh(miner, ["overwatch-2"])

    assert miner.streamers == [fresh]
    assert miner.ws_pool.removed == ["unknown"]


def test_discovered_streamer_records_its_category_and_survives_next_failed_lookup():
    twitch = LookupFailureTwitch(
        [], eligible_categories=["overwatch-2"], wildcard_categories=[]
    )
    miner = _bare_miner(twitch)

    _refresh(miner, ["overwatch-2"])
    assert [streamer.username for streamer in miner.streamers] == [
        "overwatch-2-streamer"
    ]
    added = miner.streamers[0]
    assert added.discovery_category_slugs == {"overwatch-2"}
    # FakeTwitch.check_streamer_online never reports a game, as a first
    # stream update may not have happened yet.
    assert added.stream.game_name() is None

    twitch.failing_categories = {"overwatch-2"}
    _refresh(miner, ["overwatch-2"])

    assert miner.streamers == [added]
    assert miner.ws_pool.removed == []


def test_rediscovery_replaces_recorded_categories_of_tracked_streamer():
    class SharedStreamerTwitch(FakeTwitch):
        def get_live_streamers_for_category(self, selector, **kwargs):
            self.selectors.append((selector, kwargs))
            return ["shared"]

    twitch = SharedStreamerTwitch(
        eligible_categories=["overwatch-2"], wildcard_categories=[]
    )
    miner = _bare_miner(twitch)
    _refresh(miner, ["overwatch-2"])
    shared = miner.streamers[0]
    assert shared.discovery_category_slugs == {"overwatch-2"}

    twitch.eligible_categories = ["rust"]
    _refresh(miner, ["rust"])

    # Judged by where it was found this time: a category it has left must not
    # keep shielding it from retirement.
    assert miner.streamers == [shared]
    assert shared.discovery_category_slugs == {"rust"}


def test_streamer_that_left_a_category_is_retired_when_only_that_one_fails():
    # Found under overwatch-2, then only under rust. Later rust is evaluated
    # without it while overwatch-2's lookup fails: its stale overwatch-2
    # record must not keep it.
    class MovingStreamerTwitch(FakeTwitch):
        def __init__(self, found_by_category, **kwargs):
            super().__init__(**kwargs)
            self.found_by_category = found_by_category
            self.failing_categories = set()

        def get_live_streamers_for_category(self, selector, **kwargs):
            self.selectors.append((selector, kwargs))
            if selector in self.failing_categories:
                raise CategoryLookupFailedException(f"lookup for '{selector}' failed")
            return list(self.found_by_category.get(selector, []))

    twitch = MovingStreamerTwitch(
        {"overwatch-2": ["shared"]},
        eligible_categories=["overwatch-2", "rust"],
        wildcard_categories=[],
    )
    miner = _bare_miner(twitch)
    _refresh(miner, ["overwatch-2", "rust"])
    shared = miner.streamers[0]
    twitch.found_by_category = {"rust": ["shared"]}
    _refresh(miner, ["overwatch-2", "rust"])
    assert shared.discovery_category_slugs == {"rust"}

    twitch.found_by_category = {"rust": ["other"]}
    twitch.failing_categories = {"overwatch-2"}
    _refresh(miner, ["overwatch-2", "rust"])

    assert [streamer.username for streamer in miner.streamers] == ["other"]
    assert miner.ws_pool.removed == ["shared"]


def test_discovery_failure_log_names_the_consequence_per_path(caplog):
    twitch = LookupFailureTwitch(
        ["overwatch-2"], eligible_categories=[], wildcard_categories=[]
    )
    miner = _bare_miner(twitch)
    discover = miner._TwitchChannelPointsMiner__discover_live_category_streamers

    with caplog.at_level(logging.WARNING):
        discover(["overwatch-2"], drops_enabled=True, limit=2, sort_by="VIEWERS_DESC")
        discover(
            ["overwatch-2"],
            drops_enabled=True,
            limit=2,
            sort_by="VIEWERS_DESC",
            tracking=False,
        )

    messages = [record.getMessage() for record in caplog.records]
    assert "its tracked streamers are kept until the next refresh" in messages[0]
    assert "it is skipped until the next refresh" in messages[1]
    assert "kept" not in messages[1]


def test_wildcard_keeps_deadlines_the_preferred_pass_left_unevaluated(monkeypatch):
    # The preferred pass just recorded overwatch-2 as not evaluated and kept
    # its deadline; the wildcard pass, failing the same way, must not wipe it
    # while replacing the catalog deadlines.
    twitch = _bare_twitch(monkeypatch, {"rust": datetime(2099, 1, 1)})
    twitch.campaign_lookup_unavailable = True
    twitch.unevaluated_category_slugs = {"overwatch-2"}
    twitch.category_campaign_deadlines = {
        "overwatch-2": datetime(2098, 1, 1),
        "gamex": datetime(2098, 1, 1),
        "gone": datetime(2098, 1, 1),
    }

    result = twitch.get_wildcard_categories_with_active_drops(
        exclude_category_slugs={"overwatch-2"},
        pinned_category_slugs={"gamex"},
        inventory={"present": True},
    )

    assert result == ["rust"]
    assert twitch.unevaluated_category_slugs == {"gamex"}
    assert twitch.category_campaign_deadlines == {
        "overwatch-2": datetime(2098, 1, 1),
        "gamex": datetime(2098, 1, 1),
        "rust": datetime(2099, 1, 1),
    }


def test_wildcard_evaluated_fully_replaces_deadlines_even_after_preferred_failure(
    monkeypatch,
):
    # A wildcard pass whose own lookup succeeded evaluated the whole catalog,
    # so what it did not find is genuinely gone.
    twitch = _bare_twitch(monkeypatch, {"rust": datetime(2099, 1, 1)})
    twitch.campaign_lookup_unavailable = False
    twitch.unevaluated_category_slugs = {"overwatch-2"}
    twitch.category_campaign_deadlines = {"overwatch-2": datetime(2098, 1, 1)}

    assert twitch.get_wildcard_categories_with_active_drops(
        inventory={"present": True}
    ) == ["rust"]
    assert twitch.unevaluated_category_slugs == set()
    assert twitch.category_campaign_deadlines == {"rust": datetime(2099, 1, 1)}


class ResettingFilterTwitch(FakeTwitch):
    """Each filter call resets the reported set like the real ones do, so the
    miner must read the preferred pass's set before the wildcard pass runs."""

    def filter_categories_with_active_drops(self, categories, **kwargs):
        self.filter_calls.append(kwargs)
        self.unevaluated_category_slugs = {"overwatch-2"}
        return []

    def get_wildcard_categories_with_active_drops(self, **kwargs):
        self.wildcard_calls.append(kwargs)
        self.unevaluated_category_slugs = set()
        return []


def test_wildcard_pass_stands_down_while_filter_left_preferred_unevaluated():
    # The preferred filter reported overwatch-2 as not evaluated: the wildcard
    # pass does not run (so it cannot reset that report either), and the
    # preferred streamer is kept.
    twitch = ResettingFilterTwitch(eligible_categories=[], wildcard_categories=[])
    miner = _bare_miner(twitch)
    ow_esports = _tracked_in_game("ow_esports", "Overwatch 2")
    miner.streamers = [ow_esports]
    miner.original_streamers = [10]

    _refresh(miner, ["overwatch-2"], wildcard_categories=True)

    assert twitch.wildcard_calls == []
    assert miner.streamers == [ow_esports]
    assert miner.ws_pool.removed == []


def test_wildcard_reports_tracked_streamer_categories_as_unevaluated_too(
    monkeypatch,
):
    # A tracked wildcard streamer whose stream has not reported a game is not
    # in pinned_category_slugs; the category it was discovered under still
    # counts as not evaluated when the Twitch lookup failed.
    twitch = _bare_twitch(monkeypatch, {"rust": datetime(2099, 1, 1)})
    twitch.campaign_lookup_unavailable = True
    twitch.category_campaign_deadlines = {"gamey": datetime(2098, 1, 1)}

    result = twitch.get_wildcard_categories_with_active_drops(
        pinned_category_slugs=set(),
        tracked_category_slugs={"gamey", "rust"},
        inventory={"present": True},
    )

    assert result == ["rust"]
    assert twitch.unevaluated_category_slugs == {"gamey"}
    assert twitch.category_campaign_deadlines == {
        "gamey": datetime(2098, 1, 1),
        "rust": datetime(2099, 1, 1),
    }


class TrackedAwareWildcardTwitch(FakeTwitch):
    """Wildcard filter whose Twitch lookup failed while a secondary source
    covered only 'rust': whatever the miner reports as tracked and is not
    covered is marked unevaluated, as the real filter does."""

    def get_wildcard_categories_with_active_drops(self, **kwargs):
        self.wildcard_calls.append(kwargs)
        self.unevaluated_category_slugs = (
            set(kwargs["pinned_category_slugs"]) | set(kwargs["tracked_category_slugs"])
        ) - {"rust"}
        return ["rust"]


def test_wildcard_streamer_without_stream_game_is_kept_via_recorded_category():
    twitch = TrackedAwareWildcardTwitch(eligible_categories=[], wildcard_categories=[])
    miner = _bare_miner(twitch)
    fresh = _tracked("fresh-wild", wildcard=True)
    fresh.is_online = True
    fresh.discovery_category_slugs = {"gamey"}
    assert fresh.stream.game_name() is None
    gone = _tracked("gone-wild", wildcard=True)
    gone.is_online = True
    miner.streamers = [fresh, gone]
    miner.original_streamers = [10, 20]

    _refresh(miner, ["gameA"], wildcard_categories=True)

    assert twitch.wildcard_calls[0]["tracked_category_slugs"] == {"gamey"}
    assert [streamer.username for streamer in miner.streamers] == [
        "fresh-wild",
        "rust-streamer",
    ]
    assert miner.ws_pool.removed == ["gone-wild"]


def test_failed_wildcard_campaign_lookup_is_named_in_the_refresh_summary(caplog):
    class WildcardCampaignLookupFailureTwitch(FakeTwitch):
        def get_wildcard_categories_with_active_drops(self, **kwargs):
            self.wildcard_calls.append(kwargs)
            raise CategoryLookupFailedException("drop campaign lookup failed")

    twitch = WildcardCampaignLookupFailureTwitch(
        eligible_categories=[], wildcard_categories=[]
    )
    miner = _bare_miner(twitch)
    miner.streamers = [_tracked("wild", wildcard=True)]
    miner.original_streamers = [10]

    with caplog.at_level(logging.INFO):
        _refresh(miner, ["overwatch-2"], wildcard_categories=True)

    summaries = [
        record.getMessage()
        for record in caplog.records
        if "Category refresh complete" in record.getMessage()
    ]
    assert len(summaries) == 1
    assert "wildcard pass stood down (drop campaign lookup failed)" in summaries[0]
    assert "wildcard streamer(s) discovered" not in summaries[0]


def test_genuine_empty_wildcard_pass_keeps_the_discovered_count_summary(caplog):
    twitch = FakeTwitch(eligible_categories=[], wildcard_categories=[])
    miner = _bare_miner(twitch)

    with caplog.at_level(logging.INFO):
        _refresh(miner, ["overwatch-2"], wildcard_categories=True)

    summaries = [
        record.getMessage()
        for record in caplog.records
        if "Category refresh complete" in record.getMessage()
    ]
    assert len(summaries) == 1
    assert "0 wildcard streamer(s) discovered" in summaries[0]


def test_wildcard_pass_does_not_run_when_every_preferred_lookup_failed():
    # Every preferred lookup raised, so the preferred pass "found nothing"
    # only because it could not look. Running wildcard discovery now would
    # add streamers that are never reconciled once preferred recovers.
    twitch = LookupFailureTwitch(
        ["overwatch-2", "rust"],
        eligible_categories=["overwatch-2", "rust"],
        wildcard_categories=["gamex"],
    )
    miner = _bare_miner(twitch)
    ow_esports = _tracked_in_game("ow_esports", "Overwatch 2")
    rust_streamer = _tracked_in_game("rust_streamer", "Rust")
    wild = _tracked_in_game("wild", "GameX", wildcard=True)
    miner.streamers = [ow_esports, rust_streamer, wild]
    miner.original_streamers = [10, 20, 30]

    _refresh(miner, ["overwatch-2", "rust"], wildcard_categories=True)

    assert twitch.wildcard_calls == []
    assert miner.streamers == [ow_esports, rust_streamer, wild]
    assert miner.ws_pool.removed == []


def test_category_that_keeps_failing_is_treated_as_evaluated_after_the_limit(
    caplog,
):
    # A lookup that never recovers (an expired token, say) must not keep
    # stale streamers forever or hold wildcard discovery off indefinitely.
    twitch = LookupFailureTwitch(
        ["overwatch-2", "rust"],
        eligible_categories=["overwatch-2", "rust"],
        wildcard_categories=["gamex"],
    )
    miner = _bare_miner(twitch)
    ow_esports = _tracked_in_game("ow_esports", "Overwatch 2")
    rust_streamer = _tracked_in_game("rust_streamer", "Rust")
    miner.streamers = [ow_esports, rust_streamer]
    miner.original_streamers = [10, 20]

    for _ in range(2):
        _refresh(miner, ["overwatch-2", "rust"], wildcard_categories=True)
    assert miner.streamers == [ow_esports, rust_streamer]
    assert twitch.wildcard_calls == []

    with caplog.at_level(logging.WARNING):
        _refresh(miner, ["overwatch-2", "rust"], wildcard_categories=True)

    assert ow_esports not in miner.streamers
    assert rust_streamer not in miner.streamers
    assert twitch.wildcard_calls != []
    assert any(
        "failed 3 refreshes in a row" in record.getMessage()
        for record in caplog.records
    )


def test_failure_count_starts_over_once_a_lookup_succeeds():
    miner = TwitchChannelPointsMiner.__new__(TwitchChannelPointsMiner)
    miner.category_lookup_failure_streaks = {}
    expire = miner._TwitchChannelPointsMiner__expire_unevaluated_slugs

    assert expire("preferred", {"rust"}) == {"rust"}
    assert expire("preferred", {"rust"}) == {"rust"}
    # Rust's lookup worked this time, so its count starts over.
    assert expire("preferred", set()) == set()
    assert expire("preferred", {"rust"}) == {"rust"}
    assert expire("preferred", {"rust"}) == {"rust"}
    assert expire("preferred", {"rust"}) == set()
    # The wildcard pass keeps its own count.
    assert expire("wildcard", {"rust"}) == {"rust"}


def test_wildcard_pass_runs_once_preferred_is_evaluated_and_empty():
    # Guard for the rule above: an evaluated, genuinely empty preferred pass
    # still hands idle capacity to the wildcard pass.
    twitch = LookupFailureTwitch(
        [],
        eligible_categories=["overwatch-2"],
        wildcard_categories=["gamex"],
        empty_categories=["overwatch-2"],
    )
    miner = _bare_miner(twitch)

    _refresh(miner, ["overwatch-2"], wildcard_categories=True)

    assert len(twitch.wildcard_calls) == 1
    assert [streamer.username for streamer in miner.streamers] == ["gamex-streamer"]


def test_pick_with_failed_live_check_does_not_retire_a_live_channel(monkeypatch):
    # End to end with the real lookup: limit 1, discovery finds live_a, and
    # the Drops pick (eligible here, but missing from the capped Drops
    # directory) fails its own live check. The pick is kept beside live_a
    # rather than in its place, so the evaluated category retires neither.
    real = object.__new__(Twitch)
    real.campaign_lookup_unavailable = False
    real.unevaluated_category_slugs = set()
    real.gql = SimpleNamespace(
        post_gql_request_raw=lambda operation, request: {
            "data": {
                "game": {
                    "streams": {
                        "edges": [
                            {"node": {"broadcaster": {"login": login}}}
                            for login in ("live_a",)
                        ]
                    }
                }
            }
        }
    )
    real.active_drop_campaigns = {}
    real.twitchdrops_app_campaigns = {}
    real.twitchdrops_app_game_names = {}
    real.category_campaign_eligibility = {("overwatch-2", "pick"): (1, 1)}
    real.category_log_level = logging.DEBUG
    real.last_drop_pick_streamer = "pick"

    def helix_get(self, endpoint, params):
        if endpoint == "search/categories":
            return {"data": [{"id": "ow-id", "name": "Overwatch 2"}]}
        if params.get("user_login") == "pick":
            return {}
        return {
            "data": [{"user_login": "live_a", "game_id": "ow-id", "viewer_count": 5}]
        }

    monkeypatch.setattr(Twitch, "_Twitch__helix_get", helix_get)

    class RealLookupTwitch(FakeTwitch):
        def get_live_streamers_for_category(self, selector, **kwargs):
            self.selectors.append((selector, kwargs))
            return real.get_live_streamers_for_category(selector, **kwargs)

    twitch = RealLookupTwitch(
        eligible_categories=["overwatch-2"], wildcard_categories=[]
    )
    miner = _bare_miner(twitch)
    live_a = _tracked_in_game("live_a", "Overwatch 2")
    pick = _tracked_in_game("pick", "Overwatch 2")
    miner.streamers = [live_a, pick]
    miner.original_streamers = [10, 20]

    miner._TwitchChannelPointsMiner__refresh_category_streamers(
        ["overwatch-2"],
        [],
        True,
        1,
        "VIEWERS_DESC",
        "ORDER",
        ChatPresence.NEVER,
        logging.INFO,
    )

    assert twitch.selectors[0][1]["limit"] == 1
    assert miner.streamers == [live_a, pick]
    assert miner.ws_pool.removed == []
    assert live_a.irc_chat.stopped is False


def test_add_discovered_records_categories_only_for_the_owning_pass():
    # A preferred streamer recorded under bitcraft and re-found by the
    # wildcard pass under other-game keeps its bitcraft record: only the
    # preferred reconcile ever judges it, by the preferred categories.
    miner = _bare_miner(FakeTwitch(eligible_categories=[], wildcard_categories=[]))
    shared = _tracked("shared")
    shared.discovery_category_slugs = {"bitcraft"}
    miner.streamers = [shared]
    miner.original_streamers = [10]

    added = miner._TwitchChannelPointsMiner__add_discovered_category_streamers(
        ["shared"],
        [],
        ChatPresence.NEVER,
        wildcard=True,
        category_slugs_by_username={"shared": {"other-game"}},
    )

    assert added == 0
    assert miner.streamers == [shared]
    assert shared.discovery_category_slugs == {"bitcraft"}


def test_cross_pass_rediscovery_keeps_streamer_through_a_later_failed_lookup():
    # A wildcard streamer found under other-game is re-found by the preferred
    # pass under bitcraft. That pass does not own it, so its record stays
    # {other-game}, and when other-game's lookup later fails before the
    # stream has reported a game, that record still protects it.
    class MappedLookupTwitch(FakeTwitch):
        def __init__(self, found_by_category, **kwargs):
            super().__init__(**kwargs)
            self.found_by_category = found_by_category
            self.failing_categories = set()

        def get_live_streamers_for_category(self, selector, **kwargs):
            self.selectors.append((selector, kwargs))
            if selector in self.failing_categories:
                raise CategoryLookupFailedException(f"lookup for '{selector}' failed")
            return list(self.found_by_category.get(selector, []))

    twitch = MappedLookupTwitch(
        {"other-game": ["shared"], "bitcraft": ["shared"]},
        eligible_categories=[],
        wildcard_categories=["other-game"],
    )
    miner = _bare_miner(twitch)

    _refresh(miner, ["bitcraft"], wildcard_categories=True)
    assert [streamer.username for streamer in miner.streamers] == ["shared"]
    shared = miner.streamers[0]
    assert shared.from_wildcard_category is True
    assert shared.discovery_category_slugs == {"other-game"}

    twitch.eligible_categories = ["bitcraft"]
    _refresh(miner, ["bitcraft"], wildcard_categories=True)
    assert shared.discovery_category_slugs == {"other-game"}

    twitch.eligible_categories = []
    twitch.failing_categories = {"other-game"}
    _refresh(miner, ["bitcraft"], wildcard_categories=True)

    assert shared.stream.game_name() is None
    assert miner.streamers == [shared]
    assert miner.ws_pool.removed == []


def test_config_change_refreshes_do_not_count_toward_a_category_lookup_limit():
    twitch = LookupFailureTwitch(
        ["overwatch-2"], eligible_categories=["overwatch-2"], wildcard_categories=[]
    )
    miner = _bare_miner(twitch)
    preferred = _tracked_in_game("ow_esports", "overwatch-2")
    miner.streamers = [preferred]
    miner.original_streamers = [10]

    _refresh(miner, ["overwatch-2"])
    for _ in range(3):
        miner.refresh_categories({"categories": ["overwatch-2"], "category_limit": 2})
    _refresh(miner, ["overwatch-2"])
    assert miner.streamers == [preferred]
    assert miner.ws_pool.removed == []

    _refresh(miner, ["overwatch-2"])
    assert miner.streamers == []
    assert miner.ws_pool.removed == ["ow_esports"]


def test_config_change_refreshes_do_not_count_toward_a_wildcard_lookup_limit():
    twitch = LookupFailureTwitch(
        ["gamex"], eligible_categories=[], wildcard_categories=["gamex"]
    )
    miner = _bare_miner(twitch)
    wild = _tracked_in_game("wild", "gamex", wildcard=True)
    miner.streamers = [wild]
    miner.original_streamers = [10]
    config = {"categories": ["overwatch-2"], "wildcard_categories": True}

    _refresh(miner, ["overwatch-2"], wildcard_categories=True)
    for _ in range(3):
        miner.refresh_categories(config)
    _refresh(miner, ["overwatch-2"], wildcard_categories=True)
    assert miner.streamers == [wild]
    assert miner.ws_pool.removed == []

    _refresh(miner, ["overwatch-2"], wildcard_categories=True)
    assert miner.streamers == []
    assert miner.ws_pool.removed == ["wild"]


class _StartupDone(Exception):
    """Raised once startup has built its streamers, before the session
    starts its threads."""


def _startup_streamers(twitch, categories, wildcard_categories=True):
    """Run the miner's startup up to the point its streamers are built and
    return them, with the startup discovery passes left as they ran."""

    def stop_after_discovery(streamers):
        raise _StartupDone()

    twitch.login = lambda: None
    twitch.initialize_streamers_context = stop_after_discovery
    miner = _bare_miner(twitch)
    miner.running = False
    miner.session_id = "test-session"
    miner.claim_drops_startup = False
    miner.watch_streak_cache = None

    with pytest.raises(_StartupDone):
        miner.run(
            categories=categories,
            category_limit=2,
            drop_badge_catalog=False,
            wildcard_categories=wildcard_categories,
        )
    return miner.streamers


def test_startup_runs_wildcard_discovery_when_preferred_categories_find_nobody():
    twitch = FakeTwitch(
        eligible_categories=["overwatch-2"],
        wildcard_categories=["gamex"],
        empty_categories=["overwatch-2"],
    )

    streamers = _startup_streamers(twitch, ["overwatch-2"])

    assert len(twitch.wildcard_calls) == 1
    assert [streamer.username for streamer in streamers] == ["gamex-streamer"]
    assert streamers[0].from_wildcard_category is True
    assert streamers[0].discovery_category_slugs == {"gamex"}


def test_startup_category_streamers_record_the_category_they_were_found_under():
    twitch = FakeTwitch(
        eligible_categories=["overwatch-2"], wildcard_categories=["gamex"]
    )

    streamers = _startup_streamers(twitch, ["overwatch-2"])

    assert twitch.wildcard_calls == []
    assert [streamer.username for streamer in streamers] == ["overwatch-2-streamer"]
    assert streamers[0].from_category is True
    assert streamers[0].discovery_category_slugs == {"overwatch-2"}


def test_startup_failed_preferred_lookup_keeps_wildcard_discovery_off():
    # The preferred pass found nobody only because it could not look; the
    # refresh stands the wildcard pass down for this, and so must startup.
    twitch = LookupFailureTwitch(
        ["overwatch-2"],
        eligible_categories=["overwatch-2"],
        wildcard_categories=["gamex"],
    )

    streamers = _startup_streamers(twitch, ["overwatch-2"])

    assert twitch.wildcard_calls == []
    assert streamers == []


def test_startup_adds_channels_a_failed_lookup_still_found():
    twitch = LookupFailureTwitch(
        ["overwatch-2"],
        partial_usernames=["partial"],
        eligible_categories=["overwatch-2"],
        wildcard_categories=[],
    )

    streamers = _startup_streamers(twitch, ["overwatch-2"])

    assert [streamer.username for streamer in streamers] == ["partial"]
    assert streamers[0].from_category is True
    assert streamers[0].discovery_category_slugs == {"overwatch-2"}


def test_startup_failed_campaign_lookup_keeps_wildcard_discovery_off():
    twitch = CampaignLookupFailureTwitch(
        eligible_categories=[], wildcard_categories=["gamex"]
    )

    streamers = _startup_streamers(twitch, ["overwatch-2"])

    assert len(twitch.filter_calls) == 1
    assert twitch.wildcard_calls == []
    assert streamers == []
