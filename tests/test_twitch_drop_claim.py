import copy
import importlib
import logging
import re
import threading
import time
from datetime import datetime, timedelta
from threading import Lock
from types import SimpleNamespace

import pytest

from TwitchChannelPointsMiner.classes.entities.Campaign import Campaign
from TwitchChannelPointsMiner.classes.gql.Errors import RetryError
from TwitchChannelPointsMiner.classes.Twitch import (
    _drops_dashboard_state,
    CHANNEL_QUERY_FAILURE_LIMIT,
    Twitch,
)
from TwitchChannelPointsMiner.classes.entities.Drop import utc_now
from TwitchChannelPointsMiner.constants import ANDROID_CLIENT_ID, CLIENT_ID


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
    twitch.campaign_lookup_unavailable = False
    twitch.unevaluated_category_slugs = set()
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

    # No allow block is invented: one would read as open to all channels.
    assert "allow" not in normalized
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

    def campaign_details(self, campaigns, campaign_channel_id_by_id=None, **kwargs):
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
        lambda self, campaigns, campaign_channel_id_by_id=None, **kwargs: [],
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
        lambda self, campaigns, campaign_channel_id_by_id=None, **kwargs: [],
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
        lambda self, campaigns, campaign_channel_id_by_id=None, **kwargs: [],
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
        "example-game": [{"id": "native-1", "name": "Native Campaign", "channels": []}]
    }

    names = twitch._Twitch__campaign_names_from_ids(
        ["advertised-1", "gist-1", "native-1", "unknown-id"], "example-game"
    )

    assert set(names) == {"Advertised Campaign", "Gist Campaign", "Native Campaign"}


def gist_open_campaign_twitch(monkeypatch):
    # Mirrors Hunt: Showdown 1896 (#210): every Drops-enabled channel lists
    # only a finished campaign, while the running weekly campaign, which
    # Twitch reports and the gist lists for all channels, is never listed
    # yet progresses on any of them.
    twitch = bare_twitch(monkeypatch)
    finished = campaign_data()
    finished["id"] = "finished-campaign"
    finished["timeBasedDrops"][0]["endAt"] = "2020-01-02T00:00:00Z"
    twitch.gql = SimpleNamespace(
        get_available_drops=lambda channel_id: SimpleNamespace(
            campaigns=[finished], campaigns_available=True
        )
    )
    twitch.discovered_open_drop_campaigns = []
    twitch.active_drop_campaigns = {
        "example-game": [
            {
                "id": "weekly-campaign",
                "name": "Weekly Campaign",
                "all_channels": True,
                "channels": [],
            }
        ]
    }
    twitch.twitchdrops_app_open_reported_names = {"example-game": {"weekly campaign"}}
    return twitch


def test_channel_query_omitting_gist_open_campaign_keeps_channel(monkeypatch):
    twitch = gist_open_campaign_twitch(monkeypatch)

    assert twitch._Twitch__get_campaign_ids_from_streamer(category_streamer()) == [
        "weekly-campaign"
    ]
    assert twitch.category_campaign_eligibility[("example-game", "drops-channel")] == (
        1,
        1,
    )


def test_gist_open_campaign_keeps_twitch_completion_verdict(monkeypatch):
    twitch = gist_open_campaign_twitch(monkeypatch)
    twitch.completed_drop_campaigns = {"weekly-campaign"}

    twitch._Twitch__get_campaign_ids_from_streamer(category_streamer())

    assert twitch.category_campaign_eligibility[("example-game", "drops-channel")] == (
        0,
        1,
    )


def test_unlisted_twitch_campaign_without_gist_scope_still_blocked(monkeypatch):
    # Without the gist listing it for all channels, an unlisted campaign with
    # no allow-list may be a per-streamer one (Rust) and must not qualify.
    twitch = gist_open_campaign_twitch(monkeypatch)
    twitch.twitchdrops_app_open_reported_names = {}

    assert twitch._Twitch__get_campaign_ids_from_streamer(category_streamer()) == []
    assert twitch.category_campaign_eligibility[("example-game", "drops-channel")] == (
        0,
        0,
    )


def listing_collected_campaign_twitch(monkeypatch):
    # Hunt: Showdown 1896's channels list its running Halloween campaign,
    # which this account already collected, and still not the weekly one.
    twitch = gist_open_campaign_twitch(monkeypatch)
    collected = campaign_data()
    collected["id"] = "halloween-campaign"
    twitch.gql = SimpleNamespace(
        get_available_drops=lambda channel_id: SimpleNamespace(
            campaigns=[collected], campaigns_available=True
        )
    )
    twitch.campaign_detail_attempts = {("halloween-campaign", "12345")}
    twitch.awarded_game_event_drops = {}
    twitch.completed_drop_campaigns = {"halloween-campaign"}
    return twitch


def test_channel_listing_another_campaign_keeps_gist_open_campaign(monkeypatch):
    twitch = listing_collected_campaign_twitch(monkeypatch)

    assert twitch._Twitch__get_campaign_ids_from_streamer(category_streamer()) == [
        "halloween-campaign",
        "weekly-campaign",
    ]
    assert twitch.category_campaign_eligibility[("example-game", "drops-channel")] == (
        1,
        2,
    )


def test_configured_channel_listing_another_campaign_keeps_gist_open_campaign(
    monkeypatch,
):
    twitch = listing_collected_campaign_twitch(monkeypatch)
    streamer = category_streamer()
    streamer.from_category = False

    assert twitch._Twitch__get_campaign_ids_from_streamer(streamer) == [
        "halloween-campaign",
        "weekly-campaign",
    ]


def test_channel_listing_another_campaign_without_gist_scope_lists_only_it(
    monkeypatch,
):
    twitch = listing_collected_campaign_twitch(monkeypatch)
    twitch.twitchdrops_app_open_reported_names = {}

    assert twitch._Twitch__get_campaign_ids_from_streamer(category_streamer()) == [
        "halloween-campaign"
    ]
    assert twitch.category_campaign_eligibility[("example-game", "drops-channel")] == (
        0,
        1,
    )


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
    from datetime import timedelta

    twitch = per_streamer_campaigns_twitch(
        monkeypatch,
        lambda channel_id: SimpleNamespace(campaigns=[], campaigns_available=False),
    )
    twitch.active_drop_campaigns["example-game"].append(
        {"id": "sword", "name": "Sword", "channels": ["drops-channel"]}
    )
    twitch.category_campaign_deadlines = {
        "example-game": utc_now() - timedelta(minutes=1)
    }

    twitch._Twitch__get_campaign_ids_from_streamer(category_streamer())

    assert twitch.category_campaign_eligibility[("example-game", "drops-channel")] == (
        0,
        1,
    )


def fallback_twitch_with_full_campaign(monkeypatch, minutes_left):
    # The gist summary carries no drops, but Twitch already returned the full
    # campaign to another channel's query.
    from datetime import timedelta

    twitch = per_streamer_campaigns_twitch(
        monkeypatch,
        lambda channel_id: SimpleNamespace(campaigns=[], campaigns_available=False),
    )
    twitch.awarded_game_event_drops = {}
    twitch.twitchdrops_app_campaigns["example-game"].append(
        {"id": "gist-open", "name": "General Drops", "channels": []}
    )
    now = utc_now()
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


def test_fresh_negative_verdict_is_cleared_when_channel_becomes_eligible(monkeypatch):
    # #150: a fresh negative verdict must not keep hiding a channel that became
    # eligible. The positive check clears it, so discovery's seed (which defers
    # to fresh verdicts) cannot pull the channel back to ineligible.
    twitch = per_streamer_campaigns_twitch(
        monkeypatch,
        lambda channel_id: SimpleNamespace(campaigns=[], campaigns_available=False),
    )
    twitch_module = importlib.import_module("TwitchChannelPointsMiner.classes.Twitch")
    now = [10_000.0]
    monkeypatch.setattr(twitch_module.time, "time", lambda: now[0])
    twitch.twitchdrops_app_campaigns["example-game"].append(
        {"id": "gist-open", "name": "General Drops", "channels": []}
    )
    key = ("example-game", "drops-channel")
    twitch.category_campaign_eligibility[key] = (0, 0)
    twitch.category_channel_verdicts = {key: ((0, 0), now[0] - 60)}
    twitch.evaluated_category_campaigns = {"example-game"}
    streamer = category_streamer()
    streamer.is_online = True
    streamer.settings = SimpleNamespace(claim_drops=True)
    streamer.stream.campaigns_ids = []

    assert twitch._Twitch__get_campaign_ids_from_streamer(streamer) == ["gist-open"]
    assert twitch.category_campaign_eligibility[key] == (1, 1)
    assert key not in twitch.category_channel_verdicts

    twitch._Twitch__replace_category_campaign_eligibility(
        "example-game", {"drops-channel": (1, 1)}
    )

    assert twitch.category_campaign_eligibility[key] == (1, 1)


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


def claim_response(status):
    # A claim status is a str, or a (status, is_user_account_connected) tuple
    # for a campaign whose game account may not be linked.
    if isinstance(status, tuple):
        status, connected = status
        return SimpleNamespace(
            status=status, errors=[], is_user_account_connected=connected
        )
    return SimpleNamespace(status=status, errors=[])


def unlinked_campaign_link(connected=False, url="https://example.test/link"):
    return {"campaign_id": "campaign-1", "connected": connected, "url": url}


def claim_recording_twitch(monkeypatch, statuses=("ELIGIBLE_FOR_ALL",)):
    # Records claim requests and analytics saves; `statuses` are returned in
    # order, repeating the last one. The clock is advanced through `now`.
    twitch = bare_twitch(monkeypatch)
    requests = []
    saved = []

    def claim_drop_rewards(drop_instance_id):
        requests.append(drop_instance_id)
        return claim_response(statuses[min(len(requests), len(statuses)) - 1])

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


def link_required_events(caplog):
    twitch_module = importlib.import_module("TwitchChannelPointsMiner.classes.Twitch")
    return [
        record
        for record in caplog.records
        if getattr(record, "event", None) == twitch_module.Events.DROP_LINK_REQUIRED
    ]


def test_unlinked_claim_warns_once_without_claim_event(monkeypatch, caplog):
    # Twitch accepts a claim for a campaign whose game account is not linked
    # but delivers nothing, and the inventory keeps the drop unclaimed. That
    # is not a claim: one warning per campaign, no DROP_CLAIM, no analytics.
    twitch, drop, requests, saved, _now = claim_recording_twitch(
        monkeypatch, statuses=(("ELIGIBLE_FOR_ALL", False),)
    )
    link = unlinked_campaign_link()
    sibling = SimpleNamespace(id="drop-2", drop_instance_id="instance-2", name="Crate")
    other = SimpleNamespace(id="drop-3", drop_instance_id="instance-3", name="Skin")
    other_link = {"campaign_id": "campaign-2", "connected": False, "url": None}

    with caplog.at_level(logging.INFO):
        for claimed_drop in (drop, sibling):
            assert (
                twitch.claim_drop(
                    claimed_drop,
                    campaign_link=link,
                    campaign_name_override="Hunt",
                    category_name_override="Hunt Game",
                )
                is False
            )
        assert (
            twitch.claim_drop(
                other, campaign_link=other_link, campaign_name_override="Other"
            )
            is False
        )

    assert requests == ["instance-1", "instance-2", "instance-3"]
    assert saved == []
    assert drop_claim_events(caplog) == []
    warnings = link_required_events(caplog)
    assert [record.levelno for record in warnings] == [logging.WARNING] * 2
    assert (
        'Can\'t claim Drops from "Hunt" (Hunt Game): link your game account at '
        "https://example.test/link. Earned Drops stay claimable for a while "
        "after linking." == warnings[0].getMessage()
    )
    assert (
        'Can\'t claim Drops from "Other" (Unknown game): link your game account. '
        "Earned Drops stay claimable for a while after linking."
        == warnings[1].getMessage()
    )
    assert twitch.accepted_drop_claims == {}
    assert set(twitch.unlinked_drop_claims) == {
        "instance-1",
        "instance-2",
        "instance-3",
    }
    assert twitch._Twitch__finished_drop_ids() >= {"drop-1", "drop-2", "drop-3"}


