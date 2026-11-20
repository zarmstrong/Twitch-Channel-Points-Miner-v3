"""Twitch reward campaigns (quests) that are earned by watching.

Twitch lists these apart from Drop campaigns, in the dashboard's
rewardCampaignsAvailableToUser. They are sitewide and carry no game: each
reward group names the categories whose channels count instead, and a group
can repeat, earning once per window (a day, say) a set number of times.

The account's progress on a reward group comes only from a channel's
DropChannelCampaignsProgress listing ("self" on each group). The inventory
never lists these campaigns in progress, and it reports one as completed as
soon as any of its groups is, so neither says what is left to watch.

The functions here turn such a campaign into the Drop campaign shape the rest
of the miner reads, with one time-based drop per watchable reward group.
"""

from datetime import datetime
from typing import Optional

from TwitchChannelPointsMiner.classes.entities.Drop import utc_now

# Marks a converted campaign, and each of its drops, with what the Drop
# campaign shape cannot carry.
WATCH_REWARD_KEY = "_watch_reward"
REWARD_GROUP_KEY = "_reward_group"

# The reward group statuses Twitch's own Drops tray knows. A group reporting
# any other is not mined.
KNOWN_GROUP_STATUSES = frozenset(
    ["IN_PROGRESS", "CLAIMABLE", "CLAIMED", "FULFILLED", "EXPIRED"]
)
EARNED_GROUP_STATUSES = frozenset(["CLAIMED", "FULFILLED"])

TWITCH_DATETIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _parse_datetime(value) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", TWITCH_DATETIME_FORMAT):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _count(value) -> int:
    # bool is an int subclass, but never a count.
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(value, 0)


def is_watch_reward_listing(listing) -> bool:
    """Whether a rewardCampaignsAvailableToUser entry can be earned, at least
    in part, by watching: it is sitewide and asks for watch time.

    An entry that asks only for subscriptions is not, and neither is one that
    does not say what it asks for."""
    if not isinstance(listing, dict) or listing.get("isSitewide") is False:
        return False
    requirements = listing.get("unlockRequirements")
    if not isinstance(requirements, dict):
        return False
    return _count(requirements.get("minuteWatchedGoal")) > 0


def reward_drop_instance_id(user_id, campaign_id, group_id) -> str:
    """The ID DropsPage_ClaimDropRewards takes to claim a reward group."""
    return f"{user_id}#{campaign_id}#{group_id}"


def is_watch_reward_campaign(campaign) -> bool:
    return isinstance(campaign, dict) and isinstance(
        campaign.get(WATCH_REWARD_KEY), dict
    )


def watch_reward_categories(campaign) -> list:
    """The categories ({"id", "displayName", "name"}) whose channels count
    toward a converted campaign."""
    if not is_watch_reward_campaign(campaign):
        return []
    return list(campaign[WATCH_REWARD_KEY].get("categories") or [])


def _watch_group_categories(group) -> list:
    """The categories ({"id", "displayName", "name"}) a reward group counts
    in when watching one of them can earn it, whatever its progress; empty
    when watching a category cannot earn it."""
    if not isinstance(group, dict) or not group.get("id"):
        return []
    # Only the lane the account selected on a track earns progress, and the
    # miner does not read selections. A group on a track without a lane is
    # not a choice, and earns like any other.
    if group.get("lane") is not None:
        return []
    criteria = group.get("progressCriteria")
    if not isinstance(criteria, dict) or criteria.get("requirementType") != "WATCH":
        return []
    requirements = criteria.get("requirements")
    if (
        _count(
            requirements.get("minutesWatched")
            if isinstance(requirements, dict)
            else None
        )
        <= 0
    ):
        return []
    # A group limited to channels by ID cannot be matched to a category.
    if criteria.get("channels"):
        return []
    return [
        {
            "id": str(category["id"]),
            "displayName": category.get("displayName") or category.get("name") or "",
            "name": category.get("name") or category.get("displayName") or "",
        }
        for category in criteria.get("categories") or []
        if isinstance(category, dict) and category.get("id")
    ]


