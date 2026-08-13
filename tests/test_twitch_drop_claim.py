import importlib
import logging
from types import SimpleNamespace

from TwitchChannelPointsMiner.classes.entities.Campaign import Campaign
from TwitchChannelPointsMiner.classes.Twitch import Twitch


def campaign_data():
    return {
        "id": "campaign-1",
        "game": {"displayName": "Example Game"},
        "name": "Example Campaign",
        "status": "ACTIVE",
        "allow": {"channels": []},
        "startAt": "2020-01-01T00:00:00Z",
        "endAt": "2099-01-01T00:00:00Z",
        "timeBasedDrops": [
            {
                "id": "drop-1",
                "name": "Reward",
                "benefitEdges": [{"benefit": {"name": "Badge"}}],
                "requiredMinutesWatched": 10,
                "startAt": "2020-01-01T00:00:00Z",
                "endAt": "2099-01-01T00:00:00Z",
            }
        ],
    }


def bare_twitch(monkeypatch, claim_status="ELIGIBLE_FOR_ALL"):
    twitch = object.__new__(Twitch)
    twitch.completed_drop_campaigns = set()
    twitch.campaign_game_slugs = {}
    twitch.log_drop_checks = False
    twitch.category_log_level = logging.INFO
    twitch.category_campaign_eligibility = {}
    twitch.evaluated_category_campaigns = set()
    twitch.twitchdrops_app_campaigns = {}
    twitch.advertised_drop_campaigns = {}
    twitch.campaign_channel_ids = {}
    twitch.campaign_detail_attempts = set()
    twitch.gql = SimpleNamespace(
        claim_drop_rewards=lambda drop_instance_id: SimpleNamespace(
            status=claim_status, errors=[]
        )
    )
    monkeypatch.setattr(
        Twitch, "_Twitch__drop_variant_entries_from_drop", lambda self, drop: []
    )
    return twitch


def advertised_campaign():
    campaign = campaign_data()
    campaign["timeBasedDrops"][0]["benefitEdges"] = [
        {
            "benefit": {
                "id": "reward-1",
                "name": "Reusable Reward",
                "imageAssetURL": "https://example.test/reward.png",
            }
        }
    ]
    return campaign


def category_streamer():
    return SimpleNamespace(
        username="drops-channel",
        channel_id="12345",
        from_category=True,
        from_badge_campaign=False,
        stream=SimpleNamespace(game_name=lambda: "Example Game"),
    )


def test_advertised_campaign_normalizes_null_allow(monkeypatch):
    twitch = bare_twitch(monkeypatch)
    campaign = advertised_campaign()
    campaign["allow"] = None

    normalized = twitch._Twitch__normalize_advertised_campaign(campaign)

    assert normalized["allow"] == {"channels": None}
    assert Campaign(normalized).channels == []


def test_channel_campaign_uses_broadcaster_id_and_in_window_award(monkeypatch):
    twitch = bare_twitch(monkeypatch)
    campaign = advertised_campaign()
    twitch.gql = SimpleNamespace(
        get_available_drops=lambda channel_id: SimpleNamespace(
            campaigns=[campaign], campaigns_available=True
        )
    )
    twitch.awarded_game_event_drops = {
        "reward-1": {
            "id": "reward-1",
            "name": "Reusable Reward",
            "imageURL": "https://example.test/reward.png",
            "lastAwardedAt": "2025-01-01T00:00:00Z",
        }
    }
    detail_contexts = []

    def campaign_details(self, campaigns, campaign_channel_id_by_id=None):
        detail_contexts.append(campaign_channel_id_by_id)
        return []

    monkeypatch.setattr(Twitch, "_Twitch__get_campaigns_details", campaign_details)

    assert twitch._Twitch__get_campaign_ids_from_streamer(category_streamer()) == [
        "campaign-1"
    ]
    assert detail_contexts == [{"campaign-1": "12345"}]
    assert twitch.category_campaign_eligibility[("example-game", "drops-channel")] == (
        0,
        1,
    )


def test_reused_reward_from_earlier_campaign_remains_incomplete(monkeypatch):
    twitch = bare_twitch(monkeypatch)
    campaign = advertised_campaign()
    twitch.gql = SimpleNamespace(
        get_available_drops=lambda channel_id: SimpleNamespace(
            campaigns=[campaign], campaigns_available=True
        )
    )
    twitch.awarded_game_event_drops = {
        "reward-1": {
            "id": "reward-1",
            "name": "Reusable Reward",
            "imageURL": "https://example.test/reward.png",
            "lastAwardedAt": "2019-12-31T23:59:59Z",
        }
    }
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_campaigns_details",
        lambda self, campaigns, campaign_channel_id_by_id=None: [],
    )

    twitch._Twitch__get_campaign_ids_from_streamer(category_streamer())

    assert twitch.category_campaign_eligibility[("example-game", "drops-channel")] == (
        1,
        1,
    )


