import importlib
import logging
import time
from threading import Lock
from types import SimpleNamespace

from TwitchChannelPointsMiner.classes.entities.Campaign import Campaign
from TwitchChannelPointsMiner.classes.gql.Errors import RetryError
from TwitchChannelPointsMiner.classes.Twitch import (
    CHANNEL_QUERY_FAILURE_LIMIT,
    Twitch,
)


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
    twitch.reward_campaign_ids = set()
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


def test_wildcard_external_campaign_requires_matching_twitch_campaign(monkeypatch):
    twitch = bare_twitch(monkeypatch)
    campaign = advertised_campaign()
    campaign["name"] = "Different Campaign"
    twitch.gql = SimpleNamespace(
        get_available_drops=lambda channel_id: SimpleNamespace(
            campaigns=[campaign], campaigns_available=True
        )
    )
    twitch.twitchdrops_app_campaigns = {
        "example-game": [
            {
                "id": "external-campaign",
                "name": "Expected Campaign",
                "channels": [],
            }
        ]
    }
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_campaigns_details",
        lambda self, campaigns, campaign_channel_id_by_id=None: [],
    )
    streamer = category_streamer()
    streamer.from_wildcard_category = True

    assert twitch._Twitch__get_campaign_ids_from_streamer(streamer) == []
    assert twitch.category_campaign_eligibility[("example-game", "drops-channel")] == (
        0,
        1,
    )


def test_channel_allowlisted_in_authoritative_campaign_survives_empty_channel_query(
    monkeypatch,
):
    # Regression: campaign-restricted categories (e.g. Pokémon GO co-streams)
    # resolve their channel allow-list from Twitch's own authoritative
    # campaign data, so the external gist index is intentionally left empty
    # for them. A channel's own advertised-campaigns query can still come
    # back empty even though it is allow-listed there (common for official
    # co-stream/viewing-party channels) - that must not clobber the eligibility
    # already established from the allow-list.
    twitch = bare_twitch(monkeypatch)
    twitch.gql = SimpleNamespace(
        get_available_drops=lambda channel_id: SimpleNamespace(
            campaigns=[], campaigns_available=True
        )
    )
    twitch.discovered_open_drop_campaigns = []
    twitch.active_drop_campaigns = {
        "example-game": [
            {
                "id": "restricted-campaign-1",
                "name": "Restricted Campaign",
                "channels": ["drops-channel"],
            }
        ]
    }
    twitch.category_campaign_eligibility[("example-game", "drops-channel")] = (1, 1)

    assert twitch._Twitch__get_campaign_ids_from_streamer(category_streamer()) == [
        "restricted-campaign-1"
    ]
    assert twitch.category_campaign_eligibility[("example-game", "drops-channel")] == (
        1,
        1,
    )


def test_reward_campaign_advertised_by_channel_falls_back_to_gist_drop_campaign(
    monkeypatch,
):
    # Regression: Twitch's per-channel "available drops" query advertised a
    # purchase-gated Reward Campaign for this channel (already confirmed
    # subscription/gift-gated by the account-wide evaluation, and recorded in
    # reward_campaign_ids) instead of the real, gist-known watch-time drop
    # campaign for the same game. The reward campaign must be filtered out of
    # the channel's advertised campaigns entirely -- not merely excluded from
    # the eligible count -- so the channel falls through to the gist
    # channel-allowlist fallback and resolves the real campaign, rather than
    # getting stuck with a conclusive (0, N) eligibility that would block
    # that fallback from ever running.
    twitch = bare_twitch(monkeypatch)
    reward_campaign = {
        "id": "reward-campaign-1",
        "game": {"displayName": "Example Game"},
        "name": "Sub Badge Launch",
        "timeBasedDrops": [
            {
                "id": "sub-badge-drop",
                "name": "Sub Badge",
                "requiredMinutesWatched": 0,
            }
        ],
    }
    twitch.reward_campaign_ids = {"reward-campaign-1"}
    twitch.gql = SimpleNamespace(
        get_available_drops=lambda channel_id: SimpleNamespace(
            campaigns=[reward_campaign], campaigns_available=True
        )
    )
    twitch.discovered_open_drop_campaigns = []
    twitch.twitchdrops_app_campaigns = {
        "example-game": [
            {
                "id": "real-drop-campaign-1",
                "name": "Real Drop Campaign",
                "channels": ["drops-channel"],
            }
        ]
    }

    assert twitch._Twitch__get_campaign_ids_from_streamer(category_streamer()) == [
        "real-drop-campaign-1"
    ]
    # Recorded as eligible through the gist campaign, not a conclusive (0, N).
    assert twitch.category_campaign_eligibility[("example-game", "drops-channel")] == (
        1,
        1,
    )


def test_unlisted_reward_campaign_still_falls_back_to_gist_drop_campaign(monkeypatch):
    # Regression: the account-wide evaluation didn't see this reward campaign
    # this cycle at all (it can rotate out of Twitch's dashboard/reward query
    # independently of the per-channel query), so reward_campaign_ids is
    # empty and can't be used to recognize it by ID. The channel-specific
    # query still advertises it, with no personalized claim-state fields and
    # a zero required-minutes drop -- exactly what a purchase-gated campaign
    # looks like. It must still be filtered out on its own lack of any
    # currently-watchable drop, so the channel falls through to the gist's
    # separate, genuinely incomplete campaign instead of getting stuck.
    twitch = bare_twitch(monkeypatch)
    reward_campaign = {
        "id": "reward-campaign-1",
        "game": {"displayName": "Example Game"},
        "name": "Sub Badge Launch",
        "timeBasedDrops": [
            {
                "id": "sub-badge-drop",
                "name": "Sub Badge",
                "requiredMinutesWatched": 0,
            }
        ],
    }
    assert twitch.reward_campaign_ids == set()
    twitch.gql = SimpleNamespace(
        get_available_drops=lambda channel_id: SimpleNamespace(
            campaigns=[reward_campaign], campaigns_available=True
        )
    )
    twitch.discovered_open_drop_campaigns = []
    twitch.twitchdrops_app_campaigns = {
        "example-game": [
            {
                "id": "real-drop-campaign-1",
                "name": "Real Drop Campaign",
                "channels": ["drops-channel"],
            }
        ]
    }

    assert twitch._Twitch__get_campaign_ids_from_streamer(category_streamer()) == [
        "real-drop-campaign-1"
    ]
    assert twitch.category_campaign_eligibility[("example-game", "drops-channel")] == (
        1,
        1,
    )


def test_campaign_names_from_ids_resolves_before_sync_campaigns_catches_up(monkeypatch):
    # Regression: stream.campaigns (the source __describe_campaigns reads)
    # is only populated by the slower sync_campaigns background pass, so a
    # just-selected streamer's watch log fell back to a bare "<game> drops"
    # label instead of naming the specific campaign -- unhelpful when a game
    # has more than one active drop campaign at once. Resolve names directly
    # from whichever discovery source populated campaigns_ids instead.
    twitch = bare_twitch(monkeypatch)
    twitch.advertised_drop_campaigns = {
        "advertised-1": {"id": "advertised-1", "name": "Advertised Campaign"}
    }
    twitch.twitchdrops_app_campaigns = {
        "example-game": [{"id": "gist-1", "name": "Gist Campaign", "channels": []}]
    }
    twitch.active_drop_campaigns = {
        "example-game": [
            {"id": "native-1", "name": "Native Campaign", "channels": []}
        ]
    }

    names = twitch._Twitch__campaign_names_from_ids(
        ["advertised-1", "gist-1", "native-1", "unknown-id"], "example-game"
    )

    assert set(names) == {"Advertised Campaign", "Gist Campaign", "Native Campaign"}


def test_channel_not_in_authoritative_campaign_allowlist_still_blocked(monkeypatch):
    # The same authoritative campaign exists, but this channel isn't on its
    # allow-list, so the empty channel query result must still stand.
    twitch = bare_twitch(monkeypatch)
    twitch.gql = SimpleNamespace(
        get_available_drops=lambda channel_id: SimpleNamespace(
            campaigns=[], campaigns_available=True
        )
    )
    twitch.discovered_open_drop_campaigns = []
    twitch.active_drop_campaigns = {
        "example-game": [
            {
                "id": "restricted-campaign-1",
                "name": "Restricted Campaign",
                "channels": ["some-other-channel"],
            }
        ]
    }

    assert twitch._Twitch__get_campaign_ids_from_streamer(category_streamer()) == []
    assert twitch.category_campaign_eligibility[("example-game", "drops-channel")] == (
        0,
        0,
    )


def test_unrestricted_authoritative_campaign_does_not_shield_unrelated_channel(
    monkeypatch,
):
    # Regression: an unrestricted authoritative campaign (empty "channels")
    # sitting alongside a genuinely restricted one for the same game must not
    # make every channel of that game look allow-listed. Only an explicit,
    # non-empty allow-list containing this channel should override the
    # channel's own empty query result.
    twitch = bare_twitch(monkeypatch)
    twitch.gql = SimpleNamespace(
        get_available_drops=lambda channel_id: SimpleNamespace(
            campaigns=[], campaigns_available=True
        )
    )
    twitch.discovered_open_drop_campaigns = []
    twitch.active_drop_campaigns = {
        "example-game": [
            {
                "id": "restricted-campaign-1",
                "name": "Restricted Campaign",
                "channels": ["some-other-channel"],
            },
            {
                "id": "unrestricted-campaign-1",
                "name": "Unrestricted Campaign",
                "channels": [],
            },
        ]
    }

    assert twitch._Twitch__get_campaign_ids_from_streamer(category_streamer()) == []
    assert twitch.category_campaign_eligibility[("example-game", "drops-channel")] == (
        0,
        0,
    )