def test_unlinked_claim_is_not_retried_hourly(monkeypatch, caplog):
    from TwitchChannelPointsMiner.classes.Twitch import (
        ACCEPTED_CLAIM_RETRY_SECONDS,
        UNLINKED_CLAIM_RETRY_SECONDS,
    )

    twitch, drop, requests, _saved, now = claim_recording_twitch(
        monkeypatch, statuses=(("ELIGIBLE_FOR_ALL", False),)
    )
    link = unlinked_campaign_link()

    with caplog.at_level(logging.INFO):
        assert twitch.claim_drop(drop, campaign_link=link) is False
        now[0] += ACCEPTED_CLAIM_RETRY_SECONDS
        assert twitch.claim_drop(drop, campaign_link=link) is False
        assert requests == ["instance-1"]
        now[0] += UNLINKED_CLAIM_RETRY_SECONDS - ACCEPTED_CLAIM_RETRY_SECONDS
        assert twitch.claim_drop(drop, campaign_link=link) is False

    assert requests == ["instance-1", "instance-1"]
    assert len(link_required_events(caplog)) == 1
    assert twitch.unlinked_drop_claims["instance-1"]["stamp"] == now[0]


def test_unlinked_claim_retries_when_inventory_reports_linked(monkeypatch, caplog):
    twitch, drop, requests, saved, now = claim_recording_twitch(
        monkeypatch,
        statuses=(("ELIGIBLE_FOR_ALL", False), ("ELIGIBLE_FOR_ALL", True)),
    )

    with caplog.at_level(logging.INFO):
        assert (
            twitch.claim_drop(
                drop,
                campaign_link=unlinked_campaign_link(),
                campaign_name_override="Hunt",
            )
            is False
        )
        now[0] += 60
        assert (
            twitch.claim_drop(
                drop,
                campaign_link=unlinked_campaign_link(connected=True),
                campaign_name_override="Hunt",
            )
            is True
        )

    assert requests == ["instance-1", "instance-1"]
    assert len(saved) == 1
    assert len(drop_claim_events(caplog)) == 1
    events = link_required_events(caplog)
    assert [record.levelno for record in events] == [logging.WARNING, logging.INFO]
    assert (
        'Drops from "Hunt" have been claimed; no longer waiting on a game '
        "account link" == events[1].getMessage()
    )
    assert twitch.unlinked_drop_claims == {}
    assert twitch.accepted_drop_claims == {"instance-1": now[0]}


def test_linked_campaign_logs_once_after_its_last_drop(monkeypatch, caplog):
    twitch, drop, _requests, _saved, now = claim_recording_twitch(
        monkeypatch,
        statuses=(
            ("ELIGIBLE_FOR_ALL", False),
            ("ELIGIBLE_FOR_ALL", False),
            ("ELIGIBLE_FOR_ALL", True),
        ),
    )
    sibling = SimpleNamespace(id="drop-2", drop_instance_id="instance-2", name="Crate")

    with caplog.at_level(logging.INFO):
        twitch.claim_drop(drop, campaign_link=unlinked_campaign_link())
        twitch.claim_drop(sibling, campaign_link=unlinked_campaign_link())
        now[0] += 60
        linked = unlinked_campaign_link(connected=True)
        assert twitch.claim_drop(drop, campaign_link=linked) is True
        assert twitch.claim_drop(sibling, campaign_link=linked) is True

    events = link_required_events(caplog)
    assert [record.levelno for record in events] == [logging.WARNING, logging.INFO]
    assert len(drop_claim_events(caplog)) == 2
    assert twitch.unlinked_drop_claims == {}


def test_linked_inventory_with_unlinked_response_does_not_rewarn(monkeypatch, caplog):
    # The inventory flag flipping to connected is an edge, not a level: the
    # sync pass runs every minute, so a stale flag must not claim each time.
    from TwitchChannelPointsMiner.classes.Twitch import UNLINKED_CLAIM_RETRY_SECONDS

    twitch, drop, requests, _saved, now = claim_recording_twitch(
        monkeypatch, statuses=(("ELIGIBLE_FOR_ALL", False),)
    )
    linked = unlinked_campaign_link(connected=True)

    with caplog.at_level(logging.INFO):
        assert twitch.claim_drop(drop, campaign_link=unlinked_campaign_link()) is False
        now[0] += 60
        assert twitch.claim_drop(drop, campaign_link=linked) is False
        assert requests == ["instance-1", "instance-1"]
        for _ in range(3):
            now[0] += 60
            assert twitch.claim_drop(drop, campaign_link=linked) is False
        assert requests == ["instance-1", "instance-1"]
        now[0] += UNLINKED_CLAIM_RETRY_SECONDS
        assert twitch.claim_drop(drop, campaign_link=linked) is False

    assert requests == ["instance-1"] * 3
    assert len(link_required_events(caplog)) == 1
    assert twitch.unlinked_drop_claims["instance-1"]["connected"] is True


def test_fallback_retry_that_succeeds_claims_once(monkeypatch, caplog):
    # A response without the flag counts as connected, as before this flag
    # was read.
    from TwitchChannelPointsMiner.classes.Twitch import UNLINKED_CLAIM_RETRY_SECONDS

    twitch, drop, requests, saved, now = claim_recording_twitch(
        monkeypatch, statuses=(("ELIGIBLE_FOR_ALL", False), "ELIGIBLE_FOR_ALL")
    )
    link = unlinked_campaign_link()

    with caplog.at_level(logging.INFO):
        assert twitch.claim_drop(drop, campaign_link=link) is False
        now[0] += UNLINKED_CLAIM_RETRY_SECONDS
        assert twitch.claim_drop(drop, campaign_link=link) is True
        now[0] += 60
        assert twitch.claim_drop(drop, campaign_link=link) is True

    assert requests == ["instance-1", "instance-1"]
    assert len(saved) == 1
    assert len(drop_claim_events(caplog)) == 1
    assert twitch.unlinked_drop_claims == {}


def test_already_claimed_with_unlinked_flag_is_treated_as_claimed(monkeypatch, caplog):
    # Claimed while linked, unlinked since: the reward was delivered.
    twitch, drop, _requests, _saved, _now = claim_recording_twitch(
        monkeypatch, statuses=(("DROP_INSTANCE_ALREADY_CLAIMED", False),)
    )

    with caplog.at_level(logging.INFO):
        assert twitch.claim_drop(drop, campaign_link=unlinked_campaign_link()) is True

    assert link_required_events(caplog) == []
    assert twitch.unlinked_drop_claims == {}
    assert "instance-1" in twitch.accepted_drop_claims


def test_unlinked_retry_of_accepted_claim_unaccepts_it(monkeypatch, caplog):
    from TwitchChannelPointsMiner.classes.Twitch import ACCEPTED_CLAIM_RETRY_SECONDS

    twitch, drop, _requests, _saved, now = claim_recording_twitch(
        monkeypatch, statuses=("ELIGIBLE_FOR_ALL", ("ELIGIBLE_FOR_ALL", False))
    )
    link = unlinked_campaign_link()

    with caplog.at_level(logging.INFO):
        assert twitch.claim_drop(drop, campaign_link=link) is True
        now[0] += ACCEPTED_CLAIM_RETRY_SECONDS
        assert twitch.claim_drop(drop, campaign_link=link) is False

    assert twitch.accepted_drop_claims == {}
    assert "instance-1" in twitch.unlinked_drop_claims
    assert len(link_required_events(caplog)) == 1


def unlinked_inventory_campaign():
    data = campaign_data()
    data["self"] = {"isAccountConnected": False}
    data["accountLinkURL"] = "https://example.test/link"
    data["timeBasedDrops"][0]["self"] = {
        "hasPreconditionsMet": True,
        "currentMinutesWatched": 10,
        "dropInstanceID": "instance-1",
        "isClaimed": False,
    }
    return data


def test_sync_drops_path_keeps_unlinked_drop_unclaimed(monkeypatch, caplog):
    twitch = bare_twitch(monkeypatch)
    twitch.log_drop_checks = True
    twitch.gql = SimpleNamespace(
        claim_drop_rewards=lambda drop_instance_id: claim_response(
            ("ELIGIBLE_FOR_ALL", False)
        )
    )
    campaign = Campaign(campaign_data())
    progress = unlinked_inventory_campaign()
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_inventory",
        lambda self: {"dropCampaignsInProgress": [progress]},
    )

    with caplog.at_level(logging.DEBUG):
        synced = twitch._Twitch__sync_campaigns([campaign])

    assert [drop.id for drop in synced[0].drops] == ["drop-1"]
    assert synced[0].drops[0].is_claimed is False
    assert "waiting for inventory completion" not in caplog.text
    warnings = link_required_events(caplog)
    assert len(warnings) == 1
    assert (
        'Can\'t claim Drops from "Example Campaign" (Example Game): link your '
        "game account at https://example.test/link." in warnings[0].getMessage()
    )
    assert twitch.unlinked_drop_claims["instance-1"]["campaign_id"] == "campaign-1"
    assert "drop-1" in twitch._Twitch__finished_drop_ids()


def test_claim_all_drops_skips_completion_log_for_unlinked_campaign(
    monkeypatch, caplog
):
    twitch = bare_twitch(monkeypatch)
    twitch.log_drop_checks = True
    twitch.gql = SimpleNamespace(
        claim_drop_rewards=lambda drop_instance_id: claim_response(
            ("ELIGIBLE_FOR_ALL", False)
        )
    )
    data = unlinked_inventory_campaign()
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_inventory",
        lambda self: {"dropCampaignsInProgress": [data]},
    )
    twitch_module = importlib.import_module("TwitchChannelPointsMiner.classes.Twitch")
    monkeypatch.setattr(twitch_module.time, "sleep", lambda seconds: None)

    with caplog.at_level(logging.DEBUG):
        twitch.claim_all_drops_from_inventory()

    assert "final inventory drop claimed" not in caplog.text
    assert len(link_required_events(caplog)) == 1
    entry = twitch.unlinked_drop_claims["instance-1"]
    assert entry == {
        "stamp": entry["stamp"],
        "campaign_id": "campaign-1",
        "drop_id": "drop-1",
        "campaign": "Example Campaign",
        "category": "Example Game",
        "url": "https://example.test/link",
        "drop": "Reward",
        "connected": False,
    }


def test_unlinked_entries_age_out(monkeypatch):
    from TwitchChannelPointsMiner.classes.Twitch import UNLINKED_CLAIM_RETRY_SECONDS

    twitch, drop, _requests, _saved, now = claim_recording_twitch(
        monkeypatch, statuses=(("ELIGIBLE_FOR_ALL", False),)
    )

    assert twitch.claim_drop(drop, campaign_link=unlinked_campaign_link()) is False
    assert len(twitch.unlinked_drop_report()) == 1

    # The campaign left the inventory, so nothing claims the drop again.
    now[0] += 2 * UNLINKED_CLAIM_RETRY_SECONDS
    assert twitch.unlinked_drop_report() == []
    other = SimpleNamespace(id="drop-9", drop_instance_id="instance-9", name="Other")
    twitch.claim_drop(
        other, campaign_link={"campaign_id": "campaign-9", "connected": False}
    )
    assert set(twitch.unlinked_drop_claims) == {"instance-9"}


def test_unlinked_drop_report_groups_drops_by_campaign(monkeypatch):
    twitch, _drop, _requests, _saved, now = claim_recording_twitch(
        monkeypatch,
        statuses=(("ELIGIBLE_FOR_ALL", False),) * 5 + (("ELIGIBLE_FOR_ALL", True),),
    )
    hunt = {
        "campaign_id": "campaign-1",
        "connected": False,
        "url": "https://hunt.example/link",
    }
    zone = {"campaign_id": "campaign-2", "connected": False, "url": None}
    gone = {"campaign_id": "campaign-3", "connected": False, "url": None}

    def claim(name, instance, link, campaign, category):
        return twitch.claim_drop(
            SimpleNamespace(
                id=f"drop-{instance}", drop_instance_id=instance, name=name
            ),
            campaign_link=link,
            campaign_name_override=campaign,
            category_name_override=category,
        )

    claim("Supply Crate", "h1", hunt, "Hunt 1896", "Hunt: Showdown 1896")
    claim("Random Legendary Skin", "h2", hunt, "Hunt 1896", "Hunt: Showdown 1896")
    claim("Supply Crate", "h3", hunt, "Hunt 1896", "Hunt: Showdown 1896")
    claim("Keycard", "z1", zone, "Zone Drops", "Gray Zone Warfare")
    claim("Badge", "g1", gone, "Gone", "Gone Game")
    now[0] += 60
    assert claim("Badge", "g1", {**gone, "connected": True}, "Gone", "Gone Game")

    assert twitch.unlinked_drop_report() == [
        {
            "campaign_id": "campaign-2",
            "campaign": "Zone Drops",
            "category": "Gray Zone Warfare",
            "url": None,
            "linked": False,
            "count": 1,
            "drops": ["Keycard"],
        },
        {
            "campaign_id": "campaign-1",
            "campaign": "Hunt 1896",
            "category": "Hunt: Showdown 1896",
            "url": "https://hunt.example/link",
            "linked": False,
            "count": 3,
            "drops": ["Random Legendary Skin", "Supply Crate ×2"],
        },
    ]