def test_overlapping_campaigns_with_same_reward_remain_incomplete(monkeypatch):
    twitch = bare_twitch(monkeypatch)
    first_campaign = advertised_campaign()
    second_campaign = advertised_campaign()
    second_campaign["id"] = "campaign-2"
    second_campaign["name"] = "Second Campaign"
    second_campaign["timeBasedDrops"][0]["id"] = "drop-2"
    twitch.gql = SimpleNamespace(
        get_available_drops=lambda channel_id: SimpleNamespace(
            campaigns=[first_campaign, second_campaign], campaigns_available=True
        )
    )
    twitch.awarded_game_event_drops = {
        "reward-1": {
            "id": "reward-1",
            "name": "Reusable Reward",
            "imageURL": "https://example.test/reward.png",
            "lastAwardedAt": "2025-01-01T00:00:00Z",
        }
    }
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_campaigns_details",
        lambda self, campaigns, campaign_channel_id_by_id=None: [],
    )

    twitch._Twitch__get_campaign_ids_from_streamer(category_streamer())

    assert twitch.category_campaign_eligibility[("example-game", "drops-channel")] == (
        2,
        2,
    )


def test_authoritative_channel_campaign_result_blocks_wrong_game(monkeypatch):
    twitch = bare_twitch(monkeypatch)
    twitch.gql = SimpleNamespace(
        get_available_drops=lambda channel_id: SimpleNamespace(
            campaigns=[
                {
                    "id": "unrelated",
                    "game": {"name": "Special Events"},
                    "timeBasedDrops": [],
                }
            ],
            campaigns_available=True,
        )
    )
    twitch.discovered_open_drop_campaigns = [advertised_campaign()]

    assert twitch._Twitch__get_campaign_ids_from_streamer(category_streamer()) == []
    assert twitch.category_campaign_eligibility[("example-game", "drops-channel")] == (
        0,
        0,
    )


def test_claiming_final_drop_waits_for_inventory_confirmation(monkeypatch):
    twitch = bare_twitch(monkeypatch)
    campaign = Campaign(campaign_data())
    drop = campaign.drops[0]
    drop.drop_instance_id = "instance-1"

    assert twitch.claim_drop(drop, campaign=campaign) is True
    assert twitch.completed_drop_campaigns == set()


def test_completed_campaign_overrides_category_eligibility(monkeypatch):
    twitch = bare_twitch(monkeypatch)
    twitch.completed_drop_campaigns.add("campaign-1")
    twitch.category_campaign_eligibility[("example-game", "channel")] = (1, 1)
    stream = SimpleNamespace(
        campaigns_ids=["campaign-1"],
        game_name=lambda: "Example Game",
    )
    streamer = SimpleNamespace(
        username="channel",
        from_category=True,
        settings=SimpleNamespace(claim_drops=True),
        is_online=True,
        stream=stream,
    )

    assert twitch._Twitch__category_drops_condition(streamer) is False


def test_negative_category_refresh_does_not_resurrect_collected_fallback(monkeypatch):
    twitch = bare_twitch(monkeypatch)
    twitch.evaluated_category_campaigns.add("example-game")
    twitch.twitchdrops_app_campaigns["example-game"] = [
        {"name": "Collected campaign", "channels": []}
    ]
    stream = SimpleNamespace(
        campaigns_ids=[],
        game_name=lambda: "Example Game",
    )
    streamer = SimpleNamespace(
        username="stale-channel",
        from_category=True,
        from_badge_campaign=False,
        settings=SimpleNamespace(claim_drops=True),
        is_online=True,
        stream=stream,
    )

    assert twitch._Twitch__category_drops_condition(streamer) is False


def test_discovered_eligibility_applies_to_existing_configured_streamer(monkeypatch):
    twitch = bare_twitch(monkeypatch)
    twitch.evaluated_category_campaigns.add("example-game")
    twitch.category_campaign_eligibility[("example-game", "configured")] = (1, 1)
    stream = SimpleNamespace(
        campaigns_ids=[],
        campaigns=[],
        game_name=lambda: "Example Game",
    )
    streamer = SimpleNamespace(
        username="configured",
        from_category=False,
        from_badge_campaign=False,
        settings=SimpleNamespace(claim_drops=True),
        is_online=True,
        stream=stream,
        drops_condition=lambda: False,
    )

    assert twitch._Twitch__drops_condition(streamer) is True