def repeat_drop_id(group_id, repeat_number) -> str:
    """The drop ID of one repeat of a repeating reward group."""
    return f"{group_id}#{repeat_number}"


def repeat_drop_name(group_name, repeat_number, times) -> str:
    """The name of one repeat of a repeating reward group. It says which
    repeat the drop is, not how many Twitch has counted: that number moves
    while the repeat is watched for, claimed and reported."""
    return f"{group_name} (repeat {min(repeat_number + 1, times)} of {times})"


def _reward_group_drop(group, campaign, progress_known, now) -> Optional[dict]:
    """One reward group as a time-based drop, or None when watching cannot
    earn it or its state is unknown.

    A repeating group becomes the drop for its current repeat, with the
    number of repeats granted so far in its ID, so each repeat is its own
    drop to the rest of the miner."""
    categories = _watch_group_categories(group)
    if not categories:
        return None
    criteria = group["progressCriteria"]
    required = _count(criteria["requirements"].get("minutesWatched"))

    group_self = group.get("self")
    if not isinstance(group_self, dict):
        if progress_known:
            # Twitch reports progress on the campaign's other groups but none
            # on this one: something other than watching unlocks it.
            return None
        group_self = {}
    status = group_self.get("status")
    if status is not None and status not in KNOWN_GROUP_STATUSES:
        return None

    repeat = criteria.get("repeatableConfig")
    if not isinstance(repeat, dict):
        repeat = {}
    times = max(_count(repeat.get("repeatableTimes")), 1)
    complete_all = repeat.get("shouldCompleteAllRepeatsToEarn") is True and times > 1
    grants = _count(group_self.get("grantCount"))

    window = group_self.get("currentWindow")
    window_expires = _parse_datetime(
        window.get("expiresAt") if isinstance(window, dict) else None
    )
    window_open = window_expires is None or window_expires > now
    watched = (
        min(_count(group_self.get("currentMinutesWatched")), required)
        if window_open
        else 0
    )
    next_window = _parse_datetime(group_self.get("nextEligibleAt"))

    start_at = campaign["startAt"]
    end_at = campaign["endAt"]
    claimed = False
    claimable = False
    expired = False
    # Twitch names the moment a repeating group can be earned again: a
    # repeat reported claimed is then not the group's last, whatever its
    # status says, before that moment or after it. Its minutes are the
    # claimed repeat's, not the next one's.
    reopens = grants < times and not complete_all and next_window is not None
    if status in EARNED_GROUP_STATUSES and reopens:
        watched = 0
    if status in EARNED_GROUP_STATUSES and not reopens:
        claimed = True
        watched = required
    elif status == "EXPIRED":
        # Over without being earned: nothing to watch for, and not a reward
        # the account got. A drop whose own window has closed says both.
        expired = True
        end_at = start_at
    elif status == "CLAIMABLE" and (not complete_all or grants >= times):
        # A group that pays out only once every repeat is in reports
        # CLAIMABLE after each one, and Twitch's own drops tray offers the
        # claim only with all of them counted. Until then it is a repeat
        # like any other, handled below.
        claimable = True
        watched = required
    elif grants >= times:
        claimed = True
        watched = required
    elif next_window is not None and next_window > now:
        # Earned for this window; nothing counts until the next one opens.
        start_at = next_window.strftime(TWITCH_DATETIME_FORMAT)
        watched = 0
    elif window_open and window_expires is not None:
        minutes_left = (window_expires - now).total_seconds() / 60
        if watched >= required or (required - watched) > minutes_left:
            # Either this window's minutes are in, or they cannot be before
            # it ends and takes the progress with it. Watching counts again
            # once it has ended.
            start_at = window_expires.strftime(TWITCH_DATETIME_FORMAT)
            watched = 0
        else:
            # The minutes are due before the window closes, not before the
            # campaign does: a daily repeat is ranked and reported by today's
            # deadline.
            campaign_end = _parse_datetime(end_at)
            if campaign_end is None or window_expires < campaign_end:
                end_at = window_expires.strftime(TWITCH_DATETIME_FORMAT)
    elif complete_all and watched >= required:
        # A group that pays out once every repeat is in, with repeats still
        # to come and no window named to wait out: the minutes are those of
        # the repeat counted last. Left in, the next repeat would read as
        # earned by them, and as nothing left to watch for.
        watched = 0

    # When the repeat after a claimable one opens, as far as Twitch says:
    # once claimed, the group has nothing to watch for until then. A moment
    # already past is kept too. It says the next window is open, which a
    # listing that still shows this repeat waiting to be claimed (the claim
    # is on its way, or the answer was slow) does not show otherwise.
    next_start = None
    if claimable and grants < times:
        # Twitch's own word for when the group can be earned again, else
        # the end of the window the repeat was earned in.
        opens = next_window if next_window is not None else window_expires
        if opens is not None:
            next_start = opens.strftime(TWITCH_DATETIME_FORMAT)

    drop_id = str(group["id"])
    name = group.get("name") or campaign["name"]
    drop_name = name
    repeat_number = None
    if times > 1:
        # The count is read as the repeats whose minutes are in, the way
        # Twitch's drops tray reads it (it offers the claim of a group that
        # pays out once every repeat is in when the count reaches the
        # number of repeats). So the repeat being watched has the count for
        # its number, and the one earned or waiting to be claimed is the
        # last one counted. Numbered from what Twitch sends alone, a repeat
        # keeps its ID across a restart. (Should Twitch count a repeat only
        # when it is claimed, the stored record keeps the numbers right
        # while the miner runs; a miner started while a repeat waits to be
        # claimed then numbers that one repeat one too low.)
        repeat_number = grants
        if claimed or claimable:
            repeat_number = max(min(grants, times) - 1, 0)
        drop_id = repeat_drop_id(group["id"], repeat_number)
        drop_name = repeat_drop_name(name, repeat_number, times)
    rewards = group.get("rewards")
    return {
        "id": drop_id,
        "name": drop_name,
        "startAt": start_at,
        "endAt": end_at,
        "requiredMinutesWatched": required,
        # A watch group that asks for subscriptions as well is left to the
        # checks every drop with a subscription requirement gets.
        "requiredSubs": _count(criteria["requirements"].get("subs")),
        "benefitEdges": [
            {
                "benefit": {
                    "id": reward.get("id"),
                    "name": reward.get("name") or "",
                    "distributionType": reward.get("distributionType"),
                    "imageAssetURL": reward.get("thumbnailURL"),
                }
            }
            for reward in (rewards if isinstance(rewards, list) else [])
            if isinstance(reward, dict)
        ],
        "self": {
            "isClaimed": claimed,
            "hasPreconditionsMet": True,
            "currentMinutesWatched": watched,
            "currentSubs": _count(group_self.get("currentSubs")),
            # Filled in by with_claim_instance_ids, which knows the viewer.
            "dropInstanceID": None,
        },
        REWARD_GROUP_KEY: {
            "id": str(group["id"]),
            "claimable": claimable,
            # Nothing more can be earned in the group as Twitch reports it.
            "finished": claimed or expired,
            "grants": grants,
            # The number in a repeating group's drop ID; None for a group
            # earned once.
            "repeat": repeat_number,
            "times": times,
            # The reward comes with the last repeat, not with each one.
            "complete_all": complete_all,
            "name": name,
            "next_start": next_start,
            "categories": categories,
        },
    }


