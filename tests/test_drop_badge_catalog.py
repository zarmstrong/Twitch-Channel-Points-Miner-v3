import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from TwitchChannelPointsMiner.classes.DropBadgeCatalog import (
    DropBadgeCatalog,
    badge_match_reason,
    flatten_badges,
)


class FakeResponse:
    def __init__(self, payload=None):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload if self.payload is not None else {
            "sets": [
                {
                    "set_id": "example-badge",
                    "versions": [{"id": "1", "title": "Example Badge"}],
                }
            ]
        }


class FakeSession:
    def __init__(self, payload=None):
        self.calls = 0
        self.payload = payload

    def get(self, *args, **kwargs):
        self.calls += 1
        return FakeResponse(self.payload)


class FakeScraper:
    def __init__(self):
        self.games = [
            {
                "slug": "example",
                "game": "Example Game",
                "url": "https://twitchdrops.app/game/example",
                "starts_at": "2026-01-01T00:00:00Z",
                "ends_at": "2026-01-02T00:00:00Z",
                "upcoming": False,
                "drop_count": 1,
            }
        ]
        self.scrape_calls = 0

    def scrape_front_page(self):
        return copy.deepcopy(self.games)

    def scrape(self, url):
        self.scrape_calls += 1
        drop = {
            "name": "Example Badge",
            "requirement": "Watch 1h",
            "campaign": "Campaign",
            "image_url": "reward.png",
        }
        return {
            "source": url,
            "game": "Example Game",
            "campaigns": [
                {
                    "id": f"campaign-{self.games[0]['drop_count']}",
                    "name": "Campaign",
                    "drops": [copy.deepcopy(drop)],
                }
            ],
            "upcoming_campaigns": [],
            "non_watch_campaigns": [],
            "drops": [drop],
        }


def test_flatten_badges_preserves_set_and_version_attributes():
    badges = flatten_badges(
        [
            {
                "set_id": "example",
                "versions": [
                    {
                        "id": "1",
                        "title": "Example Badge",
                        "description": "Example description",
                    }
                ],
            }
        ]
    )

    assert badges == [
        {
            "set_id": "example",
            "id": "1",
            "title": "Example Badge",
            "description": "Example description",
        }
    ]


def test_fetch_badges_rejects_non_object_json(tmp_path):
    catalog = DropBadgeCatalog(
        SimpleNamespace(),
        tmp_path,
        scraper=FakeScraper(),
        session=FakeSession([]),
    )

    with pytest.raises(ValueError, match="did not contain a JSON object"):
        catalog._fetch_badges()


def test_badge_matching_accepts_safe_title_variants():
    assert (
        badge_match_reason("Blue LED", "Example Game", "Blue LED")
        == "exact_title"
    )
    assert (
        badge_match_reason("Blue LED Badge", "Example Game", "Blue LED")
        == "exact_title_ignoring_badge_suffix"
    )
    assert (
        badge_match_reason(
            "Android Triangle",
            "Detroit: Become Human",
            "Detroit Android Triangle",
        )
        == "game_prefixed_badge_title"
    )


def test_badge_matching_rejects_unrelated_badge_words():
    assert (
        badge_match_reason(
            "Badge of Glory Emote",
            "Mobile Legends",
            "Glory Tournament Badge",
        )
        is None
    )


def test_sync_persists_catalog_and_only_scrapes_changed_games(tmp_path):
    scraper = FakeScraper()
    session = FakeSession()
    catalog = DropBadgeCatalog(
        SimpleNamespace(get_auth_token=lambda: "token"),
        tmp_path,
        scraper=scraper,
        session=session,
    )

    first = catalog.sync()
    second = catalog.sync()
    scraper.games[0]["drop_count"] = 2
    third = catalog.sync()

    assert first["scraped_games"] == 1
    assert first["confirmed_badge_rewards"] == 1
    assert len(first["new_campaigns"]) == 1
    assert first["new_campaigns"][0]["campaign"]["drops"][0][
        "badge_classification"
    ]["status"] == "BADGE"
    assert second["scraped_games"] == 0
    assert second["new_campaigns"] == []
    assert third["scraped_games"] == 1
    assert len(third["new_campaigns"]) == 1
    assert scraper.scrape_calls == 2
    assert session.calls == 1
    assert (tmp_path / "drop_badge_catalog.json").is_file()


def test_eligible_badge_campaigns_only_returns_active_unearned_watch_badges(
    tmp_path,
):
    catalog = DropBadgeCatalog(
        SimpleNamespace(get_auth_token=lambda: "token"),
        tmp_path,
        scraper=FakeScraper(),
        session=FakeSession(),
    )
    now = datetime.now(timezone.utc)
    badge_drop = {
        "name": "Example Badge",
        "requirement": "Watch 1h",
        "badge_classification": {"status": "BADGE"},
    }
    catalog.state["campaigns"] = {
        "eligible": {
            "game_slug": "example-game",
            "game": "Example Game",
            "source_group": "campaigns",
            "campaign": {
                "starts_at": (now - timedelta(hours=1)).isoformat(),
                "ends_at": (now + timedelta(hours=1)).isoformat(),
                "drops": [copy.deepcopy(badge_drop)],
            },
        },
        "upcoming": {
            "game_slug": "example-game",
            "game": "Example Game",
            "source_group": "campaigns",
            "campaign": {
                "starts_at": (now + timedelta(hours=1)).isoformat(),
                "ends_at": (now + timedelta(hours=2)).isoformat(),
                "drops": [copy.deepcopy(badge_drop)],
            },
        },
        "subscriber": {
            "game_slug": "example-game",
            "game": "Example Game",
            "source_group": "non_watch_campaigns",
            "campaign": {"drops": [copy.deepcopy(badge_drop)]},
        },
    }

    eligible = catalog.eligible_badge_campaigns()
    owned = catalog.eligible_badge_campaigns({"Example Badge"})

    assert [record["game_slug"] for record in eligible] == ["example-game"]
    assert eligible[0]["eligible_drops"][0]["name"] == "Example Badge"
    assert owned == []