def test_bulk_inventory_claim_waits_for_refreshed_inventory(monkeypatch):
    twitch = bare_twitch(monkeypatch)
    data = campaign_data()
    data["timeBasedDrops"][0]["self"] = {
        "hasPreconditionsMet": True,
        "currentMinutesWatched": 10,
        "dropInstanceID": "instance-1",
        "isClaimed": False,
    }
    inventory = {"dropCampaignsInProgress": [data]}
    monkeypatch.setattr(
        Twitch, "_Twitch__get_inventory", lambda self: inventory
    )
    twitch_module = importlib.import_module(
        "TwitchChannelPointsMiner.classes.Twitch"
    )
    monkeypatch.setattr(twitch_module.time, "sleep", lambda seconds: None)

    twitch.claim_all_drops_from_inventory()

    assert twitch.completed_drop_campaigns == set()


def test_completed_reward_campaign_ids_suppress_stale_campaigns(monkeypatch):
    twitch = bare_twitch(monkeypatch)
    inventory = {
        "completedRewardCampaigns": [
            {"id": "campaign-1"},
            {"campaign": {"id": "campaign-2"}},
        ]
    }

    completed = twitch._Twitch__completed_campaign_ids_from_inventory(inventory)
    twitch.completed_drop_campaigns.update(completed)

    assert twitch.completed_drop_campaigns == {"campaign-1", "campaign-2"}


def test_all_claimed_inventory_drops_confirm_campaign_completion(monkeypatch):
    twitch = bare_twitch(monkeypatch)
    data = campaign_data()
    data["timeBasedDrops"][0]["self"] = {
        "hasPreconditionsMet": True,
        "currentMinutesWatched": 10,
        "dropInstanceID": "instance-1",
        "isClaimed": True,
    }

    completed = twitch._Twitch__completed_campaign_ids_from_inventory(
        {"dropCampaignsInProgress": [data]}
    )

    assert completed == {"campaign-1"}


def test_completed_campaign_keeps_game_authoritative_after_twitch_removes_it(
    monkeypatch,
):
    twitch = bare_twitch(monkeypatch)
    sparse_campaign = campaign_data()
    sparse_campaign.pop("game")
    sparse_campaign.pop("timeBasedDrops")
    dashboard_campaigns = [sparse_campaign]
    inventory_campaign = campaign_data()
    inventory_campaign["timeBasedDrops"][0]["self"] = {
        "hasPreconditionsMet": True,
        "currentMinutesWatched": 0,
        "dropInstanceID": None,
        "isClaimed": False,
    }
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_drops_dashboard",
        lambda self, status="OPEN": dashboard_campaigns,
    )
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_reward_campaigns_raw_query",
        lambda self: ([], []),
    )
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_open_drop_campaigns_from_helix",
        lambda self: ([], []),
    )
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_campaigns_details",
        lambda self, campaigns: campaigns,
    )
    monkeypatch.setattr(
        Twitch,
        "_Twitch__awarded_benefits",
        lambda self, inventory: (set(), set()),
    )

    twitch._Twitch__active_drop_category_slugs_from_campaigns(
        {"dropCampaignsInProgress": [inventory_campaign]}, {"example-game"}
    )
    dashboard_campaigns.clear()

    deadlines, twitch_games = (
        twitch._Twitch__active_drop_category_slugs_from_campaigns(
            {
                "dropCampaignsInProgress": [],
                "completedRewardCampaigns": [{"id": "campaign-1"}],
            },
            {"example-game"},
        )
    )

    assert deadlines == {}
    assert twitch_games == {"example-game"}


def test_active_campaign_keeps_authenticated_channel_allowlist(monkeypatch):
    twitch = bare_twitch(monkeypatch)
    campaign = campaign_data()
    campaign["allow"] = {
        "channels": [
            {"id": "100", "name": "AllowedOne"},
            {"id": "200", "name": "AllowedTwo"},
        ]
    }
    campaign["timeBasedDrops"][0]["self"] = {
        "hasPreconditionsMet": True,
        "currentMinutesWatched": 5,
        "dropInstanceID": None,
        "isClaimed": False,
    }
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_drops_dashboard",
        lambda self, status="OPEN": [campaign],
    )
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_reward_campaigns_raw_query",
        lambda self: ([], []),
    )
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_open_drop_campaigns_from_helix",
        lambda self: ([], []),
    )
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_campaigns_details",
        lambda self, campaigns: campaigns,
    )
    monkeypatch.setattr(
        Twitch,
        "_Twitch__awarded_benefits",
        lambda self, inventory: (set(), set()),
    )

    deadlines, twitch_games = twitch._Twitch__active_drop_category_slugs_from_campaigns(
        {"dropCampaignsInProgress": [campaign]}, {"example-game"}
    )

    assert set(deadlines) == {"example-game"}
    assert twitch_games == {"example-game"}
    assert twitch.active_drop_campaigns == {
        "example-game": [
            {
                "id": "campaign-1",
                "name": "Example Campaign",
                "channels": ["allowedone", "allowedtwo"],
            }
        ]
    }


