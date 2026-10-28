import copy
import json
import logging

import pytest
from datetime import datetime
from types import SimpleNamespace

from TwitchChannelPointsMiner.classes.Twitch import Twitch
from TwitchChannelPointsMiner.classes.gql.Errors import RetryError
from TwitchChannelPointsMiner.classes.Settings import Settings
from TwitchChannelPointsMiner.classes.TwitchDropsApp import TwitchDropsAppScraper


def bare_twitch(gql):
    twitch = object.__new__(Twitch)
    twitch.gql = gql
    twitch.available_badge_names = None
    twitch.completed_badge_campaign_signature_cache = None
    twitch.twitchdrops_app_campaigns = {}
    twitch.twitchdrops_app_upcoming_starts = {}
    twitch.log_drop_checks = False
    twitch.category_log_level = logging.DEBUG
    twitch.category_campaign_eligibility = {}
    twitch.awarded_game_event_drops = {}
    return twitch


# What the real callers pass once the inventory loaded with no completions.
EMPTY_INVENTORY = {"completedRewardCampaigns": []}


def test_restricted_campaign_lookup_stops_after_total_live_limit(monkeypatch):
    twitch = bare_twitch(SimpleNamespace())
    calls = []

    def helix_get(self, endpoint, params):
        calls.append(params["user_login"])
        return {
            "data": [
                {
                    "user_login": login,
                    "game_id": "game-1",
                    "viewer_count": 100 - index,
                    "tags": ["DropsEnabled"],
                }
                for index, login in enumerate(params["user_login"][:50])
            ]
        }

    monkeypatch.setattr(Twitch, "_Twitch__helix_get", helix_get)
    campaigns = [
        {
            "channels": [f"streamer-{index}" for index in range(250)],
        }
    ]

    usernames, _ = twitch._Twitch__get_live_restricted_campaign_streamers(
        campaigns,
        "game-1",
        "Special Events",
        target_per_campaign=30,
        max_total=30,
    )

    assert len(usernames) == 30
    assert len(calls) == 1
    assert len(calls[0]) == 100


def test_restricted_campaign_lookup_trusts_campaign_channel_allowlist(monkeypatch):
    twitch = bare_twitch(SimpleNamespace())

    monkeypatch.setattr(
        Twitch,
        "_Twitch__helix_get",
        lambda self, endpoint, params: {
            "data": [
                {
                    "user_login": "ravenquest-channel",
                    "game_id": "ravenquest-id",
                    "viewer_count": 25,
                    "tags": ["Português", "DropsAtivados"],
                }
            ]
        },
    )

    usernames, _ = twitch._Twitch__get_live_restricted_campaign_streamers(
        [{"channels": ["ravenquest-channel"]}],
        "ravenquest-id",
        "RavenQuest",
    )

    assert usernames == ["ravenquest-channel"]


def test_drops_directory_filter_uses_twitch_tag_id(monkeypatch):
    monkeypatch.setattr(
        "TwitchChannelPointsMiner.classes.Twitch.DROP_ID", "official-drops-id"
    )
    calls = []

    def post_gql_request_raw(operation, request):
        calls.append((operation, request))
        return {
            "data": {
                "game": {
                    "streams": {
                        "edges": [
                            {"node": {"broadcaster": {"login": "Eligible"}}}
                        ]
                    }
                }
            }
        }

    twitch = bare_twitch(
        SimpleNamespace(post_gql_request_raw=post_gql_request_raw)
    )

    assert twitch._Twitch__get_drops_enabled_directory_logins("game") == {
        "eligible"
    }
    assert calls[0][0] == "DirectoryPage_Game"
    assert calls[0][1]["variables"]["options"]["tags"] == [
        "official-drops-id"
    ]


def test_drops_directory_filter_does_not_trust_user_tag_text(monkeypatch):
    monkeypatch.setattr(
        "TwitchChannelPointsMiner.classes.Twitch.DROP_ID", "official-drops-id"
    )
    twitch = bare_twitch(
        SimpleNamespace(
            post_gql_request_raw=lambda operation, request: {
                "data": {"game": {"streams": {"edges": []}}}
            }
        )
    )

    assert twitch._Twitch__get_drops_enabled_directory_logins("game") == set()


def test_special_events_restricted_lookup_accepts_other_game_categories(monkeypatch):
    twitch = bare_twitch(SimpleNamespace())

    monkeypatch.setattr(
        Twitch,
        "_Twitch__helix_get",
        lambda self, endpoint, params: {
            "data": [
                {
                    "user_login": "ewc-channel",
                    "game_id": "another-esports-game",
                    "viewer_count": 100,
                    "tags": ["DropsEnabled"],
                }
            ]
        },
    )

    usernames, _ = twitch._Twitch__get_live_restricted_campaign_streamers(
        [{"channels": ["ewc-channel"]}],
        "special-events-id",
        "Special Events",
        target_per_campaign=30,
        max_total=30,
    )

    assert usernames == ["ewc-channel"]


def test_normal_restricted_lookup_still_requires_matching_game(monkeypatch):
    twitch = bare_twitch(SimpleNamespace())

    monkeypatch.setattr(
        Twitch,
        "_Twitch__helix_get",
        lambda self, endpoint, params: {
            "data": [
                {
                    "user_login": "wrong-game-channel",
                    "game_id": "different-game",
                    "viewer_count": 100,
                    "tags": ["DropsEnabled"],
                }
            ]
        },
    )

    usernames, _ = twitch._Twitch__get_live_restricted_campaign_streamers(
        [{"channels": ["wrong-game-channel"]}],
        "expected-game",
        "Expected Game",
        target_per_campaign=30,
        max_total=30,
    )

    assert usernames == []


def test_badge_streamer_uses_special_events_eligibility_across_categories():
    twitch = bare_twitch(SimpleNamespace())
    twitch.category_campaign_eligibility = {
        ("special-events", "ewc-channel"): (1, 2)
    }
    streamer = SimpleNamespace(
        username="ewc-channel",
        from_category=True,
        from_badge_campaign=True,
        is_online=True,
        settings=SimpleNamespace(claim_drops=True),
        stream=SimpleNamespace(
            game_name=lambda: "Apex Legends",
            campaigns_ids=[],
        ),
    )

    assert twitch._Twitch__category_drops_condition(streamer) is True


def test_badge_streamer_that_is_also_configured_still_uses_special_events():
    # Being a badge streamer is a membership question: ranking it under
    # STREAMERS or FOLLOWERS (its highest-priority tier) must not skip the
    # special-events eligibility fallback.
    twitch = bare_twitch(SimpleNamespace())
    twitch.category_campaign_eligibility = {
        ("special-events", "ewc-channel"): (1, 2)
    }
    streamer = SimpleNamespace(
        username="ewc-channel",
        from_category=True,
        from_badge_campaign=True,
        from_followers=True,
        explicitly_configured=True,
        is_online=True,
        settings=SimpleNamespace(claim_drops=True),
        stream=SimpleNamespace(
            game_name=lambda: "Apex Legends",
            campaigns_ids=[],
        ),
    )

    assert twitch._Twitch__category_drops_condition(streamer) is True


def test_normal_category_streamer_does_not_cross_special_events_categories():
    twitch = bare_twitch(SimpleNamespace())
    twitch.category_campaign_eligibility = {
        ("special-events", "category-channel"): (1, 2)
    }
    streamer = SimpleNamespace(
        username="category-channel",
        from_category=True,
        from_badge_campaign=False,
        is_online=True,
        settings=SimpleNamespace(claim_drops=True),
        stream=SimpleNamespace(
            game_name=lambda: "Apex Legends",
            campaigns_ids=[],
        ),
    )

    assert twitch._Twitch__category_drops_condition(streamer) is False


