import logging
from types import SimpleNamespace

from TwitchChannelPointsMiner.classes.Twitch import Twitch
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
