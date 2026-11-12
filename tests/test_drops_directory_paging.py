"""Paging of the Drops-enabled game directory (#211)."""

from types import SimpleNamespace

import pytest

from TwitchChannelPointsMiner.classes.Exceptions import CategoryLookupFailedException
from TwitchChannelPointsMiner.classes.gql.Errors import RetryError

from tests.test_badge_inventory import bare_twitch


def directory_page(logins, has_next_page, cursor_prefix="c", game_id=None):
    game = {
        "streams": {
            "edges": [
                {
                    "cursor": f"{cursor_prefix}{index}",
                    "node": {"broadcaster": {"login": login}},
                }
                for index, login in enumerate(logins)
            ],
            "pageInfo": {"hasNextPage": has_next_page},
        }
    }
    if game_id is not None:
        game["id"] = game_id
    return {"data": {"game": game}}


def paged_gql(pages):
    """Answer each request with the next page; record the cursors sent."""
    cursors = []
    remaining = list(pages)

    def post_gql_request_raw(operation, request):
        assert operation == "DirectoryPage_Game"
        cursors.append(request["variables"].get("cursor"))
        page = remaining.pop(0)
        if isinstance(page, Exception):
            raise page
        return page

    return SimpleNamespace(post_gql_request_raw=post_gql_request_raw), cursors


def test_directory_reads_later_pages_and_merges_them():
    gql, cursors = paged_gql(
        [
            directory_page(["first", "shared"], True, "a"),
            directory_page(["shared", "Beyond"], False, "b"),
        ]
    )

    logins, complete = bare_twitch(gql)._Twitch__get_drops_enabled_directory("game")

    assert logins == {"first", "shared", "beyond"}
    assert cursors == [None, "a1"]
    # Twitch re-ranks between pages, so a merged list is never proof that
    # every Drops-enabled channel was seen.
    assert complete is False


def test_single_page_directory_is_still_complete():
    gql, cursors = paged_gql([directory_page(["only"], False)])

    assert bare_twitch(gql)._Twitch__get_drops_enabled_directory("game") == (
        {"only"},
        True,
    )
    assert cursors == [None]


def test_directory_paging_stops_at_the_page_cap(monkeypatch):
    monkeypatch.setattr(
        "TwitchChannelPointsMiner.classes.Twitch.DROPS_DIRECTORY_MAX_PAGES", 3
    )
    gql, cursors = paged_gql(
        [directory_page([f"p{page}"], True, f"p{page}-") for page in range(5)]
    )

    logins, complete = bare_twitch(gql)._Twitch__get_drops_enabled_directory("game")

    assert logins == {"p0", "p1", "p2"}
    assert len(cursors) == 3
    assert complete is False


def test_failed_later_page_keeps_channels_already_found():
    gql, _ = paged_gql(
        [
            directory_page(["first"], True, "a"),
            RetryError("DirectoryPage_Game", []),
        ]
    )

    assert bare_twitch(gql)._Twitch__get_drops_enabled_directory("game") == (
        {"first"},
        False,
    )


def test_later_page_with_errors_and_missing_entries_is_ignored():
    broken = directory_page(["kept", ""], True, "b")
    broken["errors"] = [{"message": "service timeout"}]
    gql, _ = paged_gql([directory_page(["first"], True, "a"), broken])

    assert bare_twitch(gql)._Twitch__get_drops_enabled_directory("game") == (
        {"first"},
        False,
    )


def test_later_page_for_another_game_is_ignored():
    gql, _ = paged_gql(
        [
            directory_page(["first"], True, "a", game_id="42"),
            directory_page(["stranger"], False, "b", game_id="99"),
        ]
    )
    twitch = bare_twitch(gql)
    twitch.game_directory_slugs = {"42": "game"}

    logins, complete = twitch._Twitch__get_drops_enabled_directory("game", game_id="42")

    assert logins == {"first"}
    assert complete is False


def test_repeated_cursor_stops_paging():
    gql, cursors = paged_gql(
        [
            directory_page(["first"], True, "same"),
            directory_page(["second"], True, "same"),
            directory_page(["never"], False, "x"),
        ]
    )

    logins, _ = bare_twitch(gql)._Twitch__get_drops_enabled_directory("game")

    assert logins == {"first", "second"}
    assert len(cursors) == 2


def test_first_page_failure_still_raises():
    gql, _ = paged_gql([RetryError("DirectoryPage_Game", [])])

    with pytest.raises(CategoryLookupFailedException):
        bare_twitch(gql)._Twitch__get_drops_enabled_directory("game")