def _category_style_streamer(username, from_badge_campaign):
    return SimpleNamespace(
        username=username,
        from_category=True,
        from_badge_campaign=from_badge_campaign,
        is_online=True,
        settings=SimpleNamespace(claim_drops=True),
        stream=SimpleNamespace(
            game_name=lambda: "Apex Legends",
            campaigns_ids=[],
        ),
    )


def test_badge_streamer_does_not_resolve_through_game_category_catalog():
    # An open campaign for the badge streamer's current game says nothing about
    # its badge campaign, so the category-catalog fallback must not make it
    # drop-eligible when its special-events eligibility is missing.
    twitch = bare_twitch(SimpleNamespace())
    twitch.completed_drop_campaigns = set()
    twitch.twitchdrops_app_campaigns = {"apex-legends": [{"channels": []}]}

    category = _category_style_streamer("shared-channel", from_badge_campaign=False)
    badge = _category_style_streamer("shared-channel", from_badge_campaign=True)

    assert twitch._Twitch__category_drops_condition(category) is True
    assert twitch._Twitch__category_drops_condition(badge) is False


def test_available_badges_returns_full_earned_badge_titles():
    gql = SimpleNamespace(
        post_gql_request_raw=lambda operation, request: {
            "data": {
                "currentUser": {
                    "availableBadges": [
                        {
                            "id": "badge-1",
                            "setID": "two-point-pickle",
                            "version": "1",
                            "title": "Two Point Pickle",
                        },
                        None,
                    ]
                }
            }
        }
    )
    twitch = bare_twitch(gql)

    assert twitch._Twitch__get_available_badge_names() == {"two point pickle"}


def test_available_badges_retries_after_unavailable_response():
    responses = iter(
        [
            {"data": {"currentUser": {"availableBadges": None}}},
            {
                "data": {
                    "currentUser": {
                        "availableBadges": [{"title": "Two Point Pickle"}]
                    }
                }
            },
        ]
    )
    twitch = bare_twitch(
        SimpleNamespace(post_gql_request_raw=lambda operation, request: next(responses))
    )

    assert twitch._Twitch__get_available_badge_names() == set()
    assert twitch.available_badge_names is None
    assert twitch._Twitch__get_available_badge_names() == {"two point pickle"}


def test_available_badges_retries_after_request_error():
    calls = []

    def post_gql_request_raw(operation, request):
        calls.append(operation)
        if len(calls) == 1:
            raise RetryError("AvailableBadges", [])
        return {
            "data": {
                "currentUser": {
                    "availableBadges": [{"title": "Two Point Pickle"}]
                }
            }
        }

    twitch = bare_twitch(SimpleNamespace(post_gql_request_raw=post_gql_request_raw))

    assert twitch._Twitch__get_available_badge_names() == set()
    assert twitch.available_badge_names is None
    assert twitch._Twitch__get_available_badge_names() == {"two point pickle"}


def test_available_badges_refreshes_successful_cache():
    responses = iter(
        [
            {
                "data": {
                    "currentUser": {"availableBadges": [{"title": "Old Badge"}]}
                }
            },
            {
                "data": {
                    "currentUser": {"availableBadges": [{"title": "New Badge"}]}
                }
            },
        ]
    )
    calls = []

    def post_gql_request_raw(operation, request):
        calls.append(operation)
        return next(responses)

    twitch = bare_twitch(SimpleNamespace(post_gql_request_raw=post_gql_request_raw))

    assert twitch._Twitch__get_available_badge_names() == {"old badge"}
    assert twitch._Twitch__get_available_badge_names() == {"old badge"}
    assert calls == ["AvailableBadges"]
    assert twitch._Twitch__get_available_badge_names(refresh=True) == {"new badge"}
    assert calls == ["AvailableBadges", "AvailableBadges"]


def test_earned_badge_completes_fallback_campaign(monkeypatch):
    gql = SimpleNamespace(
        post_gql_request_raw=lambda operation, request: {
            "data": {
                "currentUser": {
                    "availableBadges": [{"title": "Two Point Pickle"}]
                }
            }
        }
    )
    twitch = bare_twitch(gql)
    twitch.available_badge_names = {"stale badge"}
    monkeypatch.setattr(
        TwitchDropsAppScraper,
        "scrape_front_page",
        lambda self: [
            {
                "slug": "two-point-museum",
                "game": "Two Point Museum",
                "url": "https://twitchdrops.app/game/two-point-museum",
            }
        ],
    )
    monkeypatch.setattr(
        TwitchDropsAppScraper,
        "scrape",
        lambda self, category: {
            "game": "Two Point Museum",
            "campaigns": [
                {
                    "name": "TPS 10th Anniversary",
                    "ends_at": "2099-01-01T00:00:00Z",
                    "channels": [],
                    "drops": [{"name": "Two Point Pickle"}],
                }
            ],
        },
    )

    deadlines = twitch._Twitch__twitchdrops_app_fallback(
        ["two-point-museum"], set(), EMPTY_INVENTORY
    )

    assert deadlines == {}
    assert twitch.twitchdrops_app_campaigns == {}


def test_non_badge_reward_name_does_not_complete_fallback_campaign(monkeypatch):
    gql = SimpleNamespace(
        post_gql_request_raw=lambda operation, request: {
            "data": {"currentUser": {"availableBadges": []}}
        }
    )
    twitch = bare_twitch(gql)
    monkeypatch.setattr(
        TwitchDropsAppScraper,
        "scrape_front_page",
        lambda self: [
            {
                "slug": "the-elder-scrolls-online",
                "game": "The Elder Scrolls Online",
                "url": "https://twitchdrops.app/game/the-elder-scrolls-online",
            }
        ],
    )
    monkeypatch.setattr(
        TwitchDropsAppScraper,
        "scrape",
        lambda self, category: {
            "game": "The Elder Scrolls Online",
            "campaigns": [
                {
                    "name": "U51 on PTS",
                    "ends_at": "2099-01-01T00:00:00Z",
                    "channels": [],
                    "drops": [{"name": "Ouroboros Crown Crate"}],
                }
            ],
        },
    )

    deadlines = twitch._Twitch__twitchdrops_app_fallback(
        ["the-elder-scrolls-online"],
        set(),
        EMPTY_INVENTORY,
    )

    assert deadlines == {"the-elder-scrolls-online": datetime(2099, 1, 1)}
    assert "the-elder-scrolls-online" in twitch.twitchdrops_app_campaigns


def test_full_fallback_catalog_evaluates_every_indexed_game(monkeypatch):
    gql = SimpleNamespace(
        post_gql_request_raw=lambda operation, request: {
            "data": {"currentUser": {"availableBadges": []}}
        }
    )
    twitch = bare_twitch(gql)
    monkeypatch.setattr(
        TwitchDropsAppScraper,
        "scrape_front_page",
        lambda self: [
            {
                "slug": "urgent-game",
                "game": "Urgent Game",
                "url": "https://twitchdrops.app/game/urgent-game",
            },
            {
                "slug": "later-game",
                "game": "Later Game",
                "url": "https://twitchdrops.app/game/later-game",
            },
        ],
    )

    def scrape(self, category):
        slug = str(category).rstrip("/").rsplit("/", 1)[-1]
        deadline = {
            "urgent-game": "2050-01-01T00:00:00Z",
            "later-game": "2099-01-01T00:00:00Z",
        }[slug]
        return {
            "game": slug.replace("-", " ").title(),
            "campaigns": [
                {
                    "name": f"{slug} campaign",
                    "starts_at": "2020-01-01T00:00:00Z",
                    "ends_at": deadline,
                    "channels": [],
                    "drops": [{"name": f"{slug} reward"}],
                }
            ],
        }

    monkeypatch.setattr(TwitchDropsAppScraper, "scrape", scrape)

    deadlines = twitch._Twitch__twitchdrops_app_fallback(None, set(), EMPTY_INVENTORY)

    assert deadlines == {
        "urgent-game": datetime(2050, 1, 1),
        "later-game": datetime(2099, 1, 1),
    }
    assert twitch.twitchdrops_app_deadlines == deadlines
    assert twitch.twitchdrops_app_catalog_complete is True