def test_unlinked_drop_report_without_campaign_id_keeps_drops_apart(monkeypatch):
    twitch, _drop, _requests, _saved, _now = claim_recording_twitch(
        monkeypatch, statuses=(("ELIGIBLE_FOR_ALL", False),)
    )

    for instance, name in [("a1", "Badge"), ("b1", "Crate")]:
        twitch.claim_drop(
            SimpleNamespace(id=f"drop-{instance}", drop_instance_id=instance, name=name)
        )

    report = twitch.unlinked_drop_report()
    assert [group["drops"] for group in report] == [["Badge"], ["Crate"]]
    assert all(group["campaign_id"] is None for group in report)


def test_sync_pass_forgets_unlinked_claims_the_inventory_stopped_listing(
    monkeypatch, caplog
):
    from TwitchChannelPointsMiner.classes.Twitch import (
        UNLINKED_CLAIM_MISSING_GRACE_SECONDS,
    )

    twitch, drop, _requests, _saved, now = claim_recording_twitch(
        monkeypatch, statuses=(("ELIGIBLE_FOR_ALL", False),)
    )
    twitch.log_drop_checks = True
    sibling = SimpleNamespace(id="drop-2", drop_instance_id="instance-2", name="Crate")
    ended = SimpleNamespace(id="drop-9", drop_instance_id="instance-9", name="Badge")
    kept = SimpleNamespace(id="drop-5", drop_instance_id="instance-5", name="Skin")
    hunt = {"campaign_link": unlinked_campaign_link(), "campaign_name_override": "Hunt"}
    twitch.claim_drop(drop, **hunt)
    twitch.claim_drop(sibling, **hunt)
    twitch.claim_drop(ended, campaign_link={"campaign_id": "campaign-9"})
    twitch.claim_drop(kept, campaign_link={"campaign_id": "campaign-5"})
    assert len(twitch.unlinked_drop_report()) == 3

    def inventory_drop(drop_id, claimed, instance=None):
        return {
            "id": drop_id,
            "self": {"isClaimed": claimed, "dropInstanceID": instance},
        }

    # campaign-1's rewards were delivered once the account got linked,
    # campaign-9 ended, and campaign-5 still waits on its link.
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_inventory",
        lambda self: {
            "dropCampaignsInProgress": [
                {
                    "id": "campaign-1",
                    "timeBasedDrops": [
                        inventory_drop("drop-1", True),
                        inventory_drop("drop-2", True, "instance-2"),
                    ],
                },
                {
                    "id": "campaign-5",
                    "timeBasedDrops": [inventory_drop("drop-5", False, "instance-5")],
                },
            ]
        },
    )

    with caplog.at_level(logging.DEBUG):
        twitch._Twitch__sync_campaigns([])

    # Delivered rewards go at once; a missing drop survives short
    # inventories for the grace period and goes on the first sync after.
    assert [group["campaign_id"] for group in twitch.unlinked_drop_report()] == [
        "campaign-9",
        "campaign-5",
    ]
    events = link_required_events(caplog)
    assert [record.levelno for record in events[-1:]] == [logging.INFO]
    assert 'Drops from "Hunt" have been claimed' in events[-1].getMessage()
    assert "left the inventory" not in caplog.text

    now[0] += UNLINKED_CLAIM_MISSING_GRACE_SECONDS - 60
    twitch._Twitch__sync_campaigns([])
    assert len(twitch.unlinked_drop_report()) == 2

    now[0] += 60
    with caplog.at_level(logging.DEBUG):
        twitch._Twitch__sync_campaigns([])

    assert [group["campaign_id"] for group in twitch.unlinked_drop_report()] == [
        "campaign-5"
    ]
    assert len(link_required_events(caplog)) == len(events)
    assert "drop drop-9 of campaign campaign-9 left the inventory" in caplog.text


def test_skipped_unlinked_claim_picks_up_link_url(monkeypatch, caplog):
    twitch, drop, requests, _saved, now = claim_recording_twitch(
        monkeypatch, statuses=(("ELIGIBLE_FOR_ALL", False),)
    )

    with caplog.at_level(logging.INFO):
        twitch.claim_drop(drop, campaign_link={"campaign_id": "campaign-1"})
        now[0] += 60
        twitch.claim_drop(drop, campaign_link=unlinked_campaign_link())

    assert len(requests) == 1
    assert len(link_required_events(caplog)) == 1
    assert twitch.unlinked_drop_report()[0]["url"] == "https://example.test/link"


def test_sync_pass_keeps_unlinked_claims_through_short_inventories(monkeypatch):
    twitch, drop, _requests, _saved, _now = claim_recording_twitch(
        monkeypatch, statuses=(("ELIGIBLE_FOR_ALL", False),)
    )
    twitch.claim_drop(drop, campaign_link=unlinked_campaign_link())
    # Two empty inventories, a claimed listing of the same drop id under
    # another campaign, then a repeatable drop's earlier claimed instance
    # next to the pending one: none of these is this instance's delivery.
    inventories = iter(
        [
            [],
            [],
            [
                {
                    "id": "campaign-2",
                    "timeBasedDrops": [
                        {
                            "id": "drop-1",
                            "self": {"isClaimed": True, "dropInstanceID": "other"},
                        }
                    ],
                }
            ],
            [
                {
                    "id": "campaign-1",
                    "timeBasedDrops": [
                        {
                            "id": "drop-1",
                            "self": {"isClaimed": True, "dropInstanceID": "earlier"},
                        },
                        {
                            "id": "drop-1",
                            "self": {
                                "isClaimed": False,
                                "dropInstanceID": "instance-1",
                            },
                        },
                    ],
                }
            ],
        ]
    )
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_inventory",
        lambda self: {"dropCampaignsInProgress": next(inventories)},
    )

    for _ in range(4):
        twitch._Twitch__sync_campaigns([])

    assert [group["campaign_id"] for group in twitch.unlinked_drop_report()] == [
        "campaign-1"
    ]


def test_inventory_linked_but_claim_unlinked_retries_hourly(monkeypatch):
    from TwitchChannelPointsMiner.classes.Twitch import (
        ACCEPTED_CLAIM_RETRY_SECONDS,
        UNLINKED_CLAIM_RETRY_SECONDS,
    )

    twitch, drop, requests, _saved, now = claim_recording_twitch(
        monkeypatch, statuses=(("ELIGIBLE_FOR_ALL", False),)
    )
    twitch.claim_drop(drop, campaign_link=unlinked_campaign_link())
    linked = unlinked_campaign_link(connected=True)
    now[0] += 60
    twitch.claim_drop(drop, campaign_link=linked)
    assert len(requests) == 2
    now[0] += 60
    twitch.claim_drop(drop, campaign_link=linked)
    assert len(requests) == 2

    # The claim endpoint can lag the link: retry hourly, not six-hourly.
    assert ACCEPTED_CLAIM_RETRY_SECONDS < UNLINKED_CLAIM_RETRY_SECONDS
    now[0] += ACCEPTED_CLAIM_RETRY_SECONDS
    twitch.claim_drop(drop, campaign_link=linked)
    assert len(requests) == 3


def test_claim_all_drops_paces_only_sent_requests(monkeypatch):
    twitch = bare_twitch(monkeypatch)
    twitch.gql = SimpleNamespace(
        claim_drop_rewards=lambda drop_instance_id: claim_response(
            ("ELIGIBLE_FOR_ALL", False)
        )
    )
    data = unlinked_inventory_campaign()
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_inventory",
        lambda self: {"dropCampaignsInProgress": [data]},
    )
    twitch_module = importlib.import_module("TwitchChannelPointsMiner.classes.Twitch")
    sleeps = []
    monkeypatch.setattr(twitch_module.time, "sleep", lambda seconds: sleeps.append(1))

    twitch.claim_all_drops_from_inventory()
    twitch.claim_all_drops_from_inventory()

    assert len(sleeps) == 1


def test_sync_pass_keeps_listed_unlinked_claim_from_aging_out(monkeypatch):
    from TwitchChannelPointsMiner.classes.Twitch import UNLINKED_CLAIM_RETRY_SECONDS

    twitch, drop, requests, _saved, now = claim_recording_twitch(
        monkeypatch, statuses=(("ELIGIBLE_FOR_ALL", False),)
    )
    twitch.claim_drop(drop, campaign_link=unlinked_campaign_link())
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_inventory",
        lambda self: {
            "dropCampaignsInProgress": [
                {
                    "id": "campaign-1",
                    "timeBasedDrops": [
                        {
                            "id": "drop-1",
                            "self": {
                                "isClaimed": False,
                                "dropInstanceID": "instance-1",
                            },
                        }
                    ],
                }
            ]
        },
    )

    # Claims are skipped (backoff) in between; only the sync lists the drop.
    now[0] += UNLINKED_CLAIM_RETRY_SECONDS - 60
    twitch._Twitch__sync_campaigns([])
    now[0] += UNLINKED_CLAIM_RETRY_SECONDS + 60
    assert len(twitch.unlinked_drop_report()) == 1
    assert len(requests) == 1


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
    from datetime import timedelta

    ends_at = (utc_now() + timedelta(minutes=275)).strftime("%Y-%m-%dT%H:%M:%SZ")
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
    from datetime import timedelta

    ends_at = (utc_now() + timedelta(minutes=1200)).strftime("%Y-%m-%dT%H:%M:%SZ")
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
    ((game_slug, campaign_name, ends_at_epoch),) = signatures
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
        Twitch, "_Twitch__get_open_drop_campaigns_from_helix", lambda self: ([], [])
    )
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_campaigns_details",
        lambda self, campaigns, **kwargs: campaigns,
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


def test_finished_campaign_lookup_is_marked_possibly_stale(monkeypatch):
    # Twitch nulls a finished campaign's details for any login, so this lookup
    # must not count toward the TV-login warning (#223).
    twitch = bare_twitch(monkeypatch)
    for name, value in (
        ("_Twitch__get_drops_dashboard", lambda self, status="OPEN": []),
        ("_Twitch__get_open_drop_campaigns_from_helix", lambda self: ([], [])),
        ("_Twitch__awarded_benefits", lambda self, inventory: (set(), set())),
    ):
        monkeypatch.setattr(Twitch, name, value)
    calls = []
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_campaigns_details",
        lambda self, campaigns, **kwargs: calls.append((campaigns, kwargs)) or [],
    )

    twitch._Twitch__active_drop_category_slugs_from_campaigns(
        {
            "dropCampaignsInProgress": [],
            "completedRewardCampaigns": [{"id": "finished-1"}],
        },
        {"example-game"},
    )

    assert (
        [{"id": "finished-1"}],
        {"may_be_stale": True},
    ) in calls


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
    # Regression test: the initial dashboard+helix merge replaces
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
        Twitch,
        "_Twitch__get_drops_dashboard",
        lambda self, status="OPEN": [reward_campaign],
    )
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_open_drop_campaigns_from_helix",
        lambda self: ([helix_campaign], []),
    )
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_campaigns_details",
        lambda self, campaigns, **kwargs: campaigns,
    )
    monkeypatch.setattr(
        Twitch, "_Twitch__awarded_benefits", lambda self, inventory: (set(), set())
    )

    twitch._Twitch__active_drop_category_slugs_from_campaigns(
        {"dropCampaignsInProgress": []}, {"example-game"}
    )

    assert "campaign-1" in twitch.reward_campaign_ids