def per_streamer_campaigns_twitch(monkeypatch, available_drops):
    # Mirrors the Rust Isles campaigns: each item campaign reports no
    # allow-list, yet Twitch only advertises (and credits) it on one
    # streamer's channel. Random Drops-enabled channels answer null.
    twitch = bare_twitch(monkeypatch)
    twitch.gql = SimpleNamespace(get_available_drops=available_drops)
    twitch.discovered_open_drop_campaigns = [
        {
            "id": "dashboard-campaign-1",
            "name": "Dashboard Campaign",
            "game": {"displayName": "Example Game"},
            "allow": {"channels": [{"name": "some-other-channel"}]},
        }
    ]
    twitch.active_drop_campaigns = {
        "example-game": [
            {"id": "furnace", "name": "Furnace", "channels": []},
            {"id": "backpack", "name": "Backpack", "channels": []},
        ]
    }
    twitch.twitchdrops_app_campaigns = {
        "example-game": [
            {"id": "gist-1", "name": "Lg Box", "channels": ["some-other-channel"]}
        ]
    }
    twitch.category_campaign_eligibility[("example-game", "drops-channel")] = (2, 2)
    return twitch


def test_null_channel_query_ignores_unrestricted_twitch_campaigns(monkeypatch):
    # Regression: after the one allow-listed Rust campaign was claimed, every
    # random Rust channel still received the remaining per-streamer campaigns
    # because they expose no allow-list, and hours of watching earned nothing.
    twitch = per_streamer_campaigns_twitch(
        monkeypatch,
        lambda channel_id: SimpleNamespace(campaigns=[], campaigns_available=False),
    )

    assert twitch._Twitch__get_campaign_ids_from_streamer(category_streamer()) == []
    assert twitch.category_campaign_eligibility[("example-game", "drops-channel")] == (
        0,
        0,
    )


def test_null_channel_query_keeps_explicit_twitch_allowlist(monkeypatch):
    twitch = per_streamer_campaigns_twitch(
        monkeypatch,
        lambda channel_id: SimpleNamespace(campaigns=[], campaigns_available=False),
    )
    twitch.active_drop_campaigns["example-game"].append(
        {"id": "sword", "name": "Sword", "channels": ["drops-channel"]}
    )

    assert twitch._Twitch__get_campaign_ids_from_streamer(category_streamer()) == [
        "sword"
    ]
    # Recorded from what the channel check found, not discovery's seed.
    assert twitch.category_campaign_eligibility[("example-game", "drops-channel")] == (
        1,
        1,
    )


def test_answered_channel_query_excludes_completed_fallback_campaign(monkeypatch):
    twitch = per_streamer_campaigns_twitch(
        monkeypatch,
        lambda channel_id: SimpleNamespace(campaigns=[], campaigns_available=False),
    )
    twitch.active_drop_campaigns["example-game"].append(
        {"id": "sword", "name": "Sword", "channels": ["drops-channel"]}
    )
    twitch.completed_drop_campaigns = {"sword"}

    assert twitch._Twitch__get_campaign_ids_from_streamer(category_streamer()) == [
        "sword"
    ]
    assert twitch.category_campaign_eligibility[("example-game", "drops-channel")] == (
        0,
        1,
    )


def test_answered_channel_query_excludes_fallback_after_game_deadline(monkeypatch):
    from datetime import datetime, timedelta

    twitch = per_streamer_campaigns_twitch(
        monkeypatch,
        lambda channel_id: SimpleNamespace(campaigns=[], campaigns_available=False),
    )
    twitch.active_drop_campaigns["example-game"].append(
        {"id": "sword", "name": "Sword", "channels": ["drops-channel"]}
    )
    twitch.category_campaign_deadlines = {
        "example-game": datetime.utcnow() - timedelta(minutes=1)
    }

    twitch._Twitch__get_campaign_ids_from_streamer(category_streamer())

    assert twitch.category_campaign_eligibility[("example-game", "drops-channel")] == (
        0,
        1,
    )