def test_failed_full_fallback_catalog_is_still_marked_attempted(monkeypatch):
    gql = SimpleNamespace(
        post_gql_request_raw=lambda operation, request: {
            "data": {"currentUser": {"availableBadges": []}}
        }
    )
    twitch = bare_twitch(gql)
    monkeypatch.setattr(
        TwitchDropsAppScraper,
        "scrape_front_page",
        lambda self: (_ for _ in ()).throw(ValueError("unavailable")),
    )

    deadlines = twitch._Twitch__twitchdrops_app_fallback(None, set(), EMPTY_INVENTORY)

    assert deadlines == {}
    assert twitch.twitchdrops_app_deadlines == {}
    assert twitch.twitchdrops_app_catalog_complete is True


def test_current_campaign_award_completes_non_badge_fallback(monkeypatch):
    gql = SimpleNamespace(
        post_gql_request_raw=lambda operation, request: {
            "data": {"currentUser": {"availableBadges": []}}
        }
    )
    twitch = bare_twitch(gql)
    twitch.awarded_game_event_drops["reward-1"] = {
        "id": "reward-1",
        "name": "ATLS Foundation Livery",
        "lastAwardedAt": "2025-01-01T12:00:00Z",
    }
    monkeypatch.setattr(
        TwitchDropsAppScraper,
        "scrape_front_page",
        lambda self: [
            {
                "slug": "star-citizen",
                "game": "Star Citizen",
                "url": "https://twitchdrops.app/game/star-citizen",
                "starts_at": "2020-01-01T00:00:00Z",
                "ends_at": "2099-01-01T00:00:00Z",
            }
        ],
    )
    monkeypatch.setattr(
        TwitchDropsAppScraper,
        "scrape",
        lambda self, category: {
            "game": "Star Citizen",
            "campaigns": [
                {
                    "name": "Foundation Festival 2026",
                    "ends_at": "2099-01-01T00:00:00Z",
                    "channels": [],
                    "drops": [{"name": "ATLS Foundation Livery"}],
                }
            ],
        },
    )

    deadlines = twitch._Twitch__twitchdrops_app_fallback(
        ["star-citizen"], set(), EMPTY_INVENTORY
    )

    assert deadlines == {}
    assert twitch.twitchdrops_app_campaigns == {}


def test_completed_reward_campaign_signature_completes_fallback_despite_stale_award(
    monkeypatch,
):
    """A once-only cosmetic reward earned long before this campaign's gist-
    reported window must not keep the campaign "incomplete" forever:
    completedRewardCampaigns in the authenticated inventory is authoritative
    account-completion evidence and must short-circuit the lastAwardedAt/
    window heuristic __fallback_reward_was_awarded otherwise relies on.
    """
    gql = SimpleNamespace(
        post_gql_request_raw=lambda operation, request: {
            "data": {"currentUser": {"availableBadges": []}}
        }
    )
    twitch = bare_twitch(gql)
    # Earned well before this gist-reported campaign window -- on its own,
    # __fallback_reward_was_awarded would reject this as stale/unrelated.
    twitch.awarded_game_event_drops["reward-1"] = {
        "id": "reward-1",
        "name": "Corrupted Creeper Cape",
        "lastAwardedAt": "2020-01-01T12:00:00Z",
    }
    monkeypatch.setattr(
        TwitchDropsAppScraper,
        "scrape_front_page",
        lambda self: [
            {
                "slug": "minecraft",
                "game": "Minecraft",
                "url": "https://twitchdrops.app/game/minecraft",
                "starts_at": "2020-01-01T00:00:00Z",
                "ends_at": "2099-01-01T00:00:00Z",
            }
        ],
    )
    monkeypatch.setattr(
        TwitchDropsAppScraper,
        "scrape",
        lambda self, category: {
            "game": "Minecraft",
            "campaigns": [
                {
                    "name": "Corrupted Creeper Cape",
                    "ends_at": "2099-01-01T00:00:00Z",
                    "channels": [],
                    "drops": [{"name": "Corrupted Creeper Cape"}],
                }
            ],
        },
    )
    inventory = {
        "completedRewardCampaigns": [
            {
                "campaign": {
                    "name": "Corrupted Creeper Cape",
                    "endAt": "2099-01-01T00:00:00Z",
                    "game": {"displayName": "Minecraft"},
                }
            }
        ]
    }

    deadlines = twitch._Twitch__twitchdrops_app_fallback(
        ["minecraft"], set(), inventory
    )

    assert deadlines == {}
    assert twitch.twitchdrops_app_campaigns == {}


COMPLETED_MINECRAFT_INVENTORY = {
    "completedRewardCampaigns": [
        {
            "campaign": {
                "name": "Corrupted Creeper Cape",
                "endAt": "2099-01-01T00:00:00Z",
                "game": {"displayName": "Minecraft"},
            }
        }
    ]
}
PARTIAL_INVENTORY = {"dropCampaignsInProgress": [], "completedRewardCampaigns": None}


def _minecraft_gist(monkeypatch):
    """Point the gist scraper at one completed Minecraft campaign; return calls."""
    front_page_calls = []

    def scrape_front_page(self):
        front_page_calls.append(True)
        return [
            {
                "slug": "minecraft",
                "game": "Minecraft",
                "url": "https://twitchdrops.app/game/minecraft",
                "starts_at": "2020-01-01T00:00:00Z",
                "ends_at": "2099-01-01T00:00:00Z",
            }
        ]

    monkeypatch.setattr(TwitchDropsAppScraper, "scrape_front_page", scrape_front_page)
    monkeypatch.setattr(
        TwitchDropsAppScraper,
        "scrape",
        lambda self, category: {
            "game": "Minecraft",
            "campaigns": [
                {
                    "name": "Corrupted Creeper Cape",
                    "ends_at": "2099-01-01T00:00:00Z",
                    "channels": [],
                    "drops": [{"name": "Corrupted Creeper Cape"}],
                }
            ],
        },
    )
    return front_page_calls


def _badge_gql():
    return SimpleNamespace(
        post_gql_request_raw=lambda operation, request: {
            "data": {"currentUser": {"availableBadges": []}}
        }
    )


def test_drops_inventory_completions_loaded_requires_a_list():
    assert Twitch.drops_inventory_completions_loaded(COMPLETED_MINECRAFT_INVENTORY)
    assert Twitch.drops_inventory_completions_loaded({"completedRewardCampaigns": []})
    assert Twitch.drops_inventory_completions_loaded({}) is False
    assert Twitch.drops_inventory_completions_loaded(None) is False
    assert Twitch.drops_inventory_completions_loaded(PARTIAL_INVENTORY) is False
    assert (
        Twitch.drops_inventory_completions_loaded({"dropCampaignsInProgress": []})
        is False
    )


def _record(twitch, inventory):
    """What __get_inventory does with every inventory it loads."""
    twitch._Twitch__record_completed_badge_campaigns(inventory)
    return inventory


