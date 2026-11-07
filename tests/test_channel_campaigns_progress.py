import logging
from types import SimpleNamespace

from TwitchChannelPointsMiner.classes.entities.Campaign import (
    CampaignChannelScope,
    campaign_channel_scope,
    campaign_from_channel_progress,
    campaign_includes_watch_requirement,
)
from TwitchChannelPointsMiner.classes.gql.Errors import (
    GQLResponseErrors,
    RetryError,
    is_persisted_query_not_found,
)
from TwitchChannelPointsMiner.classes.gql.data.response.Error import Error
from TwitchChannelPointsMiner.classes.gql.data.Parser import Parser
from TwitchChannelPointsMiner.classes.Twitch import Twitch
from TwitchChannelPointsMiner.utils.AttemptStrategy import ExceptionContext

START_AT = "2026-10-22T16:00:00Z"
END_AT = "2026-11-12T16:59:59.999Z"


def _watch_group(group_id, minutes, reward_id):
    return {
        "id": group_id,
        "name": f"Tier {minutes}",
        "track": None,
        "lane": None,
        "progressCriteria": {
            "requirementType": "WATCH",
            "requirements": {
                "minutesWatched": minutes,
                "subs": None,
                "turboSubs": None,
            },
            "isRepeatable": False,
            "channels": None,
            "categories": [{"id": "game-1", "displayName": "Example Game"}],
        },
        "rewards": [
            {
                "id": reward_id,
                "name": f"Reward {minutes}",
                "distributionType": "DIRECT_ENTITLEMENT",
                "isAccountConnected": True,
            }
        ],
        "self": {
            "status": "IN_PROGRESS",
            "currentMinutesWatched": None,
            "currentSubs": 0,
        },
    }


def _game_wide_campaign():
    return {
        "id": "campaign-game-wide",
        "name": "Synthetic Game Campaign",
        "startAt": START_AT,
        "endAt": END_AT,
        "game": {"id": "game-1", "name": "Example Game", "__typename": "Game"},
        "rewardGroups": [
            _watch_group("group-60", 60, "reward-60"),
            _watch_group("group-120", 120, "reward-120"),
            _watch_group("group-240", 240, "reward-240"),
        ],
    }


def _sub_badge_campaign():
    return {
        "id": "campaign-sub",
        "name": "Synthetic Sub Badge",
        "startAt": START_AT,
        "endAt": END_AT,
        "game": None,
        "rewardGroups": [
            {
                "id": "group-sub",
                "name": "",
                "track": None,
                "lane": None,
                "progressCriteria": {
                    "requirementType": "SUB",
                    "requirements": {"minutesWatched": None, "subs": 1},
                    "isRepeatable": False,
                    "channels": [{"id": "100000001", "__typename": "User"}],
                    "categories": [],
                },
                "rewards": [
                    {
                        "id": "reward-sub",
                        "name": "Synthetic Badge",
                        "distributionType": "DIRECT_ENTITLEMENT",
                        "isAccountConnected": True,
                    }
                ],
                "self": {
                    "status": "IN_PROGRESS",
                    "currentMinutesWatched": None,
                    "currentSubs": 0,
                },
            }
        ],
    }


def _track_group(group_id="group-track"):
    group = _watch_group(group_id, 30, "reward-track")
    group["track"] = {"id": "track-1"}
    return group


def _lane_group(group_id="group-lane"):
    group = _watch_group(group_id, 30, "reward-lane")
    group["lane"] = {"id": "lane-1"}
    return group


def _repeatable_group(group_id="group-repeat"):
    group = _watch_group(group_id, 30, "reward-repeat")
    group["progressCriteria"]["isRepeatable"] = True
    return group


def _track_only_campaign():
    campaign = _game_wide_campaign()
    campaign["id"] = "campaign-track-only"
    campaign["rewardGroups"] = [_track_group()]
    return campaign