def test_campaign_discovery_sends_only_named_raw_queries(monkeypatch):
    # zarmstrong/Twitch-Channel-Points-Miner-v3#296: the anonymous
    # MinerRewardCampaignsRaw query failed on every sync. Its campaigns are the
    # dashboard's rewardCampaignsAvailableToUser, which stay tagged from there.
    twitch = bare_twitch(monkeypatch)
    posted = []

    def post_gql_request_raw(operation_name, request):
        posted.append((operation_name, request))
        return {"data": {}}

    twitch.gql.post_gql_request_raw = post_gql_request_raw
    reward_campaign = campaign_data()
    reward_campaign["timeBasedDrops"] = []
    reward_campaign["_is_reward_campaign"] = True
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_drops_dashboard",
        lambda self, status="OPEN": [reward_campaign],
    )
    monkeypatch.setattr(
        Twitch, "_Twitch__get_open_drop_campaigns_from_helix", lambda self: ([], [])
    )
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_campaigns_details",
        lambda self, campaigns, **kwargs: campaigns,
    )
    monkeypatch.setattr(
        Twitch, "_Twitch__awarded_benefits", lambda self, inventory: (set(), set())
    )

    twitch._Twitch__active_drop_category_slugs_from_campaigns(
        {"dropCampaignsInProgress": []}, {"example-game"}
    )

    assert "campaign-1" in twitch.reward_campaign_ids
    assert "MinerRewardCampaignsRaw" not in [name for name, _ in posted]
    for name, request in posted:
        assert re.match(rf"\s*(query|mutation)\s+{re.escape(name)}\b", request["query"])


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
                "all_channels": False,
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
        "_Twitch__get_open_drop_campaigns_from_helix",
        lambda self: ([], []),
    )
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_campaigns_details",
        lambda self, campaigns, campaign_channel_id_by_id=None, **kwargs: campaigns,
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
        "_Twitch__get_open_drop_campaigns_from_helix",
        lambda self: ([], []),
    )
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_campaigns_details",
        lambda self, campaigns, campaign_channel_id_by_id=None, **kwargs: campaigns,
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
            {
                "id": "campaign-1",
                "name": "Example Campaign",
                "all_channels": True,
                "channels": [],
            }
        ]
    }


@pytest.mark.parametrize("wrapped", [False, True])
def test_partial_completed_inventory_does_not_resurrect_category(monkeypatch, wrapped):
    twitch = bare_twitch(monkeypatch)
    twitch.awarded_game_event_drops = {}
    completed = {"id": "campaign-1", "name": "Example Campaign", "game": None}
    response = SimpleNamespace(
        inventory={
            "dropCampaignsInProgress": [],
            "gameEventDrops": [],
            "completedRewardCampaigns": [
                {"campaign": completed} if wrapped else completed
            ],
        },
        errors=[
            SimpleNamespace(
                message="Game metadata unavailable",
                path=["currentUser", "inventory", "completedRewardCampaigns", 0]
                + (["campaign"] if wrapped else [])
                + ["game"],
            )
        ],
    )
    twitch.gql = SimpleNamespace(get_inventory=lambda: response)
    monkeypatch.setattr(
        Twitch, "_Twitch__cache_drop_inventory_progress", lambda self, inventory: None
    )
    monkeypatch.setattr(
        Twitch, "_Twitch__get_drops_dashboard", lambda self, status: [campaign_data()]
    )
    monkeypatch.setattr(
        Twitch, "_Twitch__get_open_drop_campaigns_from_helix", lambda self: ([], [])
    )
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_campaigns_details",
        lambda self, campaigns, **kwargs: [campaign_data()] if campaigns else [],
    )
    monkeypatch.setattr(
        Twitch, "_Twitch__awarded_benefits", lambda self, inventory: (set(), set())
    )
    monkeypatch.setattr(
        Twitch,
        "_Twitch__twitchdrops_app_fallback",
        lambda self, categories, known_slugs, inventory: {},
    )

    inventory = twitch.get_drops_inventory()

    # Dashboard metadata cannot override the account's explicit completion,
    # even on the first load before any completion IDs have been cached.
    assert (
        twitch.filter_categories_with_active_drops(
            ["example-game"], inventory=inventory
        )
        == []
    )
    assert twitch.completed_drop_campaigns == {"campaign-1"}
    assert twitch.active_drop_campaigns == {}


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
        "_Twitch__get_open_drop_campaigns_from_helix",
        lambda self: ([], []),
    )

    def resolve_details(self, campaigns, **kwargs):
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
        "_Twitch__get_open_drop_campaigns_from_helix",
        lambda self: ([], []),
    )

    def resolve_details(self, campaigns, **kwargs):
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
        "_Twitch__get_open_drop_campaigns_from_helix",
        lambda self: ([], []),
    )
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_campaigns_details",
        lambda self, campaigns, **kwargs: [],
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

    twitch.campaign_lookup_unavailable = False

    twitch.unevaluated_category_slugs = set()
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
    twitch.campaign_lookup_unavailable = False
    twitch.unevaluated_category_slugs = set()
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
    twitch.campaign_lookup_unavailable = False
    twitch.unevaluated_category_slugs = set()
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


def test_channel_advertised_campaigns_are_recorded_on_the_stream(monkeypatch):
    twitch = bare_twitch(monkeypatch)
    campaign = advertised_campaign()
    twitch.gql = SimpleNamespace(
        get_available_drops=lambda channel_id: SimpleNamespace(
            campaigns=[campaign], campaigns_available=True
        )
    )
    twitch.awarded_game_event_drops = {}
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_campaigns_details",
        lambda self, campaigns, campaign_channel_id_by_id=None, **kwargs: [],
    )
    streamer = category_streamer()

    assert twitch._Twitch__get_campaign_ids_from_streamer(streamer) == ["campaign-1"]
    assert streamer.stream.channel_campaign_ids == ["campaign-1"]


def test_fallback_campaigns_are_not_recorded_as_channel_advertised(monkeypatch):
    twitch = bare_twitch(monkeypatch)
    twitch.gql = SimpleNamespace(
        get_available_drops=lambda channel_id: SimpleNamespace(
            campaigns=[], campaigns_available=True
        )
    )
    twitch.discovered_open_drop_campaigns = []
    twitch.twitchdrops_app_campaigns = {
        "example-game": [
            {
                "id": "gist-campaign-1",
                "name": "Gist Campaign",
                "channels": ["drops-channel"],
            }
        ]
    }
    streamer = category_streamer()
    # A stale value from an earlier refresh must not survive.
    streamer.stream.channel_campaign_ids = ["gist-campaign-1"]

    assert twitch._Twitch__get_campaign_ids_from_streamer(streamer) == [
        "gist-campaign-1"
    ]
    assert streamer.stream.channel_campaign_ids == []


def test_failed_channel_query_keeps_previously_advertised_campaigns(monkeypatch):
    twitch = bare_twitch(monkeypatch)

    def failed_query(channel_id):
        raise RetryError("DropsHighlightService_AvailableDrops", [])

    twitch.gql = SimpleNamespace(get_available_drops=failed_query)
    twitch.discovered_open_drop_campaigns = []
    twitch.twitchdrops_app_campaigns = {
        "example-game": [
            {"id": "campaign-1", "name": "Example Campaign", "channels": []}
        ]
    }
    followed = SimpleNamespace(
        username="followed-channel",
        channel_id="12345",
        from_category=False,
        from_badge_campaign=False,
        stream=SimpleNamespace(
            game_name=lambda: "Example Game",
            # Advertised by the channel on an earlier refresh; the ended
            # campaign no longer resolves and is dropped.
            channel_campaign_ids=["campaign-1", "ended-campaign"],
        ),
    )

    assert twitch._Twitch__get_campaign_ids_from_streamer(followed) == ["campaign-1"]
    # One failed query is not a verdict on the channel.
    assert followed.stream.channel_campaign_ids == ["campaign-1"]


def zero_minute_badge_campaign(game="Example Game"):
    # Mirrors what Twitch's channel query returned for Call of Duty: Modern
    # Warfare 4 channels: only the game's zero-minute badge campaign, while
    # the game-wide "Launch" campaign the account was earning also ran there.
    return {
        "id": "badge-campaign-1",
        "name": "Example Badge",
        "game": {"name": game},
        "timeBasedDrops": [
            {
                "id": "badge-drop-1",
                "name": "Example Badge",
                "requiredMinutesWatched": 0,
                "startAt": "2020-01-01T00:00:00Z",
                "endAt": "2099-01-01T00:00:00Z",
            }
        ],
    }


def game_wide_campaign_twitch(monkeypatch, channel_campaigns):
    twitch = bare_twitch(monkeypatch)
    twitch.gql = SimpleNamespace(
        get_available_drops=lambda channel_id: SimpleNamespace(
            campaigns=channel_campaigns, campaigns_available=True
        )
    )
    twitch.discovered_open_drop_campaigns = []
    twitch.active_drop_campaigns = {
        "example-game": [{"id": "launch-campaign", "name": "Launch", "channels": []}]
    }
    return twitch


def test_same_game_listing_lets_game_wide_twitch_campaign_apply(monkeypatch):
    twitch = game_wide_campaign_twitch(monkeypatch, [zero_minute_badge_campaign()])

    assert twitch._Twitch__get_campaign_ids_from_streamer(category_streamer()) == [
        "launch-campaign"
    ]
    assert twitch.category_campaign_eligibility[("example-game", "drops-channel")] == (
        1,
        1,
    )


@pytest.mark.parametrize(
    "channel_campaigns",
    [
        # Nothing listed: the per-streamer Rust case from #138 stays blocked.
        [],
        # A listing for another game proves nothing about this one.
        [zero_minute_badge_campaign(game="Another Game")],
    ],
)
def test_no_same_game_listing_keeps_game_wide_twitch_campaign_blocked(
    monkeypatch, channel_campaigns
):
    twitch = game_wide_campaign_twitch(monkeypatch, channel_campaigns)

    assert twitch._Twitch__get_campaign_ids_from_streamer(category_streamer()) == []
    assert twitch.category_campaign_eligibility[("example-game", "drops-channel")] == (
        0,
        0,
    )


def test_same_game_listing_still_excludes_completed_game_wide_campaign(monkeypatch):
    twitch = game_wide_campaign_twitch(monkeypatch, [zero_minute_badge_campaign()])
    twitch.completed_drop_campaigns = {"launch-campaign"}

    assert twitch._Twitch__get_campaign_ids_from_streamer(category_streamer()) == [
        "launch-campaign"
    ]
    # Resolved, but nothing left to earn there.
    assert twitch.category_campaign_eligibility[("example-game", "drops-channel")] == (
        0,
        1,
    )


def test_listed_campaign_rejected_by_the_channel_check_is_not_readmitted(
    monkeypatch,
):
    # The channel lists the game-wide campaign itself, and the channel check
    # rejects it (here: nothing watchable). Its game-wide copy must not slip
    # back in through the fallback, which would skip that check.
    listed = zero_minute_badge_campaign()
    listed["id"] = "launch-campaign"
    twitch = game_wide_campaign_twitch(monkeypatch, [listed])

    assert twitch._Twitch__get_campaign_ids_from_streamer(category_streamer()) == []
    assert twitch.category_campaign_eligibility[("example-game", "drops-channel")] == (
        0,
        0,
    )


def timed_campaign(campaign_id, minutes, ends_at):
    campaign = zero_minute_badge_campaign()
    campaign["id"] = campaign_id
    campaign["timeBasedDrops"][0]["requiredMinutesWatched"] = minutes
    campaign["timeBasedDrops"][0]["endAt"] = ends_at
    return campaign


@pytest.mark.parametrize(
    "dashboard_campaign, authoritative_extra",
    [
        # A purchase-gated reward summary: dashboard summaries carry no
        # allow-list, so it would look game-wide.
        (
            {"id": "reward-1", "name": "Sub Reward", "game": {"name": "Example Game"}},
            [],
        ),
        # The dashboard copy of a campaign the account-wide evaluation knows
        # is limited to other channels.
        (
            {
                "id": "restricted-1",
                "name": "Restricted",
                "game": {"name": "Example Game"},
            },
            [{"id": "restricted-1", "name": "Restricted", "channels": ["a", "b"]}],
        ),
    ],
)
def test_badge_listing_admits_only_account_evaluated_game_wide_campaigns(
    monkeypatch, dashboard_campaign, authoritative_extra
):
    twitch = game_wide_campaign_twitch(monkeypatch, [zero_minute_badge_campaign()])
    twitch.discovered_open_drop_campaigns = [dashboard_campaign]
    twitch.active_drop_campaigns["example-game"].extend(authoritative_extra)

    assert twitch._Twitch__get_campaign_ids_from_streamer(category_streamer()) == [
        "launch-campaign"
    ]