def test_drops_inventory_completions_state_tells_missing_from_errored():
    assert Twitch.drops_inventory_completions_state(None) == "unavailable"
    assert Twitch.drops_inventory_completions_state({}) == "unavailable"
    assert Twitch.drops_inventory_completions_state(EMPTY_INVENTORY) == "loaded"
    assert Twitch.drops_inventory_completions_state(PARTIAL_INVENTORY) == "errored"
    assert (
        Twitch.drops_inventory_completions_state({"dropCampaignsInProgress": []})
        == "missing"
    )


def test_completed_badge_campaign_signatures_or_cached_keeps_last_good_load():
    twitch = bare_twitch(SimpleNamespace())

    assert twitch.completed_badge_campaign_signatures_or_cached({}) is None

    signatures = twitch.completed_badge_campaign_signatures_or_cached(
        _record(twitch, COMPLETED_MINECRAFT_INVENTORY)
    )
    assert len(signatures) == 1
    assert twitch.completed_badge_campaign_signature_cache == signatures
    assert twitch.completed_badge_campaign_signatures_or_cached({}) == signatures
    assert (
        twitch.completed_badge_campaign_signatures_or_cached(PARTIAL_INVENTORY)
        == signatures
    )
    # A later good load that leaves the campaign out (for example because
    # its entry errored and was dropped) does not forget it: completing a
    # campaign is permanent, so the cache only grows.
    later = _record(twitch, {"completedRewardCampaigns": []})
    assert twitch.completed_badge_campaign_signatures_or_cached(later) == signatures
    assert twitch.completed_badge_campaign_signature_cache == signatures


def test_completed_badge_campaign_signatures_or_cached_is_read_only():
    """Only __get_inventory records completions, so consumers never re-parse
    and re-write the same inventory."""
    twitch = bare_twitch(SimpleNamespace())

    signatures = twitch.completed_badge_campaign_signatures_or_cached(
        COMPLETED_MINECRAFT_INVENTORY
    )

    assert len(signatures) == 1
    assert twitch.completed_badge_campaign_signature_cache is None


def test_completed_badge_campaign_signatures_or_cached_merges_new_completions():
    twitch = bare_twitch(SimpleNamespace())
    _record(twitch, COMPLETED_MINECRAFT_INVENTORY)
    another = _record(
        twitch,
        {
            "completedRewardCampaigns": [
                {
                    "campaign": {
                        "name": "Another",
                        "endAt": "2099-02-01T00:00:00Z",
                        "game": {"displayName": "Fortnite"},
                    }
                }
            ]
        },
    )

    merged = twitch.completed_badge_campaign_signatures_or_cached(another)

    assert len(merged) == 2
    assert ("fortnite", "another", 4073587200.0) in merged
    # Callers get a copy, so changing it cannot change the cache.
    merged.clear()
    assert len(twitch.completed_badge_campaign_signature_cache) == 2


def test_missing_completions_field_falls_back_without_standing_down():
    twitch = bare_twitch(SimpleNamespace())
    missing = {"dropCampaignsInProgress": []}

    # Never loaded: proceed with no evidence rather than stand down forever.
    assert twitch.completed_badge_campaign_signatures_or_cached(missing) == set()
    _record(twitch, COMPLETED_MINECRAFT_INVENTORY)
    assert len(twitch.completed_badge_campaign_signatures_or_cached(missing)) == 1


@pytest.mark.parametrize("unusable_inventory", [{}, PARTIAL_INVENTORY])
def test_gist_fallback_keeps_suppressing_completed_campaign_from_cached_inventory(
    monkeypatch, unusable_inventory
):
    """The gist matcher shares the badge check's completion evidence, so a
    failed or partial inventory after one good load must not resurrect a
    campaign the account already completed."""
    twitch = bare_twitch(_badge_gql())
    front_page_calls = _minecraft_gist(monkeypatch)

    assert (
        twitch._Twitch__twitchdrops_app_fallback(
            ["minecraft"], set(), _record(twitch, COMPLETED_MINECRAFT_INVENTORY)
        )
        == {}
    )
    deadlines = twitch._Twitch__twitchdrops_app_fallback(
        ["minecraft"], set(), unusable_inventory
    )

    assert deadlines == {}
    assert twitch.twitchdrops_app_campaigns == {}
    assert len(front_page_calls) == 2


@pytest.mark.parametrize("prior_load", [False, True])
@pytest.mark.parametrize("unusable_inventory", [{}, PARTIAL_INVENTORY])
def test_inventory_reader_keeps_gist_completion_cache_current(
    monkeypatch, prior_load, unusable_inventory
):
    gql = _badge_gql()
    gql.get_inventory = lambda: SimpleNamespace(
        inventory=copy.deepcopy(COMPLETED_MINECRAFT_INVENTORY), errors=[]
    )
    twitch = bare_twitch(gql)
    twitch.completed_drop_campaigns = set()
    front_page_calls = _minecraft_gist(monkeypatch)
    if prior_load:
        _record(twitch, EMPTY_INVENTORY)

    # Background campaign sync uses this reader without running badge or gist
    # discovery. Its completion evidence must still reach both consumers.
    twitch._Twitch__get_inventory()

    def unavailable():
        if not unusable_inventory:
            raise RetryError("Inventory", [])
        # __get_inventory normalizes the dict in place, and a null list only
        # means "unavailable" when Twitch reports an error for it.
        return SimpleNamespace(
            inventory=copy.deepcopy(unusable_inventory),
            errors=[
                SimpleNamespace(
                    message="service error",
                    path=["currentUser", "inventory", "completedRewardCampaigns"],
                )
            ],
        )

    gql.get_inventory = unavailable
    inventory = twitch.get_drops_inventory()
    deadlines = twitch._Twitch__twitchdrops_app_fallback(
        ["minecraft"], set(), inventory
    )

    assert deadlines == {}
    assert twitch.twitchdrops_app_campaigns == {}
    assert front_page_calls == [True]
    assert twitch.completed_badge_campaign_signature_cache == (
        twitch.completed_badge_campaign_signatures(COMPLETED_MINECRAFT_INVENTORY)
    )


def test_gist_fallback_stands_down_without_any_completion_evidence(monkeypatch, caplog):
    twitch = bare_twitch(_badge_gql())
    twitch.category_log_level = logging.INFO
    previous_deadlines = {"minecraft": datetime(2098, 1, 1)}
    twitch.twitchdrops_app_deadlines = dict(previous_deadlines)
    twitch.twitchdrops_app_campaigns = {"minecraft": ["previous cycle"]}
    front_page_calls = _minecraft_gist(monkeypatch)

    with caplog.at_level(logging.INFO):
        deadlines = twitch._Twitch__twitchdrops_app_fallback(
            ["minecraft"], set(), PARTIAL_INVENTORY
        )

    # Nothing was scraped and no stored deadline is merged as verified; the
    # stand-down is reported at the category log level.
    # The stored result may come from a run without completion evidence,
    # so none of it stays live.
    assert deadlines == {}
    assert twitch.twitchdrops_app_deadlines == {}
    assert twitch.twitchdrops_app_campaigns == {}
    assert twitch.twitchdrops_app_upcoming_starts == {}
    assert front_page_calls == []
    assert twitch.completed_badge_campaign_signature_cache is None
    skip_records = [
        record
        for record in caplog.records
        if "Skipping Twitch Drops gist check" in record.getMessage()
    ]
    assert [record.levelno for record in skip_records] == [logging.INFO]


def _inventory_twitch(monkeypatch, inventory, error_path):
    """A Twitch whose GQL returns `inventory` with one error at `error_path`."""
    response = SimpleNamespace(
        inventory=inventory,
        errors=[SimpleNamespace(message="service error", path=error_path)],
    )
    twitch = bare_twitch(SimpleNamespace(get_inventory=lambda: response))
    twitch.completed_drop_campaigns = set()
    monkeypatch.setattr(
        Twitch, "_Twitch__cache_drop_inventory_progress", lambda self, inv: None
    )
    return twitch