def test_game_wide_watch_campaign_converts_every_tier():
    result = campaign_from_channel_progress(_game_wide_campaign())

    assert result["id"] == "campaign-game-wide"
    assert result["name"] == "Synthetic Game Campaign"
    assert result["status"] == "ACTIVE"
    assert result["game"] == {
        "id": "game-1",
        "name": "Example Game",
        "__typename": "Game",
    }
    assert result["startAt"] == START_AT
    assert result["endAt"] == END_AT
    assert "allow" not in result
    assert result["summary"] == {
        "includesMWRequirement": True,
        "includesSubRequirement": False,
    }
    drops = result["timeBasedDrops"]
    assert [drop["requiredMinutesWatched"] for drop in drops] == [60, 120, 240]
    assert [drop["requiredSubs"] for drop in drops] == [0, 0, 0]
    assert [drop["id"] for drop in drops] == ["group-60", "group-120", "group-240"]
    assert drops[0]["name"] == "Tier 60"
    assert drops[0]["startAt"] == START_AT
    assert drops[0]["endAt"] == END_AT
    assert drops[0]["benefitEdges"] == [
        {
            "benefit": {
                "id": "reward-60",
                "name": "Reward 60",
                "distributionType": "DIRECT_ENTITLEMENT",
            }
        }
    ]
    for drop in drops:
        assert "self" not in drop


def test_sub_badge_campaign_is_restricted_to_its_channel():
    result = campaign_from_channel_progress(_sub_badge_campaign())

    assert result["game"] == {}
    assert result["allow"] == {"channels": [{"id": "100000001"}]}
    assert result["summary"] == {
        "includesMWRequirement": False,
        "includesSubRequirement": True,
    }
    [drop] = result["timeBasedDrops"]
    assert drop["name"] == "Synthetic Sub Badge"
    assert drop["requiredSubs"] == 1
    assert drop["requiredMinutesWatched"] == 0
    assert "self" not in drop


def test_track_lane_and_repeatable_groups_are_skipped():
    campaign = _game_wide_campaign()
    campaign["rewardGroups"] = [
        _track_group(),
        _lane_group(),
        _repeatable_group(),
        _watch_group("group-kept", 90, "reward-kept"),
    ]

    result = campaign_from_channel_progress(campaign)

    assert [drop["id"] for drop in result["timeBasedDrops"]] == ["group-kept"]


def test_groups_with_other_requirement_types_are_skipped():
    other = _watch_group("group-other", None, "reward-other")
    other["progressCriteria"]["requirementType"] = "CHEER"
    missing = _watch_group("group-missing", None, "reward-missing")
    del missing["progressCriteria"]["requirementType"]
    campaign = _game_wide_campaign()
    campaign["rewardGroups"] = [
        other,
        missing,
        _watch_group("group-kept", 30, "reward-kept"),
    ]

    result = campaign_from_channel_progress(campaign)

    # A kept CHEER group would read as a zero-minute badge.
    assert [drop["id"] for drop in result["timeBasedDrops"]] == ["group-kept"]

    campaign["rewardGroups"] = [other, missing]
    assert campaign_from_channel_progress(campaign) is None


def test_campaign_with_only_skipped_groups_returns_none():
    campaign = _game_wide_campaign()
    campaign["rewardGroups"] = [
        _track_group(),
        _lane_group(),
        _repeatable_group(),
    ]

    assert campaign_from_channel_progress(campaign) is None


def test_bad_date_or_missing_id_returns_none():
    bad_date = _game_wide_campaign()
    bad_date["startAt"] = "not-a-date"
    assert campaign_from_channel_progress(bad_date) is None

    bad_end = _game_wide_campaign()
    bad_end["endAt"] = "2026-13-45T00:00:00Z"
    assert campaign_from_channel_progress(bad_end) is None

    missing_id = _game_wide_campaign()
    del missing_id["id"]
    assert campaign_from_channel_progress(missing_id) is None

    empty_end = _game_wide_campaign()
    empty_end["endAt"] = ""
    assert campaign_from_channel_progress(empty_end) is None

    assert campaign_from_channel_progress(None) is None


def test_converted_campaign_without_channel_list_has_unknown_scope():
    # Twitch sends channels: null for channel-restricted campaigns as well as
    # game-wide ones, so a null list must not read as open to every channel.
    result = campaign_from_channel_progress(_game_wide_campaign())

    assert campaign_channel_scope(result) == (CampaignChannelScope.UNKNOWN, [])