@pytest.mark.parametrize(
    "listed",
    [
        # A rejected listing that needs watch time may be a per-streamer
        # campaign (the #138 Rust case), so it proves nothing.
        timed_campaign("own-campaign", 30, "2020-06-01T00:00:00Z"),
        # A badge outside its date window proves nothing about now.
        timed_campaign("old-badge", 0, "2020-06-01T00:00:00Z"),
    ],
)
def test_listing_that_is_not_a_running_badge_keeps_game_wide_campaign_blocked(
    monkeypatch, listed
):
    twitch = game_wide_campaign_twitch(monkeypatch, [listed])

    assert twitch._Twitch__get_campaign_ids_from_streamer(category_streamer()) == []
    assert twitch.category_campaign_eligibility[("example-game", "drops-channel")] == (
        0,
        0,
    )


def hunt_halloween_campaign():
    # Twitch's DropsHighlightService_AvailableDrops listing (782dad0f... hash)
    # of Hunt: Showdown 1896's "HS 1896 Halloween 2026", trimmed: one drop
    # needs only a subscription, the other only watch time.
    return {
        "id": "hunt-halloween",
        "name": "HS 1896 Halloween 2026",
        "game": {"name": "Example Game"},
        "summary": {
            "includesMWRequirement": True,
            "includesSubRequirement": True,
            "isSitewide": False,
            "isRewardCampaign": False,
            "isPermanentlyDismissible": False,
        },
        "timeBasedDrops": [
            {
                "id": "hunt-red",
                "name": "Hunt Red",
                "requiredSubs": 1,
                "requiredMinutesWatched": 0,
                "startAt": "2020-01-01T00:00:00Z",
                "endAt": "2099-01-01T00:00:00Z",
            },
            {
                "id": "hunt-white",
                "name": "Hunt White",
                "requiredSubs": 0,
                "requiredMinutesWatched": 15,
                "startAt": "2020-01-01T00:00:00Z",
                "endAt": "2099-01-01T00:00:00Z",
            },
        ],
    }


def listing_twitch(monkeypatch, channel_campaigns, details=None):
    twitch = bare_twitch(monkeypatch)
    twitch.gql = SimpleNamespace(
        get_available_drops=lambda channel_id: SimpleNamespace(
            campaigns=channel_campaigns, campaigns_available=True
        )
    )
    twitch.discovered_open_drop_campaigns = []
    twitch.awarded_game_event_drops = {}
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_campaigns_details",
        lambda self, campaigns, campaign_channel_id_by_id=None, **kwargs: details or [],
    )
    return twitch


def test_listing_with_a_watchable_drop_beside_a_sub_only_drop_is_kept(
    monkeypatch,
):
    twitch = listing_twitch(monkeypatch, [hunt_halloween_campaign()])

    assert twitch._Twitch__get_campaign_ids_from_streamer(category_streamer()) == [
        "hunt-halloween"
    ]
    assert twitch.category_campaign_eligibility[("example-game", "drops-channel")] == (
        1,
        1,
    )


def mixed_requirement_campaign(required_subs, current_subs=None):
    campaign = hunt_halloween_campaign()
    campaign["timeBasedDrops"] = [campaign["timeBasedDrops"][1]]
    drop = campaign["timeBasedDrops"][0]
    if required_subs is None:
        del drop["requiredSubs"]
    else:
        drop["requiredSubs"] = required_subs
    if current_subs is not None:
        drop["self"] = {
            "isClaimed": False,
            "hasPreconditionsMet": True,
            "currentMinutesWatched": 0,
            "currentSubs": current_subs,
        }
    return campaign


@pytest.mark.parametrize(
    "listed, expected_ids",
    [
        # Watch time and a subscription the account has not made: watching
        # alone cannot earn it.
        (mixed_requirement_campaign(1), []),
        (mixed_requirement_campaign(1, current_subs=0), []),
        # The account already subscribed.
        (mixed_requirement_campaign(1, current_subs=1), ["hunt-halloween"]),
        # Twitch did not say: the minutes-based check decides, as before.
        (mixed_requirement_campaign(None), ["hunt-halloween"]),
    ],
)
def test_listing_needing_an_unmet_subscription_is_rejected(
    monkeypatch, listed, expected_ids
):
    twitch = listing_twitch(monkeypatch, [listed])

    assert (
        twitch._Twitch__get_campaign_ids_from_streamer(category_streamer())
        == expected_ids
    )


@pytest.mark.parametrize(
    "summary, expected_ids",
    [
        ({"includesMWRequirement": False}, []),
        ({"includesMWRequirement": True}, ["hunt-halloween"]),
        # Missing or unreadable: walk the drops, as before.
        (None, ["hunt-halloween"]),
        ({"includesMWRequirement": None}, ["hunt-halloween"]),
    ],
)
def test_listing_without_a_watch_time_requirement_is_skipped(
    monkeypatch, summary, expected_ids
):
    listed = hunt_halloween_campaign()
    listed["summary"] = summary
    twitch = listing_twitch(monkeypatch, [listed])
    twitch.log_drop_checks = True
    logged = []
    monkeypatch.setattr(
        Twitch,
        "_Twitch__log_drop_check",
        lambda self, message, level=logging.INFO: logged.append(message),
    )

    assert (
        twitch._Twitch__get_campaign_ids_from_streamer(category_streamer())
        == expected_ids
    )
    skipped = [
        message for message in logged if "can't be earned by watching" in message
    ]
    assert len(skipped) == (0 if expected_ids else 1)


HUNT_ACTION_ITEMS = [
    "Watching {Channel} while they're live in <x:bold>{Category}</x:bold>",
    "Purchasing or gifting subscriptions to {Channel} while they're live in "
    "<x:bold>{Category}</x:bold>",
]


def drop_check_lines(monkeypatch, twitch):
    twitch.log_drop_checks = True
    logged = []
    monkeypatch.setattr(
        Twitch,
        "_Twitch__log_drop_check",
        lambda self, message, level=logging.INFO: logged.append(message),
    )
    return logged


def test_listing_logs_its_sub_gated_drops_with_twitch_earn_text(monkeypatch):
    listed = hunt_halloween_campaign()
    listed["localizedContent"] = {
        "title": "Subscribe or watch to earn different Drops!",
        "actionItems": HUNT_ACTION_ITEMS,
    }
    twitch = listing_twitch(monkeypatch, [listed])
    logged = drop_check_lines(monkeypatch, twitch)

    assert twitch._Twitch__get_campaign_ids_from_streamer(category_streamer()) == [
        "hunt-halloween"
    ]
    assert (
        "campaign 'HS 1896 Halloween 2026' on 'drops-channel': Hunt Red needs a "
        "subscription the account hasn't made; not watching for it (Twitch: "
        "Watching drops-channel while they're live in Example Game; Purchasing "
        "or gifting subscriptions to drops-channel while they're live in "
        "Example Game)"
    ) in logged


@pytest.mark.parametrize(
    "localized_content",
    [None, {}, {"actionItems": None}, {"actionItems": [None, ""]}],
)
def test_sub_gated_drop_line_without_usable_earn_text(monkeypatch, localized_content):
    listed = hunt_halloween_campaign()
    listed["localizedContent"] = localized_content
    twitch = listing_twitch(monkeypatch, [listed])
    logged = drop_check_lines(monkeypatch, twitch)

    twitch._Twitch__get_campaign_ids_from_streamer(category_streamer())

    assert (
        "campaign 'HS 1896 Halloween 2026' on 'drops-channel': Hunt Red needs a "
        "subscription the account hasn't made; not watching for it"
    ) in logged


def test_no_sub_gated_drop_line_when_the_requirement_is_met_or_unknown(
    monkeypatch,
):
    listed = hunt_halloween_campaign()
    # Twitch didn't say for the first drop; the second needs no sub.
    del listed["timeBasedDrops"][0]["requiredSubs"]
    twitch = listing_twitch(monkeypatch, [listed])
    logged = drop_check_lines(monkeypatch, twitch)

    twitch._Twitch__get_campaign_ids_from_streamer(category_streamer())

    assert not [message for message in logged if "subscription" in message]


def test_earn_text_is_not_built_when_drop_checks_are_not_logged(monkeypatch):
    listed = hunt_halloween_campaign()
    listed["localizedContent"] = {"actionItems": HUNT_ACTION_ITEMS}
    twitch = listing_twitch(monkeypatch, [listed])
    built = []
    monkeypatch.setattr(
        Twitch,
        "_Twitch__campaign_earn_text",
        staticmethod(lambda *args: built.append(args) or "text"),
    )

    # Logging off changes nothing about which campaigns are kept.
    assert twitch._Twitch__get_campaign_ids_from_streamer(category_streamer()) == [
        "hunt-halloween"
    ]
    assert built == []


def test_no_watch_time_skip_line_quotes_twitch_earn_text(monkeypatch):
    listed = hunt_halloween_campaign()
    listed["summary"] = {"includesMWRequirement": False}
    listed["localizedContent"] = {"actionItems": HUNT_ACTION_ITEMS[1:]}
    twitch = listing_twitch(monkeypatch, [listed])
    logged = drop_check_lines(monkeypatch, twitch)

    assert twitch._Twitch__get_campaign_ids_from_streamer(category_streamer()) == []
    assert (
        "campaign 'HS 1896 Halloween 2026' on 'drops-channel' can't be earned by "
        "watching (Twitch: no watch-time requirement; earned by: Purchasing or "
        "gifting subscriptions to drops-channel while they're live in Example "
        "Game); skipping"
    ) in logged


def sub_only_campaign():
    campaign = zero_minute_badge_campaign()
    campaign["id"] = "sub-reward"
    campaign["timeBasedDrops"][0]["requiredSubs"] = 1
    campaign["summary"] = {
        "includesMWRequirement": False,
        "includesSubRequirement": True,
        "isSitewide": False,
    }
    return campaign


def test_sub_only_listing_is_not_a_running_badge(monkeypatch):
    # A zero-minute drop that needs a subscription is a sub reward, and its
    # campaign may be limited to some channels: unlike a badge, it does not
    # show the game-wide campaign runs on this channel.
    twitch = game_wide_campaign_twitch(monkeypatch, [sub_only_campaign()])

    assert twitch._Twitch__get_campaign_ids_from_streamer(category_streamer()) == []
    assert twitch.category_campaign_eligibility[("example-game", "drops-channel")] == (
        0,
        0,
    )


def test_badge_listing_with_no_watch_time_summary_is_still_a_badge(monkeypatch):
    # Twitch's summary would also say a badge needs no watch time.
    badge = zero_minute_badge_campaign()
    badge["timeBasedDrops"][0]["requiredSubs"] = 0
    badge["summary"] = {"includesMWRequirement": False}
    twitch = game_wide_campaign_twitch(monkeypatch, [badge])

    assert twitch._Twitch__get_campaign_ids_from_streamer(category_streamer()) == [
        "launch-campaign"
    ]


def test_campaign_details_keep_the_listing_earn_requirements(monkeypatch):
    listed = hunt_halloween_campaign()
    details = copy.deepcopy(listed)
    del details["summary"]
    for drop in details["timeBasedDrops"]:
        del drop["requiredSubs"]
    details["allow"] = {"channels": None}
    twitch = listing_twitch(monkeypatch, [listed], details=[details])

    twitch._Twitch__get_campaign_ids_from_streamer(category_streamer())

    stored = twitch.advertised_drop_campaigns["hunt-halloween"]
    assert stored["summary"] == listed["summary"]
    assert [drop["requiredSubs"] for drop in stored["timeBasedDrops"]] == [1, 0]
    assert stored["allow"] == {"channels": None}


def test_campaign_details_requirements_win_over_the_listing(monkeypatch):
    listed = hunt_halloween_campaign()
    details = copy.deepcopy(listed)
    details["summary"] = {"includesMWRequirement": True, "isSitewide": True}
    details["timeBasedDrops"][0]["requiredSubs"] = 2
    twitch = listing_twitch(monkeypatch, [listed], details=[details])

    twitch._Twitch__get_campaign_ids_from_streamer(category_streamer())

    stored = twitch.advertised_drop_campaigns["hunt-halloween"]
    assert stored["summary"] == details["summary"]
    assert stored["timeBasedDrops"][0]["requiredSubs"] == 2