def test_inventory_error_on_completed_campaigns_list_marks_completions_unloaded(
    monkeypatch,
):
    twitch = _inventory_twitch(
        monkeypatch,
        {
            "dropCampaignsInProgress": [],
            "gameEventDrops": [],
            "completedRewardCampaigns": None,
        },
        ["currentUser", "inventory", "completedRewardCampaigns"],
    )

    loaded = twitch.get_drops_inventory()

    assert Twitch.drops_inventory_loaded(loaded) is True
    assert loaded["completedRewardCampaigns"] is None
    assert Twitch.drops_inventory_completions_loaded(loaded) is False
    assert twitch.completed_badge_campaign_signatures_or_cached(loaded) is None
    assert twitch.completed_drop_campaigns == set()


def test_inventory_error_in_one_completed_campaign_entry_preserves_ids(monkeypatch):
    """A persistent per-entry error (one stale campaign failing on every fetch)
    must not stop completions from counting as loaded, or badge discovery and
    gist matching would stand down forever. Partial entries still carry valid
    completion IDs even when their metadata cannot produce a signature."""
    surviving = {
        "campaign": {
            "id": "campaign-1",
            "name": "Corrupted Creeper Cape",
            "endAt": "2099-01-01T00:00:00Z",
            "game": {"displayName": "Minecraft"},
        }
    }
    partial = {"campaign": {"id": "campaign-2", "name": "Stale", "game": None}}
    twitch = _inventory_twitch(
        monkeypatch,
        {
            "dropCampaignsInProgress": [],
            "gameEventDrops": [],
            "completedRewardCampaigns": [surviving, partial],
        },
        ["currentUser", "inventory", "completedRewardCampaigns", 1, "campaign", "game"],
    )

    loaded = twitch.get_drops_inventory()

    assert loaded["completedRewardCampaigns"] == [surviving, partial]
    assert Twitch.drops_inventory_completions_loaded(loaded) is True
    signatures = twitch.completed_badge_campaign_signatures_or_cached(loaded)
    assert {game_slug for game_slug, _, _ in signatures} == {"minecraft"}
    assert twitch.completed_badge_campaign_signature_cache == signatures
    assert twitch.completed_drop_campaigns == {"campaign-1", "campaign-2"}


def test_earned_viewer_reward_drop_completes_fallback_despite_expired_campaign_status(
    monkeypatch,
):
    """Some Drops grant a redemption code rather than the item directly
    (e.g. Minecraft cape promos). Once earned, Twitch can report the
    campaign's own top-level status as "EXPIRED" (a calendar/global status)
    while dropCampaignsInProgress, gameEventDrops and completedRewardCampaigns
    from the regular Inventory query carry no record of it at all. Only the
    ViewerRewardDropInventory query's per-reward-group self.status /
    self.earnedReward reflects that this account actually finished it, and
    that must still complete the gist-fallback campaign.
    """

    def gql_post(operation_name, request_json):
        if operation_name == "ViewerRewardDropInventory":
            return {
                "data": {
                    "currentUser": {
                        "inventory": {
                            "viewerRewardDropCampaignsInProgress": [
                                {
                                    "id": "931cf994-acae-45e6-8796-dbc04e98371d",
                                    "status": "EXPIRED",
                                    "name": "Corrupted Creeper Cape",
                                    "rewardGroups": [
                                        {
                                            "self": {
                                                "status": "CLAIMABLE",
                                                "earnedReward": {
                                                    "name": "Corrupted Creeper Cape",
                                                },
                                            },
                                        }
                                    ],
                                }
                            ]
                        }
                    }
                }
            }
        return {"data": {"currentUser": {"availableBadges": []}}}

    gql = SimpleNamespace(post_gql_request_raw=gql_post)
    twitch = bare_twitch(gql)
    monkeypatch.setattr(
        TwitchDropsAppScraper,
        "scrape_front_page",
        lambda self: [
            {
                "slug": "minecraft",
                "game": "Minecraft",
                "url": "https://twitchdrops.app/game/minecraft",
                "starts_at": "2020-01-01T00:00:00Z",
                "ends_at": "2099-01-01T00:00:00Z",
            }
        ],
    )
    monkeypatch.setattr(
        TwitchDropsAppScraper,
        "scrape",
        lambda self, category: {
            "game": "Minecraft",
            "campaigns": [
                {
                    "name": "Corrupted Creeper Cape",
                    "ends_at": "2099-01-01T00:00:00Z",
                    "channels": [],
                    "drops": [{"name": "Corrupted Creeper Cape"}],
                }
            ],
        },
    )

    deadlines = twitch._Twitch__twitchdrops_app_fallback(
        ["minecraft"], set(), EMPTY_INVENTORY
    )

    assert deadlines == {}
    assert twitch.twitchdrops_app_campaigns == {}


def test_unearned_viewer_reward_drop_does_not_complete_fallback_campaign(monkeypatch):
    """A reward group still IN_PROGRESS (no earnedReward yet) must not be
    mistaken for a completed one."""

    def gql_post(operation_name, request_json):
        if operation_name == "ViewerRewardDropInventory":
            return {
                "data": {
                    "currentUser": {
                        "inventory": {
                            "viewerRewardDropCampaignsInProgress": [
                                {
                                    "id": "931cf994-acae-45e6-8796-dbc04e98371d",
                                    "status": "ACTIVE",
                                    "name": "Corrupted Creeper Cape",
                                    "rewardGroups": [
                                        {
                                            "self": {
                                                "status": "IN_PROGRESS",
                                                "earnedReward": None,
                                            },
                                        }
                                    ],
                                }
                            ]
                        }
                    }
                }
            }
        return {"data": {"currentUser": {"availableBadges": []}}}

    gql = SimpleNamespace(post_gql_request_raw=gql_post)
    twitch = bare_twitch(gql)
    monkeypatch.setattr(
        TwitchDropsAppScraper,
        "scrape_front_page",
        lambda self: [
            {
                "slug": "minecraft",
                "game": "Minecraft",
                "url": "https://twitchdrops.app/game/minecraft",
                "starts_at": "2020-01-01T00:00:00Z",
                "ends_at": "2099-01-01T00:00:00Z",
            }
        ],
    )
    monkeypatch.setattr(
        TwitchDropsAppScraper,
        "scrape",
        lambda self, category: {
            "game": "Minecraft",
            "campaigns": [
                {
                    "name": "Corrupted Creeper Cape",
                    "ends_at": "2099-01-01T00:00:00Z",
                    "channels": [],
                    "drops": [{"name": "Corrupted Creeper Cape"}],
                }
            ],
        },
    )

    deadlines = twitch._Twitch__twitchdrops_app_fallback(
        ["minecraft"], set(), EMPTY_INVENTORY
    )

    assert "minecraft" in deadlines
    assert twitch.twitchdrops_app_campaigns != {}


def test_old_same_named_award_does_not_complete_new_fallback_campaign():
    twitch = bare_twitch(SimpleNamespace())
    twitch.awarded_game_event_drops["old-reward"] = {
        "name": "Repeatable Crate",
        "lastAwardedAt": "2026-07-01T12:00:00Z",
    }
    campaign = {
        "starts_at": "2026-08-01T00:00:00Z",
        "ends_at": "2026-08-31T23:59:59Z",
    }

    assert (
        twitch._Twitch__fallback_reward_was_awarded("Repeatable Crate", campaign)
        is False
    )