def test_converted_campaign_with_empty_channel_list_has_unknown_scope():
    campaign = _game_wide_campaign()
    for group in campaign["rewardGroups"]:
        group["progressCriteria"]["channels"] = []

    result = campaign_from_channel_progress(campaign)

    assert "allow" not in result
    assert campaign_channel_scope(result) == (CampaignChannelScope.UNKNOWN, [])


def test_converted_campaign_keeps_a_remembered_allow_list():
    twitch = object.__new__(Twitch)
    twitch.known_campaign_scopes = {
        "campaign-game-wide": (CampaignChannelScope.RESTRICTED, ("allowedchannel",))
    }
    result = campaign_from_channel_progress(_game_wide_campaign())

    assert twitch._Twitch__known_campaign_scope("campaign-game-wide", result) == (
        CampaignChannelScope.RESTRICTED,
        ["allowedchannel"],
    )


def test_converted_sub_badge_scope_is_unknown_without_logins():
    result = campaign_from_channel_progress(_sub_badge_campaign())

    assert campaign_channel_scope(result) == (CampaignChannelScope.UNKNOWN, [])


def test_sub_badge_has_no_watch_requirement_summary():
    result = campaign_from_channel_progress(_sub_badge_campaign())

    assert campaign_includes_watch_requirement(result) is False


def test_parser_converts_channel_progress_and_drops_skipped_items():
    response = Parser().parse_drop_channel_campaigns_progress(
        {
            "data": {
                "channelDropCampaignsProgress": [
                    _game_wide_campaign(),
                    _track_only_campaign(),
                ]
            },
            "extensions": {"operationName": "DropChannelCampaignsProgress"},
        }
    )

    assert response.ids == ["campaign-game-wide"]
    assert len(response.campaigns) == 1
    assert response.campaigns[0]["id"] == "campaign-game-wide"
    assert response.campaigns_available is True


def test_parser_distinguishes_null_channel_progress():
    response = Parser().parse_drop_channel_campaigns_progress(
        {
            "data": {"channelDropCampaignsProgress": None},
            "extensions": {"operationName": "DropChannelCampaignsProgress"},
        }
    )

    assert response.ids == []
    assert response.campaigns == []
    assert response.campaigns_available is False


def _twitch_with_gql(gql):
    twitch = Twitch.__new__(Twitch)
    twitch.campaign_lookup_unavailable = False
    twitch.unevaluated_category_slugs = set()
    twitch.gql = gql
    twitch.log_drop_checks = False
    return twitch


def _listing_response(campaign_id):
    return SimpleNamespace(
        campaigns=[{"id": campaign_id, "listing": True}], campaigns_available=True
    )


def test_channel_campaigns_prefer_channel_progress_over_listing():
    progress_campaigns = [{"id": "progress-campaign"}]
    listing_calls = []

    def listing(channel_id):
        listing_calls.append(channel_id)
        return _listing_response("listing-campaign")

    twitch = _twitch_with_gql(
        SimpleNamespace(
            get_channel_drop_campaigns_progress=lambda channel_id: SimpleNamespace(
                campaigns=progress_campaigns, campaigns_available=True
            ),
            get_available_drops=listing,
        )
    )

    result = twitch._Twitch__get_campaigns_from_channel_id("1")

    assert result == (progress_campaigns, True, True)
    assert listing_calls == []


def test_channel_campaigns_fall_back_to_listing_on_retry_error():
    twitch = _twitch_with_gql(
        SimpleNamespace(
            get_channel_drop_campaigns_progress=lambda channel_id: (
                _raise(RetryError("DropChannelCampaignsProgress", []))
            ),
            get_available_drops=lambda channel_id: _listing_response(
                "listing-campaign"
            ),
        )
    )

    campaigns, available, answered = twitch._Twitch__get_campaigns_from_channel_id("1")

    assert campaigns == [{"id": "listing-campaign", "listing": True}]
    assert available is True
    assert answered is True


