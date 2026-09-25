from types import SimpleNamespace

import pytest

from TwitchChannelPointsMiner.classes.Chat import ChatPresence
from TwitchChannelPointsMiner.classes.entities.Bet import BetSettings, DelayMode
from TwitchChannelPointsMiner.classes.entities.Campaign import Campaign
from TwitchChannelPointsMiner.classes.entities.Streamer import (
    Streamer,
    StreamerSettings,
    discovery_source,
    discovery_sources,
    is_category_tier,
    is_drop_discovered,
)
from TwitchChannelPointsMiner.classes.Settings import StreamerSource


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


def streamer_settings(**overrides):
    values = {
        "make_predictions": True,
        "follow_raid": True,
        "claim_drops": True,
        "claim_moments": True,
        "watch_streak": True,
        "favorite": False,
        "points_limit": None,
        "community_goals": False,
        "bet": BetSettings(delay=6, delay_mode=DelayMode.FROM_END),
        "chat": ChatPresence.NEVER,
    }
    values.update(overrides)
    return StreamerSettings(**values)


def test_streamer_settings_default_preserves_values_and_fills_missing_ones():
    settings = StreamerSettings(make_predictions=False, community_goals=True)

    settings.default()

    assert settings.make_predictions is False
    assert settings.follow_raid is True
    assert settings.claim_drops is True
    assert settings.community_goals is True
    assert settings.favorite is False
    assert settings.points_limit is None
    assert isinstance(settings.bet, BetSettings)
    assert settings.chat is ChatPresence.ONLINE


def test_streamer_normalizes_username_and_builds_url():
    streamer = Streamer("  Some_Channel  ", settings=streamer_settings())

    assert streamer.username == "some_channel"
    assert streamer.streamer_url.endswith("/some_channel")


def test_streamer_preserves_existing_positional_source_flags():
    streamer = Streamer("channel", None, True, True, True)

    assert streamer.from_category is True
    assert streamer.explicitly_configured is True
    assert streamer.from_badge_campaign is True
    assert streamer.from_followers is False


def test_online_and_offline_transitions_update_state_and_timestamps(monkeypatch):
    times = iter([100.0, 200.0])
    monkeypatch.setattr(
        "TwitchChannelPointsMiner.classes.entities.Streamer.time.time",
        lambda: next(times),
    )
    streamer = Streamer("channel", settings=streamer_settings())

    streamer.set_online()
    streamer.set_offline()

    assert streamer.online_at == 100.0
    assert streamer.offline_at == 200.0
    assert streamer.is_online is False


def test_history_accumulates_rewards_and_marks_watch_streak_complete():
    streamer = Streamer("channel", settings=streamer_settings())

    streamer.update_history("WATCH", earned=10)
    streamer.update_history("WATCH", earned=15, counter=2)
    streamer.update_history("WATCH_STREAK", earned=450)

    assert streamer.history["WATCH"] == {"counter": 3, "amount": 25}
    assert streamer.stream.watch_streak_missing is False
    assert "WATCH (3 times, 25 gained)" in streamer.print_history()


def test_point_multipliers_are_summed_when_present():
    streamer = Streamer("channel", settings=streamer_settings())
    streamer.activeMultipliers = [{"factor": 1.2}, {"factor": 0.5}]

    assert streamer.viewer_has_points_multiplier() is True
    assert streamer.total_points_multiplier() == pytest.approx(1.7)


@pytest.mark.parametrize(
    ("mode", "delay", "expected"),
    [
        (DelayMode.FROM_START, 6, 6),
        (DelayMode.FROM_START, 60, 30),
        (DelayMode.FROM_END, 6, 24),
        (DelayMode.FROM_END, 60, 0),
        (DelayMode.PERCENTAGE, 0.5, 15),
        (None, 6, 30),
    ],
)
def test_prediction_window_modes(mode, delay, expected):
    settings = streamer_settings(bet=BetSettings(delay=delay, delay_mode=mode))
    streamer = Streamer("channel", settings=settings)

    assert streamer.get_prediction_window(30) == expected


