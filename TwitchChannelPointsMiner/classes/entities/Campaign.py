from datetime import datetime
from enum import Enum, auto

from TwitchChannelPointsMiner.classes.entities.Drop import Drop
from TwitchChannelPointsMiner.classes.Settings import Settings


def parse_datetime(datetime_str):
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(datetime_str, fmt)
        except ValueError:
            continue
    raise ValueError(f"time data '{datetime_str}' does not match format")


class CampaignChannelScope(Enum):
    """Which channels can earn a Drop campaign, as far as the miner knows."""

    OPEN = auto()  # Any channel streaming the game.
    RESTRICTED = auto()  # Only the channels on its allow-list.
    UNKNOWN = auto()  # Its channel list was never loaded.


def campaign_channel_logins(campaign: dict) -> list:
    """The campaign's allow-listed logins, lowercased and in allow-list order,
    from its own "channels" list or Twitch's allow block."""
    channels = campaign.get("channels")
    if channels is None:
        allow = campaign.get("allow")
        channels = allow.get("channels") if isinstance(allow, dict) else None

    logins = {}
    for channel in channels or []:
        if isinstance(channel, str):
            login = channel
        elif isinstance(channel, dict):
            login = channel.get("name") or channel.get("login")
        else:
            continue
        login = str(login or "").lower().strip()
        if login:
            logins.setdefault(login, None)
    return list(logins)


def campaign_channel_scope(campaign) -> tuple:
    """(scope, allow-listed logins) of a campaign record from any source; the
    logins are empty unless the scope is RESTRICTED.

    A twitchdrops.app catalog record says outright whether it is open to all
    channels (all_channels), and so do Twitch's own campaigns once stored in
    active_drop_campaigns; one that is not open yet names no channel was
    stored without its channel list. Otherwise a record naming channels is
    restricted, and one carrying a "channels" list or Twitch's allow block
    without any is open: Twitch sends {"channels": null} for a game-wide
    campaign. A record with neither, such as a dashboard summary or a
    campaign whose details failed to load, says nothing about who can earn
    it."""
    if not isinstance(campaign, dict):
        return CampaignChannelScope.UNKNOWN, []
    all_channels = campaign.get("all_channels")
    if all_channels is True:
        return CampaignChannelScope.OPEN, []
    logins = campaign_channel_logins(campaign)
    if logins:
        return CampaignChannelScope.RESTRICTED, logins
    if all_channels is False:
        return CampaignChannelScope.UNKNOWN, []
    if isinstance(campaign.get("channels"), list):
        return CampaignChannelScope.OPEN, []
    if isinstance(campaign.get("allow"), dict):
        return CampaignChannelScope.OPEN, []
    return CampaignChannelScope.UNKNOWN, []


class Campaign(object):
    __slots__ = [
        "id",
        "game",
        "name",
        "status",
        "in_inventory",
        "end_at",
        "start_at",
        "dt_match",
        "drops",
        "channels",
    ]

    def __init__(self, dict):
        self.id = dict["id"]
        self.game = dict["game"]
        self.name = dict["name"]
        self.status = dict["status"]
        allow = dict.get("allow") or {}
        self.channels = (
            []
            if allow.get("channels") is None
            else list(map(lambda x: x["id"], allow["channels"]))
        )
        self.in_inventory = False

        self.end_at = parse_datetime(dict["endAt"])
        self.start_at = parse_datetime(dict["startAt"])
        self.dt_match = self.start_at < datetime.now() < self.end_at

        self.drops = list(map(lambda x: Drop(x), dict["timeBasedDrops"]))

    def __repr__(self):
        return f"Campaign(id={self.id}, name={self.name}, game={self.game}, in_inventory={self.in_inventory})"

    def __str__(self):
        return (
            f"{self.name}, Game: {self.game['displayName']} - Drops: {len(self.drops)} pcs. - In inventory: {self.in_inventory}"
            if Settings.logger.less
            else self.__repr__()
        )

    def clear_drops(self):
        self.drops = list(
            filter(lambda x: x.dt_match is True and x.is_claimed is False, self.drops)
        )

    def has_watchable_drops(self):
        return any(
            drop.dt_match is True
            and drop.is_claimed is False
            and drop.current_minutes_watched < drop.minutes_required
            for drop in self.drops
        )

    def __eq__(self, other):
        if isinstance(other, self.__class__):
            return self.id == other.id
        else:
            return False

    def sync_drops(self, drops, callback):
        # Iterate all the drops from inventory
        for drop in drops:
            # Iterate all the drops from out campaigns array
            # After id match update with:
            # [currentMinutesWatched, hasPreconditionsMet, dropInstanceID, isClaimed]
            for i in range(len(self.drops)):
                current_id = self.drops[i].id
                if drop["id"] == current_id:
                    self.drops[i].update(drop["self"])
                    # If after update we all conditions are meet we can claim the drop
                    if self.drops[i].is_claimable is True:
                        claimed = callback(self.drops[i], campaign=self)
                        self.drops[i].is_claimed = claimed
                    break