def test_campaign_debug_summary_shows_sitewide_and_required_subs():
    summary = Twitch._Twitch__campaign_debug_summary(hunt_halloween_campaign())

    assert summary["sitewide"] is False
    assert [drop["required_subs"] for drop in summary["drops"]] == [1, 0]

    unknown = Twitch._Twitch__campaign_debug_summary(zero_minute_badge_campaign())

    assert unknown["sitewide"] is None
    assert unknown["drops"][0]["required_subs"] is None


def inventory_with_drop(required_subs, current_subs):
    drop = {
        "id": "hunt-white",
        "name": "Hunt White",
        "requiredMinutesWatched": 15,
        "self": {
            "currentMinutesWatched": 5,
            "hasPreconditionsMet": True,
            "isClaimed": False,
            "dropInstanceID": None,
        },
    }
    if required_subs is not None:
        drop["requiredSubs"] = required_subs
    if current_subs is not None:
        drop["self"]["currentSubs"] = current_subs
    return {
        "dropCampaignsInProgress": [
            {
                "id": "hunt-halloween",
                "name": "HS 1896 Halloween 2026",
                "game": {"displayName": "Example Game"},
                "timeBasedDrops": [drop],
            }
        ]
    }


@pytest.mark.parametrize(
    "required_subs, current_subs, tracked",
    [
        (1, 0, False),
        (1, None, False),
        (1, 1, True),
        (0, 0, True),
        (None, None, True),
    ],
)
def test_inventory_progress_skips_drops_needing_an_unmet_subscription(
    monkeypatch, required_subs, current_subs, tracked
):
    twitch = bare_twitch(monkeypatch)
    twitch.accepted_drop_claims = {}

    twitch._Twitch__cache_drop_inventory_progress(
        inventory_with_drop(required_subs, current_subs)
    )

    assert ("hunt-white" in twitch.drop_inventory_minutes_by_drop_id) is tracked


def test_claimed_sub_drop_still_counts_as_finished(monkeypatch):
    twitch = bare_twitch(monkeypatch)
    twitch.accepted_drop_claims = {}
    inventory = inventory_with_drop(1, 0)
    inventory["dropCampaignsInProgress"][0]["timeBasedDrops"][0]["self"][
        "isClaimed"
    ] = True

    twitch._Twitch__cache_drop_inventory_progress(inventory)

    assert "hunt-white" in twitch.drop_inventory_finished_ids


def test_inventory_overlay_marks_a_tracked_sub_drop_as_subscribed(monkeypatch):
    # A drop is in the progress snapshot only once its subscription
    # requirement was met, so its listed copy must not read as unmet.
    twitch = bare_twitch(monkeypatch)
    twitch.drop_inventory_progress_updated_at = time.time()
    twitch.drop_inventory_minutes_by_drop_id = {"hunt-white": 5}
    listed = mixed_requirement_campaign(1)

    overlaid = twitch._Twitch__with_inventory_drop_progress(listed)

    drop = overlaid["timeBasedDrops"][0]
    assert drop["self"]["currentSubs"] == 1
    assert (
        twitch._Twitch__active_incomplete_drop_deadline(
            overlaid, set(), set(), set(), log_status=False
        )
        is not None
    )


def test_gist_copy_of_a_rejected_listed_campaign_is_not_readmitted(monkeypatch):
    listed = timed_campaign("launch-campaign", 30, "2020-06-01T00:00:00Z")
    twitch = game_wide_campaign_twitch(monkeypatch, [listed])
    twitch.twitchdrops_app_campaigns = {
        "example-game": [{"id": "launch-campaign", "name": "Launch", "channels": []}]
    }

    assert twitch._Twitch__get_campaign_ids_from_streamer(category_streamer()) == []
    assert twitch.category_campaign_eligibility[("example-game", "drops-channel")] == (
        0,
        0,
    )


def _stub_campaign_sources(monkeypatch):
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_open_drop_campaigns_from_helix",
        lambda self: ([], []),
    )
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_campaigns_details",
        lambda self, campaigns, campaign_channel_id_by_id=None, **kwargs: campaigns,
    )
    monkeypatch.setattr(
        Twitch,
        "_Twitch__awarded_benefits",
        lambda self, inventory: (set(), set()),
    )


@pytest.mark.parametrize("outcome", ["failed", "partial", "loaded", "completed"])
def test_campaign_refresh_preserves_allowlist_only_for_unevaluated_games(
    monkeypatch, outcome
):
    twitch = bare_twitch(monkeypatch)
    cached_campaign = {
        "id": "restricted-campaign-1",
        "name": "Restricted Campaign",
        "channels": ["drops-channel"],
    }
    twitch.active_drop_campaigns = {"example-game": [cached_campaign]}
    twitch.discovered_open_drop_campaigns = []
    twitch.category_campaign_eligibility[("example-game", "drops-channel")] = (1, 1)
    twitch.gql = SimpleNamespace(
        get_available_drops=lambda channel_id: SimpleNamespace(
            campaigns=[], campaigns_available=True
        )
    )
    monkeypatch.setattr(
        _drops_dashboard_state, "unavailable", outcome != "loaded", raising=False
    )
    monkeypatch.setattr(Twitch, "_Twitch__get_drops_dashboard", lambda self, **kw: [])
    _stub_campaign_sources(monkeypatch)
    inventory = {}
    if outcome == "completed":
        inventory["completedRewardCampaigns"] = [
            {"id": cached_campaign["id"], "game": {"displayName": "Example Game"}}
        ]
    elif outcome == "partial":
        other_campaign = campaign_data()
        other_campaign["id"] = "other-campaign"
        other_campaign["game"] = {"displayName": "Other Game"}
        monkeypatch.setattr(
            Twitch,
            "_Twitch__get_open_drop_campaigns_from_helix",
            lambda self: ([other_campaign], []),
        )

    deadlines, evaluated_games = (
        twitch._Twitch__active_drop_category_slugs_from_campaigns(
            inventory, {"example-game", "other-game"}
        )
    )

    kept = outcome in {"failed", "partial"}
    assert "example-game" not in deadlines
    assert ("example-game" in evaluated_games) == (outcome == "completed")
    assert twitch.active_drop_campaigns.get("example-game", []) == (
        [cached_campaign] if kept else []
    )
    # The next channel refresh may answer empty for an official co-stream.
    # Losing its cached allow-list would turn that answer into a negative verdict.
    streamer = category_streamer()
    streamer.is_online = True
    streamer.settings = SimpleNamespace(claim_drops=True)
    streamer.stream.campaigns_ids = []
    assert twitch._Twitch__get_campaign_ids_from_streamer(streamer) == (
        [cached_campaign["id"]] if kept else []
    )
    assert twitch._Twitch__category_drops_condition(streamer) is kept


def test_carried_allowlists_still_expire_during_a_dashboard_outage(monkeypatch):
    # While the dashboard is down, cached allow-lists of games no source
    # reported are carried over, but not past their campaign's end or once
    # the inventory reports the campaign completed.
    twitch = bare_twitch(monkeypatch)
    now = utc_now()
    live = {"id": "live", "name": "Live", "channels": ["a"]}
    ended = {"id": "ended", "name": "Ended", "channels": ["b"]}
    done = {"id": "done", "name": "Done", "channels": ["c"]}
    twitch.active_drop_campaigns = {
        "example-game": [live, ended],
        "done-game": [done],
    }
    twitch.active_drop_campaign_deadlines = {
        "live": now + timedelta(hours=1),
        "ended": now - timedelta(minutes=1),
        "done": now + timedelta(hours=1),
    }
    twitch.completed_drop_campaigns = {"done"}
    twitch.discovered_open_drop_campaigns = []
    monkeypatch.setattr(_drops_dashboard_state, "unavailable", True, raising=False)
    monkeypatch.setattr(Twitch, "_Twitch__get_drops_dashboard", lambda self, **kw: [])
    _stub_campaign_sources(monkeypatch)

    twitch._Twitch__active_drop_category_slugs_from_campaigns(
        {}, {"example-game", "done-game"}
    )

    assert twitch.active_drop_campaigns == {"example-game": [live]}
    assert twitch.active_drop_campaign_deadlines == {"live": now + timedelta(hours=1)}


def test_outage_carries_unreported_campaigns_of_a_game_with_reported_ones(
    monkeypatch,
):
    # The inventory still reports the game's in-progress campaign, but not
    # its allow-listed one, which only the dashboard did. Missing data is not
    # a negative verdict for that campaign, so its allow-list is carried
    # beside the reported campaign, which is not duplicated.
    twitch = bare_twitch(monkeypatch)
    in_progress = {"id": "campaign-1", "name": "Example Campaign", "channels": []}
    restricted = {"id": "restricted-1", "name": "Co-stream", "channels": ["costream"]}
    twitch.active_drop_campaigns = {"example-game": [in_progress, restricted]}
    twitch.discovered_open_drop_campaigns = []
    monkeypatch.setattr(_drops_dashboard_state, "unavailable", True, raising=False)
    monkeypatch.setattr(Twitch, "_Twitch__get_drops_dashboard", lambda self, **kw: [])
    _stub_campaign_sources(monkeypatch)

    twitch._Twitch__active_drop_category_slugs_from_campaigns(
        {"dropCampaignsInProgress": [campaign_data()]}, {"example-game"}
    )

    campaign_ids = [
        campaign["id"] for campaign in twitch.active_drop_campaigns["example-game"]
    ]
    assert campaign_ids == ["campaign-1", "restricted-1"]


def test_active_campaign_records_its_campaign_end_for_later_carrying(monkeypatch):
    twitch = bare_twitch(monkeypatch)
    twitch.discovered_open_drop_campaigns = []
    campaign = campaign_data()
    campaign["endAt"] = "2099-06-01T00:00:00Z"
    monkeypatch.setattr(_drops_dashboard_state, "unavailable", False, raising=False)
    monkeypatch.setattr(Twitch, "_Twitch__get_drops_dashboard", lambda self, **kw: [])
    _stub_campaign_sources(monkeypatch)
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_open_drop_campaigns_from_helix",
        lambda self: ([campaign], []),
    )

    twitch._Twitch__active_drop_category_slugs_from_campaigns({}, {"example-game"})

    assert [entry["id"] for entry in twitch.active_drop_campaigns["example-game"]] == [
        "campaign-1"
    ]
    # The campaign's end, not its earliest incomplete drop's (2099-01-01).
    assert twitch.active_drop_campaign_deadlines == {"campaign-1": datetime(2099, 6, 1)}


@pytest.mark.parametrize(
    "raw_response, client_id, unavailable",
    [
        ({"data": {"currentUser": {"dropCampaigns": []}}}, CLIENT_ID, False),
        # A non-fatal error beside the campaign list leaves it usable.
        (
            {
                "data": {"currentUser": {"dropCampaigns": []}},
                "errors": [{"message": "timeout"}],
            },
            CLIENT_ID,
            False,
        ),
        # An invalid token or a GraphQL failure answers with no viewer.
        (
            {"data": {"currentUser": None}, "errors": [{"message": "x"}]},
            CLIENT_ID,
            True,
        ),
        ({"data": None, "errors": [{"message": "x"}]}, CLIENT_ID, True),
        ({}, CLIENT_ID, True),
        # An error that nulled the campaign list.
        (
            {
                "data": {"currentUser": {"dropCampaigns": None}},
                "errors": [{"message": "x"}],
            },
            CLIENT_ID,
            True,
        ),
        # The TV login always gets a null list without errors (Twitch
        # withholds it), so it still counts as loaded there.
        ({"data": {"currentUser": {"dropCampaigns": None}}}, CLIENT_ID, False),
        # Under the Android login the same reply evaluated nothing (#228).
        ({"data": {"currentUser": {"dropCampaigns": None}}}, ANDROID_CLIENT_ID, True),
        ({"data": {"currentUser": {"dropCampaigns": []}}}, ANDROID_CLIENT_ID, False),
    ],
)
def test_dashboard_without_viewer_data_counts_as_unavailable(
    monkeypatch, raw_response, client_id, unavailable
):
    twitch = bare_twitch(monkeypatch)
    twitch.twitch_login = SimpleNamespace(client_id=client_id)
    twitch.gql = SimpleNamespace(
        get_viewer_drops_dashboard=lambda: SimpleNamespace(raw_response=raw_response)
    )
    monkeypatch.setattr(
        _drops_dashboard_state, "unavailable", not unavailable, raising=False
    )

    assert twitch._Twitch__get_drops_dashboard(status="OPEN") == []
    assert _drops_dashboard_state.unavailable is unavailable