def test_drops_condition_requires_online_stream_with_unclaimed_drops():
    streamer = Streamer("channel", settings=streamer_settings())
    streamer.is_online = True
    streamer.stream.campaigns_ids = ["campaign-1"]
    campaign = Campaign(campaign_data())
    streamer.stream.campaigns = [campaign]

    assert streamer.drops_condition() is True

    campaign.drops = []
    assert streamer.drops_condition() is False


def test_drops_condition_is_false_when_all_drops_are_captured_but_unclaimed():
    streamer = Streamer("channel", settings=streamer_settings())
    streamer.is_online = True
    streamer.stream.campaigns_ids = ["campaign-1"]
    campaign = Campaign(campaign_data())
    campaign.drops[0].current_minutes_watched = 10
    streamer.stream.campaigns = [campaign]

    assert streamer.drops_condition() is False


def test_discovery_source_prefers_badge_then_wildcard_then_category():
    # Badge and wildcard streamers are built with from_category=True as well,
    # so the more specific flag must win over the shared one.
    assert (
        Streamer(
            "b", from_category=True, from_badge_campaign=True
        ).discovery_source()
        == StreamerSource.BADGES
    )
    assert (
        Streamer(
            "w", from_category=True, from_wildcard_category=True
        ).discovery_source()
        == StreamerSource.WILDCARD_CATEGORIES
    )
    assert (
        Streamer("c", from_category=True).discovery_source()
        == StreamerSource.CATEGORIES
    )
    assert (
        Streamer("f", from_followers=True).discovery_source()
        == StreamerSource.FOLLOWERS
    )
    assert Streamer("s").discovery_source() == StreamerSource.STREAMERS


def test_category_tier_excludes_badge_but_drop_discovered_includes_it():
    badge = Streamer("b", from_category=True, from_badge_campaign=True)
    wildcard = Streamer("w", from_category=True, from_wildcard_category=True)
    category = Streamer("c", from_category=True)
    followed = Streamer("f", from_followers=True)

    assert [is_category_tier(s) for s in (badge, wildcard, category, followed)] == [
        False,
        True,
        True,
        False,
    ]
    assert [is_drop_discovered(s) for s in (badge, wildcard, category, followed)] == [
        True,
        True,
        True,
        False,
    ]


def test_discovery_source_tolerates_stand_ins_missing_flags():
    assert discovery_source(SimpleNamespace(from_category=True)) == (
        StreamerSource.CATEGORIES
    )
    assert discovery_source(SimpleNamespace()) == StreamerSource.STREAMERS


def test_discovery_sources_lists_every_tier_a_streamer_belongs_to():
    # from_category is only the CATEGORIES tier when the streamer is not also a
    # badge or wildcard streamer, which set it as a shared marker.
    assert discovery_sources(
        Streamer("b", from_category=True, from_badge_campaign=True)
    ) == {StreamerSource.BADGES}
    assert discovery_sources(
        Streamer("w", from_category=True, from_wildcard_category=True)
    ) == {StreamerSource.WILDCARD_CATEGORIES}
    assert discovery_sources(
        Streamer("both", from_followers=True, explicitly_configured=True)
    ) == {StreamerSource.FOLLOWERS, StreamerSource.STREAMERS}


def test_discovery_source_picks_highest_ranked_of_overlapping_tiers():
    both = Streamer("both", from_followers=True, explicitly_configured=True)

    # No user ordering supplied: the miner's default puts STREAMERS first.
    assert discovery_source(both) == StreamerSource.STREAMERS
    assert (
        discovery_source(both, [StreamerSource.STREAMERS, StreamerSource.FOLLOWERS])
        == StreamerSource.STREAMERS
    )
    assert (
        discovery_source(both, [StreamerSource.FOLLOWERS, StreamerSource.STREAMERS])
        == StreamerSource.FOLLOWERS
    )
    # A partial ordering that omits the streamer's tiers still classifies it.
    assert (
        discovery_source(both, [StreamerSource.CATEGORIES]) == StreamerSource.STREAMERS
    )


def test_priority_never_turns_a_badge_streamer_into_a_category_streamer():
    badge = Streamer("b", from_category=True, from_badge_campaign=True)

    assert (
        discovery_source(
            badge, [StreamerSource.CATEGORIES, StreamerSource.BADGES]
        )
        == StreamerSource.BADGES
    )
    assert is_category_tier(badge) is False