def test_persisted_captured_drop_completes_fallback_campaign(monkeypatch, tmp_path):
    (tmp_path / "drops_by_category.json").write_text(
        json.dumps(
            {
                "drops": [
                    {
                        "category": "Star Citizen",
                        "campaign": "Foundation Festival 2026",
                        "item_name": "ATLS Foundation Livery",
                        "status": "captured",
                        "drop_end_at": "2026-08-12T19:59:59Z",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(Settings, "enable_analytics", True)
    monkeypatch.setattr(Settings, "analytics_path", str(tmp_path), raising=False)
    twitch = bare_twitch(SimpleNamespace())
    campaign = {
        "name": "Foundation Festival 2026",
        "ends_at": "2026-08-12T19:59:59Z",
    }

    captured = twitch._Twitch__captured_drop_history()

    assert twitch._Twitch__fallback_reward_was_captured(
        "ATLS Foundation Livery", campaign, "Star Citizen", captured
    )


def test_captured_drop_from_old_campaign_end_does_not_complete_fallback():
    twitch = bare_twitch(SimpleNamespace())
    campaign = {
        "name": "Recurring Campaign",
        "ends_at": "2026-08-31T23:59:59Z",
    }
    captured = [
        {
            "category": "Example Game",
            "campaign": "Recurring Campaign",
            "item_name": "Repeatable Crate",
            "status": "captured",
            "drop_end_at": "2026-07-31T23:59:59Z",
        }
    ]

    assert not twitch._Twitch__fallback_reward_was_captured(
        "Repeatable Crate", campaign, "Example Game", captured
    )


def test_captured_drop_without_end_does_not_complete_fallback():
    twitch = bare_twitch(SimpleNamespace())
    campaign = {
        "name": "Recurring Campaign",
        "ends_at": "2026-08-31T23:59:59Z",
    }
    captured = [
        {
            "category": "Example Game",
            "campaign": "Recurring Campaign",
            "item_name": "Repeatable Crate",
            "status": "captured",
            "drop_end_at": None,
        }
    ]

    assert not twitch._Twitch__fallback_reward_was_captured(
        "Repeatable Crate", campaign, "Example Game", captured
    )


def test_campaign_without_end_does_not_use_captured_drop():
    twitch = bare_twitch(SimpleNamespace())
    campaign = {"name": "Recurring Campaign", "ends_at": None}
    captured = [
        {
            "category": "Example Game",
            "campaign": "Recurring Campaign",
            "item_name": "Repeatable Crate",
            "status": "captured",
            "drop_end_at": "2026-08-31T23:59:59Z",
        }
    ]

    assert not twitch._Twitch__fallback_reward_was_captured(
        "Repeatable Crate", campaign, "Example Game", captured
    )


def test_future_campaign_in_active_report_is_not_mined_early(monkeypatch):
    gql = SimpleNamespace(
        post_gql_request_raw=lambda operation, request: {
            "data": {"currentUser": {"availableBadges": []}}
        }
    )
    twitch = bare_twitch(gql)
    monkeypatch.setattr(
        TwitchDropsAppScraper,
        "scrape_front_page",
        lambda self: [
            {
                "slug": "minecraft",
                "game": "Minecraft",
                "url": "https://twitchdrops.app/game/minecraft",
            }
        ],
    )
    monkeypatch.setattr(
        TwitchDropsAppScraper,
        "scrape",
        lambda self, category: {
            "game": "Minecraft",
            "campaigns": [
                {
                    "name": "Boss Run Marathon",
                    "starts_at": "2098-12-31T00:00:00Z",
                    "ends_at": "2099-01-01T00:00:00Z",
                    "channels": ["example"],
                    "drops": [{"name": "Frog Hoodie"}],
                }
            ],
        },
    )

    deadlines = twitch._Twitch__twitchdrops_app_fallback(
        ["minecraft"], set(), EMPTY_INVENTORY
    )

    assert deadlines == {}
    assert twitch.twitchdrops_app_campaigns == {}
    assert twitch.next_upcoming_drop_start() == datetime(2098, 12, 31)


def test_twitchdrops_app_skips_detail_requests_for_twitch_games(
    monkeypatch,
):
    twitch = bare_twitch(
        SimpleNamespace(
            post_gql_request_raw=lambda operation, request: {
                "data": {"currentUser": {"availableBadges": []}}
            }
        )
    )
    detail_requests = []
    monkeypatch.setattr(
        TwitchDropsAppScraper,
        "scrape_front_page",
        lambda self: [
            {
                "slug": "path-of-exile",
                "game": "Path of Exile",
                "url": "https://twitchdrops.app/game/path-of-exile",
                "starts_at": "2099-01-01T00:00:00Z",
                "upcoming": True,
            }
        ],
    )

    def scrape_detail(self, category):
        detail_requests.append(category)
        return {"game": "Path of Exile", "campaigns": []}

    monkeypatch.setattr(TwitchDropsAppScraper, "scrape", scrape_detail)

    known_slugs = {"path-of-exile"}
    twitch._Twitch__twitchdrops_app_fallback(
        ["path-of-exile", "not-on-front-page"],
        known_slugs,
        EMPTY_INVENTORY,
    )

    assert detail_requests == []
    assert known_slugs == {"path-of-exile"}
    assert twitch.next_upcoming_drop_start() == datetime(2099, 1, 1)


def test_game_prefixed_badge_name_matches_campaign_benefit():
    matcher = Twitch._Twitch__reward_name_is_owned

    assert matcher(
        "Android Triangle",
        {"detroit android triangle"},
        "Detroit: Become Human",
    )


def test_unrelated_prefixed_badge_name_does_not_match_campaign_benefit():
    matcher = Twitch._Twitch__reward_name_is_owned

    assert not matcher(
        "Android Triangle",
        {"unrelated android triangle"},
        "Detroit: Become Human",
    )


def test_campaign_qualified_final_fantasy_badges_match_rewards():
    matcher = Twitch._Twitch__reward_name_is_owned
    badges = {
        "final fantasy xiv fan festival 2026 eu - content unlock quest chat",
        "final fantasy xiv fan festival 2026 eu - moogle chat",
    }

    assert matcher("content unlock chat", badges, "Final Fantasy XIV Online")
    assert matcher("moogle chat", badges, "Final Fantasy XIV Online")


def test_campaign_qualified_unrelated_badge_does_not_match_reward():
    matcher = Twitch._Twitch__reward_name_is_owned

    assert not matcher(
        "Moogle Chat",
        {"unrelated fan festival 2026 eu - moogle chat"},
        "Final Fantasy XIV Online",
    )


def test_twitchdrops_app_keeps_campaigns_twitch_did_not_report(monkeypatch):
    """Twitch reporting one (completed) Rust campaign must not hide the other
    channel-restricted Rust campaigns only the external catalog lists."""
    twitch = bare_twitch(
        SimpleNamespace(
            post_gql_request_raw=lambda operation, request: {
                "data": {"currentUser": {"availableBadges": []}}
            }
        )
    )
    twitch.twitch_campaign_names_by_game = {"rust": {"rust isles facemask"}}
    monkeypatch.setattr(
        TwitchDropsAppScraper,
        "scrape_front_page",
        lambda self: [
            {
                "slug": "rust",
                "game": "Rust",
                "url": "https://twitchdrops.app/game/rust",
                "starts_at": "2020-01-01T00:00:00Z",
                "ends_at": "2099-01-01T00:00:00Z",
            }
        ],
    )
    monkeypatch.setattr(
        TwitchDropsAppScraper,
        "scrape",
        lambda self, category: {
            "game": "Rust",
            "campaigns": [
                {
                    "name": "Rust Isles  Facemask",
                    "ends_at": "2099-01-01T00:00:00Z",
                    "channels": ["facemask-streamer"],
                    "drops": [{"name": "Rust Isles Facemask"}],
                },
                {
                    "name": "Rust Isles Tac Gloves",
                    "ends_at": "2098-01-01T00:00:00Z",
                    "channels": ["gloves-streamer"],
                    "drops": [{"name": "Rust Isles Tac Gloves"}],
                },
            ],
        },
    )

    known_slugs = {"rust"}
    deadlines = twitch._Twitch__twitchdrops_app_fallback(
        ["rust"], known_slugs, EMPTY_INVENTORY
    )

    assert deadlines == {"rust": datetime(2098, 1, 1)}
    assert [
        campaign["name"] for campaign in twitch.twitchdrops_app_campaigns["rust"]
    ] == ["Rust Isles Tac Gloves"]
    assert twitch.twitchdrops_app_campaign_filtered_slugs == {"rust"}


ERRORED_GAME_PATH = [
    "currentUser",
    "inventory",
    "completedRewardCampaigns",
    0,
    "campaign",
    "game",
]


def test_errored_game_still_suppresses_completed_gist_campaign(monkeypatch):
    """A campaign that completes while its entry's game keeps erroring was
    never in the cache, so the remaining fields must identify it: the game
    becomes a wildcard, the end time never does."""
    twitch = _inventory_twitch(
        monkeypatch,
        {
            "dropCampaignsInProgress": [],
            "gameEventDrops": [],
            "completedRewardCampaigns": [
                {
                    "campaign": {
                        "id": "campaign-1",
                        "name": "Corrupted Creeper Cape",
                        "endAt": "2099-01-01T00:00:00Z",
                        "game": None,
                    }
                }
            ],
        },
        ERRORED_GAME_PATH,
    )
    twitch.gql.post_gql_request_raw = _badge_gql().post_gql_request_raw
    front_page_calls = _minecraft_gist(monkeypatch)

    inventory = twitch.get_drops_inventory()
    signatures = twitch.completed_badge_campaign_signatures_or_cached(inventory)

    assert signatures == {(None, "corrupted creeper cape", 4070908800.0)}
    # The wildcard is for this read only; the permanent cache stays exact.
    assert twitch.completed_badge_campaign_signature_cache == set()
    assert (
        twitch._Twitch__twitchdrops_app_fallback(["minecraft"], set(), inventory)
        == {}
    )
    assert front_page_calls == [True]


def test_partial_signature_matches_badge_catalog_campaign():
    from TwitchChannelPointsMiner.classes.DropBadgeCatalog import DropBadgeCatalog

    ends_at = datetime.fromisoformat("2099-01-01T00:00:00+00:00")
    record = {"game_slug": "minecraft"}
    campaign = {"name": "Corrupted Creeper Cape"}

    assert DropBadgeCatalog._matches_completed_campaign(
        record, campaign, ends_at, {(None, "corrupted creeper cape", 4070908800.0)}
    )
    # Only the game may be a wildcard: a game's sibling campaigns can end
    # together, so the game and end time alone identify nothing.
    assert not DropBadgeCatalog._matches_completed_campaign(
        record, campaign, ends_at, {("minecraft", None, 4070908800.0)}
    )
    # A different end time is a different run of the campaign.
    assert not DropBadgeCatalog._matches_completed_campaign(
        record, campaign, ends_at, {(None, "corrupted creeper cape", 4070908801.5)}
    )


def test_errored_entry_reuses_signature_from_earlier_full_read():
    twitch = bare_twitch(SimpleNamespace())
    _record(
        twitch,
        {
            "completedRewardCampaigns": [
                {
                    "campaign": {
                        "id": "campaign-1",
                        "name": "Corrupted Creeper Cape",
                        "endAt": "2099-01-01T00:00:00Z",
                        "game": {"displayName": "Minecraft"},
                    }
                }
            ]
        },
    )
    twitch.completed_badge_campaign_signature_cache = set()

    later = _record(
        twitch, {"completedRewardCampaigns": [{"campaign": {"id": "campaign-1"}}]}
    )

    assert twitch.completed_badge_campaign_signatures_or_cached(later) == {
        ("minecraft", "corrupted creeper cape", 4070908800.0)
    }
    assert getattr(twitch, "completed_badge_campaign_unsignable_count", 0) == 0


def test_entries_without_end_time_are_reported_once_and_never_wildcarded(caplog):
    twitch = bare_twitch(SimpleNamespace())
    inventory = {
        "completedRewardCampaigns": [
            None,
            {"campaign": {"id": "x", "name": "Rerun", "game": None}},
            # Over already: cannot match an active campaign, so not reported.
            {"campaign": {"name": "Old", "endAt": "2001-01-01T00:00:00Z"}},
        ]
    }

    with caplog.at_level(logging.DEBUG):
        _record(twitch, inventory)
        _record(twitch, inventory)

    assert twitch.completed_badge_campaign_signatures_or_cached(inventory) == set()
    assert twitch.completed_badge_campaign_unsignable_count == 2
    warnings = [
        record
        for record in caplog.records
        if "lack the campaign name or end time" in record.getMessage()
    ]
    assert [record.levelno for record in warnings] == [logging.WARNING]


def test_errors_without_completions_field_mark_it_errored(monkeypatch):
    """An error on a parent path can drop the field; that must not read as a
    response that no longer carries it."""
    twitch = _inventory_twitch(
        monkeypatch,
        {"dropCampaignsInProgress": [], "gameEventDrops": []},
        ["currentUser", "inventory"],
    )

    loaded = twitch.get_drops_inventory()

    assert Twitch.drops_inventory_completions_state(loaded) == "errored"
    assert twitch.completed_badge_campaign_signatures_or_cached(loaded) is None


@pytest.mark.parametrize(
    "response",
    [None, SimpleNamespace(inventory=None, errors=[])],
    ids=["no-response", "null-inventory"],
)
def test_inventory_without_a_dict_is_a_failed_load(monkeypatch, response):
    twitch = bare_twitch(SimpleNamespace(get_inventory=lambda: response))
    twitch.completed_drop_campaigns = set()

    assert twitch.get_drops_inventory() == {}


def test_gist_stand_down_is_not_repeated_by_wildcard_pass(monkeypatch, caplog):
    twitch = bare_twitch(_badge_gql())
    twitch.category_log_level = logging.INFO
    twitch.twitchdrops_app_catalog_complete = False
    _minecraft_gist(monkeypatch)
    calls = []
    original = Twitch._Twitch__twitchdrops_app_fallback

    def counting(self, *args):
        calls.append(args[0])
        return original(self, *args)

    monkeypatch.setattr(Twitch, "_Twitch__twitchdrops_app_fallback", counting)
    monkeypatch.setattr(
        Twitch,
        "_Twitch__active_drop_category_slugs_from_campaigns",
        lambda self, inventory, requested: ({}, set()),
    )

    with caplog.at_level(logging.DEBUG):
        twitch.filter_categories_with_active_drops(
            ["minecraft"], inventory=PARTIAL_INVENTORY
        )
        twitch.get_wildcard_categories_with_active_drops(
            inventory=PARTIAL_INVENTORY, refresh_external_catalog=False
        )

    # The wildcard pass checks again rather than reusing a stored result,
    # but the stand-down is reported once per streak.
    assert calls == [["minecraft"], None]
    assert [
        record.levelno
        for record in caplog.records
        if "Skipping Twitch Drops gist check" in record.getMessage()
    ] == [logging.INFO, logging.DEBUG]


def _normalized(inventory, *error_paths):
    twitch = bare_twitch(
        SimpleNamespace(
            get_inventory=lambda: SimpleNamespace(
                inventory=inventory,
                errors=[
                    SimpleNamespace(message="service error", path=path)
                    for path in error_paths
                ],
            )
        )
    )
    twitch.completed_drop_campaigns = set()
    return twitch.get_drops_inventory()


def test_null_completions_without_an_error_read_as_missing():
    """An account with no completions gets [], so an error-free null proves
    nothing: it must not seed the cache as "nothing completed", but it must
    not stand discovery down forever either."""
    twitch = bare_twitch(
        SimpleNamespace(
            get_inventory=lambda: SimpleNamespace(
                inventory={
                    "dropCampaignsInProgress": [],
                    "completedRewardCampaigns": None,
                },
                errors=[],
            )
        )
    )
    twitch.completed_drop_campaigns = set()

    loaded = twitch.get_drops_inventory()

    assert Twitch.drops_inventory_completions_state(loaded) == "missing"
    assert twitch.completed_badge_campaign_signature_cache is None
    assert twitch.completed_badge_campaign_signatures_or_cached(loaded) == set()
    # A later errored read still stands down instead of trusting the null.
    assert twitch.completed_badge_campaign_signatures_or_cached(PARTIAL_INVENTORY) is None


def test_optional_errors_keep_an_absent_completions_field_missing():
    loaded = _normalized(
        {"dropCampaignsInProgress": []}, ["currentUser", "inventory", "gameEventDrops"]
    )

    assert Twitch.drops_inventory_completions_state(loaded) == "missing"


@pytest.mark.parametrize(
    "error_path",
    [
        None,
        ["currentUser", "inventory", "gameEventDrops"],
        ["currentUser", "inventory", "completedRewardCampaigns"],
    ],
)
def test_errors_keep_a_delivered_completions_list(error_path):
    """Completion IDs in a delivered list stay usable for category discovery
    whatever the errors say."""
    loaded = _normalized(
        {"dropCampaignsInProgress": [], **COMPLETED_MINECRAFT_INVENTORY}, error_path
    )

    assert loaded["completedRewardCampaigns"] == (
        COMPLETED_MINECRAFT_INVENTORY["completedRewardCampaigns"]
    )


def test_pathless_error_marks_an_absent_completions_field_errored():
    loaded = _normalized({"dropCampaignsInProgress": []}, None)

    assert Twitch.drops_inventory_completions_state(loaded) == "errored"


def test_errored_name_does_not_suppress_sibling_campaign_of_same_game():
    """A game can run a restricted and an all-channel campaign that end
    together; an entry with only its game and end time must not mark both
    complete."""
    twitch = bare_twitch(SimpleNamespace())
    inventory = _record(
        twitch,
        {
            "completedRewardCampaigns": [
                {
                    "campaign": {
                        "id": "restricted",
                        "name": None,
                        "endAt": "2099-01-01T00:00:00Z",
                        "game": {"displayName": "Minecraft"},
                    }
                }
            ]
        },
    )

    assert twitch.completed_badge_campaign_signatures_or_cached(inventory) == set()
    assert twitch.completed_badge_campaign_unsignable_count == 1


def test_continuing_gist_stand_down_is_repeated_hourly(monkeypatch, caplog):
    import TwitchChannelPointsMiner.classes.Twitch as twitch_module

    clock = [1000.0]
    monkeypatch.setattr(twitch_module.time, "monotonic", lambda: clock[0])
    twitch = bare_twitch(_badge_gql())
    twitch.category_log_level = logging.INFO
    _minecraft_gist(monkeypatch)

    with caplog.at_level(logging.DEBUG):
        twitch._Twitch__twitchdrops_app_fallback(["minecraft"], set(), {})
        clock[0] += twitch_module.GIST_STAND_DOWN_REMINDER_SECONDS - 1
        twitch._Twitch__twitchdrops_app_fallback(["minecraft"], set(), {})
        clock[0] += 1
        twitch._Twitch__twitchdrops_app_fallback(["minecraft"], set(), {})
        # Evidence arrives: the streak ends, so the next stand-down is new.
        twitch._Twitch__twitchdrops_app_fallback(
            ["minecraft"], set(), _record(twitch, EMPTY_INVENTORY)
        )
        twitch.completed_badge_campaign_signature_cache = None
        twitch._Twitch__twitchdrops_app_fallback(["minecraft"], set(), {})

    assert [
        record.levelno
        for record in caplog.records
        if "Skipping Twitch Drops gist check" in record.getMessage()
    ] == [logging.INFO, logging.DEBUG, logging.INFO, logging.INFO]


def test_unsignable_warning_repeats_only_for_a_new_streak_or_high(
    monkeypatch, caplog
):
    import TwitchChannelPointsMiner.classes.Twitch as twitch_module

    clock = [1000.0]
    monkeypatch.setattr(twitch_module.time, "monotonic", lambda: clock[0])
    hour = twitch_module.COMPLETED_BADGE_UNSIGNABLE_WARNING_SECONDS
    twitch = bare_twitch(SimpleNamespace())
    clean = {"completedRewardCampaigns": []}
    one = {"completedRewardCampaigns": [None]}
    two = {"completedRewardCampaigns": [None, None]}

    def levels(*steps):
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            for advance, inventory in steps:
                clock[0] += advance
                _record(twitch, inventory)
        return [
            record.levelno
            for record in caplog.records
            if "lack the campaign name or end time" in record.getMessage()
        ]

    # First failure warns; a higher count within the hour does not, so
    # entries failing on alternate reads cannot warn on every read.
    assert levels((0, one), (1, two), (1, one), (1, two)) == [logging.WARNING]
    # After the hour the higher count warns once; the same count never again.
    assert levels((hour, two), (hour, two), (hour, one)) == [logging.WARNING]
    # A clean read ends the streak, so a later failure warns again.
    assert levels((0, clean), (hour, one)) == [logging.WARNING]


def test_completions_list_with_every_entry_errored_reads_as_errored():
    """GraphQL nulls a failed list item; a list of nothing but such nulls
    says no more than a null list and must not read as "nothing completed"."""
    path = ["currentUser", "inventory", "completedRewardCampaigns"]
    loaded = _normalized(
        {"dropCampaignsInProgress": [], "completedRewardCampaigns": [None, None]},
        path + [0],
        path + [1],
    )

    assert Twitch.drops_inventory_completions_state(loaded) == "errored"


@pytest.mark.parametrize(
    "error_path",
    [None, ["currentUser"], ["currentUser", "inventory", "completedRewardCampaigns"]],
)
def test_empty_completions_list_stays_loaded_despite_errors(error_path):
    """A list that failed to resolve comes back null, so [] resolved."""
    loaded = _normalized(
        {"dropCampaignsInProgress": [], "completedRewardCampaigns": []}, error_path
    )

    assert Twitch.drops_inventory_completions_state(loaded) == "loaded"


def test_completions_list_keeps_surviving_entries_and_errorless_nulls():
    path = ["currentUser", "inventory", "completedRewardCampaigns"]
    partly_errored = {
        "dropCampaignsInProgress": [],
        "completedRewardCampaigns": [
            None,
            *COMPLETED_MINECRAFT_INVENTORY["completedRewardCampaigns"],
        ],
    }
    errorless_nulls = {
        "dropCampaignsInProgress": [],
        "completedRewardCampaigns": [None],
    }

    assert (
        Twitch.drops_inventory_completions_state(
            _normalized(partly_errored, path + [0])
        )
        == "loaded"
    )
    assert (
        Twitch.drops_inventory_completions_state(_normalized(errorless_nulls))
        == "loaded"
    )