def campaign_from_reward_groups(campaign, now: Optional[datetime] = None):
    """Convert a sitewide reward campaign, as DropChannelCampaignsProgress or
    the dropsCampaign query sends it, into the Drop campaign shape, keeping
    only the reward groups watching can earn.

    The result holds every watchable group, whatever category it counts in,
    and names the first category as its game. Readers that watch for it take
    one category's view of it (in_reward_category), since a group only earns
    in its own categories. Each drop's "self" is filled from the group's own
    progress. Without any (the
    dropsCampaign query sends none) every group reads as not started, which
    is only good for finding its categories: "progress_known" says which.

    Returns None when the campaign is malformed or has no group watching a
    category could earn."""
    if not isinstance(campaign, dict):
        return None
    campaign_id = campaign.get("id")
    start_at = campaign.get("startAt")
    end_at = campaign.get("endAt")
    if not campaign_id or not _parse_datetime(start_at) or not _parse_datetime(end_at):
        return None
    now = now or utc_now()
    groups = campaign.get("rewardGroups")
    groups = [group for group in groups or [] if isinstance(group, dict)]
    progress_known = any(isinstance(group.get("self"), dict) for group in groups)
    base = {
        "id": str(campaign_id),
        "name": campaign.get("name") or str(campaign_id),
        "startAt": start_at,
        "endAt": end_at,
    }
    drops = [
        drop
        for drop in (
            _reward_group_drop(group, base, progress_known, now) for group in groups
        )
        if drop is not None
    ]
    # Every category watching can count in, including those of the groups
    # left out for now: a group Twitch reports no progress on can unlock
    # later, and its category is where that will show.
    categories = {}
    for group in groups:
        for category in _watch_group_categories(group):
            categories.setdefault(category["id"], category)
    if not categories:
        return None
    # The first category of a kept group names the game. A campaign whose
    # watch groups are all left out keeps its (empty) record: it says, on the
    # word of a channel listing, that nothing can be watched for right now.
    first = (
        drops[0][REWARD_GROUP_KEY]["categories"][0]
        if drops
        else next(iter(categories.values()))
    )
    categories = [first] + [
        category for category in categories.values() if category["id"] != first["id"]
    ]
    # A group watching could earn that Twitch reports no progress on, while
    # it does on others: one left out above until something unlocks it.
    locked = progress_known and any(
        not isinstance(group.get("self"), dict) and _watch_group_categories(group)
        for group in groups
    )
    return {
        **base,
        "status": "ACTIVE",
        "game": dict(categories[0]),
        # Any channel in the category counts.
        "allow": {"channels": None, "isEnabled": False},
        "summary": {"includesMWRequirement": True, "includesSubRequirement": False},
        "timeBasedDrops": drops,
        WATCH_REWARD_KEY: {
            "categories": categories,
            "progress_known": progress_known,
            "locked_groups": locked,
        },
    }