def test_campaign_discovery_snapshots_its_own_dashboard_outcome(monkeypatch):
    # Issue #175: the dashboard outcome is thread-local, so another thread's
    # dashboard call (the campaign sync thread) succeeding in the very window
    # between this pass's failed request and its snapshot cannot clear it.
    monkeypatch.setattr(_drops_dashboard_state, "unavailable", False, raising=False)
    twitch = bare_twitch(monkeypatch)

    def failed_dashboard(self, status="OPEN"):
        _drops_dashboard_state.unavailable = True
        other_thread = threading.Thread(
            target=lambda: setattr(_drops_dashboard_state, "unavailable", False)
        )
        other_thread.start()
        other_thread.join()
        return []

    monkeypatch.setattr(Twitch, "_Twitch__get_drops_dashboard", failed_dashboard)
    _stub_campaign_sources(monkeypatch)

    deadlines, _ = twitch._Twitch__active_drop_category_slugs_from_campaigns(
        {}, {"example-game"}
    )

    assert deadlines == {}
    assert twitch.campaign_lookup_unavailable is True


def test_campaign_discovery_records_a_loaded_dashboard(monkeypatch):
    monkeypatch.setattr(_drops_dashboard_state, "unavailable", True, raising=False)
    twitch = bare_twitch(monkeypatch)
    twitch.campaign_lookup_unavailable = True

    def loaded_dashboard(self, status="OPEN"):
        _drops_dashboard_state.unavailable = False
        return []

    monkeypatch.setattr(Twitch, "_Twitch__get_drops_dashboard", loaded_dashboard)
    _stub_campaign_sources(monkeypatch)

    twitch._Twitch__active_drop_category_slugs_from_campaigns({}, {"example-game"})

    assert twitch.campaign_lookup_unavailable is False


def test_campaign_discovery_treats_unwritten_dashboard_state_as_loaded(monkeypatch):
    # Stubs that never touch the thread-local (every existing dashboard stub)
    # keep behaving as a loaded dashboard.
    monkeypatch.delattr(_drops_dashboard_state, "unavailable", raising=False)
    twitch = bare_twitch(monkeypatch)
    twitch.campaign_lookup_unavailable = True
    monkeypatch.setattr(
        Twitch, "_Twitch__get_drops_dashboard", lambda self, status="OPEN": []
    )
    _stub_campaign_sources(monkeypatch)

    twitch._Twitch__active_drop_category_slugs_from_campaigns({}, {"example-game"})

    assert twitch.campaign_lookup_unavailable is False


# Issue #181: a campaign whose channel list is unknown -- a catalog record
# scraped without one, or a Twitch campaign whose details failed to load --
# is the same unknown campaign to every reader, never "open to all channels".

UNKNOWN_CATALOG_RECORD = {
    "id": "unknown-catalog",
    "name": "Unscraped",
    "all_channels": False,
    "channels": [],
}
UNKNOWN_TWITCH_CAMPAIGN = {
    "id": "unknown-twitch",
    "name": "Details failed",
    "all_channels": False,
    "channels": [],
}


def _campaign_without_details(source):
    campaign = campaign_data()
    del campaign["allow"]
    if source == "advertised":
        return Twitch._Twitch__normalize_advertised_campaign(
            object.__new__(Twitch), campaign
        )
    return campaign


@pytest.mark.parametrize("source", ["dashboard", "advertised"])
def test_campaign_whose_scope_was_never_learned_is_stored_as_open(monkeypatch, source):
    # DropCampaignDetails failed and nothing is remembered (as after a
    # restart), so neither the dashboard summary nor a channel's listing says
    # who can earn it. Stored as unknown, readers left it out and could retire
    # the channels earning it; it is stored as open, as before, and not
    # remembered, so its real scope replaces that once its details load.
    twitch = bare_twitch(monkeypatch)
    campaign = _campaign_without_details(source)
    if source == "advertised":
        twitch.advertised_drop_campaigns = {"campaign-1": campaign}
        dashboard = []
    else:
        dashboard = [campaign]
    monkeypatch.setattr(
        Twitch, "_Twitch__get_drops_dashboard", lambda self, **kwargs: dashboard
    )
    _stub_campaign_sources(monkeypatch)
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_campaigns_details",
        lambda self, campaigns, campaign_channel_id_by_id=None, **kwargs: [],
    )

    twitch._Twitch__active_drop_category_slugs_from_campaigns({}, {"example-game"})

    assert twitch.active_drop_campaigns == {
        "example-game": [
            {
                "id": "campaign-1",
                "name": "Example Campaign",
                "all_channels": True,
                "channels": [],
            }
        ]
    }
    assert "campaign-1" not in twitch.known_campaign_scopes

    restricted = {**campaign_data(), "allow": {"channels": [{"name": "costream"}]}}
    _sync_with_details(monkeypatch, twitch, restricted, True, {"example-game"})

    assert twitch.active_drop_campaigns["example-game"][0]["channels"] == ["costream"]


def test_inventory_allow_block_makes_an_advertised_listing_known(monkeypatch):
    twitch = bare_twitch(monkeypatch)
    advertised = _campaign_without_details("advertised")
    assert twitch._Twitch__campaign_channels_unknown(advertised)
    inventory_campaign = campaign_data()
    inventory_campaign["allow"] = {"channels": None}

    merged = twitch._Twitch__merge_campaign_inventory_progress(
        advertised, inventory_campaign
    )

    assert not twitch._Twitch__campaign_channels_unknown(merged)


def _sync_with_details(monkeypatch, twitch, campaign, details_load, requested):
    """One campaign sync pass whose dashboard lists `campaign`; its details
    come back with `campaign`'s own allow block only when `details_load`."""
    summary = {key: value for key, value in campaign.items() if key != "allow"}
    monkeypatch.setattr(
        Twitch, "_Twitch__get_drops_dashboard", lambda self, **kwargs: [summary]
    )
    _stub_campaign_sources(monkeypatch)
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_campaigns_details",
        lambda self, campaigns, campaign_channel_id_by_id=None, **kwargs: (
            [copy.deepcopy(campaign)] if details_load else []
        ),
    )
    twitch._Twitch__active_drop_category_slugs_from_campaigns({}, requested)


@pytest.mark.parametrize("pass_between", [False, True])
@pytest.mark.parametrize(
    "allow, expected",
    [
        (
            {"channels": [{"id": "1", "name": "CoStream"}]},
            {"all_channels": False, "channels": ["costream"]},
        ),
        ({"channels": None}, {"all_channels": True, "channels": []}),
    ],
)
def test_failed_details_keep_the_last_known_channel_scope(
    monkeypatch, allow, expected, pass_between
):
    # One cycle's failed DropCampaignDetails lookup must not turn a known
    # allow-list, or a game-wide campaign, into an unknown one and retire the
    # channels being watched for it. The other sync pass (wildcard or
    # configured categories) replacing active_drop_campaigns in between must
    # not lose what was known either.
    twitch = bare_twitch(monkeypatch)
    campaign = {**campaign_data(), "allow": allow}
    _sync_with_details(monkeypatch, twitch, campaign, True, {"example-game"})
    if pass_between:
        _sync_with_details(monkeypatch, twitch, campaign, True, {"other-game"})
        assert twitch.active_drop_campaigns == {}

    _sync_with_details(monkeypatch, twitch, campaign, False, {"example-game"})

    assert twitch.active_drop_campaigns == {
        "example-game": [{"id": "campaign-1", "name": "Example Campaign", **expected}]
    }


def test_null_inventory_allow_keeps_the_details_allow_list(monkeypatch):
    twitch = bare_twitch(monkeypatch)
    details = {**campaign_data(), "allow": {"channels": [{"name": "costream"}]}}
    inventory_campaign = {**campaign_data(), "allow": None}

    merged = twitch._Twitch__merge_campaign_inventory_progress(
        details, inventory_campaign
    )

    assert merged["allow"] == {"channels": [{"name": "costream"}]}


def test_drops_description_leaves_out_campaigns_with_unknown_channels(monkeypatch):
    twitch = bare_twitch(monkeypatch)
    twitch.discovered_open_drop_campaigns = []
    twitch.twitchdrops_app_campaigns = {"example-game": [UNKNOWN_CATALOG_RECORD]}
    twitch.active_drop_campaigns = {
        "example-game": [
            UNKNOWN_TWITCH_CAMPAIGN,
            {
                "id": "restricted",
                "name": "Restricted",
                "all_channels": False,
                "channels": ["drops-channel"],
            },
        ]
    }

    # Not "(3 of 3 campaigns)": only the allow-listed campaign is known.
    assert (
        twitch._Twitch__streamer_drops_description(category_streamer())
        == "Example Game drops"
    )


def test_category_drops_condition_ignores_campaigns_with_unknown_channels(
    monkeypatch,
):
    twitch = bare_twitch(monkeypatch)
    twitch.twitchdrops_app_campaigns = {"example-game": [UNKNOWN_CATALOG_RECORD]}
    twitch.active_drop_campaigns = {"example-game": [UNKNOWN_TWITCH_CAMPAIGN]}
    streamer = SimpleNamespace(
        username="drops-channel",
        from_category=True,
        from_badge_campaign=False,
        settings=SimpleNamespace(claim_drops=True),
        is_online=True,
        stream=SimpleNamespace(campaigns_ids=[], game_name=lambda: "Example Game"),
    )

    assert twitch._Twitch__category_drops_condition(streamer) is False


def test_failed_channel_query_treats_unknown_campaigns_like_discovery_does(
    monkeypatch,
):
    # The channel query failed, so campaigns open to all channels still apply
    # (see test_failed_channel_query_keeps_unrestricted_twitch_campaigns). A
    # Twitch campaign whose channels are unknown stands in for one, as master
    # and category discovery treat it; a catalog record that says it is not
    # open yet names nobody never does.
    def available_drops(channel_id):
        raise RetryError("DropsHighlightService_AvailableDrops", [])

    twitch = per_streamer_campaigns_twitch(monkeypatch, available_drops)
    twitch.active_drop_campaigns = {
        "example-game": [
            {"id": "open", "name": "Open", "all_channels": True, "channels": []},
            UNKNOWN_TWITCH_CAMPAIGN,
        ]
    }
    twitch.twitchdrops_app_campaigns = {"example-game": [UNKNOWN_CATALOG_RECORD]}

    assert sorted(
        twitch._Twitch__get_campaign_ids_from_streamer(category_streamer())
    ) == ["open", "unknown-twitch"]


def test_merge_keeps_the_fresh_allow_list_when_only_inventory_has_drops(
    monkeypatch,
):
    twitch = bare_twitch(monkeypatch)
    details = {
        **campaign_data(),
        "allow": {"channels": [{"name": "costream"}]},
        "timeBasedDrops": [],
    }
    inventory_campaign = campaign_data()
    del inventory_campaign["allow"]

    merged = twitch._Twitch__merge_campaign_inventory_progress(
        details, inventory_campaign
    )

    assert merged["allow"] == {"channels": [{"name": "costream"}]}
    assert merged["timeBasedDrops"] == inventory_campaign["timeBasedDrops"]


def test_inventory_progress_counts_a_campaign_without_allow_data_for_every_channel(
    monkeypatch,
):
    # Deliberately unchanged by #181: the inventory is the account's own
    # progress, so a campaign it lists without allow data still counts.
    twitch = bare_twitch(monkeypatch)
    campaign = campaign_data()
    del campaign["allow"]
    campaign["timeBasedDrops"][0]["self"] = {
        "hasPreconditionsMet": True,
        "currentMinutesWatched": 5,
        "dropInstanceID": None,
        "isClaimed": False,
    }

    twitch._Twitch__cache_drop_inventory_progress(
        {"dropCampaignsInProgress": [campaign]}
    )

    assert twitch.drop_inventory_progress["example-game"][0][-1] is None