def fallback_twitch_with_full_campaign(monkeypatch, minutes_left):
    # The gist summary carries no drops, but Twitch already returned the full
    # campaign to another channel's query.
    from datetime import datetime, timedelta

    twitch = per_streamer_campaigns_twitch(
        monkeypatch,
        lambda channel_id: SimpleNamespace(campaigns=[], campaigns_available=False),
    )
    twitch.awarded_game_event_drops = {}
    twitch.twitchdrops_app_campaigns["example-game"].append(
        {"id": "gist-open", "name": "General Drops", "channels": []}
    )
    now = datetime.utcnow()
    twitch.advertised_drop_campaigns["gist-open"] = {
        "id": "gist-open",
        "name": "General Drops",
        "game": {"displayName": "Example Game"},
        "timeBasedDrops": [
            {
                "id": "long-drop",
                "name": "Long Drop",
                "benefitEdges": [{"benefit": {"id": "reward", "name": "Reward"}}],
                "requiredMinutesWatched": 300,
                "startAt": (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "endAt": (now + timedelta(minutes=minutes_left)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
            }
        ],
    }
    streamer = category_streamer()
    return twitch, streamer


def test_fallback_verdict_rules_out_drops_that_cannot_finish(monkeypatch):
    twitch, streamer = fallback_twitch_with_full_campaign(monkeypatch, 100)

    assert twitch._Twitch__get_campaign_ids_from_streamer(streamer) == ["gist-open"]
    assert twitch.category_campaign_eligibility[("example-game", "drops-channel")] == (
        0,
        1,
    )
    assert twitch.category_channel_verdicts[("example-game", "drops-channel")][0] == (
        0,
        1,
    )


def test_fallback_verdict_keeps_drops_that_can_finish(monkeypatch):
    twitch, streamer = fallback_twitch_with_full_campaign(monkeypatch, 600)

    twitch._Twitch__get_campaign_ids_from_streamer(streamer)

    assert twitch.category_campaign_eligibility[("example-game", "drops-channel")] == (
        1,
        1,
    )


def test_fallback_verdict_counts_inventory_progress(monkeypatch):
    twitch, streamer = fallback_twitch_with_full_campaign(monkeypatch, 100)
    twitch.drop_inventory_minutes_by_drop_id = {"long-drop": 250}
    twitch.drop_inventory_progress_updated_at = time.time()

    twitch._Twitch__get_campaign_ids_from_streamer(streamer)

    assert twitch.category_campaign_eligibility[("example-game", "drops-channel")] == (
        1,
        1,
    )


def test_fallback_verdict_checks_full_campaign_from_open_campaigns(monkeypatch):
    twitch, streamer = fallback_twitch_with_full_campaign(monkeypatch, 100)
    full_campaign = twitch.advertised_drop_campaigns.pop("gist-open")
    twitch.discovered_open_drop_campaigns.append(full_campaign)

    twitch._Twitch__get_campaign_ids_from_streamer(streamer)

    assert twitch.category_campaign_eligibility[("example-game", "drops-channel")] == (
        0,
        1,
    )


def test_fallback_verdict_trusts_account_wide_campaign_evaluation(monkeypatch):
    # The account-wide evaluation already counted the account's progress; a
    # cached copy without it must not rule the campaign out again.
    twitch, streamer = fallback_twitch_with_full_campaign(monkeypatch, 100)
    twitch.active_drop_campaigns["example-game"].append(
        {"id": "gist-open", "name": "General Drops", "channels": ["drops-channel"]}
    )

    twitch._Twitch__get_campaign_ids_from_streamer(streamer)

    eligible, _total = twitch.category_campaign_eligibility[
        ("example-game", "drops-channel")
    ]
    assert eligible >= 1


def test_null_channel_query_keeps_unrestricted_gist_campaign(monkeypatch):
    twitch = per_streamer_campaigns_twitch(
        monkeypatch,
        lambda channel_id: SimpleNamespace(campaigns=[], campaigns_available=False),
    )
    twitch.twitchdrops_app_campaigns["example-game"].append(
        {"id": "gist-open", "name": "General Drops", "channels": []}
    )

    assert twitch._Twitch__get_campaign_ids_from_streamer(category_streamer()) == [
        "gist-open"
    ]


def test_answered_channel_query_replaces_stale_negative_verdict(monkeypatch):
    # Category discovery keeps a fresh per-channel verdict across refreshes
    # (#144), so the channel check must replace a negative one with a
    # positive verdict once the channel qualifies - deleting it would leave
    # the channel ineligible, since the game was already evaluated.
    twitch = per_streamer_campaigns_twitch(
        monkeypatch,
        lambda channel_id: SimpleNamespace(campaigns=[], campaigns_available=False),
    )
    twitch.twitchdrops_app_campaigns["example-game"].append(
        {"id": "gist-open", "name": "General Drops", "channels": []}
    )
    twitch.category_campaign_eligibility[("example-game", "drops-channel")] = (0, 0)
    twitch.evaluated_category_campaigns = {"example-game"}
    streamer = category_streamer()
    streamer.is_online = True
    streamer.settings = SimpleNamespace(claim_drops=True)
    streamer.stream.campaigns_ids = []

    assert twitch._Twitch__get_campaign_ids_from_streamer(streamer) == ["gist-open"]
    assert twitch.category_campaign_eligibility[("example-game", "drops-channel")] == (
        1,
        1,
    )
    assert ("example-game", "drops-channel") not in twitch.category_channel_verdicts
    assert twitch._Twitch__category_drops_condition(streamer) is True


def test_channel_verdicts_store_only_fresh_negative_results(monkeypatch):
    # Only negative verdicts can outrank discovery, and expired ones (for
    # example from games no longer discovered) are pruned on every record.
    twitch = bare_twitch(monkeypatch)
    twitch_module = importlib.import_module("TwitchChannelPointsMiner.classes.Twitch")
    monkeypatch.setattr(twitch_module.time, "time", lambda: 10_000)
    twitch.category_channel_verdicts = {
        ("old-game", "gone"): ((0, 0), 1_000),
        ("game", "recent"): ((0, 1), 9_900),
    }
    record = twitch._Twitch__record_channel_verdict

    record("game", "ruled-out", (0, 2))
    record("game", "recent", (1, 1))

    assert twitch.category_channel_verdicts == {
        ("game", "ruled-out"): ((0, 2), 10_000),
    }
    assert twitch.category_campaign_eligibility == {
        ("game", "ruled-out"): (0, 2),
        ("game", "recent"): (1, 1),
    }


def test_accepted_claim_stops_applying_inventory_progress(monkeypatch):
    twitch = bare_twitch(monkeypatch)
    twitch.drop_inventory_minutes_by_drop_id = {"drop-1": 9, "drop-2": 5}
    monkeypatch.setattr(
        Twitch, "_Twitch__save_drop_claim_analytics", lambda *args, **kwargs: None
    )
    drop = SimpleNamespace(id="drop-1", drop_instance_id="instance-1")

    assert twitch.claim_drop(drop) is True
    assert twitch.drop_inventory_minutes_by_drop_id == {"drop-2": 5}


def claim_recording_twitch(monkeypatch, statuses=("ELIGIBLE_FOR_ALL",)):
    # Records claim requests and analytics saves; `statuses` are returned in
    # order, repeating the last one. The clock is advanced through `now`.
    twitch = bare_twitch(monkeypatch)
    requests = []
    saved = []

    def claim_drop_rewards(drop_instance_id):
        requests.append(drop_instance_id)
        status = statuses[min(len(requests), len(statuses)) - 1]
        return SimpleNamespace(status=status, errors=[])

    twitch.gql = SimpleNamespace(claim_drop_rewards=claim_drop_rewards)
    monkeypatch.setattr(
        Twitch,
        "_Twitch__save_drop_claim_analytics",
        lambda *args, **kwargs: saved.append(args),
    )
    monkeypatch.setattr(
        Twitch,
        "_Twitch__drop_variant_entries_from_drop",
        lambda self, drop: [{"name": "Reward"}],
    )
    twitch_module = importlib.import_module("TwitchChannelPointsMiner.classes.Twitch")
    now = [1_000.0]
    monkeypatch.setattr(twitch_module.time, "monotonic", lambda: now[0])
    drop = SimpleNamespace(id="drop-1", drop_instance_id="instance-1")
    return twitch, drop, requests, saved, now


def drop_claim_events(caplog):
    twitch_module = importlib.import_module("TwitchChannelPointsMiner.classes.Twitch")
    return [
        record
        for record in caplog.records
        if getattr(record, "event", None) == twitch_module.Events.DROP_CLAIM
    ]


def test_accepted_claim_is_not_repeated_while_inventory_lags(monkeypatch, caplog):
    # Twitch can accept a claim while the inventory keeps listing the drop as
    # unclaimed; claiming it again on every pass sent a notification each time.
    twitch, drop, requests, saved, now = claim_recording_twitch(monkeypatch)

    with caplog.at_level(logging.INFO):
        assert twitch.claim_drop(drop) is True
        twitch.drop_inventory_minutes_by_drop_id = {"drop-1": 180}
        now[0] += 60
        assert twitch.claim_drop(drop) is True

    assert requests == ["instance-1"]
    assert len(saved) == 1
    assert twitch.drop_inventory_minutes_by_drop_id == {}
    assert len(drop_claim_events(caplog)) == 1


def test_skipped_accepted_claim_logs_at_debug(monkeypatch, caplog):
    twitch, drop, _requests, _saved, now = claim_recording_twitch(monkeypatch)
    twitch.log_drop_checks = True

    assert twitch.claim_drop(drop) is True
    now[0] += 60
    with caplog.at_level(logging.DEBUG):
        assert twitch.claim_drop(drop) is True

    skips = [
        record for record in caplog.records if "skipping claim" in record.getMessage()
    ]
    assert [record.levelno for record in skips] == [logging.DEBUG]


def test_accepted_claim_retries_quietly_after_backoff(monkeypatch, caplog):
    from TwitchChannelPointsMiner.classes.Twitch import ACCEPTED_CLAIM_RETRY_SECONDS

    twitch, drop, requests, saved, now = claim_recording_twitch(monkeypatch)

    with caplog.at_level(logging.INFO):
        assert twitch.claim_drop(drop) is True
        now[0] += ACCEPTED_CLAIM_RETRY_SECONDS
        assert twitch.claim_drop(drop) is True

    assert requests == ["instance-1", "instance-1"]
    assert len(saved) == 1
    assert twitch.accepted_drop_claims == {"instance-1": now[0]}
    assert len(drop_claim_events(caplog)) == 1


def test_failed_retry_waits_a_full_interval(monkeypatch):
    from TwitchChannelPointsMiner.classes.Twitch import ACCEPTED_CLAIM_RETRY_SECONDS

    twitch, drop, requests, _saved, now = claim_recording_twitch(
        monkeypatch, statuses=("ELIGIBLE_FOR_ALL", "UNEXPECTED")
    )

    assert twitch.claim_drop(drop) is True
    now[0] += ACCEPTED_CLAIM_RETRY_SECONDS
    assert twitch.claim_drop(drop) is False
    now[0] += 60
    assert twitch.claim_drop(drop) is True

    assert requests == ["instance-1", "instance-1"]


def test_already_claimed_first_response_does_not_notify(monkeypatch, caplog):
    # After a restart the in-memory record is gone; Twitch reporting the drop
    # as already claimed must not send a second claim notification.
    twitch, drop, requests, _saved, now = claim_recording_twitch(
        monkeypatch, statuses=("DROP_INSTANCE_ALREADY_CLAIMED",)
    )

    with caplog.at_level(logging.INFO):
        assert twitch.claim_drop(drop) is True
        now[0] += 60
        assert twitch.claim_drop(drop) is True

    assert requests == ["instance-1"]
    assert drop_claim_events(caplog) == []


def test_concurrent_claims_of_same_drop_send_one_request(monkeypatch, caplog):
    import threading

    twitch, drop, requests, _saved, _now = claim_recording_twitch(monkeypatch)
    started = threading.Event()
    release = threading.Event()

    def slow_claim(drop_instance_id):
        requests.append(drop_instance_id)
        started.set()
        release.wait(5)
        return SimpleNamespace(status="ELIGIBLE_FOR_ALL", errors=[])

    twitch.gql = SimpleNamespace(claim_drop_rewards=slow_claim)
    results = []
    with caplog.at_level(logging.INFO):
        first = threading.Thread(target=lambda: results.append(twitch.claim_drop(drop)))
        first.start()
        assert started.wait(5)
        second = threading.Thread(
            target=lambda: results.append(twitch.claim_drop(drop))
        )
        second.start()
        release.set()
        first.join(5)
        second.join(5)

    assert results == [True, True]
    assert requests == ["instance-1"]
    assert len(drop_claim_events(caplog)) == 1


def test_inventory_cache_skips_drops_with_accepted_claims(monkeypatch):
    twitch = bare_twitch(monkeypatch)
    twitch.accepted_drop_claims = {"instance-1": 1_000.0}
    data = campaign_data()
    data["timeBasedDrops"][0]["self"] = {
        "hasPreconditionsMet": True,
        "currentMinutesWatched": 5,
        "dropInstanceID": "instance-1",
        "isClaimed": False,
    }

    twitch._Twitch__cache_drop_inventory_progress({"dropCampaignsInProgress": [data]})

    assert twitch.drop_inventory_minutes_by_drop_id == {}


def test_failed_claim_is_retried_on_next_pass(monkeypatch):
    twitch = bare_twitch(monkeypatch)
    requests = []
    twitch.gql = SimpleNamespace(
        claim_drop_rewards=lambda drop_instance_id: requests.append(drop_instance_id)
        or SimpleNamespace(status="DROP_INSTANCE_NOT_FOUND", errors=[])
    )
    drop = SimpleNamespace(id="drop-1", drop_instance_id="instance-1")

    assert twitch.claim_drop(drop) is False
    assert twitch.claim_drop(drop) is False
    assert requests == ["instance-1", "instance-1"]


def test_failed_channel_query_keeps_negative_verdict(monkeypatch):
    def available_drops(channel_id):
        raise RetryError("DropsHighlightService_AvailableDrops", [])

    twitch = per_streamer_campaigns_twitch(monkeypatch, available_drops)
    twitch.category_campaign_eligibility[("example-game", "drops-channel")] = (0, 0)

    assert twitch._Twitch__get_campaign_ids_from_streamer(category_streamer())
    assert twitch.category_campaign_eligibility[("example-game", "drops-channel")] == (
        0,
        0,
    )


def failing_wildcard_twitch(monkeypatch):
    attempts = {"fail": True}

    def available_drops(channel_id):
        if attempts["fail"]:
            raise RetryError("DropsHighlightService_AvailableDrops", [])
        return SimpleNamespace(campaigns=[], campaigns_available=False)

    twitch = per_streamer_campaigns_twitch(monkeypatch, available_drops)
    twitch.category_channel_verdicts = {}
    streamer = category_streamer()
    streamer.from_wildcard_category = True
    return twitch, streamer, attempts


def test_repeatedly_failing_wildcard_query_rules_channel_out(monkeypatch):
    # A failed query is not a verdict, but one that keeps failing must stop
    # category refreshes from re-seeding the channel as eligible each time.
    twitch, streamer, _attempts = failing_wildcard_twitch(monkeypatch)
    check = twitch._Twitch__get_campaign_ids_from_streamer
    reseed = twitch._Twitch__replace_category_campaign_eligibility
    key = ("example-game", "drops-channel")

    for _ in range(CHANNEL_QUERY_FAILURE_LIMIT - 1):
        assert check(streamer) == []
        assert key not in twitch.category_channel_verdicts
    reseed("example-game", {"drops-channel": (1, 1)})
    assert twitch.category_campaign_eligibility[key] == (1, 1)

    assert check(streamer) == []
    assert twitch.category_channel_verdicts[key][0] == (0, 1)
    reseed("example-game", {"drops-channel": (1, 1)})
    assert twitch.category_campaign_eligibility[key] == (0, 1)


def test_answered_query_resets_wildcard_failure_count(monkeypatch):
    twitch, streamer, attempts = failing_wildcard_twitch(monkeypatch)
    check = twitch._Twitch__get_campaign_ids_from_streamer
    key = ("example-game", "drops-channel")

    for _ in range(CHANNEL_QUERY_FAILURE_LIMIT - 1):
        check(streamer)
    attempts["fail"] = False
    check(streamer)
    assert key not in twitch.channel_query_failures

    twitch.category_channel_verdicts = {}
    attempts["fail"] = True
    check(streamer)
    assert twitch.channel_query_failures[key][0] == 1
    assert key not in twitch.category_channel_verdicts


def test_wildcard_query_failures_far_apart_are_not_consecutive(monkeypatch):
    from TwitchChannelPointsMiner.classes.Twitch import (
        CHANNEL_VERDICT_FRESHNESS_SECONDS,
    )

    twitch, streamer, _attempts = failing_wildcard_twitch(monkeypatch)
    twitch_module = importlib.import_module("TwitchChannelPointsMiner.classes.Twitch")
    now = [10_000.0]
    monkeypatch.setattr(twitch_module.time, "time", lambda: now[0])
    twitch.channel_query_failures = {("other-game", "gone"): (2, now[0])}
    key = ("example-game", "drops-channel")

    for _ in range(CHANNEL_QUERY_FAILURE_LIMIT - 1):
        twitch._Twitch__get_campaign_ids_from_streamer(streamer)
    now[0] += CHANNEL_VERDICT_FRESHNESS_SECONDS + 1
    twitch._Twitch__get_campaign_ids_from_streamer(streamer)

    assert twitch.channel_query_failures == {key: (1, now[0])}
    assert key not in twitch.category_channel_verdicts


def _late_campaign_twitch(monkeypatch, minutes_by_drop_id, snapshot_age=60):
    # EXFIL Brothers Day 4: one advertised campaign whose next drop needs
    # more minutes than are left, unless the account's inventory progress on
    # that drop is counted.
    from datetime import datetime, timedelta

    ends_at = (datetime.utcnow() + timedelta(minutes=275)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    campaign = advertised_campaign()
    campaign["endAt"] = ends_at
    drop = campaign["timeBasedDrops"][0]
    drop["id"] = "drop-5"
    drop["requiredMinutesWatched"] = 300
    drop["endAt"] = ends_at
    twitch = bare_twitch(monkeypatch)
    twitch.gql = SimpleNamespace(
        get_available_drops=lambda channel_id: SimpleNamespace(
            campaigns=[campaign], campaigns_available=True
        )
    )
    twitch.drop_inventory_minutes_by_drop_id = minutes_by_drop_id
    twitch.drop_inventory_progress_updated_at = time.time() - snapshot_age
    twitch.awarded_game_event_drops = {}
    monkeypatch.setattr(
        Twitch, "_Twitch__get_campaigns_details", lambda self, *a, **k: []
    )
    return twitch


def test_channel_check_counts_inventory_progress_toward_late_drop(monkeypatch):
    # Regression: the channel query carries no per-account progress, so a
    # drop needing 300m with 275m left looked impossible even though the
    # account had already watched 240m of it, and the campaign was dropped.
    twitch = _late_campaign_twitch(monkeypatch, {"drop-5": 240})

    assert twitch._Twitch__get_campaign_ids_from_streamer(category_streamer()) == [
        "campaign-1"
    ]
    assert twitch.category_campaign_eligibility[("example-game", "drops-channel")] == (
        1,
        1,
    )


def test_channel_check_without_progress_rules_out_late_drop(monkeypatch):
    twitch = _late_campaign_twitch(monkeypatch, {})

    assert twitch._Twitch__get_campaign_ids_from_streamer(category_streamer()) == []


def _finished_daily_campaign_twitch(monkeypatch):
    # Albion Online "Dragonfire 7 - #4/7": one 180m drop per day, already
    # fully watched and claimed, but the inventory never confirms the claim
    # because the game account is not linked.
    from datetime import datetime, timedelta

    ends_at = (datetime.utcnow() + timedelta(minutes=1200)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    campaign = advertised_campaign()
    campaign["endAt"] = ends_at
    drop = campaign["timeBasedDrops"][0]
    drop["id"] = "daily-drop"
    drop["requiredMinutesWatched"] = 180
    drop["endAt"] = ends_at
    twitch = bare_twitch(monkeypatch)
    twitch.gql = SimpleNamespace(
        get_available_drops=lambda channel_id: SimpleNamespace(
            campaigns=[campaign], campaigns_available=True
        )
    )
    twitch.drop_inventory_minutes_by_drop_id = {}
    twitch.drop_inventory_progress_updated_at = time.time()
    twitch.awarded_game_event_drops = {}
    monkeypatch.setattr(
        Twitch, "_Twitch__get_campaigns_details", lambda self, *a, **k: []
    )
    return twitch, campaign


def _daily_drop_inventory(campaign, current, instance="instance-daily"):
    data = {**campaign, "game": {"displayName": "Example Game"}}
    data["timeBasedDrops"] = [
        {
            **campaign["timeBasedDrops"][0],
            "self": {
                "hasPreconditionsMet": True,
                "currentMinutesWatched": current,
                "dropInstanceID": instance,
                "isClaimed": False,
            },
        }
    ]
    return {"dropCampaignsInProgress": [data]}


def test_channel_check_rules_out_fully_watched_unconfirmed_drop(monkeypatch):
    twitch, campaign = _finished_daily_campaign_twitch(monkeypatch)
    twitch._Twitch__cache_drop_inventory_progress(_daily_drop_inventory(campaign, 180))

    twitch._Twitch__get_campaign_ids_from_streamer(category_streamer())

    assert twitch.category_campaign_eligibility[("example-game", "drops-channel")] == (
        0,
        1,
    )


def test_channel_check_rules_out_drop_with_accepted_claim(monkeypatch):
    twitch, campaign = _finished_daily_campaign_twitch(monkeypatch)
    twitch.accepted_drop_claims = {"instance-daily": 1_000.0}
    twitch._Twitch__cache_drop_inventory_progress(_daily_drop_inventory(campaign, 170))

    twitch._Twitch__get_campaign_ids_from_streamer(category_streamer())

    assert twitch.category_campaign_eligibility[("example-game", "drops-channel")] == (
        0,
        1,
    )


def test_channel_check_keeps_unfinished_daily_drop(monkeypatch):
    twitch, campaign = _finished_daily_campaign_twitch(monkeypatch)
    twitch._Twitch__cache_drop_inventory_progress(_daily_drop_inventory(campaign, 120))

    twitch._Twitch__get_campaign_ids_from_streamer(category_streamer())

    assert twitch.category_campaign_eligibility[("example-game", "drops-channel")] == (
        1,
        1,
    )


def test_accepted_claim_marks_drop_finished(monkeypatch):
    twitch, drop, _requests, _saved, _now = claim_recording_twitch(monkeypatch)

    assert twitch.claim_drop(drop) is True

    assert "drop-1" in twitch._Twitch__finished_drop_ids()


def test_accepted_drop_stays_finished_across_inventory_syncs(monkeypatch):
    # The inventory may keep listing the drop unclaimed, under minutes, after
    # its accepted_drop_claims entry was pruned; it must not look earnable.
    twitch, campaign = _finished_daily_campaign_twitch(monkeypatch)
    drop = SimpleNamespace(id="daily-drop", drop_instance_id="instance-daily")
    twitch.gql.claim_drop_rewards = lambda drop_instance_id: SimpleNamespace(
        status="ELIGIBLE_FOR_ALL", errors=[]
    )
    monkeypatch.setattr(
        Twitch, "_Twitch__save_drop_claim_analytics", lambda *args, **kwargs: None
    )
    assert twitch.claim_drop(drop) is True
    twitch.accepted_drop_claims = {}
    twitch._Twitch__cache_drop_inventory_progress(_daily_drop_inventory(campaign, 170))

    twitch._Twitch__get_campaign_ids_from_streamer(category_streamer())

    assert twitch.category_campaign_eligibility[("example-game", "drops-channel")] == (
        0,
        1,
    )


def test_fully_watched_drop_without_instance_is_not_finished(monkeypatch):
    # Until Twitch issues the claimable instance, the drop may still need
    # watching, so the channel keeps counting as eligible.
    twitch, campaign = _finished_daily_campaign_twitch(monkeypatch)
    twitch._Twitch__cache_drop_inventory_progress(
        _daily_drop_inventory(campaign, 180, instance=None)
    )

    assert twitch._Twitch__finished_drop_ids() == frozenset()


def test_channel_check_ignores_outdated_inventory_progress(monkeypatch):
    # An old snapshot can still list a drop claimed since, so it is not used.
    twitch = _late_campaign_twitch(monkeypatch, {"drop-5": 240}, snapshot_age=901)

    assert twitch._Twitch__get_campaign_ids_from_streamer(category_streamer()) == []


def test_inventory_cache_indexes_in_progress_minutes_by_drop_id(monkeypatch):
    twitch = bare_twitch(monkeypatch)
    twitch._Twitch__cache_drop_inventory_progress(
        {
            "dropCampaignsInProgress": [
                {
                    "id": "campaign-1",
                    "name": "Example Campaign",
                    "game": {"displayName": "Example Game"},
                    "timeBasedDrops": [
                        {
                            "id": "drop-5",
                            "name": "Drop 5",
                            "requiredMinutesWatched": 300,
                            "self": {"currentMinutesWatched": 240},
                        },
                        {
                            "id": "drop-4",
                            "name": "Drop 4",
                            "requiredMinutesWatched": 240,
                            "self": {"currentMinutesWatched": 240, "isClaimed": True},
                        },
                    ],
                }
            ]
        }
    )

    assert twitch.drop_inventory_minutes_by_drop_id == {"drop-5": 240}


def test_failed_channel_query_keeps_unrestricted_twitch_campaigns(monkeypatch):
    # A failed query proves nothing about the channel, so unrestricted
    # campaigns still apply and existing eligibility is left alone.
    def available_drops(channel_id):
        raise RetryError("DropsHighlightService_AvailableDrops", [])

    twitch = per_streamer_campaigns_twitch(monkeypatch, available_drops)

    assert sorted(
        twitch._Twitch__get_campaign_ids_from_streamer(category_streamer())
    ) == ["backpack", "furnace"]
    assert twitch.category_campaign_eligibility[("example-game", "drops-channel")] == (
        2,
        2,
    )


def test_drops_description_is_none_once_channel_check_confirmed_zero_campaigns(
    monkeypatch,
):
    twitch = bare_twitch(monkeypatch)
    twitch.category_campaign_eligibility[("example-game", "drops-channel")] = (0, 0)
    # An unrelated global campaign for the same game must not resurrect a
    # description - the live per-channel check already ruled this channel out.
    twitch.discovered_open_drop_campaigns = [advertised_campaign()]

    assert twitch._Twitch__streamer_drops_description(category_streamer()) is None


def test_drops_description_uses_global_catalog_without_a_channel_check_yet(
    monkeypatch,
):
    twitch = bare_twitch(monkeypatch)
    twitch.discovered_open_drop_campaigns = [advertised_campaign()]

    assert (
        twitch._Twitch__streamer_drops_description(category_streamer())
        == "Example Game drops"
    )


def test_drops_description_counts_authoritative_restricted_campaigns(monkeypatch):
    # The gist fallback is intentionally empty for an authoritative
    # campaign-restricted game, so the description must still count
    # campaigns from active_drop_campaigns rather than falling through to
    # the unscoped global catalog.
    twitch = bare_twitch(monkeypatch)
    twitch.discovered_open_drop_campaigns = []
    twitch.active_drop_campaigns = {
        "example-game": [
            {"id": "restricted-1", "name": "A", "channels": ["drops-channel"]},
            {"id": "restricted-2", "name": "B", "channels": ["some-other-channel"]},
        ]
    }

    assert (
        twitch._Twitch__streamer_drops_description(category_streamer())
        == "Example Game drops (1 of 2 campaigns)"
    )


def test_campaign_deadline_logs_include_game_name(monkeypatch, caplog):
    twitch = bare_twitch(monkeypatch)
    campaign = campaign_data()
    campaign["game"] = {"displayName": "Warframe"}
    campaign["name"] = "Prime Time #493"
    campaign["timeBasedDrops"] = [
        {
            "id": "possible-drop",
            "name": "Random Hex Treasure",
            "benefitEdges": [],
            "requiredMinutesWatched": 10,
            "startAt": "2020-01-01T00:00:00Z",
            "endAt": "2099-01-01T00:00:00Z",
        },
        {
            "id": "impossible-drop",
            "name": "Long Reward",
            "benefitEdges": [],
            "requiredMinutesWatched": 100000000,
            "startAt": "2020-01-01T00:00:00Z",
            "endAt": "2099-01-01T00:00:00Z",
        },
    ]
    caplog.set_level(logging.INFO)

    twitch._Twitch__active_incomplete_drop_deadline(
        campaign,
        completed_drop_ids=set(),
        awarded_benefit_ids=set(),
        awarded_benefit_fingerprints=set(),
    )

    assert (
        "Enough time for [Warframe] Prime Time #493 - Random Hex Treasure:"
        in caplog.text
    )
    assert (
        "Not enough time for [Warframe] Prime Time #493 - Long Reward:" in caplog.text
    )


def test_campaign_deadline_logs_unknown_for_malformed_game(monkeypatch, caplog):
    twitch = bare_twitch(monkeypatch)
    campaign = campaign_data()
    campaign["game"] = "unexpected-game-value"
    caplog.set_level(logging.INFO)

    twitch._Twitch__active_incomplete_drop_deadline(
        campaign,
        completed_drop_ids=set(),
        awarded_benefit_ids=set(),
        awarded_benefit_fingerprints=set(),
    )

    assert "Enough time for [Unknown Game] Example Campaign - Reward:" in caplog.text


def test_claiming_final_drop_waits_for_inventory_confirmation(monkeypatch):
    twitch = bare_twitch(monkeypatch)
    campaign = Campaign(campaign_data())
    drop = campaign.drops[0]
    drop.drop_instance_id = "instance-1"

    assert twitch.claim_drop(drop, campaign=campaign) is True
    assert twitch.completed_drop_campaigns == set()


def test_sync_campaigns_fallback_claims_drop_not_tracked_by_campaign_ref(monkeypatch):
    # Regression test: campaign_ref.sync_drops() only claims a drop whose id
    # is already present in campaign_ref.drops - a drop added to the
    # campaign after campaign_ref was built, or one earlier filtered out by
    # __remove_ineligible_badge_drops, is not in there. The fallback right
    # after sync_drops must still claim it rather than skip it just because
    # the campaign itself is tracked.
    twitch = bare_twitch(monkeypatch)
    campaign = Campaign(campaign_data())
    campaign.drops = []  # Simulates the drop no longer being tracked locally.

    claimed = []
    monkeypatch.setattr(
        Twitch,
        "claim_drop",
        lambda self, drop, **kwargs: claimed.append(drop.id) or True,
    )

    progress = campaign_data()
    progress["timeBasedDrops"][0]["self"] = {
        "hasPreconditionsMet": True,
        "currentMinutesWatched": 10,
        "dropInstanceID": "instance-1",
        "isClaimed": False,
    }
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_inventory",
        lambda self: {"dropCampaignsInProgress": [progress]},
    )

    twitch._Twitch__sync_campaigns([campaign])

    assert claimed == ["drop-1"]


def test_sync_campaigns_does_not_double_claim_tracked_drop(monkeypatch):
    # The double-claim fix this fallback guard exists for: a drop
    # campaign_ref.sync_drops() already claimed must not be claimed again
    # by the fallback loop right after it.
    twitch = bare_twitch(monkeypatch)
    campaign = Campaign(campaign_data())

    claimed = []
    monkeypatch.setattr(
        Twitch,
        "claim_drop",
        lambda self, drop, **kwargs: claimed.append(drop.id) or True,
    )

    progress = campaign_data()
    progress["timeBasedDrops"][0]["self"] = {
        "hasPreconditionsMet": True,
        "currentMinutesWatched": 10,
        "dropInstanceID": "instance-1",
        "isClaimed": False,
    }
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_inventory",
        lambda self: {"dropCampaignsInProgress": [progress]},
    )

    twitch._Twitch__sync_campaigns([campaign])

    assert claimed == ["drop-1"]


def test_owned_badge_is_removed_from_advertised_campaign(monkeypatch):
    twitch = bare_twitch(monkeypatch)
    twitch.available_badge_names = {"wardog"}
    twitch.drop_badge_rewards = [
        {
            "game_slug": "wardogs",
            "game": "WARDOGS",
            "campaign": "WARDOGS Beta & Launch",
            "reward_name": "WARDOG",
            "badge_names": ["WARDOG", "wardog"],
            "watch_eligible": True,
        }
    ]
    data = campaign_data()
    data["game"] = {"displayName": "WARDOGS"}
    data["name"] = "WARDOGS Beta & Launch"
    data["timeBasedDrops"][0]["name"] = "WARDOG"
    data["timeBasedDrops"][0]["benefitEdges"] = [{"benefit": {"name": "WARDOG"}}]
    campaign = Campaign(data)
    monkeypatch.setattr(Twitch, "_Twitch__get_inventory", lambda self: {})

    synced = twitch._Twitch__sync_campaigns([campaign])

    assert synced[0].drops == []


def test_unearned_badge_remains_in_advertised_campaign(monkeypatch):
    twitch = bare_twitch(monkeypatch)
    twitch.available_badge_names = {"some other badge"}
    twitch.drop_badge_rewards = [
        {
            "game_slug": "wardogs",
            "game": "WARDOGS",
            "campaign": "WARDOGS Beta & Launch",
            "reward_name": "WARDOG",
            "badge_names": ["WARDOG", "wardog"],
            "watch_eligible": True,
        }
    ]
    data = campaign_data()
    data["game"] = {"displayName": "WARDOGS"}
    data["name"] = "WARDOGS Beta & Launch"
    data["timeBasedDrops"][0]["name"] = "WARDOG"
    campaign = Campaign(data)
    monkeypatch.setattr(Twitch, "_Twitch__get_inventory", lambda self: {})

    synced = twitch._Twitch__sync_campaigns([campaign])

    assert [drop.name for drop in synced[0].drops] == ["WARDOG"]


def test_subscription_badge_is_not_treated_as_watch_eligible(monkeypatch):
    twitch = bare_twitch(monkeypatch)
    twitch.available_badge_names = set()
    twitch.drop_badge_rewards = [
        {
            "game_slug": "wardogs",
            "game": "WARDOGS",
            "campaign": "",
            "reward_name": "WARLORD",
            "badge_names": ["WARLORD", "warlord"],
            "watch_eligible": False,
        }
    ]
    data = campaign_data()
    data["game"] = {"displayName": "WARDOGS"}
    data["name"] = "WARDOGS Beta & Launch"
    data["timeBasedDrops"][0]["name"] = "WARLORD"
    data["timeBasedDrops"][0]["benefitEdges"] = [{"benefit": {"name": "WARLORD"}}]
    campaign = Campaign(data)
    monkeypatch.setattr(Twitch, "_Twitch__get_inventory", lambda self: {})

    synced = twitch._Twitch__sync_campaigns([campaign])

    assert synced[0].drops == []


def test_wardogs_campaign_with_owned_and_subscription_badges_is_ineligible(
    monkeypatch,
):
    twitch = bare_twitch(monkeypatch)
    twitch.available_badge_names = {"wardog"}
    twitch.drop_badge_rewards = [
        {
            "game_slug": "wardogs",
            "game": "WARDOGS",
            "campaign": "WARDOGS Beta & Launch",
            "reward_name": "WARDOG",
            "badge_names": ["WARDOG", "wardog"],
            "watch_eligible": True,
        },
        {
            "game_slug": "wardogs",
            "game": "WARDOGS",
            "campaign": "",
            "reward_name": "WARLORD",
            "badge_names": ["WARLORD", "warlord"],
            "watch_eligible": False,
        },
    ]
    data = campaign_data()
    data["game"] = {"displayName": "WARDOGS"}
    data["name"] = "WARDOGS Beta & Launch"
    data["timeBasedDrops"][0]["name"] = "WARDOG"
    data["timeBasedDrops"][0]["benefitEdges"] = [{"benefit": {"name": "WARDOG"}}]
    warlord = dict(data["timeBasedDrops"][0])
    warlord.update(
        {
            "id": "drop-2",
            "name": "WARLORD",
            "benefitEdges": [{"benefit": {"name": "WARLORD"}}],
        }
    )
    data["timeBasedDrops"].append(warlord)
    campaign = Campaign(data)
    monkeypatch.setattr(Twitch, "_Twitch__get_inventory", lambda self: {})

    synced = twitch._Twitch__sync_campaigns([campaign])

    assert synced[0].drops == []


def test_owned_badge_filter_preserves_other_campaign_rewards(monkeypatch):
    twitch = bare_twitch(monkeypatch)
    twitch.available_badge_names = {"wardog"}
    twitch.drop_badge_rewards = [
        {
            "game_slug": "wardogs",
            "game": "WARDOGS",
            "campaign": "WARDOGS Beta & Launch",
            "reward_name": "WARDOG",
            "badge_names": ["WARDOG", "wardog"],
            "watch_eligible": True,
        }
    ]
    data = campaign_data()
    data["game"] = {"displayName": "WARDOGS"}
    data["name"] = "WARDOGS Beta & Launch"
    data["timeBasedDrops"][0]["name"] = "WARDOG"
    ordinary_drop = dict(data["timeBasedDrops"][0])
    ordinary_drop.update(
        {
            "id": "drop-2",
            "name": "Ordinary Reward",
            "benefitEdges": [{"benefit": {"name": "Ordinary Reward"}}],
        }
    )
    data["timeBasedDrops"].append(ordinary_drop)
    campaign = Campaign(data)
    monkeypatch.setattr(Twitch, "_Twitch__get_inventory", lambda self: {})

    synced = twitch._Twitch__sync_campaigns([campaign])

    assert [drop.name for drop in synced[0].drops] == ["Ordinary Reward"]


def test_drop_eligibility_rechecks_badges_learned_after_campaign_sync(monkeypatch):
    twitch = bare_twitch(monkeypatch)
    twitch.available_badge_names = {"wardog"}
    twitch.drop_badge_rewards = []
    data = campaign_data()
    data["game"] = {"displayName": "WARDOGS"}
    data["name"] = "WARDOGS Beta & Launch"
    data["timeBasedDrops"][0]["name"] = "WARDOG"
    campaign = Campaign(data)
    stream = SimpleNamespace(
        campaigns=[campaign],
        campaigns_ids=[campaign.id],
        game_name=lambda: "WARDOGS",
    )
    streamer = SimpleNamespace(
        username="followed-channel",
        from_category=False,
        from_badge_campaign=False,
        settings=SimpleNamespace(claim_drops=True),
        is_online=True,
        stream=stream,
        drops_condition=lambda: any(item.drops for item in stream.campaigns),
    )

    assert twitch._Twitch__drops_condition(streamer) is True

    twitch.drop_badge_rewards = [
        {
            "game_slug": "wardogs",
            "game": "WARDOGS",
            "campaign": "WARDOGS Beta & Launch",
            "reward_name": "WARDOG",
            "badge_names": ["WARDOG", "wardog"],
            "watch_eligible": True,
        }
    ]

    assert twitch._Twitch__drops_condition(streamer) is False
    assert campaign.drops == []


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


def test_category_condition_uses_authoritative_restricted_campaign_when_eligibility_unset(
    monkeypatch,
):
    # Regression: when eligibility hasn't been cached yet for a campaign-
    # restricted category, the gist fallback (twitchdrops_app_campaigns) is
    # intentionally left empty once Twitch's own inventory is authoritative
    # for that game, so the condition must also honor active_drop_campaigns,
    # which is where the real allow-list ends up in that case.
    twitch = bare_twitch(monkeypatch)
    twitch.active_drop_campaigns = {
        "example-game": [
            {
                "id": "restricted-campaign-1",
                "name": "Restricted Campaign",
                "channels": ["channel"],
            }
        ]
    }
    stream = SimpleNamespace(
        campaigns_ids=[],
        game_name=lambda: "Example Game",
    )
    streamer = SimpleNamespace(
        username="channel",
        from_category=True,
        from_badge_campaign=False,
        settings=SimpleNamespace(claim_drops=True),
        is_online=True,
        stream=stream,
    )

    assert twitch._Twitch__category_drops_condition(streamer) is True


def test_category_condition_blocks_channel_not_in_authoritative_restricted_allowlist(
    monkeypatch,
):
    twitch = bare_twitch(monkeypatch)
    twitch.active_drop_campaigns = {
        "example-game": [
            {
                "id": "restricted-campaign-1",
                "name": "Restricted Campaign",
                "channels": ["some-other-channel"],
            }
        ]
    }
    stream = SimpleNamespace(
        campaigns_ids=[],
        game_name=lambda: "Example Game",
    )
    streamer = SimpleNamespace(
        username="channel",
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
    monkeypatch.setattr(Twitch, "_Twitch__get_inventory", lambda self: inventory)
    twitch_module = importlib.import_module("TwitchChannelPointsMiner.classes.Twitch")
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


def test_completed_badge_campaign_signatures_matches_by_name_and_end_time(
    monkeypatch,
):
    twitch = bare_twitch(monkeypatch)
    inventory = {
        "completedRewardCampaigns": [
            {
                "campaign": {
                    "id": "twitch-real-id-1",
                    "game": {"displayName": "Infinity Nikki"},
                    "name": "Infinity Nikki Drops Campaign",
                    "endAt": "2026-08-01T13:58:17.429Z",
                }
            },
            # Missing end time: must not produce a signature, mirroring
            # __fallback_reward_was_captured's strictness (no exact-equality
            # fallback when tolerance-based comparison can't be done).
            {
                "campaign": {
                    "id": "twitch-real-id-2",
                    "game": {"displayName": "No End Time Game"},
                    "name": "Some Campaign",
                }
            },
        ]
    }

    signatures = twitch.completed_badge_campaign_signatures(inventory)

    assert len(signatures) == 1
    (game_slug, campaign_name, ends_at_epoch), = signatures
    assert game_slug == "infinity-nikki"
    assert campaign_name == "infinity nikki drops campaign"
    from datetime import datetime, timezone

    expected_epoch = datetime(
        2026, 8, 1, 13, 58, 17, 429000, tzinfo=timezone.utc
    ).timestamp()
    assert abs(ends_at_epoch - expected_epoch) < 0.01


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


def test_all_captured_unclaimed_inventory_drops_confirm_campaign_completion(
    monkeypatch,
):
    twitch = bare_twitch(monkeypatch)
    data = campaign_data()
    data["timeBasedDrops"][0]["self"] = {
        "hasPreconditionsMet": True,
        "currentMinutesWatched": 10,
        "dropInstanceID": "instance-1",
        "isClaimed": False,
    }

    completed = twitch._Twitch__completed_campaign_ids_from_inventory(
        {"dropCampaignsInProgress": [data]}
    )

    assert completed == {"campaign-1"}


def test_partially_captured_inventory_campaign_is_not_completed(monkeypatch):
    twitch = bare_twitch(monkeypatch)
    data = campaign_data()
    data["timeBasedDrops"].append(
        {
            "id": "drop-2",
            "name": "Second Reward",
            "benefitEdges": [{"benefit": {"name": "Badge"}}],
            "requiredMinutesWatched": 10,
            "startAt": "2020-01-01T00:00:00Z",
            "endAt": "2099-01-01T00:00:00Z",
        }
    )
    data["timeBasedDrops"][0]["self"] = {
        "hasPreconditionsMet": True,
        "currentMinutesWatched": 10,
        "dropInstanceID": "instance-1",
        "isClaimed": False,
    }
    data["timeBasedDrops"][1]["self"] = {
        "hasPreconditionsMet": True,
        "currentMinutesWatched": 4,
        "dropInstanceID": None,
        "isClaimed": False,
    }

    completed = twitch._Twitch__completed_campaign_ids_from_inventory(
        {"dropCampaignsInProgress": [data]}
    )

    assert completed == set()


def test_fully_captured_unclaimed_campaign_stops_category_watch(monkeypatch):
    twitch = bare_twitch(monkeypatch)
    data = campaign_data()
    data["timeBasedDrops"][0]["self"] = {
        "hasPreconditionsMet": True,
        "currentMinutesWatched": 10,
        "dropInstanceID": "instance-1",
        "isClaimed": False,
    }
    twitch.completed_drop_campaigns.update(
        twitch._Twitch__completed_campaign_ids_from_inventory(
            {"dropCampaignsInProgress": [data]}
        )
    )
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


def _account_wide_evaluation(monkeypatch, accepted_drop_ids):
    twitch = bare_twitch(monkeypatch)
    twitch.accepted_drop_ids = frozenset(accepted_drop_ids)
    inventory_campaign = campaign_data()
    inventory_campaign["timeBasedDrops"][0]["self"] = {
        "hasPreconditionsMet": True,
        "currentMinutesWatched": 5,
        "dropInstanceID": "instance-1",
        "isClaimed": False,
    }
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_drops_dashboard",
        lambda self, status="OPEN": [campaign_data()],
    )
    monkeypatch.setattr(
        Twitch, "_Twitch__get_reward_campaigns_raw_query", lambda self: ([], [])
    )
    monkeypatch.setattr(
        Twitch, "_Twitch__get_open_drop_campaigns_from_helix", lambda self: ([], [])
    )
    monkeypatch.setattr(
        Twitch, "_Twitch__get_campaigns_details", lambda self, campaigns: campaigns
    )
    monkeypatch.setattr(
        Twitch, "_Twitch__awarded_benefits", lambda self, inventory: (set(), set())
    )
    deadlines, _games = twitch._Twitch__active_drop_category_slugs_from_campaigns(
        {"dropCampaignsInProgress": [inventory_campaign]}, {"example-game"}
    )
    return twitch, deadlines


def test_account_wide_evaluation_counts_accepted_claims_as_completed(monkeypatch):
    # The inventory may keep listing an accepted drop unclaimed, so the
    # account-wide evaluation must not mark its campaign active (which also
    # skips the per-channel recheck of that campaign).
    twitch, deadlines = _account_wide_evaluation(monkeypatch, {"drop-1"})

    assert deadlines == {}
    assert twitch.active_drop_campaigns == {}


def test_account_wide_evaluation_keeps_campaign_without_accepted_claim(monkeypatch):
    _twitch, deadlines = _account_wide_evaluation(monkeypatch, set())

    assert set(deadlines) == {"example-game"}


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

    deadlines, twitch_games = twitch._Twitch__active_drop_category_slugs_from_campaigns(
        {
            "dropCampaignsInProgress": [],
            "completedRewardCampaigns": [{"id": "campaign-1"}],
        },
        {"example-game"},
    )

    assert deadlines == {}
    assert twitch_games == {"example-game"}


def test_completed_campaign_stays_authoritative_by_name_and_leaves_advertised_cache(
    monkeypatch,
):
    # Regression: a completed campaign that a watched channel advertised
    # earlier kept being re-injected from the session cache, and its game was
    # treated as fully authoritative, hiding the game's other gist campaigns
    # until the miner restarted.
    twitch = bare_twitch(monkeypatch)
    twitch.advertised_drop_campaigns = {"campaign-1": advertised_campaign()}
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
        lambda self, campaigns, **kwargs: campaigns,
    )
    monkeypatch.setattr(
        Twitch,
        "_Twitch__awarded_benefits",
        lambda self, inventory: (set(), set()),
    )

    twitch._Twitch__active_drop_category_slugs_from_campaigns(
        {"dropCampaignsInProgress": []}, {"example-game"}
    )
    assert twitch.twitch_campaign_names_by_game == {
        "example-game": {"example campaign"}
    }

    _, twitch_games = twitch._Twitch__active_drop_category_slugs_from_campaigns(
        {
            "dropCampaignsInProgress": [],
            "completedRewardCampaigns": [{"id": "campaign-1"}],
        },
        {"example-game"},
    )

    assert twitch.advertised_drop_campaigns == {}
    assert twitch_games == {"example-game"}
    assert twitch.twitch_campaign_names_by_game == {
        "example-game": {"example campaign"}
    }


def test_reward_campaign_tag_survives_initial_merge_with_untagged_source(monkeypatch):
    # Regression test: the initial dashboard+raw_query+helix merge replaces
    # an existing campaigns_by_id entry wholesale whenever the incoming one
    # has timeBasedDrops and the existing one doesn't, with no carry-over of
    # _is_reward_campaign - silently un-excluding a purchase-gated reward
    # campaign if a later, untagged source (e.g. helix) reports the same id.
    twitch = bare_twitch(monkeypatch)

    reward_campaign = campaign_data()
    reward_campaign["timeBasedDrops"] = []
    reward_campaign["_is_reward_campaign"] = True

    helix_campaign = campaign_data()  # Same id, has timeBasedDrops, no tag.

    monkeypatch.setattr(
        Twitch, "_Twitch__get_drops_dashboard", lambda self, status="OPEN": []
    )
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_reward_campaigns_raw_query",
        lambda self: ([reward_campaign], []),
    )
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_open_drop_campaigns_from_helix",
        lambda self: ([helix_campaign], []),
    )
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_campaigns_details",
        lambda self, campaigns: campaigns,
    )
    monkeypatch.setattr(
        Twitch, "_Twitch__awarded_benefits", lambda self, inventory: (set(), set())
    )

    twitch._Twitch__active_drop_category_slugs_from_campaigns(
        {"dropCampaignsInProgress": []}, {"example-game"}
    )

    assert "campaign-1" in twitch.reward_campaign_ids


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


