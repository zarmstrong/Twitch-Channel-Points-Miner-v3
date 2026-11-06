import pytest

from TwitchChannelPointsMiner.classes.Settings import Settings
from TwitchChannelPointsMiner.classes.entities.Campaign import (
    Campaign,
    campaign_includes_watch_requirement,
)
from TwitchChannelPointsMiner.classes.entities.CommunityGoal import CommunityGoal


def drop_data(drop_id="drop-1"):
    return {
        "id": drop_id,
        "name": "Reward",
        "benefitEdges": [{"benefit": {"name": "Badge"}}],
        "requiredMinutesWatched": 10,
        "startAt": "2020-01-01T00:00:00Z",
        "endAt": "2099-01-01T00:00:00Z",
    }


def campaign_data():
    return {
        "id": "campaign-1",
        "game": {"displayName": "Example Game"},
        "name": "Example Campaign",
        "status": "ACTIVE",
        "allow": {"channels": [{"id": "100"}, {"id": "200"}]},
        "startAt": "2020-01-01T00:00:00Z",
        "endAt": "2099-01-01T00:00:00Z",
        "timeBasedDrops": [drop_data(), drop_data("drop-2")],
    }


def test_campaign_extracts_channels_and_builds_drops():
    campaign = Campaign(campaign_data())

    assert campaign.channels == ["100", "200"]
    assert [drop.id for drop in campaign.drops] == ["drop-1", "drop-2"]
    assert campaign.dt_match is True


def test_campaign_str_uses_game_name_without_display_name(monkeypatch):
    # Twitch's channel listing (DropsHighlightService_AvailableDrops) names a
    # campaign's game with id and name only.
    monkeypatch.setattr(Settings.logger, "less", True)
    data = campaign_data()
    data["game"] = {"id": "500188", "name": "Hunt: Showdown 1896"}

    assert str(Campaign(data)) == (
        "Example Campaign, Game: Hunt: Showdown 1896 - Drops: 2 pcs. "
        "- In inventory: False"
    )


def test_campaign_treats_unrestricted_channels_as_empty_list():
    data = campaign_data()
    data["allow"]["channels"] = None

    assert Campaign(data).channels == []


def test_clear_drops_removes_claimed_and_inactive_drops():
    campaign = Campaign(campaign_data())
    campaign.drops[0].is_claimed = True
    campaign.drops[1].dt_match = False

    campaign.clear_drops()

    assert campaign.drops == []


def test_has_watchable_drops_reflects_progress_and_claim_state():
    campaign = Campaign(campaign_data())

    assert campaign.has_watchable_drops() is True

    campaign.drops[0].current_minutes_watched = 10
    campaign.drops[1].current_minutes_watched = 4
    assert campaign.has_watchable_drops() is True

    campaign.drops[1].current_minutes_watched = 10
    assert campaign.has_watchable_drops() is False

    campaign.drops[1].is_claimed = True
    campaign.drops[1].current_minutes_watched = 0
    assert campaign.has_watchable_drops() is False

    campaign.drops[1].is_claimed = False
    campaign.drops[1].dt_match = False
    assert campaign.has_watchable_drops() is False


def test_has_watchable_drops_treats_null_required_minutes_as_zero():
    # Twitch can return requiredMinutesWatched: null for some drops (other
    # call sites in Twitch.py defensively do `.get(...) or 0` on the same raw
    # field). Without a fallback, current_minutes_watched (0) < None raises
    # TypeError inside has_watchable_drops instead of treating the drop as
    # trivially already watched enough.
    data = campaign_data()
    data["timeBasedDrops"][0]["requiredMinutesWatched"] = None
    campaign = Campaign(data)

    assert campaign.drops[0].minutes_required == 0
    assert campaign.has_watchable_drops() is True

    campaign.drops[1].current_minutes_watched = 10
    assert campaign.has_watchable_drops() is False


def test_has_watchable_drops_skips_drops_needing_an_unmet_subscription():
    data = campaign_data()
    data["timeBasedDrops"] = [drop_data()]
    data["timeBasedDrops"][0]["requiredSubs"] = 1
    campaign = Campaign(data)

    assert campaign.has_watchable_drops() is False

    campaign.drops[0].current_subs = 1

    assert campaign.has_watchable_drops() is True


def test_has_watchable_drops_without_required_subs_is_unchanged():
    data = campaign_data()
    data["timeBasedDrops"] = [drop_data()]
    data["timeBasedDrops"][0]["requiredSubs"] = None

    assert Campaign(data).has_watchable_drops() is True


@pytest.mark.parametrize(
    "campaign, expected",
    [
        ({"summary": {"includesMWRequirement": True}}, True),
        ({"summary": {"includesMWRequirement": False}}, False),
        ({"summary": {"includesMWRequirement": None}}, None),
        ({"summary": {"includesMWRequirement": "false"}}, None),
        ({"summary": {}}, None),
        ({"summary": None}, None),
        ({}, None),
        (None, None),
    ],
)
def test_campaign_includes_watch_requirement(campaign, expected):
    assert campaign_includes_watch_requirement(campaign) is expected


def test_sync_drops_updates_matching_drop_and_invokes_claim_callback():
    campaign = Campaign(campaign_data())
    claimed = []

    def claim(drop, campaign):
        claimed.append((drop.id, campaign.id))
        return True

    campaign.sync_drops(
        [
            {
                "id": "drop-1",
                "self": {
                    "hasPreconditionsMet": True,
                    "currentMinutesWatched": 10,
                    "dropInstanceID": "instance-1",
                    "isClaimed": False,
                },
            },
            {"id": "unknown", "self": {}},
        ],
        claim,
    )

    assert claimed == [("drop-1", "campaign-1")]
    assert campaign.drops[0].is_claimed is True
    assert campaign.drops[0].current_minutes_watched == 10


def test_community_goal_parses_graphql_and_pubsub_shapes():
    gql = CommunityGoal.from_gql(
        {
            "id": "goal-1",
            "title": "Goal",
            "isInStock": True,
            "pointsContributed": 250,
            "amountNeeded": 1000,
            "perStreamUserMaximumContribution": 100,
            "status": "STARTED",
        }
    )
    pubsub = CommunityGoal.from_pubsub(
        {
            "id": "goal-1",
            "title": "Goal",
            "is_in_stock": True,
            "points_contributed": 250,
            "goal_amount": 1000,
            "per_stream_maximum_user_contribution": 100,
            "status": "STARTED",
        }
    )

    assert gql == pubsub
    assert gql.amount_left() == 750
