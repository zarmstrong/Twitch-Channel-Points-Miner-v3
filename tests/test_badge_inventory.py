import logging
from types import SimpleNamespace

from TwitchChannelPointsMiner.classes.Twitch import Twitch
from TwitchChannelPointsMiner.classes.gql.Errors import RetryError
from twitchdrops_app_scraper import TwitchDropsAppScraper


def bare_twitch(gql):
    twitch = object.__new__(Twitch)
    twitch.gql = gql
    twitch.available_badge_names = None
    twitch.twitchdrops_app_campaigns = {}
    twitch.log_drop_checks = False
    twitch.category_log_level = logging.DEBUG
    return twitch


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
        ["two-point-museum"], set(), set()
    )

    assert deadlines == {}
    assert twitch.twitchdrops_app_campaigns == {}


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


def test_channel_campaign_discovery_accepts_uninitialized_campaign_cache(monkeypatch):
    twitch = bare_twitch(
        SimpleNamespace(
            get_id_from_login=lambda login: SimpleNamespace(id="channel-1"),
            post_gql_request_raw=lambda operation, request: {
                "data": {
                    "channel": {
                        "viewerDropCampaigns": [
                            {
                                "id": "campaign-1",
                                "name": "Detroit Badge Drop",
                                "game": {"name": "Detroit: Become Human"},
                                "timeBasedDrops": [],
                            }
                        ]
                    }
                }
            },
        )
    )
    twitch.discovered_open_drop_campaigns = None
    twitch.category_campaign_eligibility = {}
    monkeypatch.setattr(
        Twitch,
        "get_live_streamers_for_category",
        lambda self, category, drops_enabled, limit: ["streamer"],
    )
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_available_badge_names",
        lambda self, refresh: set(),
    )
    monkeypatch.setattr(Twitch, "_Twitch__log_category", lambda *args, **kwargs: None)

    twitch._Twitch__channel_advertised_campaign_fallback(
        ["detroit-become-human"], set()
    )

    assert [
        campaign["id"] for campaign in twitch.discovered_open_drop_campaigns
    ] == ["campaign-1"]