def test_channel_campaigns_fall_back_to_listing_when_progress_has_no_list():
    twitch = _twitch_with_gql(
        SimpleNamespace(
            get_channel_drop_campaigns_progress=lambda channel_id: SimpleNamespace(
                campaigns=[], campaigns_available=False
            ),
            get_available_drops=lambda channel_id: _listing_response(
                "listing-campaign"
            ),
        )
    )

    campaigns, available, answered = twitch._Twitch__get_campaigns_from_channel_id("1")

    assert campaigns == [{"id": "listing-campaign", "listing": True}]
    assert available is True
    assert answered is True


def test_channel_campaigns_use_listing_when_gql_lacks_progress_method():
    twitch = _twitch_with_gql(
        SimpleNamespace(
            get_available_drops=lambda channel_id: _listing_response(
                "listing-campaign"
            ),
        )
    )

    campaigns, available, answered = twitch._Twitch__get_campaigns_from_channel_id("1")

    assert campaigns == [{"id": "listing-campaign", "listing": True}]
    assert available is True
    assert answered is True


def test_channel_campaigns_log_progress_failure_at_debug(caplog):
    twitch = _twitch_with_gql(
        SimpleNamespace(
            get_channel_drop_campaigns_progress=lambda channel_id: (
                _raise(RetryError("DropChannelCampaignsProgress", []))
            ),
            get_available_drops=lambda channel_id: _listing_response(
                "listing-campaign"
            ),
        )
    )
    twitch.log_drop_checks = True

    with caplog.at_level(logging.DEBUG):
        twitch._Twitch__get_campaigns_from_channel_id("1")

    assert "unable to load channel campaign progress for 1" in caplog.text


def _raise(error):
    raise error


def _persisted_query_not_found():
    return RetryError(
        "DropChannelCampaignsProgress",
        [
            ExceptionContext(
                GQLResponseErrors(
                    "DropChannelCampaignsProgress",
                    [Error(False, "PersistedQueryNotFound")],
                ),
                None,
            )
        ],
    )


def test_persisted_query_not_found_is_detected_inside_a_retry_error():
    assert is_persisted_query_not_found(_persisted_query_not_found())
    assert not is_persisted_query_not_found(
        RetryError(
            "DropChannelCampaignsProgress",
            [
                ExceptionContext(
                    GQLResponseErrors(
                        "DropChannelCampaignsProgress",
                        [Error(True, "service timeout")],
                    ),
                    None,
                )
            ],
        )
    )
    assert not is_persisted_query_not_found(RetryError("x", []))
    assert not is_persisted_query_not_found(ValueError("PersistedQueryNotFound"))


def test_channel_progress_query_stops_after_persisted_query_not_found(caplog):
    progress_calls = []

    def progress(channel_id):
        progress_calls.append(channel_id)
        raise _persisted_query_not_found()

    twitch = _twitch_with_gql(
        SimpleNamespace(
            get_channel_drop_campaigns_progress=progress,
            get_available_drops=lambda channel_id: _listing_response(
                "listing-campaign"
            ),
        )
    )
    twitch.channel_progress_query_retired = False

    with caplog.at_level(logging.WARNING):
        first = twitch._Twitch__get_campaigns_from_channel_id("1")
        second = twitch._Twitch__get_campaigns_from_channel_id("2")

    listing = [{"id": "listing-campaign", "listing": True}]
    assert first == (listing, True, True)
    assert second == (listing, True, True)
    assert progress_calls == ["1"]
    assert twitch.channel_progress_query_retired is True
    assert caplog.text.count("no longer recognizes") == 1


def test_channel_progress_query_keeps_running_after_other_errors():
    progress_calls = []

    def progress(channel_id):
        progress_calls.append(channel_id)
        raise RetryError("DropChannelCampaignsProgress", [])

    twitch = _twitch_with_gql(
        SimpleNamespace(
            get_channel_drop_campaigns_progress=progress,
            get_available_drops=lambda channel_id: _listing_response(
                "listing-campaign"
            ),
        )
    )
    twitch.channel_progress_query_retired = False

    twitch._Twitch__get_campaigns_from_channel_id("1")
    twitch._Twitch__get_campaigns_from_channel_id("2")

    assert progress_calls == ["1", "2"]
    assert twitch.channel_progress_query_retired is False
