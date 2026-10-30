import pytest

from TwitchChannelPointsMiner.classes.entities.Campaign import (
    CampaignChannelScope,
    campaign_channel_scope,
)

OPEN = CampaignChannelScope.OPEN
RESTRICTED = CampaignChannelScope.RESTRICTED
UNKNOWN = CampaignChannelScope.UNKNOWN


@pytest.mark.parametrize(
    "campaign, expected",
    [
        # twitchdrops.app catalog records say outright whether they are open.
        ({"all_channels": True, "channels": []}, (OPEN, [])),
        ({"all_channels": True, "channels": ["ignored"]}, (OPEN, [])),
        (
            {"all_channels": False, "channels": ["One", "two"]},
            (RESTRICTED, ["one", "two"]),
        ),
        # Scraped without its channel list: restricted to nobody we know.
        ({"all_channels": False, "channels": []}, (UNKNOWN, [])),
        # Twitch's own records: the allow block decides.
        (
            {"allow": {"channels": [{"name": "Alpha"}, {"login": "beta"}]}},
            (RESTRICTED, ["alpha", "beta"]),
        ),
        # Twitch sends a null channel list for a game-wide campaign.
        ({"allow": {"channels": None}}, (OPEN, [])),
        # An allow-list naming no login the miner can read is not game-wide.
        ({"allow": {"channels": [{"id": "100"}, {"id": "200"}]}}, (UNKNOWN, [])),
        ({"allow": {"channels": []}}, (OPEN, [])),
        # No allow block: a dashboard summary, or details that failed to load.
        ({"id": "campaign-1", "name": "Summary"}, (UNKNOWN, [])),
        ({"allow": None}, (UNKNOWN, [])),
        # Entries that carry their own channel list without all_channels.
        ({"channels": []}, (OPEN, [])),
        ({"channels": ["Solo"]}, (RESTRICTED, ["solo"])),
        (None, (UNKNOWN, [])),
    ],
)
def test_campaign_channel_scope(campaign, expected):
    assert campaign_channel_scope(campaign) == expected
