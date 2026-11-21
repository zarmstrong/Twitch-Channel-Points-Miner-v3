from datetime import datetime
from enum import Enum, auto
from typing import Optional

from TwitchChannelPointsMiner.classes.entities.Drop import Drop, utc_now
from TwitchChannelPointsMiner.classes.entities.RewardCampaign import (
    REWARD_GROUP_KEY,
    campaign_from_reward_groups,
    watch_reward_categories,
)
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


def _campaign_channel_entries(campaign: dict) -> list:
    """The raw entries of the campaign's own "channels" list, else of
    Twitch's allow block."""
    channels = campaign.get("channels")
    if channels is None:
        allow = campaign.get("allow")
        channels = allow.get("channels") if isinstance(allow, dict) else None
    return channels or []


def campaign_channel_logins(campaign: dict) -> list:
    """The campaign's allow-listed logins, lowercased and in allow-list order,
    from its own "channels" list or Twitch's allow block."""
    logins = {}
    for channel in _campaign_channel_entries(campaign):
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
    it, and neither does a channel list with no login the miner can read."""
    if not isinstance(campaign, dict):
        return CampaignChannelScope.UNKNOWN, []
    all_channels = campaign.get("all_channels")
    if all_channels is True:
        return CampaignChannelScope.OPEN, []
    logins = campaign_channel_logins(campaign)
    if logins:
        return CampaignChannelScope.RESTRICTED, logins
    if all_channels is False or _campaign_channel_entries(campaign):
        return CampaignChannelScope.UNKNOWN, []
    if isinstance(campaign.get("channels"), list):
        return CampaignChannelScope.OPEN, []
    if isinstance(campaign.get("allow"), dict):
        return CampaignChannelScope.OPEN, []
    return CampaignChannelScope.UNKNOWN, []


def campaign_includes_watch_requirement(campaign) -> Optional[bool]:
    """Whether Twitch says any of the campaign's drops needs watch time
    (summary.includesMWRequirement), or None when it did not say.

    Only DropsHighlightService_AvailableDrops sends the summary. False means
    watching cannot earn the campaign; True does not mean the account can,
    since a drop may also need a subscription."""
    if not isinstance(campaign, dict):
        return None
    summary = campaign.get("summary")
    if not isinstance(summary, dict):
        return None
    includes_watch_requirement = summary.get("includesMWRequirement")
    if isinstance(includes_watch_requirement, bool):
        return includes_watch_requirement
    return None


def _int_or_zero(value) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return 0


def campaign_from_channel_progress(campaign) -> Optional[dict]:
    """Convert one item of Twitch's DropChannelCampaignsProgress
    data.channelDropCampaignsProgress list into the DropCampaignDetails /
    listing shape the rest of the miner reads.

    The TV-client login gets this data while DropCampaignDetails returns null
    (#223), so the channel's drops listing is built from it. Track/lane and
    repeatable reward groups, and groups whose requirementType is neither
    WATCH nor SUB, are skipped: the miner does not model them.
    A group's "self" is not copied: its status is unreliable (unstarted
    campaigns report IN_PROGRESS with currentMinutesWatched null), and
    inventory supplies progress elsewhere. Returns None when the campaign is
    malformed or no reward group is kept.

    A campaign without a game that watching a category can earn is a sitewide
    reward campaign (a quest), which campaign_from_reward_groups converts
    instead: its repeatable groups are kept, and "self" is its only source of
    progress."""
    if not isinstance(campaign, dict):
        return None
    if "game" in campaign and not campaign["game"]:
        reward_campaign = campaign_from_reward_groups(campaign)
        if reward_campaign is not None:
            return reward_campaign
    campaign_id = campaign.get("id")
    start_at = campaign.get("startAt")
    end_at = campaign.get("endAt")
    if not campaign_id or not start_at or not end_at:
        return None
    try:
        parse_datetime(start_at)
        parse_datetime(end_at)
    except (TypeError, ValueError):
        return None
    campaign_name = campaign.get("name")

    drops = []
    allow_channels = {}
    groups = campaign.get("rewardGroups")
    for group in groups if isinstance(groups, list) else []:
        if not isinstance(group, dict):
            continue
        if group.get("track") is not None or group.get("lane") is not None:
            continue
        criteria = group.get("progressCriteria")
        if not isinstance(criteria, dict):
            criteria = {}
        if criteria.get("isRepeatable") is True:
            continue
        # Only watch-time and subscription groups map onto time-based drops.
        # Any other requirement would read as a zero-minute badge.
        if criteria.get("requirementType") not in ["WATCH", "SUB"]:
            continue
        requirements = criteria.get("requirements")
        if not isinstance(requirements, dict):
            requirements = {}
        rewards = group.get("rewards")
        drops.append(
            {
                "id": group.get("id"),
                "name": group.get("name") or campaign_name or campaign_id,
                "startAt": start_at,
                "endAt": end_at,
                "requiredMinutesWatched": _int_or_zero(
                    requirements.get("minutesWatched")
                ),
                "requiredSubs": _int_or_zero(requirements.get("subs")),
                "benefitEdges": [
                    {
                        "benefit": {
                            "id": reward.get("id"),
                            "name": reward.get("name") or "",
                            "distributionType": reward.get("distributionType"),
                        }
                    }
                    for reward in (rewards if isinstance(rewards, list) else [])
                    if isinstance(reward, dict)
                ],
            }
        )
        channels = criteria.get("channels")
        if isinstance(channels, list):
            for channel in channels:
                if isinstance(channel, dict) and channel.get("id"):
                    allow_channels.setdefault(str(channel["id"]), None)
    if not drops:
        return None

    game = campaign.get("game")
    converted = {
        "id": campaign_id,
        "name": campaign_name or campaign_id,
        "status": "ACTIVE",
        "game": game if isinstance(game, dict) else {},
        "startAt": start_at,
        "endAt": end_at,
        "summary": {
            "includesMWRequirement": any(
                drop["requiredMinutesWatched"] > 0 for drop in drops
            ),
            "includesSubRequirement": any(drop["requiredSubs"] > 0 for drop in drops),
        },
        "timeBasedDrops": drops,
    }
    # Twitch sends channels: null for channel-restricted campaigns too, so a
    # missing or empty list says nothing about who can earn the campaign.
    # Leave the allow block out, as a channel's listing does, so the campaign
    # reads as unknown and a remembered allow-list still applies: an empty
    # allow block would read as open to every channel.
    if allow_channels:
        converted["allow"] = {
            "channels": [{"id": channel_id} for channel_id in allow_channels]
        }
    return converted


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
        "reward_categories",
        "reward_drop_category_ids",
        "reward_drop_report_categories",
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
        self.dt_match = self.start_at < utc_now() < self.end_at

        self.drops = list(map(lambda x: Drop(x), dict["timeBasedDrops"]))
        # A watch-earned reward campaign (see RewardCampaign.py) counts in
        # several categories, each drop in its own: the categories, and the
        # IDs of those each drop counts in. Empty for a Drop campaign.
        self.reward_categories = watch_reward_categories(dict)
        self.reward_drop_category_ids = {
            drop["id"]: {
                category["id"] for category in drop[REWARD_GROUP_KEY]["categories"]
            }
            for drop in (dict["timeBasedDrops"] if self.reward_categories else [])
            if REWARD_GROUP_KEY in drop
        }
        # The name of the one category each drop's progress and claim are
        # reported under: its first, whichever of them a stream was watched
        # in.
        self.reward_drop_report_categories = {
            drop["id"]: (
                drop[REWARD_GROUP_KEY]["categories"][0].get("displayName")
                or drop[REWARD_GROUP_KEY]["categories"][0].get("name")
            )
            for drop in (dict["timeBasedDrops"] if self.reward_categories else [])
            if (drop.get(REWARD_GROUP_KEY) or {}).get("categories")
        }

    def __repr__(self):
        return f"Campaign(id={self.id}, name={self.name}, game={self.game}, in_inventory={self.in_inventory})"

    def __str__(self):
        # A campaign listed on a channel names its game without displayName.
        game = self.game or {}
        game_name = game.get("displayName") or game.get("name") or "Unknown"
        return (
            f"{self.name}, Game: {game_name} - Drops: {len(self.drops)} pcs. - In inventory: {self.in_inventory}"
            if Settings.logger.less
            else self.__repr__()
        )

    def reward_category_for(self, game_id, game_slug, slugify):
        """The category of this reward campaign a stream of the given game
        counts in, matched by game ID or else by slugified name; None when it
        counts in none of them, or this is a Drop campaign."""
        for category in self.reward_categories:
            if game_id not in [None, ""] and category["id"] == str(game_id):
                return category
        for category in self.reward_categories:
            if game_slug and slugify(category["displayName"] or category["name"]) == (
                game_slug
            ):
                return category
        return None

    def in_reward_category(self, category):
        """This reward campaign as channels of one of its categories earn it:
        that category as its game, and the drops that count there. The drops
        are shared with this campaign, so progress synced into either shows in
        both."""
        view = object.__new__(Campaign)
        for slot in Campaign.__slots__:
            setattr(view, slot, getattr(self, slot))
        view.game = category
        view.drops = [
            drop
            for drop in self.drops
            if category["id"] in self.reward_drop_category_ids.get(drop.id, ())
        ]
        return view

    def clear_drops(self):
        self.drops = list(
            filter(lambda x: x.dt_match is True and x.is_claimed is False, self.drops)
        )

    def watchable_drops(self):
        """The drops watching can add minutes to now."""
        return [
            drop
            for drop in self.drops
            if drop.dt_match is True
            and drop.is_claimed is False
            and drop.current_minutes_watched < drop.minutes_required
            and drop.needs_subscription() is False
        ]

    def has_watchable_drops(self):
        return bool(self.watchable_drops())

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
                    if REWARD_GROUP_KEY in drop:
                        # A repeat whose window started over is listed
                        # under the same ID with the new window's dates.
                        # Left with the old ones, its progress would be
                        # reported against a deadline already past.
                        self.drops[i].set_window(drop["startAt"], drop["endAt"])
                    self.drops[i].update(drop["self"])
                    # If after update we all conditions are meet we can claim the drop
                    if self.drops[i].is_claimable is True:
                        claimed = callback(self.drops[i], campaign=self)
                        self.drops[i].is_claimed = claimed
                    break
