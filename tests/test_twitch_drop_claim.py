import importlib
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
    twitch.log_drop_checks = False
    twitch.category_campaign_eligibility = {}
    twitch.evaluated_category_campaigns = set()
    twitch.twitchdrops_app_campaigns = {}
    twitch.gql = SimpleNamespace(
        claim_drop_rewards=lambda drop_instance_id: SimpleNamespace(
            status=claim_status, errors=[]
        )
    )
    monkeypatch.setattr(
        Twitch, "_Twitch__drop_variant_entries_from_drop", lambda self, drop: []
    )
    return twitch


def test_claiming_final_drop_suppresses_stale_campaign(monkeypatch):
    twitch = bare_twitch(monkeypatch)
    campaign = Campaign(campaign_data())
    drop = campaign.drops[0]
    drop.drop_instance_id = "instance-1"

    assert twitch.claim_drop(drop, campaign=campaign) is True
    assert twitch.completed_drop_campaigns == {"campaign-1"}


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


def test_bulk_inventory_claim_marks_completed_campaign(monkeypatch):
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

    assert twitch.completed_drop_campaigns == {"campaign-1"}


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
