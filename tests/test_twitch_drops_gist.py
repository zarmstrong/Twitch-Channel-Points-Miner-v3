import copy

import pytest

from TwitchChannelPointsMiner.classes.TwitchDropsApp import (
    DROPS_GIST_URL,
    TwitchDropsGistScraper,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return copy.deepcopy(self.payload)


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(self.payload)


def test_gist_scraper_fetches_once_and_returns_independent_data():
    payload = {
        "indexed_games": [
            {"slug": "example", "url": "https://twitchdrops.app/game/example"}
        ],
        "games": [
            {
                "source": "https://twitchdrops.app/game/example",
                "game": "Example",
                "campaigns": [],
            }
        ],
    }
    session = FakeSession(payload)
    scraper = TwitchDropsGistScraper(session=session)

    index = scraper.scrape_front_page()
    report = scraper.scrape("example")
    index[0]["slug"] = "changed"
    report["game"] = "Changed"

    assert scraper.scrape_front_page()[0]["slug"] == "example"
    assert scraper.scrape("https://twitchdrops.app/game/example")["game"] == "Example"
    assert len(session.calls) == 1
    assert session.calls[0][0] == DROPS_GIST_URL
    assert session.calls[0][1]["headers"]["Accept"] == "application/json"


def test_gist_scraper_rejects_missing_game():
    scraper = TwitchDropsGistScraper(
        session=FakeSession({"indexed_games": [], "games": []})
    )

    with pytest.raises(ValueError, match="does not contain game 'missing'"):
        scraper.scrape("missing")


def test_gist_scraper_skips_non_object_game_entries():
    scraper = TwitchDropsGistScraper(
        session=FakeSession(
            {
                "indexed_games": [],
                "games": [None, "invalid", {"source": "game/example"}],
            }
        )
    )

    with pytest.raises(ValueError, match="does not contain game 'missing'"):
        scraper.scrape("missing")