def in_reward_category(campaign, category) -> dict:
    """The converted campaign as channels of one of its categories earn it:
    that category as its game, and only the drops that count there."""
    return {
        **campaign,
        "game": dict(category),
        "timeBasedDrops": [
            drop
            for drop in campaign.get("timeBasedDrops") or []
            if any(
                counted["id"] == category["id"]
                for counted in drop[REWARD_GROUP_KEY]["categories"]
            )
        ],
    }


def reward_category_views(campaign, game_slugs=None, slugify=None) -> list:
    """in_reward_category for every category of the converted campaign, or
    only for those whose slugified name is in game_slugs when it is given."""
    return [
        in_reward_category(campaign, category)
        for category in watch_reward_categories(campaign)
        if game_slugs is None
        or (
            slugify is not None
            and slugify(category["displayName"] or category["name"]) in game_slugs
        )
    ]


def with_reward_category(campaign, game_id=None, game_slugs=None, slugify=None):
    """in_reward_category for the category matching game_id, or else the
    first whose slugified name is in game_slugs. None when none matches."""
    categories = watch_reward_categories(campaign)
    if game_id not in [None, ""]:
        for category in categories:
            if category["id"] == str(game_id):
                return in_reward_category(campaign, category)
    views = (
        reward_category_views(campaign, game_slugs, slugify)
        if game_slugs is not None
        else []
    )
    return views[0] if views else None


def with_claim_instance_ids(campaign, user_id):
    """Fill in the claim ID of every drop waiting to be claimed, in place."""
    if not is_watch_reward_campaign(campaign) or user_id in [None, ""]:
        return campaign
    for drop in campaign.get("timeBasedDrops") or []:
        group = drop.get(REWARD_GROUP_KEY) if isinstance(drop, dict) else None
        if isinstance(group, dict) and group.get("claimable") is True:
            drop["self"]["dropInstanceID"] = reward_drop_instance_id(
                user_id, campaign["id"], group["id"]
            )
    return campaign