def test_subscription_reward_campaign_is_never_active_incomplete(monkeypatch):
    # Regression: a purchase-gated Reward Campaign (e.g. "subscribe or gift a
    # sub to claim this badge") must never be treated as a minable, watch-time
    # drop campaign, and its (correctly negative) verdict must not poison the
    # game's category eligibility for a separate, genuinely watchable
    # campaign that only the external gist fallback knows about.
    twitch = bare_twitch(monkeypatch)
    reward_campaign = {
        "id": "reward-campaign-1",
        "game": {"displayName": "Example Game"},
        "name": "Sub Badge Launch",
        "status": "ACTIVE",
        "allow": {"channels": []},
        "startAt": "2020-01-01T00:00:00Z",
        "endAt": "2099-01-01T00:00:00Z",
        "_is_reward_campaign": True,
        "timeBasedDrops": [
            {
                "id": "sub-badge-drop",
                "name": "Sub Badge",
                "benefitEdges": [{"benefit": {"name": "Sub Badge"}}],
                "requiredMinutesWatched": 0,
                "startAt": "2020-01-01T00:00:00Z",
                "endAt": "2099-01-01T00:00:00Z",
            }
        ],
    }
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_drops_dashboard",
        lambda self, status="OPEN": [reward_campaign],
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
        lambda self, campaigns, campaign_channel_id_by_id=None: campaigns,
    )
    monkeypatch.setattr(
        Twitch,
        "_Twitch__awarded_benefits",
        lambda self, inventory: (set(), set()),
    )

    deadlines, twitch_games = twitch._Twitch__active_drop_category_slugs_from_campaigns(
        {}, {"example-game"}
    )

    # No watchable campaign was found -- correct.
    assert deadlines == {}
    # But Twitch must not be considered to have authoritatively evaluated
    # this game, since only a purchase-gated campaign was inspected: a
    # genuinely incomplete drop campaign the gist fallback finds for this
    # same game_slug must still be allowed through in
    # filter_categories_with_active_drops.
    assert twitch_games == set()
    assert twitch.active_drop_campaigns == {}


