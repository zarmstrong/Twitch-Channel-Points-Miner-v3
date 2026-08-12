import json
import logging
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
    twitch.twitchdrops_app_campaigns = {}
    twitch.twitchdrops_app_upcoming_starts = {}
    twitch.log_drop_checks = False
    twitch.category_log_level = logging.DEBUG
    twitch.category_campaign_eligibility = {}
    twitch.awarded_game_event_drops = {}
    return twitch


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

    usernames = twitch._Twitch__get_live_restricted_campaign_streamers(
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

    usernames = twitch._Twitch__get_live_restricted_campaign_streamers(
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

    usernames = twitch._Twitch__get_live_restricted_campaign_streamers(
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

    usernames = twitch._Twitch__get_live_restricted_campaign_streamers(
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
        ["two-point-museum"], set()
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
    )

    assert deadlines == {"the-elder-scrolls-online": datetime(2099, 1, 1)}
    assert "the-elder-scrolls-online" in twitch.twitchdrops_app_campaigns


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

    deadlines = twitch._Twitch__twitchdrops_app_fallback(["star-citizen"], set())

    assert deadlines == {}
    assert twitch.twitchdrops_app_campaigns == {}


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


def test_old_same_named_award_with_matching_art_completes_fallback_campaign():
    twitch = bare_twitch(SimpleNamespace())
    twitch.awarded_game_event_drops["frog-hoodie"] = {
        "name": "Frog Hoodie",
        "imageURL": "https://example.com/frog-hoodie.png",
        "lastAwardedAt": "2026-07-01T12:00:00Z",
    }
    campaign = {
        "starts_at": "2026-08-01T00:00:00Z",
        "ends_at": "2026-08-31T23:59:59Z",
    }

    assert twitch._Twitch__fallback_reward_was_awarded(
        "Frog Hoodie",
        campaign,
        "https://example.com/frog-hoodie.png",
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

    deadlines = twitch._Twitch__twitchdrops_app_fallback(["minecraft"], set())

    assert deadlines == {}
    assert twitch.twitchdrops_app_campaigns == {}
    assert twitch.next_upcoming_drop_start() == datetime(2098, 12, 31)


def test_twitchdrops_app_front_page_filters_detail_requests_even_for_twitch_games(
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
    )

    assert detail_requests == ["https://twitchdrops.app/game/path-of-exile"]
    assert known_slugs == {"path-of-exile"}


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