def test_inventory_progress_takes_the_known_scope_without_allow_data(monkeypatch):
    # The campaign sync already learned this campaign's allow-list, so an
    # inventory record without one does not count for every channel.
    twitch = bare_twitch(monkeypatch)
    campaign = {**campaign_data(), "allow": {"channels": [{"name": "costream"}]}}
    _sync_with_details(monkeypatch, twitch, campaign, True, {"example-game"})
    inventory_campaign = campaign_data()
    del inventory_campaign["allow"]
    inventory_campaign["timeBasedDrops"][0]["self"] = {
        "hasPreconditionsMet": True,
        "currentMinutesWatched": 5,
        "dropInstanceID": None,
        "isClaimed": False,
    }

    twitch._Twitch__cache_drop_inventory_progress(
        {"dropCampaignsInProgress": [inventory_campaign]}
    )

    assert twitch.drop_inventory_progress["example-game"][0][-1] == ("costream",)


def _never_learned_campaign_twitch(monkeypatch):
    # A sync pass whose DropCampaignDetails failed for the game-wide
    # campaign, with nothing remembered about it (as after a restart).
    twitch = bare_twitch(monkeypatch)
    campaign = {**campaign_data(), "allow": {"channels": None}}
    _sync_with_details(monkeypatch, twitch, campaign, False, {"example-game"})
    twitch.discovered_open_drop_campaigns = []
    return twitch


def test_running_badge_listing_admits_a_never_learned_game_wide_campaign(
    monkeypatch,
):
    # As on master: a channel listing the game's running badge proves Drops
    # are enabled there, so the account's game-wide campaigns apply, even one
    # whose details did not load this cycle.
    twitch = _never_learned_campaign_twitch(monkeypatch)
    twitch.gql = SimpleNamespace(
        get_available_drops=lambda channel_id: SimpleNamespace(
            campaigns=[zero_minute_badge_campaign()], campaigns_available=True
        )
    )

    assert twitch._Twitch__get_campaign_ids_from_streamer(category_streamer()) == [
        "campaign-1"
    ]


def test_category_drops_condition_counts_a_never_learned_campaign(monkeypatch):
    # With no eligibility recorded yet, a category streamer still qualifies
    # through the game's campaign, as discovery searched it.
    twitch = _never_learned_campaign_twitch(monkeypatch)
    streamer = SimpleNamespace(
        username="drops-channel",
        from_category=True,
        from_badge_campaign=False,
        settings=SimpleNamespace(claim_drops=True),
        is_online=True,
        stream=SimpleNamespace(campaigns_ids=[], game_name=lambda: "Example Game"),
    )

    assert twitch._Twitch__category_drops_condition(streamer) is True


def test_never_learned_campaign_keeps_a_mixed_game_mixed_for_discovery(
    monkeypatch,
):
    # After a restart the game-wide campaign's details fail while the
    # allow-listed one's load. Discovery must still see one open campaign,
    # or the game turns restricted-only and the channels earning the
    # game-wide campaign are retired until the next category refresh.
    twitch = bare_twitch(monkeypatch)
    game_wide = {**campaign_data(), "id": "game-wide"}
    del game_wide["allow"]
    restricted = {
        **campaign_data(),
        "id": "restricted",
        "allow": {"channels": [{"name": "costream"}]},
    }
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_drops_dashboard",
        lambda self, **kwargs: [game_wide, {**restricted, "allow": None}],
    )
    _stub_campaign_sources(monkeypatch)
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_campaigns_details",
        lambda self, campaigns, campaign_channel_id_by_id=None, **kwargs: [
            copy.deepcopy(restricted)
        ],
    )

    twitch._Twitch__active_drop_category_slugs_from_campaigns({}, {"example-game"})

    stored = twitch.active_drop_campaigns["example-game"]
    assert sorted(
        Twitch._Twitch__campaign_logins_by_campaign(stored), key=lambda x: x or []
    ) == [None, ["costream"]]


def _hunt_category_twitch(monkeypatch, category_game=None, calls=None):
    """Twitch files 'Hunt: Showdown 1896' under the directory slug
    'hunt-showdown', while campaigns and streams are keyed by its name (#210).
    category_game is the CategoryGame answer, or an exception to raise."""
    calls = [] if calls is None else calls
    twitch = bare_twitch(monkeypatch)

    def post_gql_request_raw(operation, request):
        calls.append((operation, request["variables"]["slug"]))
        if isinstance(category_game, Exception):
            raise category_game
        if category_game is not None:
            return category_game
        if request["variables"]["slug"] == "hunt-showdown":
            game = {"id": "500188", "displayName": "Hunt: Showdown 1896"}
        else:
            game = None
        return {"data": {"game": game}}

    twitch.gql = SimpleNamespace(post_gql_request_raw=post_gql_request_raw)
    return twitch, calls


def test_category_given_by_twitch_slug_is_keyed_by_its_game_name(monkeypatch):
    twitch, calls = _hunt_category_twitch(monkeypatch)
    categories = [
        "hunt-showdown|psychoghost",
        "https://www.twitch.tv/directory/category/hunt-showdown?filter=drops",
        "Hunt: Showdown 1896",
    ]

    twitch.resolve_category_game_slugs(categories)
    twitch.resolve_category_game_slugs(categories)

    assert twitch.get_category_slugs(categories) == {"hunt-showdown-1896"}
    # One lookup per slug: Twitch's game for 'hunt-showdown', and none for the
    # name's own slug, which is kept as it is.
    assert calls == [
        ("CategoryGame", "hunt-showdown"),
        ("CategoryGame", "hunt-showdown-1896"),
    ]


@pytest.mark.parametrize(
    "category_game",
    [
        RetryError("CategoryGame", []),
        {"data": None, "errors": [{"message": "service unavailable"}]},
        {"data": {"game": {"id": "500188"}}},
    ],
)
def test_category_game_lookup_without_an_answer_is_tried_again(
    monkeypatch, category_game
):
    twitch, calls = _hunt_category_twitch(monkeypatch, category_game=category_game)

    twitch.resolve_category_game_slugs(["hunt-showdown"])
    twitch.resolve_category_game_slugs(["hunt-showdown"])

    assert twitch.get_category_slugs(["hunt-showdown"]) == {"hunt-showdown"}
    assert calls == [("CategoryGame", "hunt-showdown")] * 2


def test_failed_category_game_request_ends_the_lookup_until_the_next_call(
    monkeypatch,
):
    # A failed request has already waited through its retries; looking up the
    # other categories while Twitch is failing would only wait the same.
    twitch, calls = _hunt_category_twitch(
        monkeypatch, category_game=RetryError("CategoryGame", [])
    )

    twitch.resolve_category_game_slugs(["hunt-showdown", "overwatch"])

    assert calls == [("CategoryGame", "hunt-showdown")]

    twitch.gql.post_gql_request_raw = lambda operation, request: (
        calls.append((operation, request["variables"]["slug"]))
        or {"data": {"game": None}}
    )
    twitch.resolve_category_game_slugs(["hunt-showdown", "overwatch"])

    assert calls[1:] == [
        ("CategoryGame", "hunt-showdown"),
        ("CategoryGame", "overwatch"),
    ]


def test_category_given_by_twitch_slug_matches_its_games_twitch_campaign(
    monkeypatch,
):
    twitch, _ = _hunt_category_twitch(monkeypatch)
    campaign = campaign_data()
    campaign["game"] = {"displayName": "Hunt: Showdown 1896"}
    monkeypatch.setattr(
        Twitch, "_Twitch__get_drops_dashboard", lambda self, status="OPEN": [campaign]
    )
    monkeypatch.setattr(
        Twitch, "_Twitch__get_open_drop_campaigns_from_helix", lambda self: ([], [])
    )
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_campaigns_details",
        lambda self, campaigns, campaign_channel_id_by_id=None, **kwargs: campaigns,
    )
    monkeypatch.setattr(
        Twitch, "_Twitch__awarded_benefits", lambda self, inventory: (set(), set())
    )
    monkeypatch.setattr(
        Twitch,
        "_Twitch__twitchdrops_app_fallback",
        lambda self, categories, known_slugs, inventory: {},
    )
    inventory = {"dropCampaignsInProgress": [], "completedRewardCampaigns": []}

    twitch.resolve_category_game_slugs(["hunt-showdown|psychoghost"])

    # Twitch's own campaign for the game counts for the category, instead of
    # being skipped as "category_not_configured" in favor of the gist.
    assert twitch.filter_categories_with_active_drops(
        ["hunt-showdown|psychoghost"], inventory=inventory
    ) == ["hunt-showdown|psychoghost"]
    assert set(twitch.active_drop_campaigns) == {"hunt-showdown-1896"}


def claimable_inventory(connected=None):
    # One unclaimed, finished drop; `connected` sets the campaign's game-account
    # link state as the inventory reports it (None: no self edge sent).
    data = campaign_data()
    if connected is not None:
        data["self"] = {"isAccountConnected": connected}
        data["accountLinkURL"] = "https://example.test/link"
    data["timeBasedDrops"][0]["self"] = {
        "hasPreconditionsMet": True,
        "currentMinutesWatched": 10,
        "dropInstanceID": "instance-1",
        "isClaimed": False,
    }
    return data


def drop_event_twitch(monkeypatch, inventory, statuses=("ELIGIBLE_FOR_ALL",)):
    twitch, _drop, requests, _saved, now = claim_recording_twitch(
        monkeypatch, statuses=statuses
    )
    # Set by __init__ on a real Twitch; bare_twitch skips __init__.
    twitch.prompt_claim_pass_lock = Lock()
    twitch_module = importlib.import_module("TwitchChannelPointsMiner.classes.Twitch")
    monkeypatch.setattr(twitch_module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_inventory",
        lambda self: {"dropCampaignsInProgress": [inventory]},
    )
    return twitch, requests, now


def test_drop_claim_event_starts_a_background_worker(monkeypatch):
    twitch_module = importlib.import_module("TwitchChannelPointsMiner.classes.Twitch")
    started = []

    class RecordingThread:
        def __init__(self, target, name, daemon):
            started.append((target, name, daemon))

        def start(self):
            pass

    monkeypatch.setattr(twitch_module, "Thread", RecordingThread)
    twitch = object.__new__(Twitch)

    twitch.claim_drops_from_event({"drop_instance_id": "instance-1"})

    assert len(started) == 1
    target, _name, daemon = started[0]
    assert target == twitch._Twitch__claim_drops_from_event_worker
    assert daemon is True


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(None, id="no-payload"),
        pytest.param("instance-1", id="string-payload"),
        pytest.param([], id="list-payload"),
        pytest.param({}, id="empty-payload"),
        pytest.param({"drop_instance_id": ""}, id="empty-instance-id"),
        pytest.param({"drop_id": "drop-1"}, id="missing-instance-id"),
    ],
)
def test_drop_claim_event_ignores_malformed_payloads(monkeypatch, data):
    twitch_module = importlib.import_module("TwitchChannelPointsMiner.classes.Twitch")
    started = []
    monkeypatch.setattr(
        twitch_module,
        "Thread",
        lambda **kwargs: started.append(kwargs),
    )
    twitch = object.__new__(Twitch)

    twitch.claim_drops_from_event(data)

    assert started == []


def test_drop_claim_event_claims_drop_once(monkeypatch):
    twitch, requests, _now = drop_event_twitch(
        monkeypatch, claimable_inventory(connected=True)
    )

    twitch._Twitch__claim_drops_from_event_worker()

    assert requests == ["instance-1"]
    assert twitch.accepted_drop_ids == frozenset({"drop-1"})


def test_drop_claim_event_does_not_reclaim_an_accepted_drop(monkeypatch):
    # A duplicate drop-claim, or one that arrives while the inventory still
    # lists the drop as unclaimed, must not send a second claim.
    twitch, requests, _now = drop_event_twitch(
        monkeypatch, claimable_inventory(connected=True)
    )

    twitch._Twitch__claim_drops_from_event_worker()
    twitch._Twitch__claim_drops_from_event_worker()

    assert requests == ["instance-1"]


def test_drop_claim_event_does_not_repeat_claim_for_unlinked_account(monkeypatch):
    # Twitch accepts the claim but delivers nothing while the game account is
    # unlinked; the unlinked record stops later events from claiming again.
    twitch, requests, _now = drop_event_twitch(
        monkeypatch,
        claimable_inventory(connected=False),
        statuses=(("ELIGIBLE_FOR_ALL", False),),
    )

    twitch._Twitch__claim_drops_from_event_worker()
    twitch._Twitch__claim_drops_from_event_worker()

    assert requests == ["instance-1"]
    assert "instance-1" in twitch.unlinked_drop_claims