def test_watchable_campaign_survives_alongside_reward_campaign_for_same_game(
    monkeypatch,
):
    # Two campaigns for the same game: one purchase-gated (ignored), one
    # genuine watch-time drop campaign (must still be picked up normally).
    twitch = bare_twitch(monkeypatch)
    drop_campaign = campaign_data()
    drop_campaign["timeBasedDrops"][0]["self"] = {
        "hasPreconditionsMet": True,
        "currentMinutesWatched": 0,
        "dropInstanceID": None,
        "isClaimed": False,
    }
    reward_campaign = {
        "id": "reward-campaign-1",
        "game": {"displayName": "Example Game"},
        "name": "Sub Badge Launch",
        "status": "ACTIVE",
        "allow": {"channels": []},
        "startAt": "2020-01-01T00:00:00Z",
        "endAt": "2099-01-01T00:00:00Z",
        "_is_reward_campaign": True,
        "timeBasedDrops": [
            {
                "id": "sub-badge-drop",
                "name": "Sub Badge",
                "benefitEdges": [{"benefit": {"name": "Sub Badge"}}],
                "requiredMinutesWatched": 0,
                "startAt": "2020-01-01T00:00:00Z",
                "endAt": "2099-01-01T00:00:00Z",
            }
        ],
    }
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_drops_dashboard",
        lambda self, status="OPEN": [drop_campaign, reward_campaign],
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
        lambda self, campaigns, campaign_channel_id_by_id=None: campaigns,
    )
    monkeypatch.setattr(
        Twitch,
        "_Twitch__awarded_benefits",
        lambda self, inventory: (set(), set()),
    )

    deadlines, twitch_games = twitch._Twitch__active_drop_category_slugs_from_campaigns(
        {}, {"example-game"}
    )

    assert set(deadlines) == {"example-game"}
    assert twitch_games == {"example-game"}
    assert twitch.active_drop_campaigns == {
        "example-game": [
            {"id": "campaign-1", "name": "Example Campaign", "channels": []}
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

    deadlines, twitch_games = twitch._Twitch__active_drop_category_slugs_from_campaigns(
        {"completedRewardCampaigns": [{"id": "campaign-1"}]},
        {"example-game"},
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

    deadlines, twitch_games = twitch._Twitch__active_drop_category_slugs_from_campaigns(
        {"completedRewardCampaigns": [completed_campaign]},
        {"minecraft"},
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

    deadlines, twitch_games = twitch._Twitch__active_drop_category_slugs_from_campaigns(
        {"completedRewardCampaigns": [completed_record]},
        {"warhounds"},
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


class _SyncThread:
    def __init__(self, target, args=(), name=None, daemon=None):
        self._target = target
        self._args = args

    def start(self):
        self._target(*self._args)


def prompt_claim_twitch(monkeypatch, claimed):
    twitch_module = importlib.import_module("TwitchChannelPointsMiner.classes.Twitch")
    monkeypatch.setattr(twitch_module, "Thread", _SyncThread)
    monkeypatch.setattr(
        Twitch,
        "claim_all_drops_from_inventory",
        lambda self: claimed.append("claim"),
    )
    twitch = object.__new__(Twitch)
    twitch.prompt_claim_lock = Lock()
    twitch.prompt_claim_pass_lock = Lock()
    twitch.prompt_claim_last = {}
    return twitch


def test_prompt_claim_debounce_is_per_drop(monkeypatch):
    claimed = []
    twitch = prompt_claim_twitch(monkeypatch, claimed)
    drop = SimpleNamespace(
        name="Reward", minutes_required=10, drop_instance_id="instance-1"
    )
    other_drop = SimpleNamespace(
        name="Second Reward", minutes_required=10, drop_instance_id="instance-2"
    )
    campaign = SimpleNamespace(id="campaign-1", name="Example campaign")

    twitch._Twitch__claim_completed_drop_promptly(drop, campaign)
    twitch._Twitch__claim_completed_drop_promptly(drop, campaign)
    twitch._Twitch__claim_completed_drop_promptly(other_drop, campaign)

    # Repeats for the same drop are debounced, but a second drop completing
    # within the debounce window still gets its own prompt claim.
    assert claimed == ["claim", "claim"]
    assert set(twitch.prompt_claim_last) == {"instance-1", "instance-2"}


def test_prompt_claim_never_debounces_a_first_claim_on_a_fresh_clock(monkeypatch):
    # time.monotonic() is time since boot, so on a fresh machine it can be
    # below the debounce window. A first claim must not be treated as a
    # repeat of a "never claimed" default.
    claimed = []
    twitch = prompt_claim_twitch(monkeypatch, claimed)
    twitch_module = importlib.import_module("TwitchChannelPointsMiner.classes.Twitch")
    monkeypatch.setattr(twitch_module.time, "monotonic", lambda: 100.0)
    drop = SimpleNamespace(
        name="Reward", minutes_required=10, drop_instance_id="instance-1"
    )
    campaign = SimpleNamespace(id="campaign-1", name="Example campaign")

    twitch._Twitch__claim_completed_drop_promptly(drop, campaign)

    assert claimed == ["claim"]
    assert set(twitch.prompt_claim_last) == {"instance-1"}


def test_prompt_claim_skips_while_another_claim_pass_is_running(monkeypatch):
    claimed = []
    twitch = prompt_claim_twitch(monkeypatch, claimed)
    drop = SimpleNamespace(
        name="Reward", minutes_required=10, drop_instance_id="instance-1"
    )
    campaign = SimpleNamespace(id="campaign-1", name="Example campaign")

    twitch.prompt_claim_pass_lock.acquire()
    try:
        twitch._Twitch__claim_completed_drop_promptly(drop, campaign)
    finally:
        twitch.prompt_claim_pass_lock.release()

    # Nothing was actually claimed (another pass was running), so the
    # optimistically-recorded debounce entry must be cleared rather than left
    # behind unusable - the sync cycle still covers this drop on its own
    # ~30-minute cadence, independent of this debounce.
    assert claimed == []
    assert twitch.prompt_claim_last == {}


def test_prompt_claim_clears_debounce_when_claim_raises(monkeypatch):
    twitch_module = importlib.import_module("TwitchChannelPointsMiner.classes.Twitch")
    monkeypatch.setattr(twitch_module, "Thread", _SyncThread)
    monkeypatch.setattr(
        Twitch,
        "claim_all_drops_from_inventory",
        lambda self: (_ for _ in ()).throw(RuntimeError("transient failure")),
    )
    twitch = object.__new__(Twitch)
    twitch.prompt_claim_lock = Lock()
    twitch.prompt_claim_pass_lock = Lock()
    twitch.prompt_claim_last = {}
    drop = SimpleNamespace(
        name="Reward", minutes_required=10, drop_instance_id="instance-1"
    )
    campaign = SimpleNamespace(id="campaign-1", name="Example campaign")

    twitch._Twitch__claim_completed_drop_promptly(drop, campaign)

    # The claim attempt failed, so the debounce entry must be cleared rather
    # than left behind unusable.
    assert twitch.prompt_claim_last == {}