def test_completed_campaign_game_is_resolved_when_open_dashboard_omits_it(
    monkeypatch,
):
    twitch = bare_twitch(monkeypatch)
    detail_requests = []
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_drops_dashboard",
        lambda self, status="OPEN": [],
    )
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_reward_campaigns_raw_query",
        lambda self: ([], []),
    )
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_open_drop_campaigns_from_helix",
        lambda self: ([], []),
    )

    def resolve_details(self, campaigns):
        detail_requests.extend(campaigns)
        return [campaign_data()] if campaigns else []

    monkeypatch.setattr(Twitch, "_Twitch__get_campaigns_details", resolve_details)
    monkeypatch.setattr(
        Twitch,
        "_Twitch__awarded_benefits",
        lambda self, inventory: (set(), set()),
    )

    deadlines, twitch_games = (
        twitch._Twitch__active_drop_category_slugs_from_campaigns(
            {"completedRewardCampaigns": [{"id": "campaign-1"}]},
            {"example-game"},
        )
    )

    assert detail_requests == [{"id": "campaign-1"}]
    assert deadlines == {}
    assert twitch_games == {"example-game"}


def test_full_completed_inventory_campaign_prevents_fallback_resurrection(monkeypatch):
    twitch = bare_twitch(monkeypatch)
    detail_requests = []
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_drops_dashboard",
        lambda self, status="OPEN": [],
    )
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_reward_campaigns_raw_query",
        lambda self: ([], []),
    )
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_open_drop_campaigns_from_helix",
        lambda self: ([], []),
    )

    def resolve_details(self, campaigns):
        detail_requests.extend(campaigns)
        return []

    monkeypatch.setattr(Twitch, "_Twitch__get_campaigns_details", resolve_details)
    monkeypatch.setattr(
        Twitch,
        "_Twitch__awarded_benefits",
        lambda self, inventory: (set(), set()),
    )
    completed_campaign = {
        "id": "minecraft-campaign",
        "name": "Boss Run Marathon",
        "status": "COMPLETED",
        "game": {"id": "27471", "displayName": "Minecraft"},
        "rewards": [{"name": "Frog Hoodie"}],
    }

    deadlines, twitch_games = (
        twitch._Twitch__active_drop_category_slugs_from_campaigns(
            {"completedRewardCampaigns": [completed_campaign]},
            {"minecraft"},
        )
    )

    assert detail_requests == []
    assert deadlines == {}
    assert twitch_games == {"minecraft"}
    assert twitch.campaign_game_slugs == {"minecraft-campaign": "minecraft"}


def test_wrapped_completed_inventory_campaign_prevents_fallback_resurrection(
    monkeypatch,
):
    twitch = bare_twitch(monkeypatch)
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_drops_dashboard",
        lambda self, status="OPEN": [],
    )
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_reward_campaigns_raw_query",
        lambda self: ([], []),
    )
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_open_drop_campaigns_from_helix",
        lambda self: ([], []),
    )
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_campaigns_details",
        lambda self, campaigns: [],
    )
    monkeypatch.setattr(
        Twitch,
        "_Twitch__awarded_benefits",
        lambda self, inventory: (set(), set()),
    )
    completed_record = {
        "campaign": {
            "id": "warhounds-campaign",
            "name": "Closed Playtest",
            "status": "COMPLETED",
            "game": {"displayName": "Warhounds"},
        }
    }

    deadlines, twitch_games = (
        twitch._Twitch__active_drop_category_slugs_from_campaigns(
            {"completedRewardCampaigns": [completed_record]},
            {"warhounds"},
        )
    )

    assert deadlines == {}
    assert twitch_games == {"warhounds"}
    assert twitch.campaign_game_slugs == {"warhounds-campaign": "warhounds"}


def test_drop_report_snapshot_uses_analytics_mutex():
    class RecordingLock:
        def __init__(self):
            self.entered = 0

        def __enter__(self):
            self.entered += 1

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    twitch = object.__new__(Twitch)
    twitch.analytics_mutex = RecordingLock()
    twitch.drop_report_state = {"drop": {"current_minutes_watched": 25}}

    snapshot = twitch.drop_report_snapshot()

    assert snapshot == {"drop": {"current_minutes_watched": 25}}
    assert twitch.analytics_mutex.entered == 1
    assert snapshot is not twitch.drop_report_state
    assert snapshot["drop"] is not twitch.drop_report_state["drop"]
