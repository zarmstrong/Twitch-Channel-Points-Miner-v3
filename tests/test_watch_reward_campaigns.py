"""Reward campaigns (quests) earned by watching (#348).

The payloads follow what Twitch sent for a real sitewide campaign: no game,
one repeating watch-time group, subscription groups, and a final group Twitch
reports no progress on.
"""

import copy
import functools
import importlib
import inspect
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from TwitchChannelPointsMiner.classes import Twitch as twitch_module
from TwitchChannelPointsMiner.classes.entities.Campaign import (
    Campaign,
    CampaignChannelScope,
    campaign_channel_scope,
    campaign_from_channel_progress,
)
from TwitchChannelPointsMiner.classes.entities.Drop import (
    Drop,
    drop_sub_requirement_unmet,
    utc_now,
)
from TwitchChannelPointsMiner.classes.entities.RewardCampaign import (
    campaign_from_reward_groups,
    is_watch_reward_campaign,
    is_watch_reward_listing,
    reward_category_views,
    with_claim_instance_ids,
    with_reward_category,
)
from TwitchChannelPointsMiner.classes.gql.Errors import RetryError
from TwitchChannelPointsMiner.classes.Settings import Events
from TwitchChannelPointsMiner.classes.Twitch import (
    DROP_FULL_INVENTORY_SYNC_SECONDS,
    WATCH_REWARD_FINISHED_REFRESH_SECONDS,
    WATCH_REWARD_PROGRESS_REFRESH_SECONDS,
    WATCH_REWARD_UNREAD_RETRIES,
    WATCH_REWARD_STALE_COPY_SECONDS,
    WATCH_REWARD_UNLISTED_SECONDS,
    Twitch,
)
from TwitchChannelPointsMiner.TwitchChannelPointsMiner import (
    MAIN_LOOP_REWARD_CHECK_SECONDS,
    TwitchChannelPointsMiner,
)

miner_module = importlib.import_module(
    "TwitchChannelPointsMiner.TwitchChannelPointsMiner"
)
# The claim thread's wait for a claim it is to send again, which the tests
# that run the thread inline replace (see claims_run_inline).
REAL_CLAIM_RETRY_WAIT = Twitch._Twitch__wait_for_reward_claim_retry

# The real clock: the Twitch-level tests convert through the channel listing,
# which reads it. Windows and waits below are hours away, so a test taking a
# few seconds changes nothing.
NOW = utc_now().replace(microsecond=0)
START_AT = "2020-01-01T00:00:00Z"
END_AT = "2099-01-01T00:00:00Z"
CATEGORY = {"id": "777", "displayName": "Example Game", "__typename": "Game"}
OTHER_CATEGORY = {"id": "888", "displayName": "Other Game", "__typename": "Game"}


class InlineThread:
    """Stands in for the thread a channel listing claims on: the claim
    runs when it is started, so a test sees its result at once."""

    started = []

    def __init__(self, target, **kwargs):
        self.target = target

    def start(self):
        InlineThread.started.append(self)
        self.target()

    def is_alive(self):
        return False


class HeldThread(InlineThread):
    """A claim thread that has started and not finished."""

    def start(self):
        InlineThread.started.append(self)

    def is_alive(self):
        return True


@pytest.fixture(autouse=True)
def claims_run_inline(monkeypatch):
    monkeypatch.setattr(InlineThread, "started", [])
    monkeypatch.setattr(twitch_module, "Thread", InlineThread)
    # Run inline, the claim thread cannot wait for the moment a refused
    # claim is due again: it ends there, as it does when the miner stops,
    # and leaves the moment noted (see claim_again_when_due).
    monkeypatch.setattr(
        twitch_module.Twitch,
        "_Twitch__wait_for_reward_claim_retry",
        lambda self, retry_at, wake: False,
    )


def claim_on_the_claim_thread(twitch):
    """Ask the claim thread for a pass over the waiting rewards, as a
    channel listing does for the campaigns it stores."""
    for campaign in list(twitch.watch_reward_campaigns.values()):
        twitch._Twitch__claim_watch_reward_drops_soon(campaign)


def claim_retries(twitch) -> dict:
    """When the claim thread sends each campaign's claims again."""
    return getattr(twitch, "watch_reward_claim_retries", None) or {}


def claim_again_when_due(twitch):
    """The claim thread once the moment has come for every claim it is to
    send again."""
    assert claim_retries(twitch)
    for campaign_id in twitch.watch_reward_claim_retries:
        twitch.watch_reward_claim_retries[campaign_id] = utc_now() - timedelta(
            seconds=1
        )
    twitch._Twitch__claim_stored_watch_reward_drops()


def stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def watch_group(group_self="unset", times=6, categories=None, **criteria):
    group = {
        "id": "group-watch",
        "name": "Daily Chest",
        "track": None,
        "lane": None,
        "progressCriteria": {
            "requirementType": "WATCH",
            "requirements": {"minutesWatched": 60, "subs": None},
            "isRepeatable": times > 1,
            "repeatableConfig": (
                {
                    "repeatableTimes": times,
                    "resetIntervalSeconds": 86400,
                    "shouldCompleteAllRepeatsToEarn": False,
                }
                if times > 1
                else None
            ),
            "channels": None,
            "categories": [CATEGORY] if categories is None else categories,
            **criteria,
        },
        "rewards": [
            {"id": "reward-chest", "name": "Chest", "distributionType": "POOL"}
        ],
    }
    if group_self != "unset":
        group["self"] = group_self
    return group


def progress(status="IN_PROGRESS", minutes=0, grants=0, window=None, next_at=None):
    return {
        "status": status,
        "currentMinutesWatched": minutes,
        "currentSubs": 0,
        "grantCount": grants,
        "nextEligibleAt": next_at,
        "currentWindow": window,
    }


def open_window():
    return {
        "startedAt": stamp(NOW - timedelta(hours=1)),
        "expiresAt": stamp(NOW + timedelta(hours=23)),
    }


def sub_group():
    return {
        "id": "group-sub",
        "name": "Sub Badge",
        "track": None,
        "lane": None,
        "progressCriteria": {
            "requirementType": "SUB",
            "requirements": {"minutesWatched": None, "subs": 1},
            "isRepeatable": False,
            "repeatableConfig": None,
            "channels": None,
            "categories": [CATEGORY],
        },
        "rewards": [{"id": "reward-sub", "name": "Sub Badge"}],
        "self": progress(status="CLAIMED"),
    }


def final_group():
    # Unlocked by earning everything else: Twitch sends no progress for it.
    group = watch_group(times=1)
    group["id"] = "group-final"
    group["name"] = "Ultimate Reward"
    group["self"] = None
    return group


def reward_campaign(*groups):
    return {
        "id": "quest-1",
        "name": "Launch Celebration",
        "startAt": START_AT,
        "endAt": END_AT,
        "game": None,
        "rewardGroups": list(groups),
    }


def convert(*groups):
    return campaign_from_reward_groups(reward_campaign(*groups), now=NOW)


def listing(**overrides):
    return {
        "id": "quest-1",
        "name": "Launch Celebration",
        "status": "UNKNOWN",
        "startsAt": "2020-01-01T00:00:00Z",
        "endsAt": "2099-01-01T00:00:00Z",
        "isSitewide": True,
        "game": None,
        "unlockRequirements": {"subsGoal": 0, "minuteWatchedGoal": 60},
        "_is_reward_campaign": True,
        **overrides,
    }


@pytest.mark.parametrize(
    "entry,expected",
    [
        (listing(), True),
        (listing(unlockRequirements={"subsGoal": 3, "minuteWatchedGoal": 60}), True),
        (listing(unlockRequirements={"subsGoal": 1, "minuteWatchedGoal": 0}), False),
        (listing(unlockRequirements=None), False),
        (listing(isSitewide=False), False),
        (None, False),
    ],
)
def test_listing_is_watch_earned_only_when_it_asks_for_watch_time(entry, expected):
    assert is_watch_reward_listing(entry) is expected


def test_campaign_takes_its_game_from_the_watch_groups_category():
    result = convert(
        watch_group(progress(minutes=12, window=open_window())),
        sub_group(),
        final_group(),
    )

    assert result["game"] == {
        "id": "777",
        "displayName": "Example Game",
        "name": "Example Game",
    }
    assert campaign_channel_scope(result) == (CampaignChannelScope.OPEN, [])
    # The subscription group and the group without progress are left out.
    [drop] = result["timeBasedDrops"]
    assert drop["id"] == "group-watch#0"
    assert drop["name"] == "Daily Chest (repeat 1 of 6)"
    assert drop["requiredMinutesWatched"] == 60
    # Due before today's window closes, not before the campaign does.
    assert drop["startAt"] == START_AT
    assert drop["endAt"] == open_window()["expiresAt"]
    assert drop["self"] == {
        "isClaimed": False,
        "hasPreconditionsMet": True,
        "currentMinutesWatched": 12,
        "currentSubs": 0,
        "dropInstanceID": None,
    }
    assert result["_watch_reward"]["progress_known"] is True
    # The entities read the converted shape as it is.
    campaign = Campaign(copy.deepcopy(result))
    assert [entity.minutes_required for entity in campaign.drops] == [60]
    Drop(drop).update(drop["self"])


def test_claimable_repeat_keeps_its_drop_id_and_gets_a_claim_id():
    result = convert(
        watch_group(progress(status="CLAIMABLE", minutes=60, window=open_window()))
    )

    [drop] = with_claim_instance_ids(result, "viewer-1")["timeBasedDrops"]
    assert drop["id"] == "group-watch#0"
    assert drop["self"]["currentMinutesWatched"] == 60
    assert drop["self"]["dropInstanceID"] == "viewer-1#quest-1#group-watch"


def test_only_claimable_drops_get_a_claim_id():
    result = convert(watch_group(progress(minutes=59, window=open_window())))

    [drop] = with_claim_instance_ids(result, "viewer-1")["timeBasedDrops"]
    assert drop["self"]["dropInstanceID"] is None


@pytest.mark.parametrize(
    "group_self,starts",
    [
        # Twitch names the next window.
        (
            progress(grants=1, next_at=stamp(NOW + timedelta(hours=5))),
            NOW + timedelta(hours=5),
        ),
        # This window's minutes are in; nothing counts until it ends.
        (
            progress(minutes=60, grants=1, window=open_window()),
            NOW + timedelta(hours=23),
        ),
    ],
)
def test_earned_repeat_waits_for_the_next_window(group_self, starts):
    [drop] = convert(watch_group(group_self))["timeBasedDrops"]

    assert drop["id"] == "group-watch#1"
    assert drop["startAt"] == stamp(starts)
    assert drop["self"]["currentMinutesWatched"] == 0
    assert drop["self"]["isClaimed"] is False
    assert Drop(drop).dt_match is False


def test_minutes_from_an_expired_window_do_not_count():
    expired = {
        "startedAt": stamp(NOW - timedelta(hours=30)),
        "expiresAt": stamp(NOW - timedelta(hours=6)),
    }

    [drop] = convert(watch_group(progress(minutes=40, window=expired)))[
        "timeBasedDrops"
    ]

    assert drop["startAt"] == START_AT
    assert drop["self"]["currentMinutesWatched"] == 0


@pytest.mark.parametrize(
    "group_self",
    [progress(grants=6), progress(status="CLAIMED", grants=2), progress("FULFILLED")],
)
def test_finished_group_reads_as_claimed(group_self):
    [drop] = convert(watch_group(group_self))["timeBasedDrops"]

    assert drop["self"]["isClaimed"] is True
    assert drop["_reward_group"]["finished"] is True


def test_expired_group_is_over_but_not_claimed():
    # Twitch gave up on the group before it was earned.
    [drop] = convert(watch_group(progress(status="EXPIRED", minutes=10)))[
        "timeBasedDrops"
    ]

    assert drop["self"]["isClaimed"] is False
    assert drop["self"]["currentMinutesWatched"] == 10
    assert drop["_reward_group"]["finished"] is True
    assert Drop(drop).dt_match is False


def test_progress_that_cannot_finish_before_its_window_ends_is_lost():
    closing = {
        "startedAt": stamp(NOW - timedelta(hours=23, minutes=55)),
        "expiresAt": stamp(NOW + timedelta(minutes=5)),
    }

    # 50 minutes to go, five left: the window takes the 10 minutes with it.
    [drop] = convert(watch_group(progress(minutes=10, window=closing)))[
        "timeBasedDrops"
    ]
    assert drop["startAt"] == closing["expiresAt"]
    assert drop["self"]["currentMinutesWatched"] == 0

    # Three minutes to go, five left: still worth finishing.
    [drop] = convert(watch_group(progress(minutes=57, window=closing)))[
        "timeBasedDrops"
    ]
    assert drop["startAt"] == START_AT
    assert drop["self"]["currentMinutesWatched"] == 57


def test_repeat_is_due_when_its_window_closes_or_the_campaign_ends():
    window = open_window()
    [drop] = convert(watch_group(progress(minutes=12, window=window)))["timeBasedDrops"]
    assert drop["endAt"] == window["expiresAt"]

    # No window yet: the campaign's end is the only deadline known.
    [drop] = convert(watch_group(progress(minutes=0)))["timeBasedDrops"]
    assert drop["endAt"] == END_AT

    # The campaign ends before the window would.
    ending = reward_campaign(watch_group(progress(minutes=12, window=window)))
    ending["endAt"] = stamp(NOW + timedelta(hours=2))
    [drop] = campaign_from_reward_groups(ending, now=NOW)["timeBasedDrops"]
    assert drop["endAt"] == ending["endAt"]

    # A reward waiting to be claimed is not lost with the window.
    [drop] = convert(
        watch_group(progress(status="CLAIMABLE", minutes=60, window=window))
    )["timeBasedDrops"]
    assert drop["endAt"] == END_AT


def test_single_grant_group_keeps_its_group_id():
    [drop] = convert(watch_group(progress(minutes=5), times=1))["timeBasedDrops"]

    assert drop["id"] == "group-watch"
    assert drop["name"] == "Daily Chest"


def test_groups_without_progress_read_as_not_started_when_none_has_any():
    # The dropsCampaign lookup sends no "self" at all.
    result = convert(watch_group(), sub_group() | {"self": None}, final_group())

    assert result["_watch_reward"]["progress_known"] is False
    assert [drop["id"] for drop in result["timeBasedDrops"]] == [
        "group-watch#0",
        "group-final",
    ]
    assert all(
        drop["self"]["currentMinutesWatched"] == 0 for drop in result["timeBasedDrops"]
    )


@pytest.mark.parametrize(
    "group",
    [
        watch_group(progress()) | {"track": "track-1", "lane": "lane-1"},
        watch_group(progress(), channels=[{"id": "1"}]),
        watch_group(progress(), categories=[]),
        watch_group(progress(), requirements={"minutesWatched": 0, "subs": None}),
        watch_group(progress(), requirementType="CHEER"),
    ],
)
def test_groups_watching_a_category_cannot_earn_are_left_out(group):
    assert convert(group) is None


def test_group_on_a_track_without_a_lane_is_kept():
    # Only a lane is a choice the account has to make.
    result = convert(watch_group(progress(minutes=5)) | {"track": "track-1"})

    assert [drop["id"] for drop in result["timeBasedDrops"]] == ["group-watch#0"]


def test_campaign_whose_watch_groups_are_all_left_out_keeps_an_empty_record():
    # A status the miner does not know, and a group Twitch reports no
    # progress on beside a subscription group it does: nothing to watch for,
    # but still a reward campaign, with the categories to look in later.
    for groups in (
        [watch_group(progress(status="SOMETHING_NEW"))],
        [final_group(), sub_group()],
    ):
        result = convert(*groups)

        assert result["timeBasedDrops"] == []
        assert result["game"]["id"] == "777"
        assert result["_watch_reward"]["progress_known"] is True
        assert [c["id"] for c in result["_watch_reward"]["categories"]] == ["777"]
        assert Campaign(result).drops == []


def test_malformed_campaign_is_not_converted():
    campaign = reward_campaign(watch_group(progress()))
    del campaign["endAt"]

    assert campaign_from_reward_groups(campaign, now=NOW) is None
    assert campaign_from_reward_groups(None, now=NOW) is None


def test_channel_listing_converts_a_campaign_without_a_game():
    result = campaign_from_channel_progress(
        reward_campaign(watch_group(progress(minutes=3)), sub_group())
    )

    assert is_watch_reward_campaign(result)
    assert result["game"]["displayName"] == "Example Game"
    assert [drop["id"] for drop in result["timeBasedDrops"]] == ["group-watch#0"]


def slugify(name):
    return name.lower().replace(" ", "-")


def test_category_is_chosen_by_game_id_then_by_slug():
    result = convert(
        watch_group(progress(), categories=[CATEGORY, OTHER_CATEGORY]),
    )

    assert with_reward_category(result, game_id="777")["game"]["id"] == "777"
    assert with_reward_category(result, game_id="888")["game"]["id"] == "888"
    assert (
        with_reward_category(result, game_slugs={"other-game"}, slugify=slugify)[
            "game"
        ]["displayName"]
        == "Other Game"
    )
    assert with_reward_category(result, game_id="999") is None
    assert [
        view["game"]["id"]
        for view in reward_category_views(result, {"other-game"}, slugify)
    ] == ["888"]
    assert [view["game"]["id"] for view in reward_category_views(result)] == [
        "777",
        "888",
    ]


def two_category_groups(first_self, second_self):
    """One group that counts only in CATEGORY and one only in OTHER_CATEGORY."""
    second = watch_group(second_self, categories=[OTHER_CATEGORY])
    second["id"] = "group-other"
    return watch_group(first_self), second


def test_category_view_holds_only_the_groups_that_count_there():
    result = convert(
        *two_category_groups(progress(status="CLAIMED", grants=6), progress(minutes=5))
    )

    assert [drop["id"] for drop in result["timeBasedDrops"]] == [
        "group-watch#5",
        "group-other#0",
    ]
    first = with_reward_category(result, game_id="777")
    second = with_reward_category(result, game_id="888")
    assert [drop["id"] for drop in first["timeBasedDrops"]] == ["group-watch#5"]
    assert [drop["id"] for drop in second["timeBasedDrops"]] == ["group-other#0"]
    # The stored record itself is left whole.
    assert len(result["timeBasedDrops"]) == 2


def bare_twitch(gql=None):
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
    twitch.awarded_game_event_drops = {}
    twitch.twitch_login = SimpleNamespace(
        get_user_id=lambda: "viewer-1", username="viewer"
    )
    twitch.gql = gql or SimpleNamespace()
    return twitch


def channel_listing(*groups, silent_channels=()):
    """A gql stub whose channel listing holds the campaign with these groups,
    except on silent_channels, which list nothing. Claims are recorded on
    gql.claims."""
    calls = []

    def progress_query(channel_id):
        calls.append(channel_id)
        converted = (
            None
            if channel_id in silent_channels
            else campaign_from_channel_progress(reward_campaign(*groups))
        )
        return SimpleNamespace(
            campaigns=[] if converted is None else [converted],
            campaigns_available=True,
        )

    gql = SimpleNamespace(get_channel_drop_campaigns_progress=progress_query)
    gql.claims = []
    gql.claim_drop_rewards = lambda instance_id: gql.claims.append(
        instance_id
    ) or SimpleNamespace(status="ELIGIBLE_FOR_ALL", errors=[])
    return gql, calls


def test_channel_listing_stores_the_campaign_and_reopens_it(monkeypatch):
    gql, _ = channel_listing(
        watch_group(progress(status="CLAIMABLE", minutes=60, window=open_window()))
    )
    twitch = bare_twitch(gql)
    monkeypatch.setattr(
        Twitch, "_Twitch__drop_variant_entries_from_drop", lambda self, drop: []
    )
    # The inventory reported it completed, and an earlier evaluation filed it
    # as purchase-gated.
    twitch.completed_drop_campaigns = {"quest-1", "other"}
    twitch.reward_campaign_ids = {"quest-1"}

    campaigns, available, answered = twitch._Twitch__get_campaigns_from_channel_id("1")

    assert (available, answered) == (True, True)
    [campaign] = campaigns
    assert campaign is twitch.watch_reward_campaigns["quest-1"]
    assert campaign["_is_reward_campaign"] is False
    assert twitch.completed_drop_campaigns == {"other"}
    assert twitch.reward_campaign_ids == set()
    # Seen waiting to be claimed, so claimed without waiting for a sync.
    assert gql.claims == ["viewer-1#quest-1#group-watch"]

    # The next listing still reports it claimable: not sent twice.
    twitch._Twitch__get_campaigns_from_channel_id("1")
    assert gql.claims == ["viewer-1#quest-1#group-watch"]


def test_failed_claim_does_not_lose_the_channel_listing():
    gql, _ = channel_listing(watch_group(progress(status="CLAIMABLE", minutes=60)))
    del gql.claim_drop_rewards
    twitch = bare_twitch(gql)

    campaigns, available, answered = twitch._Twitch__get_campaigns_from_channel_id("1")

    assert (available, answered) == (True, True)
    assert [campaign["id"] for campaign in campaigns] == ["quest-1"]


def test_lookup_without_progress_does_not_replace_known_progress():
    twitch = bare_twitch()
    known = twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(minutes=30, window=open_window())))
    )

    kept = twitch._Twitch__remember_watch_reward_campaign(convert(watch_group()))

    assert kept is known
    assert known["timeBasedDrops"][0]["self"]["currentMinutesWatched"] == 30


def test_lookup_racing_a_channel_listing_does_not_overwrite_its_progress(monkeypatch):
    # The reward-group lookup (discovery thread) is preparing its record
    # when a channel listing (another thread) stores one with progress.
    twitch = bare_twitch()
    with_progress = convert(watch_group(progress(minutes=30, window=open_window())))
    real = twitch_module.with_claim_instance_ids
    interleaved = []

    def prepare(campaign, user_id):
        prepared = real(campaign, user_id)
        if not interleaved:
            interleaved.append(True)
            twitch._Twitch__remember_watch_reward_campaign(with_progress)
        return prepared

    monkeypatch.setattr(twitch_module, "with_claim_instance_ids", prepare)

    twitch._Twitch__remember_watch_reward_campaign(convert(watch_group()))

    [drop] = twitch.watch_reward_campaigns["quest-1"]["timeBasedDrops"]
    assert drop["self"]["currentMinutesWatched"] == 30


def test_inventory_completed_flag_is_ignored_for_a_stored_campaign():
    twitch = bare_twitch()
    inventory = {
        "completedRewardCampaigns": [
            {"id": "quest-1", "status": "COMPLETED"},
            {"id": "quest-2", "status": "COMPLETED"},
        ]
    }
    assert twitch._Twitch__completed_campaign_ids_from_inventory(inventory) == {
        "quest-1",
        "quest-2",
    }

    twitch._Twitch__remember_watch_reward_campaign(convert(watch_group(progress())))

    assert twitch._Twitch__completed_campaign_ids_from_inventory(inventory) == {
        "quest-2"
    }


def test_campaign_details_answer_a_stored_campaign_without_asking_twitch():
    requested = []

    def batch(operation_name, payload):
        requested.append([item["variables"]["dropID"] for item in payload])
        return [{"data": {"user": {"dropCampaign": {"id": "campaign-1", "game": {}}}}}]

    twitch = bare_twitch(SimpleNamespace(post_gql_request_batch_raw=batch))
    twitch.campaign_details_withheld_streak = 0
    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(), categories=[CATEGORY, OTHER_CATEGORY]))
    )
    filed = with_reward_category(
        twitch.watch_reward_campaigns["quest-1"], game_id="888"
    )

    details = twitch._Twitch__get_campaigns_details(
        [{"id": "campaign-1"}, filed, {"id": "quest-1"}]
    )

    assert requested == [["campaign-1"]]
    assert [campaign["id"] for campaign in details] == [
        "campaign-1",
        "quest-1",
        "quest-1",
    ]
    # The category the caller filed it under is kept; a bare ID gets the first.
    assert [campaign["game"].get("id") for campaign in details[1:]] == ["888", "777"]
    assert details[1] is not twitch.watch_reward_campaigns["quest-1"]


def evaluate(
    monkeypatch,
    twitch,
    dashboard,
    inventory=None,
    requested=("example-game",),
    channels=("4242",),
    directory_calls=None,
    raw_query_patched=False,
):
    """Run the account-wide evaluation. Every category's directory lists
    `channels`; each directory request is appended to directory_calls. The
    hand-written listing query returns nothing unless the test patched it."""
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_drops_dashboard",
        lambda self, status="OPEN": copy.deepcopy(dashboard),
    )
    if not raw_query_patched:
        monkeypatch.setattr(
            Twitch, "_Twitch__get_reward_campaigns_raw_query", lambda self: ([], [])
        )
    monkeypatch.setattr(
        Twitch, "_Twitch__awarded_benefits", lambda self, inventory: (set(), set())
    )
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_game_directory_slug",
        lambda self, game_id: f"slug-{game_id}",
    )
    monkeypatch.setattr(
        Twitch, "_Twitch__drop_variant_entries_from_drop", lambda self, drop: []
    )

    def directory(self, request_slug, game_slug, cursor=None, drops_tag=True):
        if directory_calls is not None:
            directory_calls.append((request_slug, drops_tag))
        return {
            "data": {
                "game": {
                    "streams": {
                        "edges": [
                            {"node": {"broadcaster": {"id": channel_id}}}
                            for channel_id in channels
                        ]
                    }
                }
            }
        }

    monkeypatch.setattr(Twitch, "_Twitch__request_drops_directory", directory)
    return twitch._Twitch__active_drop_category_slugs_from_campaigns(
        inventory or {}, None if requested is None else set(requested)
    )


def evaluation_twitch(*groups, structure="default", silent_channels=()):
    """Twitch whose reward-group lookup returns the structure and whose
    channel listing returns the campaign with these groups."""
    gql, listing_calls = channel_listing(*groups, silent_channels=silent_channels)
    lookups = []
    if structure == "default":
        structure = reward_campaign(watch_group(), sub_group() | {"self": None})

    def post_gql_request_raw(operation_name, payload):
        lookups.append((operation_name, payload["variables"]))
        return {"data": {"dropsCampaign": copy.deepcopy(structure)}}

    gql.post_gql_request_raw = post_gql_request_raw
    gql.post_gql_request_batch_raw = lambda operation_name, payload: pytest.fail(
        "DropCampaignDetails must not be asked for a stored reward campaign"
    )
    return bare_twitch(gql), lookups, listing_calls


def test_evaluation_marks_the_category_of_a_watch_earned_reward_campaign(monkeypatch):
    twitch, lookups, listing_calls = evaluation_twitch(
        watch_group(progress(minutes=12, window=open_window())), sub_group()
    )
    # Twitch lists the campaign as completed as soon as a sub reward is claimed.
    inventory = {"completedRewardCampaigns": [{"id": "quest-1", "status": "COMPLETED"}]}

    deadlines, twitch_games = evaluate(monkeypatch, twitch, [listing()], inventory)

    assert lookups == [("RewardCampaignDetails", {"id": "quest-1"})]
    # Progress comes from one live channel of the category.
    assert listing_calls == ["4242"]
    assert set(deadlines) == {"example-game"}
    assert twitch_games == {"example-game"}
    assert twitch.active_drop_campaigns["example-game"] == [
        {
            "id": "quest-1",
            "name": "Launch Celebration",
            "all_channels": True,
            "channels": [],
        }
    ]
    assert "quest-1" not in twitch.reward_campaign_ids
    assert "quest-1" not in twitch.completed_drop_campaigns

    # A second evaluation reuses the stored record and its recent progress.
    evaluate(monkeypatch, twitch, [listing()], inventory)
    assert len(lookups) == 1 and listing_calls == ["4242"]


def test_evaluation_reads_progress_again_once_it_is_old(monkeypatch):
    twitch, _, listing_calls = evaluation_twitch(watch_group(progress(minutes=12)))
    evaluate(monkeypatch, twitch, [listing()])
    twitch.watch_reward_campaigns["quest-1"]["_watch_reward"]["read_at"] -= 3600

    evaluate(monkeypatch, twitch, [listing()])

    assert listing_calls == ["4242", "4242"]


def test_evaluation_reads_a_finished_campaign_again_but_less_often(monkeypatch):
    # A group Twitch reported no progress on can unlock once the others are
    # earned, so a finished campaign is not final.
    twitch, _, listing_calls = evaluation_twitch(
        watch_group(progress(status="CLAIMED", grants=6))
    )
    evaluate(monkeypatch, twitch, [listing()])
    details = twitch.watch_reward_campaigns["quest-1"]["_watch_reward"]

    details["read_at"] -= WATCH_REWARD_FINISHED_REFRESH_SECONDS - 60
    evaluate(monkeypatch, twitch, [listing()])
    assert listing_calls == ["4242"]

    details["read_at"] -= 120
    evaluate(monkeypatch, twitch, [listing()])
    assert listing_calls == ["4242", "4242"]


def test_evaluation_finds_a_group_that_unlocks_after_the_others_finish(monkeypatch):
    groups = [watch_group(progress(status="CLAIMED", grants=6)), final_group()]
    gql, _ = channel_listing()
    gql.get_channel_drop_campaigns_progress = lambda channel_id: SimpleNamespace(
        campaigns=[campaign_from_channel_progress(reward_campaign(*groups))],
        campaigns_available=True,
    )
    gql.post_gql_request_raw = lambda operation_name, payload: {
        "data": {"dropsCampaign": reward_campaign(watch_group(), final_group())}
    }
    twitch = bare_twitch(gql)
    assert evaluate(monkeypatch, twitch, [listing()])[0] == {}

    groups[1] = final_group() | {"self": progress(minutes=0)}
    twitch.watch_reward_campaigns["quest-1"]["_watch_reward"]["read_at"] -= (
        WATCH_REWARD_FINISHED_REFRESH_SECONDS + 1
    )

    deadlines, _ = evaluate(monkeypatch, twitch, [listing()])
    assert set(deadlines) == {"example-game"}


def test_evaluation_marks_every_configured_category_of_the_campaign(monkeypatch):
    twitch, _, _ = evaluation_twitch(
        watch_group(progress(minutes=5), categories=[CATEGORY, OTHER_CATEGORY]),
        structure=reward_campaign(watch_group(categories=[CATEGORY, OTHER_CATEGORY])),
    )

    deadlines, twitch_games = evaluate(
        monkeypatch, twitch, [listing()], requested=("example-game", "other-game")
    )

    assert set(deadlines) == {"example-game", "other-game"}
    assert twitch_games == {"example-game", "other-game"}
    assert [
        [campaign["id"] for campaign in twitch.active_drop_campaigns[slug]]
        for slug in ("example-game", "other-game")
    ] == [["quest-1"], ["quest-1"]]


def test_evaluation_leaves_out_a_category_whose_own_group_is_finished(monkeypatch):
    groups = two_category_groups(
        progress(status="CLAIMED", grants=6), progress(minutes=5)
    )
    twitch, _, _ = evaluation_twitch(
        *groups, structure=reward_campaign(*two_category_groups("unset", "unset"))
    )

    deadlines, _ = evaluate(
        monkeypatch, twitch, [listing()], requested=("example-game", "other-game")
    )

    assert set(deadlines) == {"other-game"}
    assert set(twitch.active_drop_campaigns) == {"other-game"}


@pytest.mark.parametrize(
    "group_self",
    [
        # Today's repeat is earned; the next window opens tomorrow.
        progress(grants=1, next_at="2099-01-01T00:00:00Z"),
        # Waiting to be claimed: nothing left to watch for.
        progress(status="CLAIMABLE", minutes=60),
        # Every repeat is in.
        progress(status="CLAIMED", grants=6),
    ],
)
def test_evaluation_leaves_the_category_out_while_nothing_can_be_watched(
    monkeypatch, group_self
):
    twitch, _, _ = evaluation_twitch(watch_group(group_self))

    deadlines, _ = evaluate(monkeypatch, twitch, [listing()])

    assert deadlines == {}
    assert twitch.active_drop_campaigns == {}


def test_evaluation_still_skips_a_subscription_only_reward_campaign(monkeypatch):
    twitch, lookups, listing_calls = evaluation_twitch(watch_group(progress()))
    sub_only = listing(unlockRequirements={"subsGoal": 1, "minuteWatchedGoal": 0})

    deadlines, _ = evaluate(monkeypatch, twitch, [sub_only])

    assert deadlines == {}
    assert lookups == [] and listing_calls == []
    assert twitch.reward_campaign_ids == {"quest-1"}
    assert twitch._Twitch__watch_reward_campaigns() == {}


def test_evaluation_skips_a_campaign_whose_groups_watching_cannot_earn(monkeypatch):
    twitch, lookups, _ = evaluation_twitch(
        structure=reward_campaign(sub_group() | {"self": None})
    )

    deadlines, _ = evaluate(monkeypatch, twitch, [listing()])
    evaluate(monkeypatch, twitch, [listing()])

    assert deadlines == {}
    # The failed lookup is not repeated on the next refresh.
    assert len(lookups) == 1
    assert twitch.reward_campaign_ids == {"quest-1"}


def test_evaluation_does_not_probe_a_category_that_is_not_configured(monkeypatch):
    twitch, lookups, listing_calls = evaluation_twitch(watch_group(progress()))

    deadlines, _ = evaluate(
        monkeypatch, twitch, [listing()], requested=("something-else",)
    )

    assert deadlines == {}
    assert len(lookups) == 1 and listing_calls == []


def category_streamer():
    return SimpleNamespace(
        username="quest-channel",
        channel_id="4242",
        from_category=True,
        from_badge_campaign=False,
        stream=SimpleNamespace(
            game_name=lambda: "Other Game", game={"id": "888", "name": "Other Game"}
        ),
    )


def test_channel_check_counts_the_campaign_under_the_streamed_category(monkeypatch):
    gql, _ = channel_listing(
        watch_group(
            progress(minutes=12, window=open_window()),
            categories=[CATEGORY, OTHER_CATEGORY],
        ),
        sub_group(),
    )
    twitch = bare_twitch(gql)
    twitch.campaign_details_withheld_streak = 0
    # A stale account-wide verdict must not exclude it by ID.
    twitch.reward_campaign_ids = {"quest-1"}
    twitch.completed_drop_campaigns = {"quest-1"}

    assert twitch._Twitch__get_campaign_ids_from_streamer(category_streamer()) == [
        "quest-1"
    ]
    assert twitch.category_campaign_eligibility[("other-game", "quest-channel")] == (
        1,
        1,
    )
    assert twitch.advertised_drop_campaigns["quest-1"]["game"]["id"] == "888"


def test_channel_check_rejects_the_campaign_while_it_waits_for_the_next_window(
    monkeypatch,
):
    gql, _ = channel_listing(
        watch_group(
            progress(grants=1, next_at="2099-01-01T00:00:00Z"),
            categories=[OTHER_CATEGORY],
        )
    )
    twitch = bare_twitch(gql)
    twitch.campaign_details_withheld_streak = 0

    assert twitch._Twitch__get_campaign_ids_from_streamer(category_streamer()) == []


@pytest.mark.parametrize("in_progress", [[], None])
def test_sync_claims_a_claimable_reward_group_the_inventory_never_lists(
    monkeypatch, in_progress
):
    # Twitch can also send no in-progress list at all (null).
    claimed = []
    twitch = bare_twitch(
        SimpleNamespace(
            claim_drop_rewards=lambda instance_id: claimed.append(instance_id)
            or SimpleNamespace(status="ELIGIBLE_FOR_ALL", errors=[])
        )
    )
    monkeypatch.setattr(
        Twitch, "_Twitch__drop_variant_entries_from_drop", lambda self, drop: []
    )
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_inventory",
        lambda self: {"dropCampaignsInProgress": in_progress},
    )
    monkeypatch.setattr(
        Twitch, "_Twitch__save_drop_snapshot_from_dict", lambda self, *a, **k: None
    )
    stored = twitch._Twitch__remember_watch_reward_campaign(
        campaign_from_reward_groups(
            {
                **reward_campaign(
                    watch_group(progress(status="CLAIMABLE", minutes=60))
                ),
                "startAt": "2020-01-01T00:00:00Z",
                "endAt": "2099-01-01T00:00:00Z",
            }
        )
    )

    twitch._Twitch__sync_campaigns([Campaign(copy.deepcopy(stored))])

    assert claimed == ["viewer-1#quest-1#group-watch"]
    assert "group-watch#0" in twitch.accepted_drop_ids


def test_next_repeat_claimable_within_the_hour_is_sent_to_twitch(monkeypatch):
    # Both repeats share the claim ID; only the drop tells them apart.
    sent = []
    twitch = bare_twitch(
        SimpleNamespace(
            claim_drop_rewards=lambda instance_id: sent.append(instance_id)
            or SimpleNamespace(status="ELIGIBLE_FOR_ALL", errors=[])
        )
    )
    monkeypatch.setattr(
        Twitch, "_Twitch__drop_variant_entries_from_drop", lambda self, drop: []
    )
    instance_id = "viewer-1#quest-1#group-watch"

    def claimable(grants):
        [drop_data] = convert(
            watch_group(progress(status="CLAIMABLE", minutes=60, grants=grants))
        )["timeBasedDrops"]
        drop = Drop(drop_data)
        drop.update({**drop_data["self"], "dropInstanceID": instance_id})
        return drop

    assert twitch.claim_drop(claimable(1)) is True
    # The same repeat again, before the listing catches up: not re-sent.
    assert twitch.claim_drop(claimable(1)) is True
    assert sent == [instance_id]

    assert twitch.claim_drop(claimable(2)) is True
    assert sent == [instance_id, instance_id]
    assert twitch.accepted_drop_ids >= {"group-watch#0", "group-watch#1"}


def test_accepted_claim_does_not_hide_the_next_repeat():
    twitch = bare_twitch()
    twitch.accepted_drop_ids = frozenset({"group-watch#0", "other-drop"})

    def listed(group_self):
        [drop] = twitch._Twitch__remember_watch_reward_campaign(
            convert(watch_group(group_self))
        )["timeBasedDrops"]
        return drop["id"]

    assert listed(progress(status="CLAIMABLE", minutes=60, grants=1)) == (
        "group-watch#0"
    )
    # The next repeat is a drop of its own, so the accepted claim stays.
    assert listed(progress(minutes=0, grants=1)) == "group-watch#1"
    assert twitch.accepted_drop_ids == {"group-watch#0", "other-drop"}


def test_evaluation_claims_a_reward_waiting_to_be_claimed(monkeypatch):
    # Nothing is left to watch, so no streamer is loaded and no campaign sync
    # may be running to claim it.
    twitch, _, _ = evaluation_twitch(
        watch_group(progress(status="CLAIMABLE", minutes=60))
    )

    deadlines, _ = evaluate(monkeypatch, twitch, [listing()])

    assert deadlines == {}
    assert twitch.gql.claims == ["viewer-1#quest-1#group-watch"]


def test_evaluation_retries_a_claim_that_failed_when_it_was_listed(monkeypatch):
    twitch, _, _ = evaluation_twitch(
        watch_group(progress(status="CLAIMABLE", minutes=60))
    )
    working_claim = twitch.gql.claim_drop_rewards
    twitch.gql.claim_drop_rewards = lambda instance_id: SimpleNamespace(
        status=None, errors=["service unavailable"]
    )
    evaluate(monkeypatch, twitch, [listing()])
    assert twitch.gql.claims == []

    # The record is still recent, so no listing is read; the claim is retried.
    twitch.gql.claim_drop_rewards = working_claim
    evaluate(monkeypatch, twitch, [listing()])

    assert twitch.gql.claims == ["viewer-1#quest-1#group-watch"]


def test_completed_record_with_a_game_does_not_replace_the_listing(monkeypatch):
    # Some inventory responses carry the whole completed campaign, filed
    # under "Special Events".
    twitch, lookups, _ = evaluation_twitch(watch_group(progress(minutes=12)))
    inventory = {
        "completedRewardCampaigns": [
            {
                "id": "quest-1",
                "name": "Launch Celebration",
                "status": "COMPLETED",
                "game": {"id": "509663", "displayName": "Special Events"},
            }
        ]
    }

    for _ in range(2):
        deadlines, _ = evaluate(monkeypatch, twitch, [listing()], inventory)
        assert set(deadlines) == {"example-game"}
    assert len(lookups) == 1
    assert "quest-1" not in twitch.completed_drop_campaigns


def test_progress_is_read_from_any_live_channel_not_only_drops_tagged_ones(
    monkeypatch,
):
    twitch, _, _ = evaluation_twitch(watch_group(progress(minutes=12)))
    directory_calls = []

    evaluate(monkeypatch, twitch, [listing()], directory_calls=directory_calls)

    assert directory_calls == [("slug-777", False)]


def test_progress_read_moves_on_when_a_channel_does_not_list_the_campaign(
    monkeypatch,
):
    twitch, _, listing_calls = evaluation_twitch(
        watch_group(progress(grants=1, next_at="2099-01-01T00:00:00Z")),
        silent_channels={"1"},
    )
    evaluate(monkeypatch, twitch, [listing()], channels=("1", "2", "3"))
    # First read: the lookup's record has no progress, "1" lists nothing.
    assert listing_calls == ["1", "2"]

    # Later the same stale leader must not keep the old progress in place.
    twitch.watch_reward_campaigns["quest-1"]["_watch_reward"]["read_at"] -= 3600
    evaluate(monkeypatch, twitch, [listing()], channels=("1", "2", "3"))
    assert listing_calls == ["1", "2", "1", "2"]


def test_progress_read_gives_up_after_a_few_channels(monkeypatch):
    twitch, _, listing_calls = evaluation_twitch(
        watch_group(progress(), categories=[CATEGORY, OTHER_CATEGORY]),
        structure=reward_campaign(watch_group(categories=[CATEGORY, OTHER_CATEGORY])),
        silent_channels={"1", "2", "3", "4", "5"},
    )
    directory_calls = []

    evaluate(
        monkeypatch,
        twitch,
        [listing()],
        requested=("other-game",),
        channels=("1", "2", "3", "4", "5"),
        directory_calls=directory_calls,
    )

    # The configured category first, two channels in each, four in all.
    assert directory_calls == [("slug-888", False), ("slug-777", False)]
    assert listing_calls == ["1", "2", "3", "4"]


def test_progress_read_asks_the_channels_after_those_that_did_not_answer(
    monkeypatch,
):
    # One category whose directory keeps its order: the first two channels
    # never list the campaign, the third does.
    twitch, _, listing_calls = evaluation_twitch(
        watch_group(progress(minutes=12)), silent_channels={"1", "2"}
    )

    def read():
        del listing_calls[:]
        evaluate(monkeypatch, twitch, [listing()], channels=("1", "2", "3"))
        return list(listing_calls)

    assert read() == ["1", "2"]
    details = twitch.watch_reward_campaigns["quest-1"]["_watch_reward"]
    assert details.get("progress_known") is not True

    details["refresh_attempted_at"] -= WATCH_REWARD_PROGRESS_REFRESH_SECONDS + 1
    assert read() == ["3"]
    assert (
        twitch.watch_reward_campaigns["quest-1"]["_watch_reward"]["progress_known"]
        is True
    )


def test_progress_read_starts_over_once_every_channel_was_asked(monkeypatch):
    twitch, _, listing_calls = evaluation_twitch(
        watch_group(progress(minutes=12)), silent_channels={"1", "2", "3"}
    )

    def read():
        del listing_calls[:]
        evaluate(monkeypatch, twitch, [listing()], channels=("1", "2", "3"))
        details = twitch.watch_reward_campaigns["quest-1"]["_watch_reward"]
        details["refresh_attempted_at"] -= WATCH_REWARD_PROGRESS_REFRESH_SECONDS + 1
        return list(listing_calls)

    assert [read(), read(), read()] == [["1", "2"], ["3"], ["1", "2"]]


def locked_group_twitch(monkeypatch, *groups, moment=None):
    """A Twitch at `moment` (NOW when not given) that stored a listing of the
    groups, and the time it was stored at."""
    moment = moment or NOW
    stored_at = moment.replace(tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(twitch_module, "utc_now", lambda: moment)
    monkeypatch.setattr(twitch_module.time, "time", lambda: stored_at)
    twitch = bare_twitch()
    twitch._Twitch__remember_watch_reward_campaign(convert(*groups))
    return twitch, stored_at


def test_group_that_can_unlock_is_read_for_after_the_last_claim_is_accepted(
    monkeypatch,
):
    # The record goes on saying "claimable" until a listing replaces it, and
    # with nothing left to watch for none comes before the ordinary refresh.
    monkeypatch.setattr(
        Twitch, "_Twitch__drop_variant_entries_from_drop", lambda self, drop: []
    )
    gql, _ = channel_listing()
    twitch = bare_twitch(gql)

    twitch._Twitch__store_listed_watch_rewards(
        [
            convert(
                watch_group(progress(status="CLAIMABLE", minutes=60), times=1),
                final_group(),
            )
        ]
    )

    assert gql.claims == ["viewer-1#quest-1#group-watch"]
    [read_due] = twitch.watch_reward_pending_starts
    assert (read_due - utc_now()).total_seconds() == pytest.approx(
        WATCH_REWARD_FINISHED_REFRESH_SECONDS, abs=5
    )


def test_no_read_is_set_for_a_locked_group_while_a_repeat_is_still_to_come(
    monkeypatch,
):
    # The claimed repeat is not its group's last: its next window brings
    # the miner back.
    monkeypatch.setattr(
        Twitch, "_Twitch__drop_variant_entries_from_drop", lambda self, drop: []
    )
    gql, _ = channel_listing()
    twitch = bare_twitch(gql)
    window = open_window()

    twitch._Twitch__store_listed_watch_rewards(
        [
            convert(
                watch_group(
                    progress(status="CLAIMABLE", minutes=60, grants=1, window=window)
                ),
                final_group(),
            )
        ]
    )

    assert gql.claims == ["viewer-1#quest-1#group-watch"]
    assert twitch.watch_reward_pending_starts == {NOW + timedelta(hours=23)}


def test_group_that_unlocks_after_the_others_finish_is_read_for_within_the_hour(
    monkeypatch,
):
    # Every other group is finished, so no window is waited for and nothing
    # is watched in the category: only the ordinary refresh, hours off, read
    # a channel listing again, which can be after the group's deadline.
    twitch, stored_at = locked_group_twitch(
        monkeypatch, watch_group(progress(status="CLAIMED", grants=6)), final_group()
    )

    read_due = NOW + timedelta(seconds=WATCH_REWARD_FINISHED_REFRESH_SECONDS)
    assert twitch.watch_reward_pending_starts == {read_due}
    assert twitch.take_opened_watch_reward_start() is False

    monkeypatch.setattr(twitch_module, "utc_now", lambda: read_due)
    assert twitch.take_opened_watch_reward_start() is True
    # The read made then is due: the record is as old as a finished
    # campaign's may get.
    monkeypatch.setattr(
        twitch_module.time,
        "time",
        lambda: stored_at + WATCH_REWARD_FINISHED_REFRESH_SECONDS,
    )
    asked = []
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_game_directory_slug",
        lambda self, game_id: asked.append(game_id),
    )
    twitch._Twitch__refresh_watch_reward_progress(
        twitch.watch_reward_campaigns["quest-1"]
    )
    assert asked == ["777"]
    # No channel answered: asked again an hour on, not left to the ordinary
    # refresh.
    assert twitch.watch_reward_pending_starts == {
        read_due + timedelta(seconds=WATCH_REWARD_FINISHED_REFRESH_SECONDS)
    }


@pytest.mark.parametrize(
    "groups",
    [
        # Nothing left out: Twitch reports on every group.
        [watch_group(progress(status="CLAIMED", grants=6))],
        # A repeat still to watch: its listings are read while it is.
        [
            watch_group(progress(minutes=10, window=open_window())),
            final_group(),
        ],
    ],
)
def test_campaign_without_a_locked_group_to_wait_for_is_not_read_for(
    monkeypatch, groups
):
    twitch, _ = locked_group_twitch(monkeypatch, *groups)

    assert not [
        moment
        for moment in twitch.watch_reward_pending_starts
        if moment == NOW + timedelta(seconds=WATCH_REWARD_FINISHED_REFRESH_SECONDS)
    ]


def test_locked_group_is_not_read_for_after_the_campaign_ends(monkeypatch):
    ends_at = datetime.strptime(END_AT, "%Y-%m-%dT%H:%M:%SZ")
    twitch, _ = locked_group_twitch(
        monkeypatch,
        watch_group(progress(status="CLAIMED", grants=6)),
        final_group(),
        moment=ends_at - timedelta(minutes=5),
    )

    assert not twitch.watch_reward_pending_starts


def test_locked_group_is_read_for_sooner_when_the_campaign_ends_within_the_hour(
    monkeypatch,
):
    # The last of the other groups is earned 45 minutes before the end, and
    # the group it unlocks takes 15: the read an hour on would be too late.
    ends_at = datetime.strptime(END_AT, "%Y-%m-%dT%H:%M:%SZ")
    stored_at = ends_at - timedelta(minutes=45)
    groups = (watch_group(progress(status="CLAIMED", grants=6)), final_group())
    twitch, _, listing_calls = evaluation_twitch(*groups)
    clock = [stored_at]
    monkeypatch.setattr(twitch_module, "utc_now", lambda: clock[0])
    monkeypatch.setattr(
        twitch_module.time,
        "time",
        lambda: clock[0].replace(tzinfo=timezone.utc).timestamp(),
    )
    twitch._Twitch__remember_watch_reward_campaign(convert(*groups))

    read_due = stored_at + timedelta(seconds=WATCH_REWARD_PROGRESS_REFRESH_SECONDS)
    assert twitch.watch_reward_pending_starts == {read_due}

    # And the read is made then, not held back for the hour.
    clock[0] = read_due + timedelta(seconds=1)
    evaluate(monkeypatch, twitch, [listing()])
    assert listing_calls == ["4242"]


def test_locked_groups_category_is_kept_so_its_unlock_is_noticed(monkeypatch):
    # The only group with progress counts in a category that is not
    # configured; the locked one counts in the configured category.
    locked = final_group()
    locked["progressCriteria"]["categories"] = [OTHER_CATEGORY]
    groups = [watch_group(progress(status="CLAIMED", grants=6)), locked]
    gql, listing_calls = channel_listing()
    gql.get_channel_drop_campaigns_progress = lambda channel_id: (
        listing_calls.append(channel_id)
        or SimpleNamespace(
            campaigns=[campaign_from_channel_progress(reward_campaign(*groups))],
            campaigns_available=True,
        )
    )
    gql.post_gql_request_raw = lambda operation_name, payload: {
        "data": {"dropsCampaign": reward_campaign(watch_group(), locked)}
    }
    twitch = bare_twitch(gql)

    assert (
        evaluate(monkeypatch, twitch, [listing()], requested=("other-game",))[0] == {}
    )
    stored = twitch.watch_reward_campaigns["quest-1"]
    assert [c["id"] for c in stored["_watch_reward"]["categories"]] == ["777", "888"]
    assert stored["game"]["id"] == "777"

    groups[1] = locked | {"self": progress(minutes=0)}
    stored["_watch_reward"]["read_at"] -= WATCH_REWARD_FINISHED_REFRESH_SECONDS + 1
    deadlines, _ = evaluate(monkeypatch, twitch, [listing()], requested=("other-game",))

    assert set(deadlines) == {"other-game"}


def test_stream_gets_the_part_of_the_campaign_that_counts_in_its_category():
    twitch = bare_twitch()
    campaign = Campaign(
        convert(*two_category_groups(progress(minutes=5), progress(minutes=7)))
    )

    def view_for(game):
        return twitch._Twitch__campaign_for_stream(campaign, SimpleNamespace(game=game))

    first = view_for({"id": "777", "name": "Example Game"})
    # Without a game ID the name decides.
    second = view_for({"displayName": "Other Game"})

    assert first.game["id"] == "777"
    assert [drop.id for drop in first.drops] == ["group-watch#0"]
    assert second.game["id"] == "888"
    assert [drop.id for drop in second.drops] == ["group-other#0"]
    assert first.id == second.id == "quest-1"
    assert view_for({"id": "999", "name": "Unrelated"}) is None
    # The drops are shared with the whole campaign: synced progress shows.
    campaign.drops[1].update_minutes(30)
    assert second.drops[0].current_minutes_watched == 30


def test_drop_campaign_is_matched_to_a_stream_by_its_game():
    twitch = bare_twitch()
    campaign = Campaign(
        {
            "id": "campaign-1",
            "name": "Drops",
            "status": "ACTIVE",
            "game": {"id": "777", "displayName": "Example Game"},
            "startAt": START_AT,
            "endAt": END_AT,
            "timeBasedDrops": [],
        }
    )

    def view_for(game_id):
        return twitch._Twitch__campaign_for_stream(
            campaign, SimpleNamespace(game={"id": game_id})
        )

    assert campaign.reward_categories == []
    assert view_for("777") is campaign
    assert view_for("888") is None


def test_listing_with_every_watch_group_locked_replaces_the_optimistic_lookup(
    monkeypatch,
):
    # The lookup sends no progress, so its record reads every group as not
    # started. The channel then reports progress only on the subscription
    # group: the watch group is locked, and the category must not be mined.
    twitch, _, _ = evaluation_twitch(
        final_group(),
        sub_group(),
        structure=reward_campaign(final_group(), sub_group() | {"self": None}),
    )

    deadlines, _ = evaluate(monkeypatch, twitch, [listing()])

    assert deadlines == {}
    stored = twitch.watch_reward_campaigns["quest-1"]
    assert stored["_watch_reward"]["progress_known"] is True
    assert stored["timeBasedDrops"] == []


def test_repeat_after_one_seen_claimable_first_is_claimed_too(monkeypatch):
    gql, _ = channel_listing()
    twitch = bare_twitch(gql)
    monkeypatch.setattr(
        Twitch, "_Twitch__drop_variant_entries_from_drop", lambda self, drop: []
    )

    def listed(group_self):
        twitch._Twitch__store_listed_watch_rewards(
            [campaign_from_channel_progress(reward_campaign(watch_group(group_self)))]
        )

    listed(progress(status="CLAIMABLE", minutes=60, grants=1))
    assert len(gql.claims) == 1
    # Twitch counted the repeat before its claim, so the next one starts
    # with the same count, and is claimable again within the hour.
    listed(progress(minutes=10, grants=1))
    listed(progress(status="CLAIMABLE", minutes=60, grants=1))

    assert len(gql.claims) == 2


def test_progress_entries_file_each_group_under_its_own_category():
    twitch = bare_twitch()
    both = watch_group(progress(minutes=3), categories=[OTHER_CATEGORY, CATEGORY])
    both["id"] = "group-both"
    twitch._Twitch__remember_watch_reward_campaign(
        convert(*two_category_groups(progress(minutes=5), progress(minutes=7)), both)
    )

    entries = twitch._Twitch__watch_reward_progress_entries()

    # A group that counts in two categories is reported once, under its first.
    assert [
        (entry["game"]["displayName"], [drop["id"] for drop in entry["timeBasedDrops"]])
        for entry in entries
    ] == [
        ("Example Game", ["group-watch#0"]),
        ("Other Game", ["group-other#0", "group-both#0"]),
    ]
    assert all(entry["id"] == "quest-1" for entry in entries)
    # Copies: the sync may change them freely.
    entries[0]["timeBasedDrops"][0]["self"]["currentMinutesWatched"] = 99
    stored = twitch.watch_reward_campaigns["quest-1"]
    assert stored["timeBasedDrops"][0]["self"]["currentMinutesWatched"] == 5


def test_accepted_claim_is_not_sent_again_from_the_stored_record(monkeypatch):
    # No channel listing replaces the record for hours once the miner has
    # left the category; the claim must not be re-sent every hour meanwhile.
    gql, _ = channel_listing(watch_group(progress(status="CLAIMABLE", minutes=60)))
    twitch = bare_twitch(gql)
    monkeypatch.setattr(
        Twitch, "_Twitch__drop_variant_entries_from_drop", lambda self, drop: []
    )
    twitch._Twitch__get_campaigns_from_channel_id("1")
    assert len(gql.claims) == 1
    stored = twitch.watch_reward_campaigns["quest-1"]
    assert stored["timeBasedDrops"][0]["self"]["dropInstanceID"] is None

    # Long after the claim's debounce has run out.
    twitch.accepted_drop_claims = {}
    twitch._Twitch__claim_waiting_watch_rewards_by_deadline()
    assert len(gql.claims) == 1

    # A listing that still reports it claimable brings the claim back.
    twitch._Twitch__get_campaigns_from_channel_id("1")
    assert len(gql.claims) == 2


def test_error_while_claiming_does_not_fail_the_channel_listing(monkeypatch):
    gql, _ = channel_listing(watch_group(progress(status="CLAIMABLE", minutes=60)))
    twitch = bare_twitch(gql)

    def broken_claim(self, campaign, drop_dict):
        raise OSError("analytics disk full")

    monkeypatch.setattr(Twitch, "_Twitch__claim_watch_reward_drop", broken_claim)

    campaigns, available, answered = twitch._Twitch__get_campaigns_from_channel_id("1")

    assert (available, answered) == (True, True)
    assert [campaign["id"] for campaign in campaigns] == ["quest-1"]


def test_reward_campaign_belongs_to_every_category_it_counts_in():
    # A forced category streamer is checked against the channel's listing,
    # whose record names only the campaign's first category.
    twitch = bare_twitch()
    campaign = convert(watch_group(progress(), categories=[CATEGORY, OTHER_CATEGORY]))

    assert twitch._Twitch__campaign_is_for_game(campaign, "777", "example-game")
    assert twitch._Twitch__campaign_is_for_game(campaign, "888", "other-game")
    assert twitch._Twitch__campaign_is_for_game(campaign, None, "other-game")
    assert not twitch._Twitch__campaign_is_for_game(campaign, "999", "unrelated")


def test_watch_requirements_from_the_second_listing_source_are_kept(monkeypatch):
    # The dashboard's entry is kept for an ID both sources list; here only
    # the hand-written query says what the campaign asks for.
    twitch, _, _ = evaluation_twitch(watch_group(progress(minutes=12)))
    dashboard_entry = listing()
    del dashboard_entry["unlockRequirements"], dashboard_entry["isSitewide"]
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_reward_campaigns_raw_query",
        lambda self: ([listing()], {}),
    )

    deadlines, _ = evaluate(
        monkeypatch, twitch, [dashboard_entry], raw_query_patched=True
    )

    assert set(deadlines) == {"example-game"}


def test_campaign_no_channel_reports_progress_for_is_not_watched_for(monkeypatch):
    # Only the reward-group lookup answers. It reads every group as not
    # started, which is also how a quest the account already finished looks.
    twitch, lookups, listing_calls = evaluation_twitch(
        watch_group(progress(minutes=5)), silent_channels={"1", "2", "3"}
    )

    deadlines, twitch_games = evaluate(
        monkeypatch, twitch, [listing()], channels=("1", "2", "3")
    )

    assert deadlines == {}
    assert twitch.active_drop_campaigns == {}
    assert listing_calls == ["1", "2"]
    assert (
        twitch.watch_reward_campaigns["quest-1"]["_watch_reward"]["progress_known"]
        is False
    )

    # The failed read is not repeated on the next evaluation, which can come
    # minutes later.
    evaluate(monkeypatch, twitch, [listing()], channels=("4",))
    assert listing_calls == ["1", "2"]

    # Later, once a channel lists it, the category is active.
    twitch.watch_reward_campaigns["quest-1"]["_watch_reward"][
        "refresh_attempted_at"
    ] -= 3600
    deadlines, _ = evaluate(monkeypatch, twitch, [listing()], channels=("4",))
    assert set(deadlines) == {"example-game"}
    assert listing_calls == ["1", "2", "4"]
    assert len(lookups) == 1


def test_user_id_failure_does_not_lose_the_channel_listing():
    gql, _ = channel_listing(watch_group(progress(status="CLAIMABLE", minutes=60)))
    twitch = bare_twitch(gql)

    def no_user_id():
        raise ConnectionError("no route to host")

    twitch.twitch_login = SimpleNamespace(get_user_id=no_user_id, username="viewer")

    campaigns, available, answered = twitch._Twitch__get_campaigns_from_channel_id("1")

    assert (available, answered) == (True, True)
    [campaign] = campaigns
    # Stored without a claim ID, so nothing is claimed until a later listing.
    assert campaign["timeBasedDrops"][0]["self"]["dropInstanceID"] is None
    assert gql.claims == []
    # Which is read for when the next read is due: nothing is watched for a
    # reward waiting to be claimed, so none would come by itself.
    [read_due] = twitch.watch_reward_pending_starts
    assert (read_due - utc_now()).total_seconds() == pytest.approx(
        WATCH_REWARD_PROGRESS_REFRESH_SECONDS, abs=5
    )


def test_progress_read_tries_only_a_few_of_a_campaigns_many_categories(monkeypatch):
    many = [
        {"id": str(number), "displayName": f"Game {number}"} for number in range(20)
    ]
    twitch, _, listing_calls = evaluation_twitch(
        watch_group(progress(), categories=many),
        structure=reward_campaign(watch_group(categories=many)),
    )
    directory_calls = []

    # No category has a live channel.
    evaluate(
        monkeypatch,
        twitch,
        [listing()],
        requested=("game-7",),
        channels=(),
        directory_calls=directory_calls,
    )

    # The configured category first, then the campaign's own order.
    assert [slug for slug, _ in directory_calls] == [
        "slug-7",
        "slug-0",
        "slug-1",
        "slug-2",
    ]
    assert listing_calls == []

    # Still nothing ten minutes later: the read goes on down the list, so a
    # live channel in any category is found in the end.
    details = twitch.watch_reward_campaigns["quest-1"]["_watch_reward"]
    seen = set()
    for _ in range(5):
        del directory_calls[:]
        details["refresh_attempted_at"] -= WATCH_REWARD_PROGRESS_REFRESH_SECONDS + 1
        evaluate(
            monkeypatch,
            twitch,
            [listing()],
            requested=("game-7",),
            channels=(),
            directory_calls=directory_calls,
        )
        assert len(directory_calls) == 4
        seen.update(slug for slug, _ in directory_calls)
    assert seen == {f"slug-{number}" for number in range(20)}


def test_stored_campaign_the_listings_leave_out_is_still_evaluated(monkeypatch):
    # Whether Twitch keeps listing a campaign between its repeats is not
    # known; the record of one seen on a channel does not depend on it.
    twitch, _, listing_calls = evaluation_twitch(
        watch_group(progress(minutes=12, window=open_window()))
    )
    assert set(evaluate(monkeypatch, twitch, [listing()])[0]) == {"example-game"}
    details = twitch.watch_reward_campaigns["quest-1"]["_watch_reward"]

    assert set(evaluate(monkeypatch, twitch, [])[0]) == {"example-game"}

    # Its progress is still kept up to date.
    details["read_at"] -= WATCH_REWARD_PROGRESS_REFRESH_SECONDS + 1
    assert set(evaluate(monkeypatch, twitch, [])[0]) == {"example-game"}
    assert listing_calls == ["4242", "4242"]


def test_unlisted_campaign_no_channel_reports_any_more_is_not_watched_for(
    monkeypatch,
):
    # Twitch may have withdrawn it: neither the listings nor any channel
    # name it, so the stored progress is no ground to stay in the category.
    twitch, _, _ = evaluation_twitch(
        watch_group(progress(minutes=12, window=open_window())),
        silent_channels=("9",),
    )
    evaluate(monkeypatch, twitch, [listing()])
    details = twitch.watch_reward_campaigns["quest-1"]["_watch_reward"]
    details["read_at"] -= WATCH_REWARD_UNLISTED_SECONDS + 1
    # A channel the miner watched advertised it earlier in the session;
    # that record is no newer.
    twitch.advertised_drop_campaigns = {
        "quest-1": twitch.watch_reward_campaigns["quest-1"]
    }

    assert evaluate(monkeypatch, twitch, [], channels=("9",))[0] == {}
    # Listed, the same record is still trusted.
    details["refresh_attempted_at"] = 0
    deadlines = evaluate(monkeypatch, twitch, [listing()], channels=("9",))[0]
    assert set(deadlines) == {"example-game"}


def test_ended_campaign_left_in_the_store_is_not_evaluated(monkeypatch):
    twitch, _, _ = evaluation_twitch(
        watch_group(progress(minutes=12, window=open_window()))
    )
    evaluate(monkeypatch, twitch, [listing()])
    twitch.watch_reward_campaigns["quest-1"]["endAt"] = "2020-06-01T00:00:00Z"

    assert evaluate(monkeypatch, twitch, [])[0] == {}


def test_slow_listing_does_not_undo_a_claim_made_from_a_newer_one(monkeypatch):
    # Two threads list channels. The one that asked first is answered last,
    # after the other saw the reward claimable and claimed it.
    monkeypatch.setattr(
        Twitch, "_Twitch__drop_variant_entries_from_drop", lambda self, drop: []
    )
    in_progress = watch_group(progress(minutes=59, window=open_window()))
    claimable = watch_group(
        progress(status="CLAIMABLE", minutes=60, window=open_window())
    )
    gql, _ = channel_listing(claimable)
    twitch = bare_twitch(gql)
    newer = gql.get_channel_drop_campaigns_progress
    interleaved = []
    # A coarse clock stamps both requests alike; their order still counts.
    stopped = time.time()
    monkeypatch.setattr(twitch_module.time, "time", lambda: stopped)

    def slow(channel_id):
        if not interleaved:
            interleaved.append(True)
            twitch._Twitch__get_campaigns_from_channel_id("2")
            return SimpleNamespace(
                campaigns=[
                    campaign_from_channel_progress(reward_campaign(in_progress))
                ],
                campaigns_available=True,
            )
        return newer(channel_id)

    gql.get_channel_drop_campaigns_progress = slow

    [campaign], _, _ = twitch._Twitch__get_campaigns_from_channel_id("1")

    assert gql.claims == ["viewer-1#quest-1#group-watch"]
    # The older answer is dropped: its caller gets the record in force.
    assert campaign is twitch.watch_reward_campaigns["quest-1"]
    [drop] = campaign["timeBasedDrops"]
    assert drop["_reward_group"]["claimable"] is True

    # Still claimable on the next listing, and still not sent twice.
    twitch._Twitch__get_campaigns_from_channel_id("1")
    assert gql.claims == ["viewer-1#quest-1#group-watch"]


def test_progress_read_passes_over_only_the_categories_it_tried(monkeypatch):
    # Channels that list nothing use up the read's four channel listings
    # after two categories; the other two are tried by the next read.
    four = [{"id": str(number), "displayName": f"Game {number}"} for number in range(4)]
    twitch, _, listing_calls = evaluation_twitch(
        watch_group(progress(), categories=four),
        structure=reward_campaign(watch_group(categories=four)),
        silent_channels=("8", "9"),
    )
    directory_calls = []

    def read():
        del directory_calls[:]
        evaluate(
            monkeypatch,
            twitch,
            [listing()],
            requested=("game-0",),
            channels=("8", "9"),
            directory_calls=directory_calls,
        )
        return [slug for slug, _ in directory_calls]

    assert read() == ["slug-0", "slug-1"]
    details = twitch.watch_reward_campaigns["quest-1"]["_watch_reward"]
    details["refresh_attempted_at"] -= WATCH_REWARD_PROGRESS_REFRESH_SECONDS + 1
    assert read() == ["slug-2", "slug-3"]
    assert listing_calls == ["8", "9"] * 4


def test_claim_answered_after_the_next_repeat_was_listed_does_not_hide_it(
    monkeypatch,
):
    # Twitch counts the repeat before its claim, so the next repeat has the
    # claimed one's count. Another thread stores it while the claim request
    # is on its way.
    monkeypatch.setattr(
        Twitch, "_Twitch__drop_variant_entries_from_drop", lambda self, drop: []
    )
    groups = [
        watch_group(
            progress(status="CLAIMABLE", minutes=60, grants=1, window=open_window())
        )
    ]
    gql, _ = channel_listing()
    gql.get_channel_drop_campaigns_progress = lambda channel_id: SimpleNamespace(
        campaigns=[campaign_from_channel_progress(reward_campaign(*groups))],
        campaigns_available=True,
    )
    twitch = bare_twitch(gql)
    accept = gql.claim_drop_rewards

    def slow_claim(instance_id):
        if not gql.claims:
            twitch._Twitch__remember_watch_reward_campaign(
                convert(
                    watch_group(progress(minutes=5, grants=1, window=open_window()))
                )
            )
        return accept(instance_id)

    gql.claim_drop_rewards = slow_claim

    twitch._Twitch__get_campaigns_from_channel_id("1")

    assert gql.claims == ["viewer-1#quest-1#group-watch"]
    assert twitch.accepted_drop_ids == {"group-watch#0"}
    [drop] = twitch.watch_reward_campaigns["quest-1"]["timeBasedDrops"]
    assert drop["id"] == "group-watch#1"

    # The repeat stored meanwhile is claimed in its turn.
    twitch._Twitch__get_campaigns_from_channel_id("1")
    assert gql.claims == ["viewer-1#quest-1#group-watch"] * 2


def test_window_opening_of_a_waiting_repeat_is_reported_once(monkeypatch):
    twitch = bare_twitch()
    assert twitch.take_opened_watch_reward_start() is False
    opens = NOW + timedelta(hours=1)
    waiting = convert(watch_group(progress(grants=1, next_at=stamp(opens))))

    # Listed twice with the same opening.
    twitch._Twitch__remember_watch_reward_campaign(waiting)
    twitch._Twitch__remember_watch_reward_campaign(waiting)
    assert twitch.take_opened_watch_reward_start() is False

    # The window opens between two passes of the main loop: the pass after
    # it is still told, once.
    monkeypatch.setattr(twitch_module, "utc_now", lambda: opens + timedelta(seconds=40))
    assert twitch.take_opened_watch_reward_start() is True
    assert twitch.take_opened_watch_reward_start() is False


def test_listing_claims_on_a_thread_of_its_own(monkeypatch):
    # Listings are read on the thread that sends the minute-watched events,
    # which a slow claim must not hold up.
    monkeypatch.setattr(twitch_module, "Thread", HeldThread)
    gql, _ = channel_listing(
        watch_group(progress(status="CLAIMABLE", minutes=60, window=open_window()))
    )
    twitch = bare_twitch(gql)

    [campaign], _, _ = twitch._Twitch__get_campaigns_from_channel_id("1")

    assert campaign["timeBasedDrops"][0]["_reward_group"]["claimable"] is True
    [thread] = InlineThread.started
    assert gql.claims == []

    # No second thread while that one runs.
    twitch._Twitch__get_campaigns_from_channel_id("1")
    assert InlineThread.started == [thread]

    monkeypatch.setattr(
        Twitch, "_Twitch__drop_variant_entries_from_drop", lambda self, drop: []
    )
    thread.target()
    assert gql.claims == ["viewer-1#quest-1#group-watch"]


def test_listing_starts_no_claim_thread_once_the_miner_is_stopping(monkeypatch):
    # Shutdown has looked for the claim thread by then, and would not wait
    # for one started after.
    monkeypatch.setattr(twitch_module, "Thread", HeldThread)
    claimable = progress(status="CLAIMABLE", minutes=60, window=open_window())
    gql, _ = channel_listing(watch_group(claimable))
    twitch = bare_twitch(gql)
    twitch.running = False

    twitch._Twitch__get_campaigns_from_channel_id("1")

    assert InlineThread.started == []
    assert getattr(twitch, "watch_reward_claim_thread", None) is None


def test_listing_with_nothing_to_claim_starts_no_thread():
    gql, _ = channel_listing(watch_group(progress(minutes=12, window=open_window())))

    bare_twitch(gql)._Twitch__get_campaigns_from_channel_id("1")

    assert InlineThread.started == []


def test_repeat_keeps_its_drop_id_when_twitch_counts_it_before_the_claim():
    # Watched as "#0", claimable with one repeat counted, and the next
    # repeat watched with that same count: three listings, two repeats.
    twitch = bare_twitch()

    def listed(group_self):
        [drop] = twitch._Twitch__remember_watch_reward_campaign(
            convert(watch_group(group_self))
        )["timeBasedDrops"]
        return drop["id"], drop["name"]

    first = ("group-watch#0", "Daily Chest (repeat 1 of 6)")
    assert listed(progress(minutes=59, window=open_window())) == first
    claimable = progress(status="CLAIMABLE", minutes=60, grants=1, window=open_window())
    assert listed(claimable) == first
    # Still waiting to be claimed on the next listing.
    assert listed(claimable) == first
    assert listed(progress(minutes=5, grants=1, window=open_window())) == (
        "group-watch#1",
        "Daily Chest (repeat 2 of 6)",
    )


def claimed_repeat_twitch(monkeypatch, opens_again):
    """A Twitch that claimed the first repeat of a group and then read it
    as claimed, the next to be earned from `opens_again`; with the gql stub
    and the window the repeat was earned in."""
    monkeypatch.setattr(
        Twitch, "_Twitch__drop_variant_entries_from_drop", lambda self, drop: []
    )
    gql, _ = channel_listing()
    twitch = bare_twitch(gql)
    window = open_window()
    for group_self in (
        progress(status="CLAIMABLE", minutes=60, grants=1, window=window),
        progress(
            status="CLAIMED",
            minutes=60,
            grants=1,
            window=window,
            next_at=stamp(opens_again),
        ),
    ):
        twitch._Twitch__store_listed_watch_rewards([convert(watch_group(group_self))])
    assert gql.claims == ["viewer-1#quest-1#group-watch"]
    # Long after the claim's debounce has run out.
    twitch.accepted_drop_claims = {}
    return twitch, gql, window


def test_listing_of_a_claimed_repeat_as_waiting_again_is_not_the_next_repeat(
    monkeypatch,
):
    # Twitch answers a later request from before the claim. The next repeat
    # cannot be earned before its window opens.
    twitch, gql, window = claimed_repeat_twitch(
        monkeypatch, datetime.now(timezone.utc) + timedelta(hours=23)
    )
    [waiting] = twitch.watch_reward_campaigns["quest-1"]["timeBasedDrops"]
    assert waiting["id"] == "group-watch#1"

    twitch._Twitch__store_listed_watch_rewards(
        [
            convert(
                watch_group(
                    progress(status="CLAIMABLE", minutes=60, grants=1, window=window)
                )
            )
        ]
    )

    assert twitch.watch_reward_campaigns["quest-1"]["timeBasedDrops"] == [waiting]
    assert gql.claims == ["viewer-1#quest-1#group-watch"]
    assert twitch.accepted_drop_ids == {"group-watch#0"}


def test_group_claimable_in_an_answer_with_an_older_state_of_another_is_claimed(
    monkeypatch,
):
    # The answer shows the claimed repeat waiting again, and a second group
    # that has its minutes in since: that one is not held back with it.
    twitch, gql, window = claimed_repeat_twitch(
        monkeypatch, datetime.now(timezone.utc) + timedelta(hours=23)
    )
    [waiting] = twitch.watch_reward_campaigns["quest-1"]["timeBasedDrops"]
    second = watch_group(progress(status="CLAIMABLE", minutes=60, window=window)) | {
        "id": "group-two",
        "name": "Weekly Chest",
    }

    twitch._Twitch__store_listed_watch_rewards(
        [
            convert(
                watch_group(
                    progress(status="CLAIMABLE", minutes=60, grants=1, window=window)
                ),
                second,
            )
        ]
    )

    stored = twitch.watch_reward_campaigns["quest-1"]["timeBasedDrops"]
    assert stored[0] == waiting
    assert stored[0] is not waiting
    assert gql.claims == [
        "viewer-1#quest-1#group-watch",
        "viewer-1#quest-1#group-two",
    ]
    assert "group-watch#1" not in twitch.accepted_drop_ids


def test_repeat_claimable_once_its_window_has_opened_is_stored_and_claimed(
    monkeypatch,
):
    # The window the stored record waited for has opened: the next repeat
    # can be the one waiting now.
    twitch, gql, window = claimed_repeat_twitch(
        monkeypatch, datetime.now(timezone.utc) + timedelta(hours=23)
    )
    [waiting] = twitch.watch_reward_campaigns["quest-1"]["timeBasedDrops"]
    waiting["startAt"] = stamp(datetime.now(timezone.utc) - timedelta(minutes=61))

    twitch._Twitch__store_listed_watch_rewards(
        [
            convert(
                watch_group(
                    progress(status="CLAIMABLE", minutes=60, grants=1, window=window)
                )
            )
        ]
    )

    assert gql.claims == ["viewer-1#quest-1#group-watch"] * 2
    assert twitch.accepted_drop_ids == {"group-watch#0", "group-watch#1"}


def test_claimable_repeat_seen_first_does_not_take_an_older_repeats_id():
    # The stored record is of a repeat claimed long ago; the listing reports
    # a later one claimable. Nothing says they are the same repeat.
    twitch = bare_twitch()
    stale = progress(status="CLAIMABLE", minutes=60, grants=0, window=open_window())
    newer = progress(status="CLAIMABLE", minutes=60, grants=1, window=open_window())

    twitch._Twitch__remember_watch_reward_campaign(convert(watch_group(stale)))
    [drop] = twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(newer))
    )["timeBasedDrops"]

    assert drop["id"] == "group-watch#1"


def repeat_ids(listings, restart_between=False):
    """The drop ID each listing of a three-repeat group is stored under, by
    one miner or by one started anew before every listing."""
    twitch = bare_twitch()
    ids = []
    for group_self in listings:
        if restart_between:
            twitch = bare_twitch()
        [drop] = twitch._Twitch__remember_watch_reward_campaign(
            convert(watch_group(group_self, times=3))
        )["timeBasedDrops"]
        ids.append(drop["id"])
    return ids


def claimable_repeat(grants):
    return progress(status="CLAIMABLE", minutes=60, grants=grants, window=open_window())


def watched_repeat(grants, minutes=5):
    return progress(minutes=minutes, grants=grants, window=open_window())


@pytest.mark.parametrize("restart_between", [False, True])
def test_each_repeat_has_a_drop_id_no_earlier_repeat_had(restart_between):
    # Twitch counts each repeat once its minutes are in. The numbers come
    # from what it sends alone, so a restart at any point changes none.
    assert repeat_ids(
        [
            watched_repeat(0),
            claimable_repeat(1),
            watched_repeat(1),
            # A window that starts over is the same repeat: none was granted.
            watched_repeat(1, minutes=0),
            claimable_repeat(2),
            watched_repeat(2),
            claimable_repeat(3),
            progress(status="CLAIMED", grants=3),
        ],
        restart_between=restart_between,
    ) == [f"group-watch#{number}" for number in (0, 0, 1, 1, 1, 2, 2, 2)]


def test_repeats_are_numbered_the_same_if_twitch_counts_them_when_claimed():
    assert repeat_ids(
        [
            watched_repeat(0),
            claimable_repeat(0),
            watched_repeat(1),
            claimable_repeat(1),
            watched_repeat(2),
            claimable_repeat(2),
            progress(status="CLAIMED", grants=3),
        ]
    ) == [f"group-watch#{number}" for number in (0, 0, 1, 1, 2, 2, 2)]


def test_repeat_after_one_never_counted_gets_a_number_of_its_own():
    # Counted when claimed, and the claim was never made: the next repeat
    # is watched with the count the lost one had.
    assert repeat_ids([watched_repeat(0), claimable_repeat(0), watched_repeat(0)]) == [
        "group-watch#0",
        "group-watch#0",
        "group-watch#1",
    ]


def test_repeat_is_named_for_its_number_not_for_the_count():
    # Announced when it is claimed, by which time Twitch may have counted
    # it: the first repeat earned is not "0 earned", nor the second.
    twitch = bare_twitch()

    def listed(group_self):
        [drop] = twitch._Twitch__remember_watch_reward_campaign(
            convert(watch_group(group_self, times=3))
        )["timeBasedDrops"]
        return drop["name"]

    assert [
        listed(watched_repeat(0)),
        listed(claimable_repeat(1)),
        listed(watched_repeat(1)),
        # Counted when claimed after all, and this claim never made: the
        # stored record numbers the next repeat, and names it to match.
        listed(claimable_repeat(1)),
        listed(watched_repeat(1)),
        listed(progress(status="CLAIMED", grants=3)),
    ] == [
        "Daily Chest (repeat 1 of 3)",
        "Daily Chest (repeat 1 of 3)",
        "Daily Chest (repeat 2 of 3)",
        "Daily Chest (repeat 2 of 3)",
        "Daily Chest (repeat 3 of 3)",
        "Daily Chest (repeat 3 of 3)",
    ]
    # A group earned once keeps its own name.
    [once] = convert(watch_group(progress(minutes=5), times=1))["timeBasedDrops"]
    assert once["name"] == "Daily Chest"


def test_repeat_is_numbered_after_the_one_before_a_listing_left_the_group_out():
    twitch = bare_twitch()

    def listed(*groups):
        return [
            drop["id"]
            for drop in twitch._Twitch__remember_watch_reward_campaign(
                convert(*groups)
            )["timeBasedDrops"]
        ]

    other = watch_group(progress(minutes=3), times=1) | {"id": "group-other"}
    claimable = progress(status="CLAIMABLE", minutes=60)
    assert listed(watch_group(claimable), other) == ["group-watch#0", "group-other"]
    # Twitch reports the other group's progress but none on this one.
    assert listed(watch_group(None), other) == ["group-other"]
    assert listed(watch_group(progress(minutes=5)), other) == [
        "group-watch#1",
        "group-other",
    ]


def test_progress_cache_holds_the_reward_drops_that_can_be_watched_now():
    # The stall check and the channel pick read this cache; the inventory
    # it is built from never lists a reward campaign.
    twitch = bare_twitch()
    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(minutes=12, window=open_window())))
    )

    twitch._Twitch__cache_drop_inventory_progress({"dropCampaignsInProgress": None})

    assert twitch.drop_inventory_progress == {
        "example-game": (
            (
                "quest-1",
                "Launch Celebration",
                "group-watch#0",
                "Daily Chest (repeat 1 of 6)",
                12,
                60,
                None,
            ),
        )
    }

    # A newer listing reaches the cache without waiting for the next
    # inventory read: left at 12 minutes, the channel would look stalled.
    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(minutes=22, window=open_window())))
    )
    [row] = twitch.drop_inventory_progress["example-game"]
    assert row[4] == 22
    assert twitch.drop_inventory_minutes_by_drop_id == {"group-watch#0": 22}

    # A repeat waiting for its next window is not progress to watch for.
    twitch._Twitch__remember_watch_reward_campaign(
        convert(
            watch_group(progress(grants=1, next_at=stamp(NOW + timedelta(hours=1))))
        )
    )
    assert twitch.drop_inventory_progress == {}
    twitch._Twitch__cache_drop_inventory_progress({})
    assert twitch.drop_inventory_progress == {}


def test_progress_cache_is_not_built_by_a_listing_alone():
    # Readers take a built cache as the inventory's word on every game; a
    # listing only updates one an inventory read has built.
    twitch = bare_twitch()
    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(minutes=12, window=open_window())))
    )

    assert not getattr(twitch, "drop_inventory_progress", None)


def test_progress_cache_lists_a_drop_under_every_category_it_counts_in():
    # The stall check looks up the game being watched, which can be the
    # second category of the group.
    twitch = bare_twitch()
    twitch._Twitch__remember_watch_reward_campaign(
        convert(
            watch_group(
                progress(minutes=12, window=open_window()),
                categories=[CATEGORY, OTHER_CATEGORY],
            )
        )
    )

    twitch._Twitch__cache_drop_inventory_progress({})

    assert set(twitch.drop_inventory_progress) == {"example-game", "other-game"}
    assert twitch.drop_inventory_minutes_by_drop_id == {"group-watch#0": 12}


def test_claimable_reward_goes_into_the_cache_as_finished():
    twitch = bare_twitch()
    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(minutes=12, window=open_window())))
    )
    twitch._Twitch__cache_drop_inventory_progress({})

    twitch._Twitch__remember_watch_reward_campaign(
        convert(
            watch_group(progress(status="CLAIMABLE", minutes=60, window=open_window()))
        )
    )

    assert twitch.drop_inventory_progress == {}
    assert twitch.drop_inventory_minutes_by_drop_id == {}
    assert twitch.drop_inventory_finished_ids == {"group-watch#0"}


def test_finished_group_is_its_last_repeat_not_one_more():
    # Six repeats are watched as #0 to #5 whichever way Twitch counts.
    [earned] = convert(watch_group(progress(status="CLAIMED", grants=6)))[
        "timeBasedDrops"
    ]
    assert (earned["id"], earned["name"]) == (
        "group-watch#5",
        "Daily Chest (repeat 6 of 6)",
    )

    # The last repeat, counted before its claim and seen claimable first.
    [waiting] = convert(
        watch_group(progress(status="CLAIMABLE", minutes=60, grants=6))
    )["timeBasedDrops"]
    assert waiting["id"] == "group-watch#5"

    # Counted once claimed: the first repeat, earned.
    [first] = convert(watch_group(progress(status="CLAIMED", grants=1)))[
        "timeBasedDrops"
    ]
    assert first["id"] == "group-watch#0"


def test_repeat_twitch_reports_claimed_is_not_the_last_when_it_names_the_next():
    # The repeat is done and Twitch says when the group can be earned again:
    # the next repeat, opening then. Read as the group's last, nothing would
    # bring the miner back for the five still to come.
    opens = NOW + timedelta(hours=3)
    twitch = bare_twitch()

    [drop] = twitch._Twitch__remember_watch_reward_campaign(
        convert(
            watch_group(
                progress(status="CLAIMED", minutes=60, grants=1, next_at=stamp(opens))
            )
        )
    )["timeBasedDrops"]

    assert (drop["id"], drop["startAt"]) == ("group-watch#1", stamp(opens))
    assert drop["self"]["isClaimed"] is False
    assert drop["_reward_group"]["finished"] is False
    assert twitch.watch_reward_pending_starts == {opens}

    # The moment has passed and Twitch has yet to say anything else: the
    # same repeat, open now, and not one more that was claimed.
    just_past = progress(
        status="CLAIMED",
        minutes=60,
        grants=1,
        window={
            "startedAt": stamp(NOW - timedelta(hours=24)),
            "expiresAt": stamp(NOW - timedelta(seconds=20)),
        },
        next_at=stamp(NOW - timedelta(seconds=20)),
    )
    [drop] = twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(just_past))
    )["timeBasedDrops"]
    assert (drop["id"], drop["startAt"]) == ("group-watch#1", START_AT)
    assert drop["self"]["isClaimed"] is False
    assert drop["self"]["currentMinutesWatched"] == 0
    assert drop["_reward_group"]["finished"] is False

    # The last repeat has no next one, whatever moment comes with it.
    [last] = convert(
        watch_group(progress(status="CLAIMED", grants=6, next_at=stamp(opens)))
    )["timeBasedDrops"]
    assert (last["id"], last["_reward_group"]["finished"]) == ("group-watch#5", True)
    # Nor has a group that pays out once, reported claimed.
    all_in = watch_group(progress(status="CLAIMED", grants=2, next_at=stamp(opens)))
    all_in["progressCriteria"]["repeatableConfig"][
        "shouldCompleteAllRepeatsToEarn"
    ] = True
    [paid] = convert(all_in)["timeBasedDrops"]
    assert paid["_reward_group"]["finished"] is True


def test_claimable_repeat_schedules_the_return_for_its_next_window():
    # Claimed during an evaluation, it leaves nothing to watch and so no
    # channel to report the next window: Twitch's word for it is kept.
    opens = NOW + timedelta(hours=1)

    def learned(**changes):
        twitch = bare_twitch()
        claimable = progress(
            status="CLAIMABLE", minutes=60, window=open_window(), next_at=stamp(opens)
        )
        [drop] = twitch._Twitch__remember_watch_reward_campaign(
            convert(watch_group(claimable | changes))
        )["timeBasedDrops"]
        return drop["_reward_group"]["next_start"], twitch.watch_reward_pending_starts

    assert learned() == (stamp(opens), {opens})
    # Without a next eligibility time, the window's end is when to look.
    window_ends = NOW + timedelta(hours=23)
    assert learned(nextEligibleAt=None) == (stamp(window_ends), {window_ends})
    # The last repeat has no next window.
    assert learned(grantCount=6) == (None, set())


def test_claimable_repeat_listed_after_its_next_window_opened_is_looked_at_again(
    monkeypatch,
):
    # The answer crossed the boundary: still the repeat before, waiting to
    # be claimed, and both moments Twitch names are just past. The opening
    # scheduled from an earlier listing has been acted on already.
    just_past = NOW - timedelta(seconds=20)
    claimable = progress(
        status="CLAIMABLE",
        minutes=60,
        grants=1,
        window={
            "startedAt": stamp(NOW - timedelta(hours=24)),
            "expiresAt": stamp(NOW - timedelta(seconds=40)),
        },
        next_at=stamp(just_past),
    )
    twitch, _, listing_calls = evaluation_twitch(
        watch_group(progress(minutes=1, grants=1, window=open_window()))
    )
    twitch.watch_reward_pending_starts = set()

    [drop] = twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(claimable))
    )["timeBasedDrops"]

    assert drop["_reward_group"]["next_start"] == stamp(just_past)
    [retry_at] = twitch.watch_reward_pending_starts
    assert (retry_at - utc_now()).total_seconds() == pytest.approx(
        WATCH_REWARD_PROGRESS_REFRESH_SECONDS, abs=5
    )

    # However recent, the record is read again by the refresh that follows.
    assert set(evaluate(monkeypatch, twitch, [listing()])[0]) == {"example-game"}
    assert listing_calls == ["4242"]


def test_next_window_is_the_one_twitch_names_although_the_old_one_is_still_open():
    # Eligible again since a moment ago, while the window the repeat was
    # earned in has hours left: the later moment is not when to come back.
    eligible = NOW - timedelta(seconds=20)
    claimable = progress(
        status="CLAIMABLE",
        minutes=60,
        grants=1,
        window=open_window(),
        next_at=stamp(eligible),
    )

    [drop] = convert(watch_group(claimable))["timeBasedDrops"]

    assert drop["_reward_group"]["next_start"] == stamp(eligible)


def test_past_window_end_stands_in_for_a_missing_next_eligibility_time():
    ended = NOW - timedelta(seconds=40)
    claimable = progress(
        status="CLAIMABLE",
        minutes=60,
        grants=1,
        window={
            "startedAt": stamp(NOW - timedelta(hours=24)),
            "expiresAt": stamp(ended),
        },
    )

    [drop] = convert(watch_group(claimable))["timeBasedDrops"]
    assert drop["_reward_group"]["next_start"] == stamp(ended)

    # Neither named: nothing to go by.
    [drop] = convert(watch_group(claimable | {"currentWindow": None}))["timeBasedDrops"]
    assert drop["_reward_group"]["next_start"] is None


def test_category_refresh_runs_when_a_reward_window_has_opened():
    # The stand-in has nothing to claim with: the main loop sends no claim
    # for the moment. A refused one is the claim thread's to send again.
    miner = object.__new__(TwitchChannelPointsMiner)
    opened = []
    miner.twitch = SimpleNamespace(
        take_opened_watch_reward_start=lambda: bool(opened) and opened.pop(),
    )
    refresh_at = miner._TwitchChannelPointsMiner__refresh_for_opened_reward_window
    scheduled = time.time() + 6 * 3600

    assert refresh_at(scheduled) == scheduled

    opened.append(True)
    assert refresh_at(scheduled) <= time.time()
    assert refresh_at(scheduled) == scheduled

    # Category refreshes are off (only streamers are configured, whose
    # channels earn a reward campaign too): the moment is not left to end
    # every wait of the loop.
    opened.append(True)
    assert refresh_at(None) is None
    assert opened == []


def test_main_loop_answers_a_reward_campaign_before_its_other_checks():
    # A window can open in its campaign's last minutes; the update check
    # and the channel points refresh can each take that long.
    # The loop runs inside run(), which no test can drive: its order is read
    # from the source.
    loop = inspect.getsource(TwitchChannelPointsMiner.run).split(
        "if self.__wait_for_next_pass():", 1
    )[1]
    answered = loop.index("self.__refresh_for_opened_reward_window(")
    for check in (
        # Written to disk and synced: slow on a stalled volume.
        "self.__save_daily_report_state_safely()",
        "self.__send_daily_report_if_due()",
        "self.__check_for_update()",
        "self.__check_websockets()",
        "self.__refresh_channel_points_context()",
    ):
        assert answered < loop.index(check)


def main_loop_miner(monkeypatch, pending_starts, wait_seconds=30):
    """A miner whose main loop wait runs on a fake clock, and the seconds
    each of its sleeps was asked for."""
    miner = object.__new__(TwitchChannelPointsMiner)
    miner.twitch = bare_twitch()
    miner.twitch.watch_reward_pending_starts = set(pending_starts)
    clock = [1000.0]
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        clock[0] += seconds
        return False

    miner.twitch.restart_requested = SimpleNamespace(wait=sleep)
    # The module's own names only: the real clock is left to everything else.
    monkeypatch.setattr(
        miner_module, "time", SimpleNamespace(monotonic=lambda: clock[0])
    )
    monkeypatch.setattr(
        miner_module,
        "random",
        SimpleNamespace(uniform=lambda low, high: wait_seconds),
    )
    return miner, slept


def test_main_loop_wait_ends_when_a_reward_campaign_calls_for_a_refresh(monkeypatch):
    # A window that opens in less than the loop's wait is learned of from a
    # channel listing, while the loop waits.
    miner, slept = main_loop_miner(monkeypatch, [])
    wait = miner._TwitchChannelPointsMiner__wait_for_next_pass
    sleep = miner.twitch.restart_requested.wait

    def sleep_then_note(seconds):
        if len(slept) == 1:
            miner.twitch.watch_reward_pending_starts.add(utc_now())
        return sleep(seconds)

    miner.twitch.restart_requested = SimpleNamespace(wait=sleep_then_note)

    assert wait() is False

    assert slept == [MAIN_LOOP_REWARD_CHECK_SECONDS] * 2


def test_main_loop_wait_runs_its_time_without_a_refresh_to_make(monkeypatch):
    # Nothing due yet: the wait is the usual one.
    miner, slept = main_loop_miner(monkeypatch, [utc_now() + timedelta(hours=1)])
    wait = miner._TwitchChannelPointsMiner__wait_for_next_pass

    assert wait() is False
    assert sum(slept) == pytest.approx(30)

    # One due is answered on the pass this returns to at once.
    miner.twitch.watch_reward_pending_starts = {utc_now() - timedelta(seconds=1)}
    slept.clear()
    assert wait() is False
    assert slept == []


def test_main_loop_wait_reports_a_requested_restart(monkeypatch):
    miner, _ = main_loop_miner(monkeypatch, [])
    miner.twitch.restart_requested = SimpleNamespace(wait=lambda seconds: True)

    assert miner._TwitchChannelPointsMiner__wait_for_next_pass() is True


def test_record_that_predates_an_opened_window_is_read_again(monkeypatch):
    # Listed and claimed minutes before the next window: when the refresh
    # scheduled for that window comes, the record is recent, and still shows
    # the claimed repeat with its minutes in.
    twitch, _, listing_calls = evaluation_twitch(
        watch_group(progress(minutes=5, grants=1, window=open_window()))
    )
    twitch.twitchdrops_app_upcoming_starts = {}
    twitch._Twitch__remember_watch_reward_campaign(
        convert(
            watch_group(
                progress(
                    status="CLAIMABLE",
                    minutes=60,
                    window=open_window(),
                    next_at=stamp(NOW + timedelta(minutes=5)),
                )
            )
        )
    )
    assert evaluate(monkeypatch, twitch, [listing()])[0] == {}
    assert listing_calls == []

    # The window opens.
    [drop] = twitch.watch_reward_campaigns["quest-1"]["timeBasedDrops"]
    drop["_reward_group"]["next_start"] = stamp(NOW - timedelta(seconds=30))

    assert set(evaluate(monkeypatch, twitch, [listing()])[0]) == {"example-game"}
    assert listing_calls == ["4242"]


def test_first_read_no_channel_answered_is_made_again_when_the_next_is_due(
    monkeypatch,
):
    # Seen for the first time, and the channels asked list nothing. Nothing
    # is watched for the campaign until one reports it, so no listing comes
    # along either, and the ordinary category refresh can be hours off.
    twitch, _, _ = evaluation_twitch(
        watch_group(progress(minutes=5)), silent_channels={"1", "2", "3"}
    )
    twitch.watch_reward_pending_starts = set()

    assert evaluate(monkeypatch, twitch, [listing()], channels=("1", "2", "3"))[0] == {}

    [retry_at] = twitch.watch_reward_pending_starts
    assert (retry_at - utc_now()).total_seconds() == pytest.approx(
        WATCH_REWARD_PROGRESS_REFRESH_SECONDS, abs=5
    )

    # A read that is not due yet adds no second retry.
    evaluate(monkeypatch, twitch, [listing()], channels=("1", "2", "3"))
    assert twitch.watch_reward_pending_starts == {retry_at}


def test_first_read_is_not_made_again_early_for_as_long_as_the_campaign_runs(
    monkeypatch,
):
    # Channels may leave a campaign out for good (one the account already
    # finished looks no different), and each early read costs a category
    # refresh: after a few it is left to the ordinary one.
    twitch, _, listing_calls = evaluation_twitch(
        watch_group(progress(minutes=5)), silent_channels={"1", "2", "3"}
    )
    details = None
    noted = []
    for _ in range(WATCH_REWARD_UNREAD_RETRIES + 2):
        twitch.watch_reward_pending_starts = set()
        evaluate(monkeypatch, twitch, [listing()], channels=("1", "2", "3"))
        noted.append(len(twitch.watch_reward_pending_starts))
        details = twitch.watch_reward_campaigns["quest-1"]["_watch_reward"]
        details["refresh_attempted_at"] -= 3600

    assert noted == [1] * WATCH_REWARD_UNREAD_RETRIES + [0, 0]
    # The ordinary refresh still reads it.
    assert len(listing_calls) > 2 * WATCH_REWARD_UNREAD_RETRIES


def test_first_read_with_no_channel_live_is_left_to_the_ordinary_refresh(
    monkeypatch,
):
    twitch, _, listing_calls = evaluation_twitch(watch_group(progress(minutes=5)))
    twitch.watch_reward_pending_starts = set()

    assert evaluate(monkeypatch, twitch, [listing()], channels=())[0] == {}

    assert listing_calls == []
    assert twitch.watch_reward_pending_starts == set()


def test_read_for_an_opened_window_is_retried_when_no_channel_answers(monkeypatch):
    twitch, _, _ = evaluation_twitch(
        watch_group(progress(minutes=5, grants=1, window=open_window())),
        silent_channels=("9",),
    )
    twitch._Twitch__remember_watch_reward_campaign(
        convert(
            watch_group(progress(status="CLAIMABLE", minutes=60, window=open_window()))
        )
    )
    [drop] = twitch.watch_reward_campaigns["quest-1"]["timeBasedDrops"]
    # The window after the claimed repeat has opened, and its opening was
    # acted on.
    drop["_reward_group"]["next_start"] = stamp(NOW - timedelta(seconds=30))
    twitch.watch_reward_pending_starts = set()

    assert evaluate(monkeypatch, twitch, [listing()], channels=("9",))[0] == {}

    # Looked for again when the next read is due, not at the ordinary
    # refresh hours later.
    [retry_at] = twitch.watch_reward_pending_starts
    assert (retry_at - utc_now()).total_seconds() == pytest.approx(
        WATCH_REWARD_PROGRESS_REFRESH_SECONDS, abs=5
    )

    # A read that is not due yet adds no second retry.
    evaluate(monkeypatch, twitch, [listing()], channels=("9",))
    assert twitch.watch_reward_pending_starts == {retry_at}


def test_repeat_whose_wait_ended_is_looked_for_again_when_nobody_is_live(
    monkeypatch,
):
    # Earned for this window and waiting for the next: the wait is the
    # drop's own start. When it comes, no channel is live in the category.
    twitch, _, listing_calls = evaluation_twitch(watch_group(progress(grants=1)))
    twitch._Twitch__remember_watch_reward_campaign(
        convert(
            watch_group(progress(grants=1, next_at=stamp(NOW + timedelta(minutes=5))))
        )
    )
    stored = twitch.watch_reward_campaigns["quest-1"]
    [drop] = stored["timeBasedDrops"]
    assert drop["startAt"] == stamp(NOW + timedelta(minutes=5))
    drop["startAt"] = stamp(NOW - timedelta(seconds=30))
    stored["_watch_reward"]["read_at"] -= 600
    twitch.watch_reward_pending_starts = set()

    evaluate(monkeypatch, twitch, [listing()], channels=())

    [retry_at] = twitch.watch_reward_pending_starts
    assert (retry_at - utc_now()).total_seconds() == pytest.approx(
        WATCH_REWARD_PROGRESS_REFRESH_SECONDS, abs=5
    )
    assert listing_calls == []


def window_ending_in(minutes):
    return {
        "startedAt": stamp(NOW - timedelta(hours=23)),
        "expiresAt": stamp(NOW + timedelta(minutes=minutes)),
    }


def record_whose_window_just_closed(twitch):
    """Store a repeat one minute short with two minutes of its window left,
    then let the window close: the record is three minutes old."""
    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(minutes=59, window=window_ending_in(2))))
    )
    stored = twitch.watch_reward_campaigns["quest-1"]
    [drop] = stored["timeBasedDrops"]
    assert drop["endAt"] == stamp(NOW + timedelta(minutes=2))
    drop["endAt"] = stamp(NOW - timedelta(minutes=1))
    stored["_watch_reward"]["read_at"] -= 180
    twitch.watch_reward_pending_starts = set()


def test_repeat_whose_window_closed_unfinished_is_read_again(monkeypatch):
    # The record is recent, but all it knows is a window that is over. The
    # repeat starts over in the next one.
    twitch, _, listing_calls = evaluation_twitch(watch_group(progress(minutes=0)))
    record_whose_window_just_closed(twitch)

    assert set(evaluate(monkeypatch, twitch, [listing()])[0]) == {"example-game"}
    assert listing_calls == ["4242"]


def test_repeat_whose_window_closed_is_looked_for_again_when_nobody_is_live(
    monkeypatch,
):
    twitch, _, _ = evaluation_twitch(watch_group(progress(minutes=0)))
    record_whose_window_just_closed(twitch)

    assert evaluate(monkeypatch, twitch, [listing()], channels=())[0] == {}

    [retry_at] = twitch.watch_reward_pending_starts
    assert (retry_at - utc_now()).total_seconds() == pytest.approx(
        WATCH_REWARD_PROGRESS_REFRESH_SECONDS, abs=5
    )


def test_stored_claim_is_sent_with_its_id_although_another_thread_took_it(
    monkeypatch,
):
    # Another claimer's accepted claim takes the claim ID out of the shared
    # record between this one's check and its reading of the drop.
    gql, _ = channel_listing()
    twitch = bare_twitch(gql)
    monkeypatch.setattr(
        Twitch, "_Twitch__drop_variant_entries_from_drop", lambda self, drop: []
    )
    stored = twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(status="CLAIMABLE", minutes=60, grants=1)))
    )
    [stored_drop] = stored["timeBasedDrops"]

    def drop_built_as_the_id_is_taken(drop_dict):
        stored_drop["self"]["dropInstanceID"] = None
        return Drop(drop_dict)

    monkeypatch.setattr(twitch_module, "Drop", drop_built_as_the_id_is_taken)

    twitch._Twitch__claim_waiting_watch_rewards_by_deadline()

    assert gql.claims == ["viewer-1#quest-1#group-watch"]


def test_campaign_ending_is_not_a_window_opening():
    twitch = bare_twitch()
    campaign = convert(watch_group(progress(minutes=5)))
    campaign["endAt"] = stamp(NOW - timedelta(minutes=1))
    for drop in campaign["timeBasedDrops"]:
        drop["endAt"] = campaign["endAt"]
    stored = twitch._Twitch__remember_watch_reward_campaign(campaign)
    stored["_watch_reward"]["read_at"] -= 180

    assert twitch._Twitch__watch_reward_windows_opened(stored) == []


def drops_listing_twitch(progress_query):
    """Twitch whose channel progress query is `progress_query` and whose
    drops listing, asked in its place, names one Drop campaign."""
    drop_campaign = {"id": "drop-campaign", "name": "Some Drops"}
    gql = SimpleNamespace(
        get_available_drops=lambda channel_id: SimpleNamespace(
            campaigns=[drop_campaign], campaigns_available=True
        )
    )
    if progress_query is not None:
        gql.get_channel_drop_campaigns_progress = progress_query
    twitch = bare_twitch(gql)
    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(minutes=5)))
    )
    return twitch


def failing_progress_query(channel_id):
    raise RetryError("DropChannelCampaignsProgress", [])


def null_progress_query(channel_id):
    return SimpleNamespace(campaigns=[], campaigns_available=False)


@pytest.mark.parametrize(
    "progress_query", [failing_progress_query, null_progress_query]
)
def test_drops_listing_asked_instead_still_counts_the_stored_reward_campaign(
    progress_query,
):
    # The listing has the channel's Drop campaigns only. Without the stored
    # campaign the channel would read as not running the quest for as long
    # as the progress query fails.
    twitch = drops_listing_twitch(progress_query)

    campaigns, available, answered = twitch._Twitch__get_campaigns_from_channel_id("1")

    assert [campaign["id"] for campaign in campaigns] == ["drop-campaign", "quest-1"]
    assert campaigns[1] is twitch.watch_reward_campaigns["quest-1"]
    assert (available, answered) == (True, True)


def test_drops_listing_while_the_progress_query_is_retired_counts_it_too():
    twitch = drops_listing_twitch(
        lambda channel_id: pytest.fail("not asked while it is retired")
    )
    twitch.channel_progress_query_retired_until = time.time() + 600

    campaigns, _, _ = twitch._Twitch__get_campaigns_from_channel_id("1")

    assert [campaign["id"] for campaign in campaigns] == ["drop-campaign", "quest-1"]


def test_drops_listing_leaves_out_a_stored_reward_campaign_named_as_closed():
    # Twitch's account-wide listing named it as closed ahead of its end
    # date; only the stored record would say it counts on this channel.
    twitch = drops_listing_twitch(failing_progress_query)
    twitch.watch_reward_campaigns["quest-1"]["_watch_reward"]["named_closed"] = True

    campaigns, _, _ = twitch._Twitch__get_campaigns_from_channel_id("1")

    assert [campaign["id"] for campaign in campaigns] == ["drop-campaign"]


def test_drops_listing_leaves_out_a_stored_reward_campaign_that_is_over():
    twitch = drops_listing_twitch(failing_progress_query)
    twitch.watch_reward_campaigns["quest-1"]["endAt"] = "2020-02-01T00:00:00Z"

    campaigns, _, _ = twitch._Twitch__get_campaigns_from_channel_id("1")

    assert [campaign["id"] for campaign in campaigns] == ["drop-campaign"]


def test_start_the_record_was_read_after_is_not_an_opening(monkeypatch):
    # The campaign's own start, long past, or a window the last listing
    # already reported open: an empty category is left to the ordinary
    # refresh, as for any campaign.
    twitch, _, _ = evaluation_twitch(watch_group(progress(minutes=5)))
    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(minutes=5)))
    )
    stored = twitch.watch_reward_campaigns["quest-1"]
    stored["_watch_reward"]["read_at"] -= 2 * WATCH_REWARD_PROGRESS_REFRESH_SECONDS
    twitch.watch_reward_pending_starts = set()

    evaluate(monkeypatch, twitch, [listing()], channels=())

    assert twitch.watch_reward_pending_starts == set()


def other_campaign(group_self):
    """A second reward campaign, converted."""
    group = watch_group(group_self)
    group["id"] = "group-two"
    return campaign_from_reward_groups(
        reward_campaign(group) | {"id": "quest-2"}, now=NOW
    )


def test_claim_thread_also_claims_what_was_listed_while_it_was_busy(monkeypatch):
    monkeypatch.setattr(
        Twitch, "_Twitch__drop_variant_entries_from_drop", lambda self, drop: []
    )
    claimable = progress(status="CLAIMABLE", minutes=60, window=open_window())
    gql, _ = channel_listing()
    twitch = bare_twitch(gql)
    accept = gql.claim_drop_rewards

    def claim_while_another_is_listed(instance_id):
        if not gql.claims:
            # Another thread's listing: the claim thread is busy, so no
            # second one is started for it.
            twitch._Twitch__store_listed_watch_rewards([other_campaign(claimable)])
            assert len(InlineThread.started) == 1
        return accept(instance_id)

    gql.claim_drop_rewards = claim_while_another_is_listed

    twitch._Twitch__store_listed_watch_rewards([convert(watch_group(claimable))])

    assert gql.claims == [
        "viewer-1#quest-1#group-watch",
        "viewer-1#quest-2#group-two",
    ]
    # Done: the next claimable listing starts a thread again.
    assert twitch.watch_reward_claim_thread is None


def test_claim_thread_claims_every_stored_campaign(monkeypatch):
    # Two claimable campaigns in one listing: the thread started for the
    # first is still running when the second is stored.
    monkeypatch.setattr(twitch_module, "Thread", HeldThread)
    monkeypatch.setattr(
        Twitch, "_Twitch__drop_variant_entries_from_drop", lambda self, drop: []
    )
    claimable = progress(status="CLAIMABLE", minutes=60, window=open_window())
    gql, _ = channel_listing()
    twitch = bare_twitch(gql)

    twitch._Twitch__store_listed_watch_rewards(
        [convert(watch_group(claimable)), other_campaign(claimable)]
    )
    [thread] = InlineThread.started
    thread.target()

    assert gql.claims == [
        "viewer-1#quest-1#group-watch",
        "viewer-1#quest-2#group-two",
    ]


def test_claim_thread_sends_no_further_claim_once_the_miner_is_stopping(
    monkeypatch,
):
    # Shutdown waits for this thread; what is left is claimed next run.
    monkeypatch.setattr(twitch_module, "Thread", HeldThread)
    monkeypatch.setattr(
        Twitch, "_Twitch__drop_variant_entries_from_drop", lambda self, drop: []
    )
    claimable = progress(status="CLAIMABLE", minutes=60, window=open_window())
    gql, _ = channel_listing()
    twitch = bare_twitch(gql)
    accept = gql.claim_drop_rewards

    def claim_as_the_miner_stops(instance_id):
        twitch.running = False
        # Another listing asks for claims meanwhile.
        twitch.watch_reward_claim_requested = True
        return accept(instance_id)

    gql.claim_drop_rewards = claim_as_the_miner_stops
    twitch._Twitch__store_listed_watch_rewards(
        [convert(watch_group(claimable)), other_campaign(claimable)]
    )
    [thread] = InlineThread.started
    thread.target()

    assert gql.claims == ["viewer-1#quest-1#group-watch"]
    assert twitch.watch_reward_claim_thread is None


def test_no_further_group_of_a_campaign_is_claimed_once_the_miner_is_stopping(
    monkeypatch,
):
    # Two groups of one campaign wait to be claimed. Shutdown waits for the
    # claim being sent, not for one started after it.
    monkeypatch.setattr(
        Twitch, "_Twitch__drop_variant_entries_from_drop", lambda self, drop: []
    )
    gql, _ = channel_listing()
    twitch = bare_twitch(gql)
    accept = gql.claim_drop_rewards

    def claim_as_the_miner_stops(instance_id):
        twitch.running = False
        return accept(instance_id)

    gql.claim_drop_rewards = claim_as_the_miner_stops
    claimable = progress(status="CLAIMABLE", minutes=60)

    twitch._Twitch__store_listed_watch_rewards(
        [convert(*two_category_groups(claimable, claimable))]
    )

    assert gql.claims == ["viewer-1#quest-1#group-watch"]


def test_slow_thread_does_not_publish_its_older_record_to_the_progress_cache(
    monkeypatch,
):
    # Thread A stores its listing and is overtaken, before it updates the
    # cache, by thread B storing and publishing a newer one.
    twitch = bare_twitch()
    twitch._Twitch__cache_drop_inventory_progress({})
    publish = Twitch._Twitch__publish_watch_reward_progress
    overtaken = []

    def publish_after_being_overtaken(self, record):
        if not overtaken:
            overtaken.append(True)
            twitch._Twitch__remember_watch_reward_campaign(
                convert(watch_group(progress(minutes=22, window=open_window())))
            )
        return publish(self, record)

    monkeypatch.setattr(
        Twitch,
        "_Twitch__publish_watch_reward_progress",
        publish_after_being_overtaken,
    )

    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(minutes=12, window=open_window())))
    )

    [row] = twitch.drop_inventory_progress["example-game"]
    assert row[4] == 22


def test_sync_claims_a_reward_group_under_the_category_it_is_filed_in(monkeypatch):
    # The local campaign is named for its first category; the group that
    # the sync finds claimable counts only in the second.
    claims = []
    twitch = bare_twitch()
    monkeypatch.setattr(
        Twitch, "_Twitch__get_inventory", lambda self: {"dropCampaignsInProgress": []}
    )
    monkeypatch.setattr(
        Twitch, "_Twitch__save_drop_snapshot_from_dict", lambda self, *a, **k: None
    )
    monkeypatch.setattr(
        Twitch,
        "claim_drop",
        lambda self, drop, **kwargs: claims.append(
            (drop.id, kwargs.get("category_name_override"))
        )
        or True,
    )
    stored = twitch._Twitch__remember_watch_reward_campaign(
        convert(
            *two_category_groups(
                progress(minutes=5, window=open_window()),
                progress(status="CLAIMABLE", minutes=60, window=open_window()),
            )
        )
    )
    campaign = Campaign(copy.deepcopy(stored))
    assert campaign.game["displayName"] == "Example Game"

    twitch._Twitch__sync_campaigns([campaign])

    assert claims == [("group-other#0", "Other Game")]


def test_window_that_starts_over_is_reported_from_its_own_minutes():
    # The window closed at 40 of 60 minutes: no repeat granted, so the next
    # one counts from zero under the same drop ID. Progress is only saved
    # when it rises.
    twitch = bare_twitch()
    twitch.drop_progress_last_saved = {
        "group-watch#0": 40,
        "group-watch#0|art|Daily Chest|Launch Celebration|Example Game|Chest|": 40,
        "group-watch#01|unrelated": 7,
        "other-drop": 30,
    }
    twitch.drop_status_last_saved = {
        "group-watch#0|art|Daily Chest|Launch Celebration|Example Game|Chest|": (
            "in_progress"
        )
    }
    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(minutes=40, window=open_window())))
    )
    assert len(twitch.drop_progress_last_saved) == 4

    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(minutes=5, window=open_window())))
    )

    assert twitch.drop_progress_last_saved == {
        "group-watch#01|unrelated": 7,
        "other-drop": 30,
    }
    assert twitch.drop_status_last_saved == {}


def snapshot_twitch():
    twitch = bare_twitch()
    twitch.drop_progress_last_saved = {}
    twitch.drop_status_last_saved = {}
    return twitch


def test_snapshot_from_before_a_window_started_over_is_not_saved():
    # The sync copied the record at 40 minutes; a listing then reported the
    # window started over. Saved now, the copy's 40 minutes would keep the
    # new window's out of the report until they passed them.
    twitch = snapshot_twitch()
    key = "group-watch#0|art|Daily Chest|Launch Celebration|Example Game|Chest|"
    for minutes in (40, 5):
        twitch._Twitch__remember_watch_reward_campaign(
            convert(watch_group(progress(minutes=minutes, window=open_window())))
        )
    written = []

    def save(minutes):
        return twitch._Twitch__save_drop_snapshot_if_new(
            key, minutes, "in_progress", lambda: written.append(minutes)
        )

    assert save(40) is False
    assert twitch.drop_progress_last_saved == {}
    assert save(5) is True
    assert save(5) is False
    assert twitch.drop_progress_last_saved == {key: 5}
    assert written == [5]

    # The minute watcher's copy likewise.
    def save_watched(minutes):
        drop = SimpleNamespace(id="group-watch#0", current_minutes_watched=minutes)
        return twitch._Twitch__save_watched_drop_progress_if_new(
            drop, lambda: written.append(("watched", minutes))
        )

    assert save_watched(40) is None
    assert save_watched(5) == -1
    assert save_watched(5) is None
    assert written == [5, ("watched", 5)]

    # A drop that is no reward campaign's is saved as ever.
    assert twitch._Twitch__save_drop_snapshot_if_new(
        "other-drop|art", 40, "in_progress", lambda: None
    )


def test_failed_snapshot_of_the_window_before_does_not_replace_the_new_one():
    # The sync copied the record at 5 minutes. Listings since took the
    # window to 40, saw it start over, and have the new one at 6. Looked at
    # now, the copy's deadline has passed: "failed", with fewer minutes than
    # the record, which the check for an older window's minutes lets by.
    twitch = snapshot_twitch()
    for minutes in (5, 40, 6):
        twitch._Twitch__remember_watch_reward_campaign(
            convert(watch_group(progress(minutes=minutes, window=open_window())))
        )

    def save(minutes, status):
        return twitch._Twitch__save_drop_snapshot_if_new(
            "group-watch#0|art", minutes, status, lambda: None
        )

    assert save(6, "in_progress") is True
    assert save(5, "failed") is False
    assert twitch.drop_status_last_saved == {"group-watch#0|art": "in_progress"}

    # The record's own deadline passed with nothing newer listed: failed.
    [drop] = twitch.watch_reward_campaigns["quest-1"]["timeBasedDrops"]
    drop["endAt"] = stamp(utc_now() - timedelta(minutes=1))
    assert save(6, "failed") is True


def test_copy_from_before_a_window_started_over_stays_out_over_later_listings():
    twitch = snapshot_twitch()
    for minutes in (40, 5, 6, 7):
        twitch._Twitch__remember_watch_reward_campaign(
            convert(watch_group(progress(minutes=minutes, window=open_window())))
        )

    def save(minutes):
        return twitch._Twitch__save_drop_snapshot_if_new(
            "group-watch#0|art", minutes, "in_progress", lambda: None
        )

    assert save(40) is False
    assert save(7) is True


def test_minutes_ahead_of_the_stored_record_are_progress_not_an_older_copy():
    # The minute watcher can learn of minutes before the next listing does.
    # Only a window that just started over makes more minutes than the
    # record has a sign of an older copy.
    twitch = snapshot_twitch()
    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(minutes=30, window=open_window())))
    )
    watched = SimpleNamespace(id="group-watch#0", current_minutes_watched=35)

    assert twitch._Twitch__save_watched_drop_progress_if_new(watched, lambda: None) == (
        -1
    )
    assert twitch._Twitch__save_drop_snapshot_if_new(
        "group-watch#0|art", 36, "in_progress", lambda: None
    )


def test_minutes_ahead_of_the_record_count_again_some_time_after_a_restart():
    twitch = snapshot_twitch()
    for minutes in (40, 5):
        twitch._Twitch__remember_watch_reward_campaign(
            convert(watch_group(progress(minutes=minutes, window=open_window())))
        )
    details = twitch.watch_reward_campaigns["quest-1"]["_watch_reward"]
    details["restarted_at"] = {
        "group-watch#0": time.time() - WATCH_REWARD_STALE_COPY_SECONDS - 1
    }

    assert twitch._Twitch__save_drop_snapshot_if_new(
        "group-watch#0|art", 9, "in_progress", lambda: None
    )
    # And the mark is not carried into the next record.
    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(minutes=6, window=open_window())))
    )
    details = twitch.watch_reward_campaigns["quest-1"]["_watch_reward"]
    assert details["restarted_at"] == {}


def test_stored_record_is_never_grown_in_place(monkeypatch):
    # Other threads deep-copy the record without a lock. A key or a set
    # member added meanwhile fails the copy ("changed size during
    # iteration"), so later writes only replace values that were there.
    twitch, _, _ = evaluation_twitch(
        watch_group(progress(minutes=12)), silent_channels={"1", "2"}
    )
    stored = twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(minutes=12)))
    )
    details = stored["_watch_reward"]
    keys = set(details)
    unanswered = details["refresh_channels_unanswered"]
    assert isinstance(unanswered, frozenset)

    # A read that asks channels, records them as unanswered, moves the
    # category offset on and re-checks the categories.
    details["read_at"] -= 3601
    evaluate(monkeypatch, twitch, [listing()], channels=("1", "2"))

    assert twitch.watch_reward_campaigns["quest-1"] is stored
    assert set(details) == keys
    assert details["refresh_channels_unanswered"] == {"1", "2"}
    assert unanswered == frozenset()
    assert details["categories_checked_at"] and details["refresh_attempted_at"]


def test_record_never_read_is_not_grown_in_place_by_a_read_no_channel_answers(
    monkeypatch,
):
    # The same for the count of the reads made early for it.
    twitch, _, _ = evaluation_twitch(
        watch_group(progress(minutes=5)), silent_channels={"1", "2", "3"}
    )
    evaluate(monkeypatch, twitch, [listing()], channels=())
    details = twitch.watch_reward_campaigns["quest-1"]["_watch_reward"]
    assert details["progress_known"] is False
    keys = set(details)

    details["refresh_attempted_at"] -= 3600
    evaluate(monkeypatch, twitch, [listing()], channels=("1", "2", "3"))

    assert twitch.watch_reward_campaigns["quest-1"]["_watch_reward"] is details
    assert details["unread_retries"] == 1
    assert set(details) == keys


def test_counted_repeat_of_a_pay_once_group_leaves_the_next_its_minutes_to_earn():
    # Twitch names neither a window nor when the group can be earned again,
    # and still shows the minutes of the repeat it counted.
    for status in ("IN_PROGRESS", "CLAIMABLE"):
        [drop] = convert(
            all_repeats_group(progress(status=status, minutes=60, grants=1))
        )["timeBasedDrops"]

        assert drop["id"] == "group-watch#1"
        assert drop["self"]["currentMinutesWatched"] == 0
        assert drop["self"]["isClaimed"] is False
        assert drop["_reward_group"]["claimable"] is False


def test_failed_snapshot_of_a_drop_since_earned_is_not_saved():
    # The reward also needed a subscription. The sync copied it with the
    # minutes in and the subscription unmet, and looks at its copy after
    # the deadline: "failed", with the minutes the earned record has.
    twitch = snapshot_twitch()
    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(status="CLAIMED"), times=1))
    )

    assert not twitch._Twitch__save_drop_snapshot_if_new(
        "group-watch|art", 60, "failed", lambda: None
    )
    assert twitch._Twitch__save_drop_snapshot_if_new(
        "group-watch|art", 60, "captured", lambda: None
    )


def test_first_read_is_made_again_when_a_categorys_directory_name_is_not_had(
    monkeypatch,
):
    # The request for the name the directory goes by failed: no channel
    # could be asked, which is not the same as none being live.
    twitch, _, _ = evaluation_twitch(watch_group(progress(minutes=5)))
    evaluate(monkeypatch, twitch, [listing()], channels=())
    stored = twitch.watch_reward_campaigns["quest-1"]
    stored["_watch_reward"]["refresh_attempted_at"] -= 3600
    twitch.watch_reward_pending_starts = set()
    monkeypatch.setattr(
        Twitch, "_Twitch__get_game_directory_slug", lambda self, game_id: None
    )

    twitch._Twitch__refresh_watch_reward_progress(stored, {"example-game"})

    assert len(twitch.watch_reward_pending_starts) == 1


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="needs time.tzset")
def test_claims_keep_their_order_across_a_change_of_the_clocks(monkeypatch):
    # Twitch's moments are UTC. Read as local time, 02:50 falls in the hour
    # the clocks skip in Denver that night and lands after 03:10.
    monkeypatch.setattr(
        Twitch, "_Twitch__drop_variant_entries_from_drop", lambda self, drop: []
    )
    claimable = progress(status="CLAIMABLE", minutes=60, window=open_window())
    gql, _ = channel_listing()
    twitch = bare_twitch(gql)
    twitch._Twitch__remember_watch_reward_campaign(convert(watch_group(claimable)))
    twitch._Twitch__remember_watch_reward_campaign(other_campaign(claimable))
    twitch.watch_reward_campaigns["quest-1"]["endAt"] = "2027-03-14T03:10:00Z"
    twitch.watch_reward_campaigns["quest-2"]["endAt"] = "2027-03-14T02:50:00Z"
    monkeypatch.setattr(twitch_module, "utc_now", lambda: datetime(2027, 3, 14, 2, 0))
    try:
        with monkeypatch.context() as local_time:
            local_time.setenv("TZ", "America/Denver")
            time.tzset()
            claim_on_the_claim_thread(twitch)
    finally:
        time.tzset()

    assert gql.claims == [
        "viewer-1#quest-2#group-two",
        "viewer-1#quest-1#group-watch",
    ]


def test_snapshot_of_a_group_that_is_over_is_saved_with_the_minutes_it_got_to():
    # Nothing is watched for it any more, so no newer minutes are held back.
    twitch = snapshot_twitch()
    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(status="EXPIRED")))
    )

    assert twitch._Twitch__save_drop_snapshot_if_new(
        "group-watch#0|art", 40, "failed", lambda: None
    )


@pytest.mark.parametrize(
    "group_self",
    [
        progress(status="CLAIMABLE", minutes=60, grants=1),
        progress(status="CLAIMED", grants=6),
    ],
)
def test_snapshot_from_before_a_reward_was_earned_is_not_saved(group_self):
    # The sync copied the record at 59 minutes; the reward was then listed
    # as earned and its claim reported. The copy would put it back to in
    # progress, for good if nothing reports the drop again.
    twitch = snapshot_twitch()
    twitch._Twitch__remember_watch_reward_campaign(convert(watch_group(group_self)))
    [drop] = twitch.watch_reward_campaigns["quest-1"]["timeBasedDrops"]
    key = f"{drop['id']}|art"

    def save(minutes, status):
        return twitch._Twitch__save_drop_snapshot_if_new(
            key, minutes, status, lambda: None
        )

    assert save(59, "in_progress") is False
    # The minute watcher reports in progress until its own copy of the drop
    # is told of the claim, with every minute in or not.
    for minutes in (59, 60):
        watched = SimpleNamespace(id=drop["id"], current_minutes_watched=minutes)
        assert (
            twitch._Twitch__save_watched_drop_progress_if_new(watched, lambda: None)
            is None
        )
    assert save(60, "captured") is True


def test_progress_snapshot_of_a_repeat_before_the_stored_one_is_not_saved():
    # The record has moved on to the next repeat: the one before is over,
    # claimed or lost, and its row is not put back to in progress.
    twitch = snapshot_twitch()
    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(minutes=5, grants=2)))
    )

    def save(drop_id, status="in_progress"):
        return twitch._Twitch__save_drop_snapshot_if_new(
            f"{drop_id}|art", 59, status, lambda: None
        )

    assert save("group-watch#1") is False
    watched = SimpleNamespace(id="group-watch#0", current_minutes_watched=59)
    assert (
        twitch._Twitch__save_watched_drop_progress_if_new(watched, lambda: None) is None
    )
    # Nor to failed: a copy taken before the repeat was counted reads as
    # that once its window has closed.
    assert save("group-watch#1", "failed") is False
    # Its claim, reported late, still is; so is a drop of another group.
    assert save("group-watch#1", "captured") is True
    assert save("group-watch-two#1") is True
    assert save("group-watch#x") is True


def test_snapshot_is_written_in_order_without_the_lock_records_are_read_under():
    # Approved before a listing reports the window started over and written
    # after it, an older copy's minutes would replace the new window's in
    # the report: the write is made under the report-order lock, which a
    # listing takes too. Not under the lock the records are read under,
    # which a stalled write would keep from the claim thread.
    twitch = snapshot_twitch()
    records = twitch_module._watch_reward_lock(twitch)
    order = twitch_module._drop_report_order_lock(twitch)
    held = []
    drop = SimpleNamespace(id="drop-1", current_minutes_watched=3)

    def save():
        held.append((order.locked(), records.locked()))

    assert twitch._Twitch__save_drop_snapshot_if_new(
        "drop-1|art", 3, "in_progress", save
    )
    assert twitch._Twitch__save_watched_drop_progress_if_new(drop, save) == (-1)
    assert held == [(True, False), (True, False)]
    assert (order.locked(), records.locked()) == (False, False)

    # Nor is either kept by a write that fails.
    def fail():
        raise OSError("disk full")

    drop.current_minutes_watched = 4
    with pytest.raises(OSError):
        twitch._Twitch__save_drop_snapshot_if_new("drop-1|art", 4, "in_progress", fail)
    with pytest.raises(OSError):
        twitch._Twitch__save_watched_drop_progress_if_new(drop, fail)
    assert (order.locked(), records.locked()) == (False, False)


class StalledWrite:
    """A report row whose write does not return until the test lets it:
    started on a thread of its own with one of the two methods that save
    a row. `writing` is set once the write has begun."""

    def __init__(self, twitch, watcher, drop_id="other-drop", minutes=3):
        self.writing = threading.Event()
        self.finish = threading.Event()
        self.written = []
        if watcher:
            drop = SimpleNamespace(id=drop_id, current_minutes_watched=minutes)
            target = functools.partial(
                twitch._Twitch__save_watched_drop_progress_if_new, drop, self.save
            )
        else:
            target = functools.partial(
                twitch._Twitch__save_drop_snapshot_if_new,
                f"{drop_id}|art",
                minutes,
                "in_progress",
                self.save,
            )
        self.minutes = minutes
        self.thread = threading.Thread(target=target, daemon=True)

    def save(self):
        self.writing.set()
        self.finish.wait(timeout=30)
        self.written.append(self.minutes)

    def start(self):
        self.thread.start()
        assert self.writing.wait(timeout=5)

    def end(self):
        self.finish.set()
        self.thread.join(timeout=5)
        assert not self.thread.is_alive()


def own_locks(twitch):
    # Its own, not the ones every Twitch built without __init__ shares: a
    # thread a failed test leaves waiting would hold up the tests after it.
    twitch.watch_reward_lock = threading.Lock()
    twitch.drop_report_order_lock = threading.Lock()
    return twitch


@pytest.mark.parametrize("watcher", [False, True])
def test_stalled_report_write_does_not_keep_a_claim_from_being_sent_again(
    monkeypatch, watcher
):
    # Twitch refused the claim, and it is due again shortly before the
    # campaign ends; the window of a repeat has opened too. A row of the
    # report, of any drop, is being written to a disk that does not answer:
    # the main loop still sees both moments have come and starts the claim
    # thread, and the claim thread still sends the claim.
    twitch, gql = refused_claim_twitch(
        monkeypatch,
        convert(watch_group(progress(status="CLAIMABLE", minutes=60), times=1)),
    )
    own_locks(twitch)
    twitch.drop_progress_last_saved = {}
    twitch.drop_status_last_saved = {}
    assert claim_retries(twitch)
    assert twitch.watch_reward_claim_thread is None
    for campaign_id in twitch.watch_reward_claim_retries:
        twitch.watch_reward_claim_retries[campaign_id] = utc_now() - timedelta(
            seconds=1
        )
    twitch.watch_reward_pending_starts = {utc_now() - timedelta(seconds=1)}
    sent = threading.Event()

    def accept(instance_id):
        answer = gql.accept(instance_id)
        sent.set()
        return answer

    gql.claim_drop_rewards = accept
    monkeypatch.setattr(twitch_module, "Thread", threading.Thread)
    seen = []
    claim_threads = []

    def main_loop_pass():
        # What the main loop calls while it waits between passes.
        seen.append(twitch.watch_reward_start_due())
        seen.append(twitch.take_opened_watch_reward_start())
        twitch.start_unattended_watch_reward_claims()
        claim_threads.append(twitch.watch_reward_claim_thread)

    main_loop = threading.Thread(target=main_loop_pass, daemon=True)
    write = StalledWrite(twitch, watcher)
    write.start()
    try:
        main_loop.start()
        main_loop.join(timeout=5)
        claimed_meanwhile = sent.wait(timeout=5)
        still_writing = write.written == []
    finally:
        write.end()
    for claim_thread in claim_threads:
        if claim_thread is not None:
            claim_thread.join(timeout=5)
            assert not claim_thread.is_alive()

    assert not main_loop.is_alive()
    assert seen == [True, True]
    assert claim_threads and claim_threads[0] is not None
    assert claimed_meanwhile and still_writing
    assert gql.claims == ["viewer-1#quest-1#group-watch"]
    assert twitch.watch_reward_claim_thread is None


class OrderLock:
    """The report-order lock, telling when a thread comes to wait for it
    while another holds it."""

    def __init__(self):
        self.lock = threading.Lock()
        self.waited_for = threading.Event()

    def __enter__(self):
        if not self.lock.acquire(blocking=False):
            self.waited_for.set()
            self.lock.acquire()

    def __exit__(self, *error):
        self.lock.release()

    def locked(self):
        return self.lock.locked()


def started_over_listing(twitch):
    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(minutes=5, window=open_window())))
    )


def earned_listing(twitch):
    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(minutes=5, grants=1, window=open_window())))
    )


def accepted_claim(twitch):
    twitch._Twitch__mark_drop_accepted(SimpleNamespace(id="group-watch#0"))


@pytest.mark.parametrize("watcher", [False, True])
@pytest.mark.parametrize(
    "change, minutes_after",
    [(started_over_listing, 5), (earned_listing, 5), (accepted_claim, 40)],
)
def test_row_decided_before_a_record_changed_is_written_before_it_does(
    watcher, change, minutes_after
):
    # The sync or the minute watcher has a copy of the record at 40 minutes
    # and is writing its row. A listing that finds the window started over
    # (or the reward earned), and a claim Twitch accepts, wait for the write:
    # they change what the row was decided on, and what follows from them in
    # the report must come after it.
    twitch = own_locks(snapshot_twitch())
    twitch.drop_report_order_lock = OrderLock()
    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(minutes=40, window=open_window())))
    )
    [drop] = twitch.watch_reward_campaigns["quest-1"]["timeBasedDrops"]
    key = "group-watch#0" if watcher else "group-watch#0|art"
    write = StalledWrite(twitch, watcher, drop_id="group-watch#0", minutes=40)
    changed = threading.Thread(target=change, args=(twitch,), daemon=True)
    write.start()
    try:
        changed.start()
        waited = twitch.drop_report_order_lock.waited_for.wait(timeout=5)
        [unchanged] = twitch.watch_reward_campaigns["quest-1"]["timeBasedDrops"]
        saved_meanwhile = dict(twitch.drop_progress_last_saved)
        # What the claim thread and the main loop read is not held up.
        records_free = not twitch.watch_reward_lock.locked()
    finally:
        write.end()
    changed.join(timeout=5)

    assert not changed.is_alive()
    assert waited and records_free
    assert unchanged is drop and saved_meanwhile == {key: 40}
    assert write.written == [40]
    [stored] = twitch.watch_reward_campaigns["quest-1"]["timeBasedDrops"]
    assert stored["self"]["currentMinutesWatched"] == minutes_after
    if change is started_over_listing:
        # Forgotten after the write, so the new window's minutes are saved.
        assert twitch.drop_progress_last_saved == {}
        assert twitch._Twitch__save_drop_snapshot_if_new(
            "group-watch#0|art", 5, "in_progress", lambda: None
        )


def category_retry_twitch(group_self, end_at=END_AT):
    twitch = bare_twitch()
    twitch.watch_reward_pending_starts = set()
    campaign = convert(watch_group(group_self))
    campaign["endAt"] = end_at
    twitch._Twitch__remember_watch_reward_campaign(campaign)
    twitch.watch_reward_pending_starts = set()
    return twitch


def test_failed_channel_lookup_of_a_category_with_a_repeat_to_watch_is_retried():
    twitch = category_retry_twitch(progress(minutes=5, window=open_window()))

    twitch.retry_failed_watch_reward_category({"example-game", None})

    [retry_at] = twitch.watch_reward_pending_starts
    wait = (retry_at - utc_now()).total_seconds()
    assert 0 < wait <= WATCH_REWARD_PROGRESS_REFRESH_SECONDS


@pytest.mark.parametrize(
    "group_self, slugs, end_at",
    [
        # Another category's lookup failed.
        (progress(minutes=5), {"other-game"}, END_AT),
        # Nothing to watch for: waiting to be claimed, earned, not open yet.
        (progress(status="CLAIMABLE", minutes=60, grants=6), {"example-game"}, END_AT),
        (progress(status="CLAIMED", grants=6), {"example-game"}, END_AT),
        (progress(grants=1, next_at="2098-01-01T00:00:00Z"), {"example-game"}, END_AT),
        # The campaign is over.
        (progress(minutes=5), {"example-game"}, "2020-02-01T00:00:00Z"),
    ],
)
def test_failed_channel_lookup_is_left_to_the_ordinary_refresh(
    group_self, slugs, end_at
):
    twitch = category_retry_twitch(group_self, end_at)

    twitch.retry_failed_watch_reward_category(slugs)

    assert twitch.watch_reward_pending_starts == set()


def test_claim_answered_after_the_next_repeat_turned_claimable_is_not_its_claim(
    monkeypatch,
):
    # The answer to a claim is slow. Meanwhile the next repeat is listed in
    # progress and then claimable, with the count of the one claimed.
    monkeypatch.setattr(
        Twitch, "_Twitch__drop_variant_entries_from_drop", lambda self, drop: []
    )
    claimable = watch_group(
        progress(status="CLAIMABLE", minutes=60, window=open_window())
    )
    gql, _ = channel_listing(claimable)
    twitch = bare_twitch(gql)
    accept = gql.claim_drop_rewards
    waiting_after_the_first = []

    def slow_claim(instance_id):
        if not gql.claims:
            for group_self in (progress(minutes=5, window=open_window()), None):
                twitch._Twitch__remember_watch_reward_campaign(
                    convert(watch_group(group_self) if group_self else claimable)
                )
        else:
            [drop] = twitch.watch_reward_campaigns["quest-1"]["timeBasedDrops"]
            waiting_after_the_first.append(
                (
                    drop["id"],
                    drop["self"]["dropInstanceID"],
                    set(twitch.accepted_drop_ids),
                )
            )
        return accept(instance_id)

    gql.claim_drop_rewards = slow_claim

    twitch._Twitch__get_campaigns_from_channel_id("1")

    # The repeat that turned claimable meanwhile kept its claim ID, and so
    # is claimed in its turn: the thread looks again after every claim.
    assert waiting_after_the_first == [
        ("group-watch#1", "viewer-1#quest-1#group-watch", {"group-watch#0"})
    ]
    assert gql.claims == ["viewer-1#quest-1#group-watch"] * 2
    assert twitch.accepted_drop_ids == {"group-watch#0", "group-watch#1"}


def test_claim_answered_after_the_same_repeat_was_listed_again_is_accepted(
    monkeypatch,
):
    monkeypatch.setattr(
        Twitch, "_Twitch__drop_variant_entries_from_drop", lambda self, drop: []
    )
    claimable = watch_group(
        progress(status="CLAIMABLE", minutes=60, window=open_window())
    )
    gql, _ = channel_listing(claimable)
    twitch = bare_twitch(gql)
    accept = gql.claim_drop_rewards

    def slow_claim(instance_id):
        twitch._Twitch__remember_watch_reward_campaign(convert(claimable))
        return accept(instance_id)

    gql.claim_drop_rewards = slow_claim

    twitch._Twitch__get_campaigns_from_channel_id("1")
    twitch._Twitch__get_campaigns_from_channel_id("1")

    assert gql.claims == ["viewer-1#quest-1#group-watch"]
    assert "group-watch#0" in twitch.accepted_drop_ids


def test_claim_thread_leaves_a_campaign_that_is_over_alone(monkeypatch):
    # Its claim kept failing while it ran; sent again now, it would hold
    # the claim lock ahead of rewards that can still be had.
    monkeypatch.setattr(
        Twitch, "_Twitch__drop_variant_entries_from_drop", lambda self, drop: []
    )
    claimable = progress(status="CLAIMABLE", minutes=60, window=open_window())
    gql, _ = channel_listing()
    twitch = bare_twitch(gql)
    twitch._Twitch__remember_watch_reward_campaign(convert(watch_group(claimable)))
    twitch.watch_reward_campaigns["quest-1"]["endAt"] = "2020-06-01T00:00:00Z"

    twitch._Twitch__store_listed_watch_rewards([other_campaign(claimable)])

    assert gql.claims == ["viewer-1#quest-2#group-two"]


def test_claim_thread_sends_the_claim_with_the_least_time_left_first(monkeypatch):
    # The campaign stored first has days left and a claim Twitch keeps
    # refusing, which can take over a minute with its retries; the other
    # ends in under one.
    monkeypatch.setattr(
        Twitch, "_Twitch__drop_variant_entries_from_drop", lambda self, drop: []
    )
    claimable = progress(status="CLAIMABLE", minutes=60, window=open_window())
    gql, _ = channel_listing()
    twitch = bare_twitch(gql)
    twitch._Twitch__remember_watch_reward_campaign(convert(watch_group(claimable)))
    twitch._Twitch__remember_watch_reward_campaign(other_campaign(claimable))
    soon = datetime.now(timezone.utc) + timedelta(seconds=40)
    twitch.watch_reward_campaigns["quest-1"]["endAt"] = "2099-01-01T00:00:00Z"
    twitch.watch_reward_campaigns["quest-2"]["endAt"] = soon.strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    claim_on_the_claim_thread(twitch)

    assert gql.claims == [
        "viewer-1#quest-2#group-two",
        "viewer-1#quest-1#group-watch",
    ]


def test_listing_is_stored_whole_before_its_first_claim_is_sent(monkeypatch):
    # The thread starts with the first claimable campaign of the listing.
    # Started before the second was stored, it would send the first one's
    # claim ahead of the one that ends in under a minute.
    monkeypatch.setattr(
        Twitch, "_Twitch__drop_variant_entries_from_drop", lambda self, drop: []
    )
    claimable = progress(status="CLAIMABLE", minutes=60, window=open_window())
    gql, _ = channel_listing()
    twitch = bare_twitch(gql)
    lasting = convert(watch_group(claimable)) | {"endAt": "2099-01-01T00:00:00Z"}
    ending = other_campaign(claimable) | {
        "endAt": stamp(datetime.now(timezone.utc) + timedelta(seconds=40))
    }

    twitch._Twitch__store_listed_watch_rewards([lasting, ending])

    assert gql.claims == [
        "viewer-1#quest-2#group-two",
        "viewer-1#quest-1#group-watch",
    ]


def test_repeat_about_to_be_followed_is_claimed_before_the_campaigns_other_groups(
    monkeypatch,
):
    # Two groups of one campaign wait for their claim. The next repeat of
    # the second opens in under a minute; the first has until the end.
    monkeypatch.setattr(
        Twitch, "_Twitch__drop_variant_entries_from_drop", lambda self, drop: []
    )
    claimable = progress(status="CLAIMABLE", minutes=60, window=open_window())
    second = watch_group(claimable) | {"id": "group-two", "name": "Weekly Chest"}
    gql, _ = channel_listing()
    twitch = bare_twitch(gql)
    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(claimable), second)
    )
    stored = twitch.watch_reward_campaigns["quest-1"]
    stored["endAt"] = "2099-01-01T00:00:00Z"
    by_group = {drop["_reward_group"]["id"]: drop for drop in stored["timeBasedDrops"]}
    by_group["group-watch"]["_reward_group"]["next_start"] = None
    by_group["group-two"]["_reward_group"]["next_start"] = stamp(
        datetime.now(timezone.utc) + timedelta(seconds=40)
    )

    claim_on_the_claim_thread(twitch)

    assert gql.claims == [
        "viewer-1#quest-1#group-two",
        "viewer-1#quest-1#group-watch",
    ]


def two_waiting_groups_campaign(twitch, second_opens_again_in=None):
    """Store quest-1 with two groups waiting for their claim, lasting for
    years; the next repeat of the second opens in the seconds given."""
    claimable = progress(status="CLAIMABLE", minutes=60, window=open_window())
    second = watch_group(claimable) | {"id": "group-two", "name": "Weekly Chest"}
    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(claimable), second)
    )
    stored = twitch.watch_reward_campaigns["quest-1"]
    stored["endAt"] = "2099-01-01T00:00:00Z"
    for drop in stored["timeBasedDrops"]:
        opens = drop["_reward_group"]["id"] == "group-two" and second_opens_again_in
        drop["_reward_group"]["next_start"] = (
            stamp(datetime.now(timezone.utc) + timedelta(seconds=opens))
            if opens
            else None
        )


def third_campaign(group_self, ends_in):
    group = watch_group(group_self) | {"id": "group-three"}
    return campaign_from_reward_groups(
        reward_campaign(group) | {"id": "quest-3"}, now=NOW
    ) | {"endAt": stamp(datetime.now(timezone.utc) + timedelta(seconds=ends_in))}


def test_claims_are_ordered_by_time_left_across_campaigns(monkeypatch):
    # One campaign has a repeat to claim within seconds and a reward good
    # for years; another campaign ends in half a minute. The long-lived
    # claim can take over a minute: it goes last, not with its campaign.
    monkeypatch.setattr(
        Twitch, "_Twitch__drop_variant_entries_from_drop", lambda self, drop: []
    )
    claimable = progress(status="CLAIMABLE", minutes=60, window=open_window())
    gql, _ = channel_listing()
    twitch = bare_twitch(gql)
    two_waiting_groups_campaign(twitch, second_opens_again_in=10)
    twitch._Twitch__remember_watch_reward_campaign(third_campaign(claimable, 30))

    claim_on_the_claim_thread(twitch)

    assert gql.claims == [
        "viewer-1#quest-1#group-two",
        "viewer-1#quest-3#group-three",
        "viewer-1#quest-1#group-watch",
    ]


def test_claim_in_a_campaign_named_as_closed_is_sent_after_the_others(monkeypatch):
    # Twitch may still accept it, or refuse it for over a minute: the
    # campaign that runs is not made to wait for that, with more time left.
    monkeypatch.setattr(
        Twitch, "_Twitch__drop_variant_entries_from_drop", lambda self, drop: []
    )
    claimable = progress(status="CLAIMABLE", minutes=60, window=open_window())
    gql, _ = channel_listing()
    twitch = bare_twitch(gql)
    twitch._Twitch__remember_watch_reward_campaign(third_campaign(claimable, 30))
    twitch._Twitch__remember_watch_reward_campaign(other_campaign(claimable))
    twitch.watch_reward_campaigns["quest-3"]["_watch_reward"]["named_closed"] = True

    claim_on_the_claim_thread(twitch)

    assert gql.claims == [
        "viewer-1#quest-2#group-two",
        "viewer-1#quest-3#group-three",
    ]


def test_reward_listed_during_a_claim_is_claimed_next_when_it_has_less_time(
    monkeypatch,
):
    monkeypatch.setattr(
        Twitch, "_Twitch__drop_variant_entries_from_drop", lambda self, drop: []
    )
    claimable = progress(status="CLAIMABLE", minutes=60, window=open_window())
    gql, _ = channel_listing()
    twitch = bare_twitch(gql)
    two_waiting_groups_campaign(twitch)
    accept = gql.claim_drop_rewards

    def claim_while_another_is_listed(instance_id):
        if not gql.claims:
            twitch._Twitch__store_listed_watch_rewards([third_campaign(claimable, 40)])
        return accept(instance_id)

    gql.claim_drop_rewards = claim_while_another_is_listed
    claim_on_the_claim_thread(twitch)

    assert gql.claims == [
        "viewer-1#quest-1#group-watch",
        "viewer-1#quest-3#group-three",
        "viewer-1#quest-1#group-two",
    ]


def test_refused_claim_is_sent_once_in_a_pass_of_the_claim_thread(monkeypatch):
    # It stays waiting after the try; the next reward is not held up by it.
    twitch, gql = refused_claim_twitch(
        monkeypatch,
        convert(watch_group(progress(status="CLAIMABLE", minutes=60), times=1)),
    )
    sent = []
    refuse = gql.claim_drop_rewards

    def count(instance_id):
        sent.append(instance_id)
        return refuse(instance_id)

    gql.claim_drop_rewards = count
    claim_on_the_claim_thread(twitch)

    assert len(sent) == 1


def test_campaign_a_listing_names_as_closed_is_not_evaluated_from_the_store(
    monkeypatch,
):
    # Stored while it ran, with an end date still ahead. Twitch then lists
    # it as disabled: that is not a listing that leaves it out.
    twitch, _, _ = evaluation_twitch(
        watch_group(progress(minutes=12, window=open_window()))
    )
    assert set(evaluate(monkeypatch, twitch, [listing()])[0]) == {"example-game"}

    deadlines, _ = evaluate(monkeypatch, twitch, [listing(status="DISABLED")])

    assert deadlines == {}


def test_campaign_named_as_closed_stays_out_when_the_next_listing_omits_it(
    monkeypatch,
):
    # Disabled in one listing and left out of the next: the stored record
    # still says it runs, and a channel may go on listing it.
    twitch, _, _ = evaluation_twitch(
        watch_group(progress(minutes=12, window=open_window()))
    )
    assert set(evaluate(monkeypatch, twitch, [listing()])[0]) == {"example-game"}
    assert evaluate(monkeypatch, twitch, [listing(status="DISABLED")])[0] == {}

    assert evaluate(monkeypatch, twitch, [])[0] == {}
    twitch._Twitch__get_campaigns_from_channel_id("4242")
    assert evaluate(monkeypatch, twitch, [])[0] == {}

    # Named as open again, it is kept through a listing that omits it.
    assert set(evaluate(monkeypatch, twitch, [listing()])[0]) == {"example-game"}
    assert set(evaluate(monkeypatch, twitch, [])[0]) == {"example-game"}


def test_record_is_not_grown_in_place_when_its_campaign_is_named_closed(
    monkeypatch,
):
    # Other threads deep-copy the record without a lock.
    twitch, _, _ = evaluation_twitch(
        watch_group(progress(minutes=12, window=open_window()))
    )
    evaluate(monkeypatch, twitch, [listing()])
    details = twitch.watch_reward_campaigns["quest-1"]["_watch_reward"]
    keys = set(details)

    evaluate(monkeypatch, twitch, [listing(status="DISABLED")])

    assert twitch.watch_reward_campaigns["quest-1"]["_watch_reward"] is details
    assert details["named_closed"] is True
    assert set(details) == keys


def sync_stored_reward_campaigns(monkeypatch, twitch):
    """Run the campaign sync over an entity for every stored campaign, with
    an inventory that lists nothing."""
    monkeypatch.setattr(
        Twitch, "_Twitch__get_inventory", lambda self: {"dropCampaignsInProgress": []}
    )
    monkeypatch.setattr(
        Twitch, "_Twitch__save_drop_snapshot_from_dict", lambda self, *a, **k: None
    )
    twitch._Twitch__sync_campaigns(
        [
            Campaign(copy.deepcopy(stored))
            for stored in twitch.watch_reward_campaigns.values()
        ]
    )


def test_sync_sends_the_claim_with_the_least_time_left_first(monkeypatch):
    # The sync goes through the campaigns as they are stored. Its claims do
    # not: the long-lived one can take over a minute.
    monkeypatch.setattr(
        Twitch, "_Twitch__drop_variant_entries_from_drop", lambda self, drop: []
    )
    claimable = progress(status="CLAIMABLE", minutes=60, window=open_window())
    gql, _ = channel_listing()
    twitch = bare_twitch(gql)
    two_waiting_groups_campaign(twitch, second_opens_again_in=10)
    twitch._Twitch__remember_watch_reward_campaign(third_campaign(claimable, 30))

    sync_stored_reward_campaigns(monkeypatch, twitch)

    assert gql.claims == [
        "viewer-1#quest-1#group-two",
        "viewer-1#quest-3#group-three",
        "viewer-1#quest-1#group-watch",
    ]


def test_sync_sends_a_refused_claim_once(monkeypatch):
    twitch, gql = refused_claim_twitch(
        monkeypatch,
        convert(watch_group(progress(status="CLAIMABLE", minutes=60), times=1)),
    )
    sent = []
    refuse = gql.claim_drop_rewards

    def count(instance_id):
        sent.append(instance_id)
        return refuse(instance_id)

    gql.claim_drop_rewards = count

    sync_stored_reward_campaigns(monkeypatch, twitch)

    assert len(sent) == 1


class TurnLock:
    """The claim pass lock, telling when another thread than the test's
    comes to wait for it."""

    def __init__(self, another_waits):
        self.lock = threading.RLock()
        self.another_waits = another_waits

    def __enter__(self):
        if threading.current_thread() is not threading.main_thread():
            self.another_waits.set()
        self.lock.acquire()

    def __exit__(self, *error):
        self.lock.release()


def test_claimer_that_waited_for_anothers_claim_chooses_when_its_turn_comes(
    monkeypatch,
):
    # The sync, say, comes to claim while the claim thread waits for the
    # answer to a slow claim, which Twitch then refuses. A reward ending in
    # half a minute was listed meanwhile: it goes before a second try of the
    # slow one.
    monkeypatch.setattr(
        Twitch, "_Twitch__drop_variant_entries_from_drop", lambda self, drop: []
    )
    claimable = progress(status="CLAIMABLE", minutes=60, window=open_window())
    gql, _ = channel_listing()
    twitch = bare_twitch(gql)
    twitch._Twitch__remember_watch_reward_campaign(convert(watch_group(claimable)))
    twitch.watch_reward_campaigns["quest-1"]["endAt"] = "2099-01-01T00:00:00Z"

    another_ready = threading.Event()
    turn = TurnLock(another_ready)
    monkeypatch.setattr(
        twitch_module, "_watch_reward_claim_pass_lock", lambda twitch: turn, False
    )
    choose = Twitch._Twitch__waiting_reward_claims_by_deadline

    def choose_and_tell(self, campaigns, tried=()):
        chosen = choose(self, campaigns, tried)
        if threading.current_thread() is not threading.main_thread():
            another_ready.set()
        return chosen

    monkeypatch.setattr(
        Twitch, "_Twitch__waiting_reward_claims_by_deadline", choose_and_tell
    )
    another = threading.Thread(
        target=twitch._Twitch__claim_waiting_watch_rewards_by_deadline, daemon=True
    )
    # This claimer leaves the reward listed meanwhile to the other.
    left_to_the_other = set()
    sent = []
    accept = gql.claim_drop_rewards

    def refuse_the_first(instance_id):
        sent.append(instance_id)
        if len(sent) > 1:
            return accept(instance_id)
        another.start()
        assert another_ready.wait(timeout=5)
        listed = twitch._Twitch__remember_watch_reward_campaign(
            third_campaign(claimable, 30)
        )
        left_to_the_other.add(
            twitch._Twitch__reward_claim_key(listed, listed["timeBasedDrops"][0])
        )
        return SimpleNamespace(status=None, errors=["service unavailable"])

    gql.claim_drop_rewards = refuse_the_first

    twitch._Twitch__claim_waiting_watch_rewards_by_deadline(left_to_the_other)
    another.join(timeout=5)

    assert not another.is_alive()
    assert sent == [
        "viewer-1#quest-1#group-watch",
        "viewer-1#quest-3#group-three",
        "viewer-1#quest-1#group-watch",
    ]


def test_evaluation_sends_the_claim_with_the_least_time_left_first(monkeypatch):
    # Evaluated in the order they are stored; the one stored last ends in
    # half a minute.
    twitch, _, _ = evaluation_twitch(
        watch_group(progress(minutes=12, window=open_window()))
    )
    evaluate(monkeypatch, twitch, [listing()])
    claimable = progress(status="CLAIMABLE", minutes=60, window=open_window())
    twitch._Twitch__remember_watch_reward_campaign(other_campaign(claimable))
    twitch._Twitch__remember_watch_reward_campaign(third_campaign(claimable, 30))
    twitch.watch_reward_campaigns["quest-2"]["endAt"] = "2099-01-01T00:00:00Z"
    assert twitch.gql.claims == []

    evaluate(monkeypatch, twitch, [listing()])

    assert twitch.gql.claims == [
        "viewer-1#quest-3#group-three",
        "viewer-1#quest-2#group-two",
    ]


def test_claim_thread_counts_the_next_repeat_as_the_end_of_a_waiting_one(
    monkeypatch,
):
    # A repeat may not be there to claim once the next has started, which
    # comes before the end of the campaign stored ahead of it.
    monkeypatch.setattr(
        Twitch, "_Twitch__drop_variant_entries_from_drop", lambda self, drop: []
    )
    claimable = progress(status="CLAIMABLE", minutes=60, window=open_window())
    gql, _ = channel_listing()
    twitch = bare_twitch(gql)
    twitch._Twitch__remember_watch_reward_campaign(convert(watch_group(claimable)))
    twitch._Twitch__remember_watch_reward_campaign(other_campaign(claimable))
    now = datetime.now(timezone.utc)
    for campaign_id, seconds in (("quest-1", 3600), ("quest-2", 7200)):
        twitch.watch_reward_campaigns[campaign_id]["endAt"] = (
            now + timedelta(seconds=seconds)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    [waiting] = twitch.watch_reward_campaigns["quest-2"]["timeBasedDrops"]
    waiting["_reward_group"]["next_start"] = (now + timedelta(seconds=40)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    claim_on_the_claim_thread(twitch)

    assert gql.claims == [
        "viewer-1#quest-2#group-two",
        "viewer-1#quest-1#group-watch",
    ]


def test_known_campaign_is_evaluated_when_a_listing_omits_its_requirements(
    monkeypatch,
):
    # The dashboard names it without watch requirements, and the source
    # that sends them failed this time. Seen as watch-earned before, it is
    # not a purchase-gated reward now.
    twitch, _, _ = evaluation_twitch(
        watch_group(progress(minutes=12, window=open_window()))
    )
    assert set(evaluate(monkeypatch, twitch, [listing()])[0]) == {"example-game"}
    bare = {
        key: value
        for key, value in listing().items()
        if key not in ("unlockRequirements", "isSitewide")
    }

    assert set(evaluate(monkeypatch, twitch, [bare])[0]) == {"example-game"}
    assert "quest-1" not in twitch.reward_campaign_ids


def test_window_opening_met_while_a_failed_read_backs_off_is_retried(monkeypatch):
    # The read failed minutes before the window opened. The refresh made
    # for the opening may not read again yet, and the opening is used up.
    twitch, _, listing_calls = evaluation_twitch(
        watch_group(progress(minutes=5, grants=1, window=open_window()))
    )
    twitch._Twitch__remember_watch_reward_campaign(
        convert(
            watch_group(progress(status="CLAIMABLE", minutes=60, window=open_window()))
        )
    )
    stored = twitch.watch_reward_campaigns["quest-1"]
    stored["timeBasedDrops"][0]["_reward_group"]["next_start"] = stamp(
        NOW - timedelta(seconds=30)
    )
    attempted_at = time.time() - 300
    stored["_watch_reward"]["refresh_attempted_at"] = attempted_at
    twitch.watch_reward_pending_starts = set()

    assert evaluate(monkeypatch, twitch, [listing()])[0] == {}
    assert listing_calls == []

    [retry_at] = twitch.watch_reward_pending_starts
    due_in = (retry_at - utc_now()).total_seconds()
    assert due_in == pytest.approx(WATCH_REWARD_PROGRESS_REFRESH_SECONDS - 300, abs=5)
    # Asked again before then, it is the same retry.
    evaluate(monkeypatch, twitch, [listing()])
    assert twitch.watch_reward_pending_starts == {retry_at}


def test_wildcard_channel_keeps_a_reward_campaign_the_catalog_does_not_list():
    # The external catalog lists another campaign for the game. A wildcard
    # channel is held to the catalog's campaign names, which never include
    # a reward campaign.
    gql, _ = channel_listing(
        watch_group(
            progress(minutes=12, window=open_window()), categories=[OTHER_CATEGORY]
        )
    )
    twitch = bare_twitch(gql)
    twitch.campaign_details_withheld_streak = 0
    twitch.twitchdrops_app_campaigns = {
        "other-game": [{"id": "gist-1", "name": "Catalog Campaign", "channels": []}]
    }
    streamer = category_streamer()
    streamer.from_category = False
    streamer.from_wildcard_category = True

    assert twitch._Twitch__get_campaign_ids_from_streamer(streamer) == ["quest-1"]


def test_group_that_pays_out_once_every_repeat_is_in_claims_only_then():
    # Twitch reports such a group CLAIMABLE after each repeat; its own
    # drops tray offers the claim once all of them are counted.
    def all_repeats_group(group_self):
        group = watch_group(group_self)
        group["progressCriteria"]["repeatableConfig"][
            "shouldCompleteAllRepeatsToEarn"
        ] = True
        return with_claim_instance_ids(convert(group), "viewer-1")["timeBasedDrops"][0]

    part_way = all_repeats_group(
        progress(status="CLAIMABLE", minutes=60, grants=2, window=open_window())
    )
    assert part_way["_reward_group"]["claimable"] is False
    assert part_way["self"]["dropInstanceID"] is None
    # This window's minutes are in; the next repeat starts when it closes.
    assert part_way["startAt"] == open_window()["expiresAt"]

    complete = all_repeats_group(
        progress(status="CLAIMABLE", minutes=60, grants=6, window=open_window())
    )
    assert complete["_reward_group"]["claimable"] is True
    assert complete["self"]["dropInstanceID"] == "viewer-1#quest-1#group-watch"


def test_stream_view_keeps_the_category_each_drop_is_reported_under():
    campaign = Campaign(
        convert(
            watch_group(
                progress(minutes=12, window=open_window()),
                categories=[CATEGORY, OTHER_CATEGORY],
            )
        )
    )

    view = campaign.in_reward_category(OTHER_CATEGORY)

    assert view.game["displayName"] == "Other Game"
    assert view.reward_drop_report_categories == {"group-watch#0": "Example Game"}


def test_category_added_to_a_campaign_nobody_lists_is_found(monkeypatch):
    # Stored with one category, which is not configured, so no channel is
    # ever listed for it. Twitch then adds the configured category.
    structure = reward_campaign(watch_group())
    twitch, lookups, listing_calls = evaluation_twitch(
        watch_group(
            progress(minutes=12, window=open_window()),
            categories=[CATEGORY, OTHER_CATEGORY],
        ),
        structure=structure,
    )
    assert (
        evaluate(monkeypatch, twitch, [listing()], requested=("other-game",))[0] == {}
    )
    assert len(lookups) == 1 and listing_calls == []

    structure["rewardGroups"][0]["progressCriteria"]["categories"].append(
        OTHER_CATEGORY
    )
    # Not asked again right away.
    evaluate(monkeypatch, twitch, [listing()], requested=("other-game",))
    assert len(lookups) == 1

    details = twitch.watch_reward_campaigns["quest-1"]["_watch_reward"]
    details["read_at"] -= 3601
    deadlines = evaluate(monkeypatch, twitch, [listing()], requested=("other-game",))[0]

    assert len(lookups) == 2
    assert listing_calls == ["4242"]
    assert set(deadlines) == {"other-game"}


def test_category_added_while_the_known_one_has_nobody_live_is_found(monkeypatch):
    # The configured category it was stored with has no live channel, so no
    # listing brings the campaign's categories up to date. Twitch adds a
    # second configured category.
    structure = reward_campaign(watch_group())
    twitch, lookups, _ = evaluation_twitch(
        watch_group(progress(minutes=12)), structure=structure
    )
    directory_calls = []

    def read():
        del directory_calls[:]
        evaluate(
            monkeypatch,
            twitch,
            [listing()],
            requested=("example-game", "other-game"),
            channels=(),
            directory_calls=directory_calls,
        )
        return [slug for slug, _ in directory_calls]

    assert read() == ["slug-777"]
    structure["rewardGroups"][0]["progressCriteria"]["categories"].append(
        OTHER_CATEGORY
    )
    details = twitch.watch_reward_campaigns["quest-1"]["_watch_reward"]
    details["read_at"] -= 3601
    details["refresh_attempted_at"] -= 3601

    assert sorted(read()) == ["slug-777", "slug-888"]
    assert len(lookups) == 2
    # Asked once an hour, not on every evaluation.
    read()
    assert len(lookups) == 2


def test_watch_group_that_also_asks_for_subscriptions_keeps_that_requirement():
    group = watch_group(progress(minutes=5))
    group["progressCriteria"]["requirements"]["subs"] = 2

    [drop] = convert(group)["timeBasedDrops"]

    assert drop["requiredSubs"] == 2
    assert drop_sub_requirement_unmet(drop) is True
    [plain] = convert(watch_group(progress(minutes=5)))["timeBasedDrops"]
    assert plain["requiredSubs"] == 0


def test_opened_window_is_acted_on_again_when_its_refresh_evaluated_nothing(
    monkeypatch,
):
    # The inventory did not load in the refresh made for the opening: no
    # campaign was evaluated, and the opening is used up.
    twitch, _, _ = evaluation_twitch(
        watch_group(progress(minutes=5, grants=1, window=open_window()))
    )
    twitch.retry_unevaluated_watch_reward_opening()
    assert not getattr(twitch, "watch_reward_pending_starts", None)

    twitch.watch_reward_pending_starts = {utc_now() - timedelta(seconds=10)}
    assert twitch.take_opened_watch_reward_start() is True
    twitch.retry_unevaluated_watch_reward_opening()

    [retry_at] = twitch.watch_reward_pending_starts
    assert (retry_at - utc_now()).total_seconds() == pytest.approx(
        WATCH_REWARD_PROGRESS_REFRESH_SECONDS, abs=5
    )

    # A refresh that evaluates the campaigns settles it: all that is left to
    # wait for is the close of the window the repeat is being watched in.
    twitch.watch_reward_pending_starts = set()
    evaluate(monkeypatch, twitch, [listing()])
    twitch.retry_unevaluated_watch_reward_opening()
    assert twitch.watch_reward_pending_starts == {NOW + timedelta(hours=23)}


def test_opened_window_is_acted_on_again_when_its_evaluation_fails_part_way(
    monkeypatch,
):
    twitch, _, _ = evaluation_twitch(
        watch_group(progress(minutes=5, grants=1, window=open_window()))
    )
    twitch.watch_reward_pending_starts = {utc_now() - timedelta(seconds=10)}
    assert twitch.take_opened_watch_reward_start() is True

    def broken(self, campaign, preferred_slugs=None):
        raise OSError("disk full")

    monkeypatch.setattr(Twitch, "_Twitch__refresh_watch_reward_progress", broken)
    with pytest.raises(OSError):
        evaluate(monkeypatch, twitch, [listing()])
    twitch.retry_unevaluated_watch_reward_opening()

    assert len(twitch.watch_reward_pending_starts) == 1


def test_opened_window_is_not_retried_for_categories_with_drops_off():
    # Nothing is ever evaluated for them, so there is nothing to wait for.
    twitch = bare_twitch()
    twitch.watch_reward_pending_starts = {utc_now() - timedelta(seconds=10)}
    assert twitch.take_opened_watch_reward_start() is True

    assert twitch.filter_categories_with_active_drops(
        ["Example Game"], drops_enabled=False
    ) == ["Example Game"]
    twitch.retry_unevaluated_watch_reward_opening()

    assert twitch.watch_reward_pending_starts == set()


def remembered_lines(caplog, *group_selves, times=6):
    """The INFO lines logged while a campaign's listings are stored one
    after another, each with the group in the next of `group_selves`."""
    twitch = bare_twitch()
    with caplog.at_level(logging.INFO, logger=twitch_module.__name__):
        for group_self in group_selves:
            twitch._Twitch__remember_watch_reward_campaign(
                convert(watch_group(group_self, times=times))
            )
    return [record for record in caplog.records if record.levelno == logging.INFO]


def test_minutes_passing_a_quarter_are_logged(caplog):
    # A reward group's minutes come from channel listings, so the progress
    # bar of a drop (printed on a quarter reached one minute at a time) says
    # nothing about it.
    records = remembered_lines(
        caplog,
        progress(minutes=12, window=open_window()),
        progress(minutes=16, window=open_window()),
    )

    assert [record.getMessage() for record in records] == [
        "Launch Celebration - Daily Chest (repeat 1 of 6): 16/60 minutes watched"
    ]
    # A line for the console and the log file, not for a notifier.
    assert records[0].skip_telegram is True


def test_first_minutes_are_logged(caplog):
    records = remembered_lines(
        caplog,
        progress(minutes=0, window=open_window()),
        progress(minutes=2, window=open_window()),
    )

    assert [record.getMessage() for record in records] == [
        "Launch Celebration - Daily Chest (repeat 1 of 6): 2/60 minutes watched"
    ]


@pytest.mark.parametrize(
    "before, after",
    [
        # The first listing has nothing to compare with.
        (14, 14),
        # More minutes within one quarter: a listing arrives every couple
        # of minutes, and a drop's progress is not reported that often.
        (16, 28),
    ],
)
def test_listing_within_the_same_quarter_logs_nothing(caplog, before, after):
    records = remembered_lines(
        caplog,
        progress(minutes=before, window=open_window()),
        progress(minutes=after, window=open_window()),
    )

    assert records == []


def test_repeat_twitch_counts_without_a_claim_is_logged_as_earned(caplog):
    # What Twitch reports for a group it counts by itself: the minutes in,
    # the count up, and no CLAIMABLE in between, so no claim line either.
    records = remembered_lines(
        caplog,
        progress(minutes=52, window=open_window()),
        progress(minutes=60, grants=1, window=open_window()),
    )

    assert [record.getMessage() for record in records] == [
        "Reward earned: Launch Celebration - Daily Chest (repeat 1 of 6)"
    ]
    assert records[0].event == Events.DROP_CLAIM
    assert getattr(records[0], "skip_telegram", False) is False


def test_single_reward_twitch_counts_without_a_claim_is_logged_as_earned(caplog):
    records = remembered_lines(
        caplog,
        progress(minutes=9),
        progress(status="CLAIMED", minutes=15, grants=1),
        times=1,
    )

    assert [record.getMessage() for record in records] == [
        "Reward earned: Launch Celebration - Daily Chest"
    ]


def test_claimed_repeat_is_not_logged_as_earned_again(caplog):
    # Its claim has a line of its own.
    records = remembered_lines(
        caplog,
        progress(status="CLAIMABLE", minutes=60, window=open_window()),
        progress(minutes=0, grants=1, window=open_window()),
    )

    assert records == []


def report_twitch(monkeypatch, tmp_path, analytics=True):
    """A Twitch that writes report rows, to tmp_path when analytics are on."""
    twitch = snapshot_twitch()
    twitch.analytics_mutex = twitch_module.RLock()
    twitch.drop_report_state = {}
    twitch.track_drop_item_art = True
    monkeypatch.setattr(twitch_module.Settings, "enable_analytics", analytics)
    monkeypatch.setattr(
        twitch_module.Settings, "analytics_path", str(tmp_path), raising=False
    )
    return twitch


def save_synced_rows(twitch):
    """Write the report rows the inventory sync writes for the stored
    campaign's drops."""
    for entry in twitch._Twitch__watch_reward_progress_entries():
        for drop_dict in entry["timeBasedDrops"]:
            twitch._Twitch__save_drop_snapshot_from_dict(
                drop_dict,
                campaign_name_override=entry["name"],
                category_name_override=entry["game"]["displayName"],
            )


def report_rows(twitch, tmp_path=None):
    """The report rows by drop ID: this session's, or the saved file's."""
    if tmp_path is None:
        rows = twitch.drop_report_state.values()
    else:
        rows = json.loads((tmp_path / "drops_by_category.json").read_text())["drops"]
    return {row["drop_id"]: row for row in rows}


def test_synced_row_of_a_repeat_is_named_for_the_repeat(monkeypatch, tmp_path):
    # Named for its reward ("Chest"), every repeat's row would read the same,
    # and differ from the name the minute watcher writes the same row under.
    twitch = report_twitch(monkeypatch, tmp_path)
    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(minutes=20, window=open_window())))
    )

    save_synced_rows(twitch)

    row = report_rows(twitch, tmp_path)["group-watch#0"]
    assert row["item_name"] == "Daily Chest (repeat 1 of 6)"
    assert row["benefit"] == "Chest"
    assert (row["current_minutes_watched"], row["status"]) == (20, "in_progress")


def test_watcher_reports_a_window_that_started_over_by_its_own_deadline(
    monkeypatch, tmp_path
):
    # The campaign's entities were built during a window that then closed at
    # 40 of 60 minutes. The next one opened, under the same drop ID, before
    # they were built again: the sync gives them its minutes, and the minute
    # watcher reports from them.
    twitch = report_twitch(monkeypatch, tmp_path)
    monkeypatch.setattr(
        Twitch, "_Twitch__get_inventory", lambda self: {"dropCampaignsInProgress": []}
    )
    stored = twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(minutes=40, window=open_window())))
    )
    campaign = Campaign(copy.deepcopy(stored))
    [drop] = campaign.drops
    # The first window's deadline, since passed.
    drop.end_at = utc_now() - timedelta(minutes=5)

    def watch_a_minute():
        twitch._Twitch__sync_campaigns([campaign])
        return twitch._Twitch__save_watched_drop_progress_if_new(
            drop,
            lambda: twitch._Twitch__save_drop_progress_analytics(
                drop, campaign=campaign, streamer_username="streamer"
            ),
        )

    new_window = open_window()
    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(minutes=3, window=new_window)))
    )
    assert watch_a_minute() is not None

    row = report_rows(twitch, tmp_path)["group-watch#0"]
    assert row["streamer"] == "streamer"
    assert row["current_minutes_watched"] == 3
    assert row["drop_end_at"] == new_window["expiresAt"]
    assert row["failed_to_achieve"] is False


def test_repeat_whose_window_has_not_opened_gets_no_row(monkeypatch, tmp_path):
    # The first repeat is in and counted; the second cannot be earned before
    # the window ends. A row for it at 0 minutes reads as a reward the miner
    # is failing to earn.
    twitch = report_twitch(monkeypatch, tmp_path)
    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(minutes=60, grants=1, window=open_window())))
    )

    save_synced_rows(twitch)

    assert report_rows(twitch) == {}


def test_row_of_a_repeat_twitch_counted_without_a_claim_is_finished(
    monkeypatch, tmp_path
):
    # Nothing claims such a repeat, so nothing else writes its last row: it
    # stayed at the minutes of the listing before, as in progress, for good.
    twitch = report_twitch(monkeypatch, tmp_path)
    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(minutes=59, window=open_window())))
    )
    save_synced_rows(twitch)
    assert report_rows(twitch, tmp_path)["group-watch#0"]["status"] == "in_progress"

    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(minutes=60, grants=1, window=open_window())))
    )

    for rows in (report_rows(twitch), report_rows(twitch, tmp_path)):
        row = rows["group-watch#0"]
        assert row["status"] == "captured"
        assert (row["current_minutes_watched"], row["percentage_progress"]) == (
            60,
            100,
        )
        assert row["failed_to_achieve"] is False


@pytest.mark.parametrize("analytics", [True, False])
def test_repeat_twitch_counted_before_its_row_was_written_gets_one(
    monkeypatch, tmp_path, analytics
):
    # Started a minute short of the repeat: Twitch counted it before the
    # sync or the minute watcher wrote its row. There was none to mark as
    # earned, and none was written later, so the reward never showed.
    twitch = report_twitch(monkeypatch, tmp_path, analytics=analytics)
    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(minutes=59, window=open_window())))
    )
    assert report_rows(twitch) == {}

    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(minutes=60, grants=1, window=open_window())))
    )

    for rows in [report_rows(twitch)] + (
        [report_rows(twitch, tmp_path)] if analytics else []
    ):
        assert list(rows) == ["group-watch#0"]
        row = rows["group-watch#0"]
        assert (row["status"], row["item_name"], row["category"]) == (
            "captured",
            "Daily Chest (repeat 1 of 6)",
            "Example Game",
        )
        assert (row["current_minutes_watched"], row["percentage_progress"]) == (
            60,
            100,
        )
        assert row["campaign"] == "Launch Celebration"
        assert row["failed_to_achieve"] is False
    # The watcher's copy of the repeat is older than the record now.
    assert (
        twitch._Twitch__save_drop_snapshot_if_new(
            "group-watch#0|late", 60, "in_progress", lambda: None
        )
        is False
    )


def test_counted_repeat_gets_its_row_although_the_report_could_not_be_read(
    monkeypatch, tmp_path
):
    # The repeat is only at hand with the record that shows it counted: left
    # for the next record, as the rows to mark are, it would get no row.
    twitch = report_twitch(monkeypatch, tmp_path)
    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(minutes=59, window=open_window())))
    )

    def unreadable(finish):
        raise OSError("report in use")

    monkeypatch.setattr(
        Twitch, "_Twitch__finish_report_file_rows", staticmethod(unreadable)
    )

    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(minutes=60, grants=1, window=open_window())))
    )

    for rows in (report_rows(twitch), report_rows(twitch, tmp_path)):
        row = rows["group-watch#0"]
        assert (row["status"], row["current_minutes_watched"]) == ("captured", 60)


def test_counted_repeat_whose_row_could_not_be_written_gets_it_later(
    monkeypatch, tmp_path
):
    # The repeat is in no later record, so nothing else writes its row.
    twitch = report_twitch(monkeypatch, tmp_path)
    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(minutes=59, window=open_window())))
    )
    counted = convert(watch_group(progress(minutes=60, grants=1, window=open_window())))
    real_replace = twitch_module.os.replace
    failures = [OSError("disk full")]

    def failing_once(source, target):
        if failures:
            raise failures.pop()
        return real_replace(source, target)

    monkeypatch.setattr(twitch_module.os, "replace", failing_once)

    twitch._Twitch__remember_watch_reward_campaign(copy.deepcopy(counted))
    assert not (tmp_path / "drops_by_category.json").exists()

    twitch._Twitch__remember_watch_reward_campaign(copy.deepcopy(counted))

    row = report_rows(twitch, tmp_path)["group-watch#0"]
    assert (row["status"], row["current_minutes_watched"]) == ("captured", 60)
    assert row["campaign"] == "Launch Celebration"
    # Written, it is not kept to be written again.
    assert twitch.watch_reward_unwritten_rows == {}


def all_repeats_group(group_self):
    """A group of three repeats that pays out once, with the last."""
    group = watch_group(group_self, times=3)
    group["progressCriteria"]["repeatableConfig"][
        "shouldCompleteAllRepeatsToEarn"
    ] = True
    return group


def test_repeats_toward_one_reward_are_captured_with_it_and_not_before(
    monkeypatch, tmp_path
):
    # A repeat counted before the last earns nothing yet: "captured" would
    # show a reward the account does not have.
    twitch = report_twitch(monkeypatch, tmp_path)

    def listed(**group_self):
        stored = twitch._Twitch__remember_watch_reward_campaign(
            convert(all_repeats_group(progress(window=open_window(), **group_self)))
        )
        save_synced_rows(twitch)
        return stored["timeBasedDrops"][0]["id"]

    def rows_by_id():
        saved = report_rows(twitch, tmp_path)
        assert saved == report_rows(twitch)
        return {
            drop_id: (row["status"], row["current_minutes_watched"])
            for drop_id, row in saved.items()
        }

    assert listed(minutes=59) == "group-watch#0"
    assert listed(minutes=3, grants=1) == "group-watch#1"
    # Its minutes are in, and it is left in progress.
    assert rows_by_id() == {
        "group-watch#0": ("in_progress", 60),
        "group-watch#1": ("in_progress", 3),
    }

    assert listed(minutes=5, grants=2) == "group-watch#2"
    assert rows_by_id()["group-watch#1"] == ("in_progress", 60)

    assert listed(status="CLAIMED", minutes=60, grants=3) == "group-watch#2"
    assert rows_by_id() == {
        "group-watch#0": ("captured", 60),
        "group-watch#1": ("captured", 60),
        "group-watch#2": ("captured", 60),
    }


def test_repeats_toward_one_reward_are_captured_when_its_claim_is_accepted(
    monkeypatch, tmp_path
):
    # No listing need follow the claim: nothing is left to watch for.
    twitch = report_twitch(monkeypatch, tmp_path)
    gql, _ = channel_listing()
    twitch.gql = gql

    def listed(**group_self):
        twitch._Twitch__store_listed_watch_rewards(
            [convert(all_repeats_group(progress(window=open_window(), **group_self)))]
        )
        save_synced_rows(twitch)

    listed(minutes=59)
    listed(minutes=58, grants=1)
    listed(minutes=57, grants=2)
    assert gql.claims == []

    listed(status="CLAIMABLE", minutes=60, grants=3)

    assert gql.claims == ["viewer-1#quest-1#group-watch"]
    rows = report_rows(twitch, tmp_path)
    assert {drop_id: row["status"] for drop_id, row in rows.items()} == {
        "group-watch#0": "captured",
        "group-watch#1": "captured",
        "group-watch#2": "captured",
    }


def test_repeat_toward_one_reward_counted_before_its_row_gets_none(
    monkeypatch, tmp_path
):
    # A row written for it would be one of an earned reward.
    twitch = report_twitch(monkeypatch, tmp_path)
    for group_self in (
        progress(minutes=59, window=open_window()),
        progress(minutes=3, grants=1, window=open_window()),
    ):
        twitch._Twitch__remember_watch_reward_campaign(
            convert(all_repeats_group(group_self))
        )

    assert report_rows(twitch) == {}


def test_repeat_waiting_to_be_claimed_gets_no_row_as_earned(monkeypatch, tmp_path):
    twitch = report_twitch(monkeypatch, tmp_path)
    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(minutes=59, window=open_window())))
    )

    twitch._Twitch__remember_watch_reward_campaign(
        convert(
            watch_group(
                progress(status="CLAIMABLE", minutes=60, grants=1, window=open_window())
            )
        )
    )

    assert report_rows(twitch) == {}


def test_row_left_in_progress_by_an_earlier_session_is_finished(monkeypatch, tmp_path):
    # The miner was restarted between the repeat's last listing and the one
    # that showed it counted: the first record of the new session shows the
    # next repeat, and the saved row is all that is left of the earlier one.
    (tmp_path / "drops_by_category.json").write_text(
        json.dumps(
            {
                "drops": [
                    {
                        "drop_id": "group-watch#0",
                        "category": "Example Game",
                        "item_name": "Daily Chest (repeat 1 of 6)",
                        "status": "in_progress",
                        "current_minutes_watched": 59,
                        "minutes_required": 60,
                        "percentage_progress": 98,
                    },
                    {
                        "drop_id": "another-drop",
                        "category": "Example Game",
                        "status": "in_progress",
                        "current_minutes_watched": 5,
                        "minutes_required": 60,
                    },
                ]
            }
        )
    )
    twitch = report_twitch(monkeypatch, tmp_path)

    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(minutes=60, grants=1, window=open_window())))
    )

    rows = report_rows(twitch, tmp_path)
    assert rows["group-watch#0"]["status"] == "captured"
    assert rows["group-watch#0"]["current_minutes_watched"] == 60
    assert rows["another-drop"]["status"] == "in_progress"


def test_finishing_earned_rows_reads_the_report_file_once_per_repeat(
    monkeypatch, tmp_path
):
    # A record is stored every couple of minutes while a group is watched.
    twitch = report_twitch(monkeypatch, tmp_path)
    (tmp_path / "drops_by_category.json").write_text(json.dumps({"drops": []}))
    opened = []
    real_open = open

    def counting_open(path, *args, **kwargs):
        opened.append(str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(twitch_module, "open", counting_open, raising=False)
    for _ in range(3):
        twitch._Twitch__remember_watch_reward_campaign(
            convert(watch_group(progress(minutes=60, grants=1, window=open_window())))
        )

    assert len(opened) == 1


def failed_row(drop_id="group-watch#0", minutes=59):
    return {
        "drop_id": drop_id,
        "category": "Example Game",
        "status": "failed",
        "failed_to_achieve": True,
        "current_minutes_watched": minutes,
        "minutes_required": 60,
    }


def test_row_written_as_failed_for_a_repeat_seen_earned_is_finished(
    monkeypatch, tmp_path
):
    # The sync copied the record at 59 minutes and wrote the row after the
    # window had closed, as failed; Twitch had counted the repeat meanwhile.
    twitch = report_twitch(monkeypatch, tmp_path)
    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(minutes=59, window=open_window())))
    )
    (tmp_path / "drops_by_category.json").write_text(
        json.dumps({"drops": [failed_row()]})
    )

    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(minutes=60, grants=1, window=open_window())))
    )

    row = report_rows(twitch, tmp_path)["group-watch#0"]
    assert (row["status"], row["failed_to_achieve"]) == ("captured", False)
    assert row["current_minutes_watched"] == 60


def test_row_an_earlier_session_left_as_failed_is_not_finished_by_the_count(
    monkeypatch, tmp_path
):
    # On the first record of a session the count is all there is to go by:
    # one repeat was earned. Should Twitch count a repeat when it is
    # claimed, that can be the second, after a first lost unclaimed.
    (tmp_path / "drops_by_category.json").write_text(
        json.dumps({"drops": [failed_row()]})
    )
    twitch = report_twitch(monkeypatch, tmp_path)

    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(minutes=5, grants=1, window=open_window())))
    )

    row = report_rows(twitch, tmp_path)["group-watch#0"]
    assert (row["status"], row["failed_to_achieve"]) == ("failed", True)


def test_failed_snapshot_of_a_lost_repeat_is_saved_and_of_an_earned_one_is_not():
    def save(twitch, status):
        return twitch._Twitch__save_drop_snapshot_if_new(
            "group-watch#0|art", 59, status, lambda: None
        )

    # Lost waiting to be claimed: the count did not move, the next repeat
    # is being watched. Its last snapshot does say failed.
    lost = snapshot_twitch()
    for group_self in (
        progress(minutes=59, window=open_window()),
        progress(status="CLAIMABLE", minutes=60, window=open_window()),
        progress(minutes=3, window=open_window()),
    ):
        stored = lost._Twitch__remember_watch_reward_campaign(
            convert(watch_group(group_self))
        )
    assert stored["timeBasedDrops"][0]["id"] == "group-watch#1"
    assert save(lost, "in_progress") is False
    assert save(lost, "failed") is True

    # Counted by Twitch: a copy from before, looked at after the window
    # closed, says failed of a repeat that was earned.
    earned = snapshot_twitch()
    for group_self in (
        progress(minutes=59, window=open_window()),
        progress(minutes=60, grants=1, window=open_window()),
    ):
        earned._Twitch__remember_watch_reward_campaign(convert(watch_group(group_self)))
    assert save(earned, "failed") is False

    # Counted by Twitch before the miner claimed it: no record shows the
    # count go up after the claim, which alone says the repeat was earned.
    claimed = snapshot_twitch()
    for group_self in (
        progress(minutes=59, window=open_window()),
        progress(status="CLAIMABLE", minutes=60, grants=1, window=open_window()),
    ):
        stored = claimed._Twitch__remember_watch_reward_campaign(
            convert(watch_group(group_self))
        )
    assert stored["timeBasedDrops"][0]["id"] == "group-watch#0"
    claimed._Twitch__mark_drop_accepted(Drop(stored["timeBasedDrops"][0]))
    stored = claimed._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(minutes=3, grants=1, window=open_window())))
    )
    assert stored["timeBasedDrops"][0]["id"] == "group-watch#1"
    assert save(claimed, "failed") is False


def test_reward_group_that_still_needs_a_subscription_has_no_progress_row():
    # A row says the group is being earned by watching, which keeps the
    # channel in the Drops slot. Watching alone cannot earn this one.
    twitch = bare_twitch()
    needs_subs = watch_group(progress(minutes=5, window=open_window()))
    needs_subs["progressCriteria"]["requirements"]["subs"] = 2
    stored = twitch._Twitch__remember_watch_reward_campaign(convert(needs_subs))

    rows, _ = twitch._Twitch__watch_reward_progress_rows([stored])

    assert rows == {}
    plain = twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(minutes=5, window=open_window())))
    )
    rows, _ = twitch._Twitch__watch_reward_progress_rows([plain])
    assert [row[2] for row in rows["example-game"]] == ["group-watch#0"]


@pytest.mark.parametrize(
    "ends_in, status, failed",
    [
        (timedelta(hours=1), "in_progress", False),
        # Its minutes were in; the subscription it also needs was not.
        (timedelta(hours=-1), "failed", True),
    ],
)
def test_reward_that_needed_a_subscription_too_is_failed_once_its_time_is_up(
    monkeypatch, tmp_path, ends_in, status, failed
):
    twitch = report_twitch(monkeypatch, tmp_path)
    needs_subs = watch_group(progress(minutes=60), times=1)
    needs_subs["progressCriteria"]["requirements"]["subs"] = 2
    [drop] = convert(needs_subs)["timeBasedDrops"]
    drop["endAt"] = stamp(utc_now() + ends_in)

    twitch._Twitch__save_drop_snapshot_from_dict(
        drop,
        campaign_name_override="Launch Celebration",
        category_name_override="Example Game",
    )

    for rows in (report_rows(twitch), report_rows(twitch, tmp_path)):
        row = rows["group-watch"]
        assert (row["status"], row["failed_to_achieve"]) == (status, failed)
        assert row["current_minutes_watched"] == 60


def test_close_of_the_window_a_repeat_is_watched_in_is_waited_for():
    # Unfinished when its window closes, the repeat starts over in the next
    # one. With its channels offline by then, nothing else would look at the
    # category again before the ordinary refresh, hours later.
    twitch = bare_twitch()
    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(minutes=5, window=open_window())))
    )

    assert twitch.watch_reward_pending_starts == {NOW + timedelta(hours=23)}


def test_group_due_when_the_campaign_ends_has_no_window_to_wait_for():
    twitch = bare_twitch()
    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(minutes=5), times=1))
    )

    assert not getattr(twitch, "watch_reward_pending_starts", None)


def test_copy_from_before_a_window_started_over_stays_out_until_a_full_sync():
    # Only a full inventory sync renews the minute watcher's copy of a drop.
    # Until one has, its minutes are those of the window before.
    twitch = snapshot_twitch()
    for minutes in (40, 5):
        twitch._Twitch__remember_watch_reward_campaign(
            convert(watch_group(progress(minutes=minutes, window=open_window())))
        )
    details = twitch.watch_reward_campaigns["quest-1"]["_watch_reward"]
    details["restarted_at"] = {
        "group-watch#0": time.time() - DROP_FULL_INVENTORY_SYNC_SECONDS - 60
    }
    watched = SimpleNamespace(id="group-watch#0", current_minutes_watched=40)

    assert (
        twitch._Twitch__save_watched_drop_progress_if_new(watched, lambda: None) is None
    )
    assert WATCH_REWARD_STALE_COPY_SECONDS >= 2 * DROP_FULL_INVENTORY_SYNC_SECONDS


@pytest.mark.parametrize(
    "times, before, earned, drop_id",
    [
        # A single reward, and the last repeat of one: no later repeat
        # follows whose record would show this one earned.
        (1, progress(minutes=50), progress("CLAIMED", 60, grants=1), "group-watch"),
        (
            6,
            progress(minutes=50, grants=5, window=open_window()),
            progress("CLAIMED", 60, grants=6, window=open_window()),
            "group-watch#5",
        ),
    ],
)
def test_row_of_the_last_reward_twitch_counted_is_finished_without_a_sync(
    monkeypatch, tmp_path, times, before, earned, drop_id
):
    # The inventory sync writes an earned drop's row too, but not for a
    # campaign that has ended by the time it runs.
    twitch = report_twitch(monkeypatch, tmp_path)
    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(before, times=times))
    )
    save_synced_rows(twitch)
    assert report_rows(twitch, tmp_path)[drop_id]["status"] == "in_progress"

    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(earned, times=times))
    )

    row = report_rows(twitch, tmp_path)[drop_id]
    assert (row["status"], row["current_minutes_watched"]) == ("captured", 60)


def test_earned_rows_are_finished_by_a_later_record_when_the_report_cannot_be_saved(
    monkeypatch, tmp_path
):
    twitch = report_twitch(monkeypatch, tmp_path)
    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(minutes=59, window=open_window())))
    )
    save_synced_rows(twitch)
    earned = convert(watch_group(progress(minutes=60, grants=1, window=open_window())))
    real_replace = twitch_module.os.replace
    failures = [OSError("disk full")]

    def failing_once(source, target):
        if failures:
            raise failures.pop()
        return real_replace(source, target)

    monkeypatch.setattr(twitch_module.os, "replace", failing_once)

    # The record is stored all the same: its caller reads an error as a
    # listing that failed.
    stored = twitch._Twitch__remember_watch_reward_campaign(copy.deepcopy(earned))
    assert stored["timeBasedDrops"][0]["id"] == "group-watch#1"
    assert report_rows(twitch, tmp_path)["group-watch#0"]["status"] == "in_progress"

    twitch._Twitch__remember_watch_reward_campaign(copy.deepcopy(earned))

    assert report_rows(twitch, tmp_path)["group-watch#0"]["status"] == "captured"


def test_repeat_is_known_as_earned_as_soon_as_its_record_is_stored(
    monkeypatch, tmp_path
):
    # The sync holds a copy of the repeat from before it was counted, and
    # looks at it after its window closed: "failed". Between the record
    # being stored and the row being marked as earned, that snapshot passed
    # for a lost repeat's, and written after the mark it undid it for good.
    twitch = report_twitch(monkeypatch, tmp_path)
    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(minutes=59, window=open_window())))
    )
    save_synced_rows(twitch)
    saved = []

    def save_copy_as_failed(self, campaign_id):
        saved.append(
            twitch._Twitch__save_drop_snapshot_if_new(
                "group-watch#0|copy", 59, "failed", lambda: None
            )
        )

    # What runs between the two.
    monkeypatch.setattr(
        Twitch, "_Twitch__publish_watch_reward_progress", save_copy_as_failed
    )
    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(minutes=60, grants=1, window=open_window())))
    )

    assert saved == [False]
    assert report_rows(twitch, tmp_path)["group-watch#0"]["status"] == "captured"


def test_counted_repeat_gets_its_row_when_another_record_had_the_rows_marked(
    monkeypatch, tmp_path
):
    # Two threads store listings. The other one's record, stored between
    # this one being stored and its rows being marked, marks every row
    # known as earned, and knows nothing of the repeat this one saw counted
    # before it had a row.
    twitch = report_twitch(monkeypatch, tmp_path)
    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(minutes=59, window=open_window())))
    )

    def other_record_marks_the_rows(self, campaign_id):
        twitch.watch_reward_finished_rows = twitch.watch_reward_earned_rows

    monkeypatch.setattr(
        Twitch, "_Twitch__publish_watch_reward_progress", other_record_marks_the_rows
    )
    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(minutes=60, grants=1, window=open_window())))
    )

    assert report_rows(twitch, tmp_path)["group-watch#0"]["status"] == "captured"


def test_older_copy_of_a_group_the_listing_leaves_out_is_not_saved(
    monkeypatch, tmp_path
):
    # Twitch stops reporting progress on the group for a while. The sync's
    # copy of its earned repeat, from before it was counted, would be saved
    # as failed over the earned row, which is marked only once.
    twitch = report_twitch(monkeypatch, tmp_path)
    other = watch_group(progress(minutes=5), times=1, categories=[OTHER_CATEGORY])
    other["id"] = "group-other"
    for group_self in (
        progress(minutes=59, window=open_window()),
        progress(minutes=60, grants=1, window=open_window()),
    ):
        twitch._Twitch__remember_watch_reward_campaign(
            convert(watch_group(group_self), other)
        )
    stored = twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(None), other)
    )
    assert [drop["id"] for drop in stored["timeBasedDrops"]] == ["group-other"]
    assert report_rows(twitch, tmp_path)["group-watch#0"]["status"] == "captured"

    def save(status):
        return twitch._Twitch__save_drop_snapshot_if_new(
            "group-watch#0|copy", 59, status, lambda: None
        )

    assert save("failed") is False
    assert save("in_progress") is False


@pytest.mark.parametrize(
    "ends_in, retry_in",
    [
        # The usual wait would end after the campaign: its last minutes.
        (timedelta(minutes=5), timedelta(minutes=3)),
        # Already inside them: once a minute.
        (timedelta(seconds=150), timedelta(seconds=60)),
        # In the last minute a minute's wait would end after it too.
        (timedelta(seconds=40), timedelta(seconds=20)),
        (timedelta(hours=1), timedelta(seconds=WATCH_REWARD_PROGRESS_REFRESH_SECONDS)),
    ],
)
def test_claim_twitch_did_not_accept_is_tried_again_before_the_campaign_ends(
    monkeypatch, ends_in, retry_in
):
    twitch, _, _ = evaluation_twitch(
        watch_group(progress(status="CLAIMABLE", minutes=60), times=1)
    )
    twitch.gql.claim_drop_rewards = lambda instance_id: SimpleNamespace(
        status=None, errors=["service unavailable"]
    )
    evaluate(monkeypatch, twitch, [listing()])
    stored = twitch.watch_reward_campaigns["quest-1"]
    stored["endAt"] = stamp(utc_now() + ends_in)

    twitch._Twitch__retry_unaccepted_reward_claims("quest-1")

    # One moment a campaign: the one counted from its last try.
    [retry_at] = claim_retries(twitch).values()
    assert (retry_at - utc_now()).total_seconds() == pytest.approx(
        retry_in.total_seconds(), abs=3
    )
    assert retry_at < twitch._Twitch__parse_twitch_datetime(stored["endAt"])


def test_claim_twitch_did_not_accept_is_tried_again_without_a_streamer(monkeypatch):
    # Nothing is left to watch, so no campaign sync runs to send the claim
    # again, and the ordinary category refresh can come after the campaign
    # has ended.
    twitch, _, _ = evaluation_twitch(
        watch_group(progress(status="CLAIMABLE", minutes=60), times=1)
    )
    working_claim = twitch.gql.claim_drop_rewards
    twitch.gql.claim_drop_rewards = lambda instance_id: SimpleNamespace(
        status=None, errors=["service unavailable"]
    )

    evaluate(monkeypatch, twitch, [listing()])

    # Noted for the claim thread, once for the campaign, and not as a
    # moment that calls for the main loop.
    [retry_at] = claim_retries(twitch).values()
    assert (retry_at - utc_now()).total_seconds() == pytest.approx(
        WATCH_REWARD_PROGRESS_REFRESH_SECONDS, abs=5
    )
    assert not getattr(twitch, "watch_reward_pending_starts", None)

    # Accepted when the thread sends it again, it leaves nothing to come
    # back for.
    twitch.gql.claim_drop_rewards = working_claim
    claim_again_when_due(twitch)

    assert twitch.gql.claims == ["viewer-1#quest-1#group-watch"]
    assert claim_retries(twitch) == {}


def test_claim_the_evaluation_gets_accepted_leaves_no_retry(monkeypatch):
    twitch, _, _ = evaluation_twitch(
        watch_group(progress(status="CLAIMABLE", minutes=60), times=1)
    )
    working_claim = twitch.gql.claim_drop_rewards
    twitch.gql.claim_drop_rewards = lambda instance_id: SimpleNamespace(
        status=None, errors=["service unavailable"]
    )
    evaluate(monkeypatch, twitch, [listing()])
    assert claim_retries(twitch)

    twitch.gql.claim_drop_rewards = working_claim
    evaluate(monkeypatch, twitch, [listing()])

    assert twitch.gql.claims == ["viewer-1#quest-1#group-watch"]
    assert claim_retries(twitch) == {}


def test_claim_a_listing_sent_that_twitch_did_not_accept_is_tried_again(monkeypatch):
    # Found waiting to be claimed by a watched channel's listing, 40 seconds
    # before the campaign ends. The sync's next pass and the next listing
    # can both come after the end.
    monkeypatch.setattr(
        Twitch, "_Twitch__drop_variant_entries_from_drop", lambda self, drop: []
    )
    gql, _ = channel_listing()
    twitch = bare_twitch(gql)
    accept = gql.claim_drop_rewards
    gql.claim_drop_rewards = lambda instance_id: SimpleNamespace(
        status=None, errors=["service unavailable"]
    )
    ends_at = utc_now() + timedelta(seconds=40)
    claimable = campaign_from_reward_groups(
        reward_campaign(watch_group(progress(status="CLAIMABLE", minutes=60), times=1))
        | {"endAt": stamp(ends_at)},
        now=NOW,
    )

    twitch._Twitch__store_listed_watch_rewards([claimable])

    [retry_at] = claim_retries(twitch).values()
    assert utc_now() < retry_at < ends_at

    # Accepted, there is nothing to come back for.
    gql.claim_drop_rewards = accept
    twitch._Twitch__store_listed_watch_rewards([copy.deepcopy(claimable)])

    assert gql.claims == ["viewer-1#quest-1#group-watch"]
    assert claim_retries(twitch) == {}


def refused_claim_twitch(monkeypatch, claimable):
    """A Twitch with the claimable campaign stored, whose claim Twitch
    refused, and the gql stub: gql.accept is a claim that goes through."""
    monkeypatch.setattr(
        Twitch, "_Twitch__drop_variant_entries_from_drop", lambda self, drop: []
    )
    gql, _ = channel_listing()
    twitch = bare_twitch(gql)
    gql.accept = gql.claim_drop_rewards
    gql.claim_drop_rewards = lambda instance_id: SimpleNamespace(
        status=None, errors=["service unavailable"]
    )
    twitch._Twitch__store_listed_watch_rewards([claimable])
    return twitch, gql


def test_refused_claim_is_tried_again_before_the_next_repeat_opens(monkeypatch):
    # The campaign runs for years; the repeat waiting to be claimed may not
    # be there to claim once the next one has started, 40 seconds from now.
    opens = utc_now().replace(microsecond=0) + timedelta(seconds=40)
    twitch, _ = refused_claim_twitch(
        monkeypatch,
        convert(
            watch_group(
                progress(
                    status="CLAIMABLE",
                    minutes=60,
                    grants=1,
                    window={
                        "startedAt": stamp(opens - timedelta(hours=24)),
                        "expiresAt": stamp(opens),
                    },
                )
            )
        ),
    )

    # The claim again before the opening, on the claim thread. The opening
    # itself is the main loop's, for the category refresh.
    [retry_at] = claim_retries(twitch).values()
    assert utc_now() < retry_at < opens
    assert twitch.watch_reward_pending_starts == {opens}
    monkeypatch.setattr(twitch_module, "utc_now", lambda: opens + timedelta(seconds=1))
    assert twitch.take_opened_watch_reward_start() is True


def test_waiting_claims_are_sent_again_without_a_category_refresh(monkeypatch):
    # Only streamers are configured, or categories the campaign does not
    # count in: no refresh sends the claim again when its moment comes.
    twitch, gql = refused_claim_twitch(
        monkeypatch,
        convert(watch_group(progress(status="CLAIMABLE", minutes=60), times=1)),
    )
    gql.claim_drop_rewards = gql.accept

    claim_again_when_due(twitch)

    assert gql.claims == ["viewer-1#quest-1#group-watch"]
    assert "group-watch" in twitch.accepted_drop_ids

    # Nothing is waiting any more, and no moment is left to wait for.
    assert claim_retries(twitch) == {}
    claim_on_the_claim_thread(twitch)
    assert gql.claims == ["viewer-1#quest-1#group-watch"]


def test_waiting_claim_of_a_campaign_that_is_over_is_not_sent_again(monkeypatch):
    twitch, gql = refused_claim_twitch(
        monkeypatch,
        convert(watch_group(progress(status="CLAIMABLE", minutes=60), times=1)),
    )
    gql.claim_drop_rewards = gql.accept
    twitch.watch_reward_campaigns["quest-1"]["endAt"] = stamp(
        utc_now() - timedelta(minutes=1)
    )

    claim_again_when_due(twitch)

    assert gql.claims == []
    # Nor is its moment kept, to keep a thread waiting for nothing.
    assert claim_retries(twitch) == {}


def test_row_of_a_repeat_lost_waiting_to_be_claimed_is_not_finished(
    monkeypatch, tmp_path
):
    # Should Twitch count a repeat only when it is claimed, one whose claim
    # never went through is followed by the next with the count unchanged.
    # Its number is below the current one, but it was not earned.
    (tmp_path / "drops_by_category.json").write_text(
        json.dumps(
            {
                "drops": [
                    {
                        "drop_id": "group-watch#0",
                        "category": "Example Game",
                        "status": "failed",
                        "failed_to_achieve": True,
                        "current_minutes_watched": 59,
                        "minutes_required": 60,
                    }
                ]
            }
        )
    )
    twitch = report_twitch(monkeypatch, tmp_path)
    for group_self in (
        progress(minutes=59, window=open_window()),
        progress(status="CLAIMABLE", minutes=60, window=open_window()),
        progress(minutes=3, window=open_window()),
    ):
        stored = twitch._Twitch__remember_watch_reward_campaign(
            convert(watch_group(group_self))
        )

    assert stored["timeBasedDrops"][0]["id"] == "group-watch#1"
    row = report_rows(twitch, tmp_path)["group-watch#0"]
    assert (row["status"], row["failed_to_achieve"]) == ("failed", True)


def test_count_going_up_later_does_not_finish_the_row_of_a_lost_repeat(
    monkeypatch, tmp_path
):
    # Counted on the claim, repeat 1 was lost and repeat 2 earned: the count
    # of 1 says one repeat was earned, not that it was the first.
    def row(drop_id, status, minutes):
        return {
            "drop_id": drop_id,
            "category": "Example Game",
            "status": status,
            "failed_to_achieve": status == "failed",
            "current_minutes_watched": minutes,
            "minutes_required": 60,
        }

    (tmp_path / "drops_by_category.json").write_text(
        json.dumps({"drops": [row("group-watch#0", "failed", 59)]})
    )
    twitch = report_twitch(monkeypatch, tmp_path)
    for group_self in (
        progress(minutes=59, window=open_window()),
        progress(status="CLAIMABLE", minutes=60, window=open_window()),
        # The window ended with the claim not made: the next repeat.
        progress(minutes=58, window=open_window()),
    ):
        twitch._Twitch__remember_watch_reward_campaign(convert(watch_group(group_self)))
    save_synced_rows(twitch)
    assert report_rows(twitch, tmp_path)["group-watch#1"]["status"] == "in_progress"

    for group_self in (
        progress(status="CLAIMABLE", minutes=60, window=open_window()),
        progress(minutes=2, grants=1, window=open_window()),
    ):
        stored = twitch._Twitch__remember_watch_reward_campaign(
            convert(watch_group(group_self))
        )

    assert stored["timeBasedDrops"][0]["id"] == "group-watch#2"
    rows = report_rows(twitch, tmp_path)
    assert rows["group-watch#0"]["status"] == "failed"
    assert rows["group-watch#1"]["status"] == "captured"


def test_minutes_alone_do_not_report_a_reward_that_needs_a_subscription_as_earned(
    monkeypatch, tmp_path
):
    twitch = report_twitch(monkeypatch, tmp_path)
    needs_subs = watch_group(progress(minutes=60), times=1)
    needs_subs["progressCriteria"]["requirements"]["subs"] = 2
    twitch._Twitch__remember_watch_reward_campaign(convert(needs_subs))

    save_synced_rows(twitch)

    row = report_rows(twitch, tmp_path)["group-watch"]
    assert (row["status"], row["current_minutes_watched"]) == ("in_progress", 60)


def test_repeat_counted_toward_a_reward_that_comes_with_the_last_is_not_earned(
    caplog,
):
    # Twitch counts each repeat of such a group; the reward is earned, and
    # claimed, once all of them are in.
    def all_repeats(group_self):
        group = watch_group(group_self)
        group["progressCriteria"]["repeatableConfig"][
            "shouldCompleteAllRepeatsToEarn"
        ] = True
        return convert(group)

    twitch = bare_twitch()
    with caplog.at_level(logging.INFO, logger=twitch_module.__name__):
        for group_self in (
            progress(minutes=52, window=open_window()),
            progress(minutes=60, grants=1, window=open_window()),
        ):
            twitch._Twitch__remember_watch_reward_campaign(all_repeats(group_self))

    [record] = [r for r in caplog.records if r.levelno == logging.INFO]
    assert record.getMessage() == (
        "Launch Celebration - Daily Chest (repeat 1 of 6): counted, 1 of 6 "
        "repeats in; the reward comes with the last one"
    )
    assert record.event == Events.DROP_STATUS
    assert record.skip_telegram is True


def test_repeat_counted_and_waiting_to_be_claimed_is_not_logged_as_earned(caplog):
    # Twitch counted it with its minutes, and it has to be claimed: the
    # claim is not sent yet, and may be refused.
    records = remembered_lines(
        caplog,
        progress(minutes=52, window=open_window()),
        progress(status="CLAIMABLE", minutes=60, grants=1, window=open_window()),
    )

    assert [record.getMessage() for record in records] == [
        "Launch Celebration - Daily Chest (repeat 1 of 6): 60/60 minutes watched"
    ]
    assert records[0].event == Events.DROP_STATUS


def test_row_of_a_repeat_waiting_to_be_claimed_is_left_to_its_claim(
    monkeypatch, tmp_path
):
    twitch = report_twitch(monkeypatch, tmp_path)
    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(minutes=52, window=open_window())))
    )
    save_synced_rows(twitch)

    twitch._Twitch__remember_watch_reward_campaign(
        convert(
            watch_group(
                progress(status="CLAIMABLE", minutes=60, grants=1, window=open_window())
            )
        )
    )

    row = report_rows(twitch, tmp_path)["group-watch#0"]
    assert (row["status"], row["current_minutes_watched"]) == ("in_progress", 52)


def test_claim_thread_that_could_not_be_started_has_the_claim_sent_again(
    monkeypatch,
):
    # No other listing need come before the campaign ends.
    class NoThreadLeft(InlineThread):
        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(
        Twitch, "_Twitch__drop_variant_entries_from_drop", lambda self, drop: []
    )
    gql, _ = channel_listing()
    twitch = bare_twitch(gql)
    monkeypatch.setattr(twitch_module, "Thread", NoThreadLeft)

    twitch._Twitch__store_listed_watch_rewards(
        [convert(watch_group(progress(status="CLAIMABLE", minutes=60), times=1))]
    )

    assert gql.claims == []
    [retry_at] = claim_retries(twitch).values()
    assert (retry_at - utc_now()).total_seconds() == pytest.approx(
        WATCH_REWARD_PROGRESS_REFRESH_SECONDS, abs=5
    )

    # No thread waits for that moment, so the main loop looks for it. Not
    # due yet, it starts nothing.
    monkeypatch.setattr(twitch_module, "Thread", InlineThread)
    twitch.start_unattended_watch_reward_claims()
    assert gql.claims == []

    # When it is, the loop has a thread started, which sends the claims.
    twitch.watch_reward_claim_retries["quest-1"] = utc_now() - timedelta(seconds=1)
    twitch.start_unattended_watch_reward_claims()

    assert gql.claims == ["viewer-1#quest-1#group-watch"]
    assert claim_retries(twitch) == {}


def test_claim_due_again_is_put_off_when_no_thread_can_be_started_for_it(
    monkeypatch,
):
    # Left due, it would have the main loop ask for a thread on every look.
    class NoThreadLeft(InlineThread):
        def start(self):
            raise RuntimeError("can't start new thread")

    twitch, gql = refused_claim_twitch(
        monkeypatch,
        convert(watch_group(progress(status="CLAIMABLE", minutes=60), times=1)),
    )
    gql.claim_drop_rewards = gql.accept
    twitch.watch_reward_claim_retries["quest-1"] = utc_now() - timedelta(seconds=1)
    monkeypatch.setattr(twitch_module, "Thread", NoThreadLeft)

    twitch.start_unattended_watch_reward_claims()

    assert gql.claims == []
    assert twitch.watch_reward_claim_thread is None
    [retry_at] = claim_retries(twitch).values()
    assert (retry_at - utc_now()).total_seconds() == pytest.approx(
        WATCH_REWARD_PROGRESS_REFRESH_SECONDS, abs=5
    )


def miner_between_passes(monkeypatch, twitch):
    """A miner around the Twitch given, whose wait for the next pass of
    its main loop is over at once."""
    miner = object.__new__(TwitchChannelPointsMiner)
    miner.twitch = twitch
    twitch.restart_requested = SimpleNamespace(wait=lambda seconds: False)
    monkeypatch.setattr(
        miner_module, "random", SimpleNamespace(uniform=lambda low, high: 0.01)
    )
    return miner


def test_main_loop_has_a_thread_started_for_a_claim_due_with_none_to_send_it(
    monkeypatch,
):
    # No thread could be had when the moment was noted, or the one that
    # waited is gone. The loop is the one thread sure to be there.
    twitch, gql = refused_claim_twitch(
        monkeypatch,
        convert(watch_group(progress(status="CLAIMABLE", minutes=60), times=1)),
    )
    gql.claim_drop_rewards = gql.accept
    twitch.watch_reward_claim_retries["quest-1"] = utc_now() - timedelta(seconds=1)
    assert twitch.watch_reward_claim_thread is None
    miner = miner_between_passes(monkeypatch, twitch)

    assert miner._TwitchChannelPointsMiner__wait_for_next_pass() is False

    assert gql.claims == ["viewer-1#quest-1#group-watch"]


def test_main_loop_wait_survives_an_error_looking_for_a_claim_due_again(
    monkeypatch, caplog
):
    def fail(self):
        raise KeyError("timeBasedDrops")

    monkeypatch.setattr(Twitch, "start_unattended_watch_reward_claims", fail)
    miner = miner_between_passes(monkeypatch, bare_twitch())

    with caplog.at_level(logging.ERROR):
        assert miner._TwitchChannelPointsMiner__wait_for_next_pass() is False

    assert "reward campaign claim to send again" in caplog.text


def test_main_loop_leaves_a_claim_due_again_to_the_thread_that_waits_for_it(
    monkeypatch,
):
    # The claim thread is there, about to send it: the loop neither sends
    # the claim itself nor takes the moment as a call for a category
    # refresh, which would send it once more.
    twitch, gql = refused_claim_twitch(
        monkeypatch,
        convert(watch_group(progress(status="CLAIMABLE", minutes=60), times=1)),
    )
    gql.claim_drop_rewards = gql.accept
    twitch.watch_reward_claim_retries["quest-1"] = utc_now() - timedelta(seconds=1)
    twitch.watch_reward_claim_thread = HeldThread(target=None)
    started = list(InlineThread.started)
    miner = miner_between_passes(monkeypatch, twitch)

    assert miner._TwitchChannelPointsMiner__wait_for_next_pass() is False
    scheduled = time.time() + 3600
    refresh_at = miner._TwitchChannelPointsMiner__refresh_for_opened_reward_window
    assert refresh_at(scheduled) == scheduled

    assert twitch.watch_reward_start_due() is False
    assert gql.claims == []
    assert InlineThread.started == started
    assert list(claim_retries(twitch)) == ["quest-1"]


def test_no_thread_is_started_for_a_claim_to_send_again_once_the_miner_stops(
    monkeypatch,
):
    twitch, _ = refused_claim_twitch(
        monkeypatch,
        convert(watch_group(progress(status="CLAIMABLE", minutes=60), times=1)),
    )
    started = list(InlineThread.started)
    twitch.running = False

    twitch._Twitch__retry_unaccepted_reward_claims("quest-1")
    twitch.watch_reward_claim_retries["quest-1"] = utc_now() - timedelta(seconds=1)
    twitch.start_unattended_watch_reward_claims()

    assert InlineThread.started == started


def eventually(condition, seconds=5.0) -> bool:
    """Whether the condition comes true within the seconds given. For what
    another thread is about to do."""
    until = time.monotonic() + seconds
    while not condition():
        if time.monotonic() >= until:
            return False
        time.sleep(0.002)
    return True


@pytest.fixture
def real_claim_thread(monkeypatch):
    """Claims run on a real thread, which waits for the moments it is to
    send a refused claim again. `ahead` moves the miner's clock on to bring
    such a moment about; `started` holds the threads started; `twitches`
    are stopped when the test is over."""
    clock = SimpleNamespace(ahead=timedelta(0), started=[], twitches=[])

    class CountedThread(threading.Thread):
        def start(self):
            clock.started.append(self)
            super().start()

    monkeypatch.setattr(twitch_module, "Thread", CountedThread)
    monkeypatch.setattr(
        Twitch, "_Twitch__wait_for_reward_claim_retry", REAL_CLAIM_RETRY_WAIT
    )
    # How often the waiting thread reads the clock.
    monkeypatch.setattr(twitch_module, "WATCH_REWARD_CLAIM_STOP_CHECK_SECONDS", 0.005)
    monkeypatch.setattr(twitch_module, "utc_now", lambda: utc_now() + clock.ahead)
    yield clock
    for twitch in clock.twitches:
        twitch.running = False
    for thread in clock.started:
        thread.join(timeout=5)


def refusing_gql():
    """A gql stub that refuses every claim, and the claims it was sent.
    gql.accept is a claim that goes through."""
    gql, _ = channel_listing()
    gql.accept = gql.claim_drop_rewards
    sent = []

    def refuse(instance_id):
        sent.append(instance_id)
        return SimpleNamespace(status=None, errors=["service unavailable"])

    gql.claim_drop_rewards = refuse
    return gql, sent


def waiting_claim_thread(monkeypatch, clock):
    """A Twitch whose claim thread has had the claim of quest-1 refused and
    is to send it again, with the gql stub and the claims sent so far."""
    monkeypatch.setattr(
        Twitch, "_Twitch__drop_variant_entries_from_drop", lambda self, drop: []
    )
    gql, sent = refusing_gql()
    twitch = bare_twitch(gql)
    clock.twitches.append(twitch)
    twitch._Twitch__store_listed_watch_rewards(
        [convert(watch_group(progress(status="CLAIMABLE", minutes=60), times=1))]
    )
    assert eventually(lambda: len(sent) == 1 and claim_retries(twitch))
    return twitch, gql, sent


def test_claim_thread_sends_a_refused_claim_again_with_no_main_loop_to_ask(
    monkeypatch, real_claim_thread
):
    # No main loop runs here: it may be busy for longer than the reward
    # has left. The thread that sent the claim waits for the moment itself.
    twitch, _, sent = waiting_claim_thread(monkeypatch, real_claim_thread)
    [thread] = real_claim_thread.started

    real_claim_thread.ahead = timedelta(
        seconds=WATCH_REWARD_PROGRESS_REFRESH_SECONDS - 30
    )
    time.sleep(0.05)
    assert thread.is_alive()
    assert len(sent) == 1

    real_claim_thread.ahead = timedelta(
        seconds=WATCH_REWARD_PROGRESS_REFRESH_SECONDS + 1
    )
    assert eventually(lambda: len(sent) == 2)
    assert sent == ["viewer-1#quest-1#group-watch"] * 2

    # Refused again, it is waited for again, by the same thread. The main
    # loop was never called for.
    assert eventually(lambda: bool(claim_retries(twitch)))
    assert thread.is_alive()
    assert real_claim_thread.started == [thread]
    assert not getattr(twitch, "watch_reward_pending_starts", None)


def test_claim_thread_is_started_to_wait_for_a_claim_another_had_refused(
    monkeypatch, real_claim_thread
):
    # The campaign sync's pass, say, on its own thread. The claim thread it
    # starts waits for the moment: it does not send the claim again at once.
    monkeypatch.setattr(
        Twitch, "_Twitch__drop_variant_entries_from_drop", lambda self, drop: []
    )
    gql, sent = refusing_gql()
    twitch = bare_twitch(gql)
    real_claim_thread.twitches.append(twitch)
    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(status="CLAIMABLE", minutes=60), times=1))
    )

    twitch._Twitch__claim_waiting_watch_rewards_by_deadline()

    [thread] = real_claim_thread.started
    time.sleep(0.05)
    assert thread.is_alive()
    assert len(sent) == 1

    real_claim_thread.ahead = timedelta(
        seconds=WATCH_REWARD_PROGRESS_REFRESH_SECONDS + 1
    )
    assert eventually(lambda: len(sent) == 2)


def test_stopping_ends_a_claim_thread_that_waits_to_send_a_claim_again(
    monkeypatch, real_claim_thread
):
    # The shutdown joins the thread before it saves the report for the
    # last time: it is not kept waiting for a moment minutes off.
    twitch, _, sent = waiting_claim_thread(monkeypatch, real_claim_thread)
    [thread] = real_claim_thread.started
    assert thread.is_alive()

    twitch.running = False
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert twitch.watch_reward_claim_thread is None
    # Nor is the claim sent when its moment comes.
    real_claim_thread.ahead = timedelta(
        seconds=WATCH_REWARD_PROGRESS_REFRESH_SECONDS + 1
    )
    twitch.start_unattended_watch_reward_claims()
    assert len(sent) == 1
    assert real_claim_thread.started == [thread]


def test_reward_listed_while_the_claim_thread_waits_is_claimed_at_once(
    monkeypatch, real_claim_thread
):
    # The thread waits ten minutes to send a refused claim again. A reward
    # a channel lists meanwhile is not held up behind that.
    twitch, gql, _ = waiting_claim_thread(monkeypatch, real_claim_thread)
    [thread] = real_claim_thread.started
    gql.claim_drop_rewards = gql.accept
    claimable = progress(status="CLAIMABLE", minutes=60, window=open_window())

    twitch._Twitch__store_listed_watch_rewards([other_campaign(claimable)])

    # The pass it asks for sends every waiting claim.
    assert eventually(lambda: len(gql.claims) == 2)
    assert set(gql.claims) == {
        "viewer-1#quest-2#group-two",
        "viewer-1#quest-1#group-watch",
    }
    assert real_claim_thread.ahead == timedelta(0)
    # With nothing left to claim or to send again, the thread ends.
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert twitch.watch_reward_claim_thread is None
    assert claim_retries(twitch) == {}
    assert real_claim_thread.started == [thread]


def test_claim_thread_ends_when_another_gets_the_claim_it_waited_to_send_again(
    monkeypatch, real_claim_thread
):
    twitch, gql, _ = waiting_claim_thread(monkeypatch, real_claim_thread)
    [thread] = real_claim_thread.started
    gql.claim_drop_rewards = gql.accept

    # The campaign sync's pass, say.
    twitch._Twitch__claim_waiting_watch_rewards_by_deadline()

    assert gql.claims == ["viewer-1#quest-1#group-watch"]
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert twitch.watch_reward_claim_thread is None
    assert real_claim_thread.started == [thread]


def test_claim_to_send_again_is_not_dropped_for_a_record_replaced_meanwhile(
    monkeypatch,
):
    # The category evaluation, say, finds nothing of the campaign waiting to
    # be claimed and so has nothing to send again. Before it has acted on
    # that, a channel listing stores the reward as claimable, its claim is
    # refused and the moment to send it again is noted. That moment belongs
    # to the record now stored, not to the one the evaluation read.
    monkeypatch.setattr(
        Twitch, "_Twitch__drop_variant_entries_from_drop", lambda self, drop: []
    )
    twitch = bare_twitch()
    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(minutes=30, window=open_window()), times=1))
    )
    assert claim_retries(twitch) == {}
    claimable = convert(watch_group(progress(status="CLAIMABLE", minutes=60), times=1))
    # As a listing stores it: with the ID its claim is sent under.
    claimable["timeBasedDrops"][0]["self"]["dropInstanceID"] = "claim-id"
    evaluation = threading.current_thread()
    listed = threading.Event()
    seen = []

    def listing_stores_the_reward_and_its_claim_is_refused():
        with twitch_module._watch_reward_lock(twitch):
            twitch.watch_reward_campaigns["quest-1"] = claimable
        seen.append(twitch._Twitch__note_reward_claim_retry("quest-1"))
        listed.set()

    listing_thread = threading.Thread(
        target=listing_stores_the_reward_and_its_claim_is_refused
    )
    is_open = Twitch._Twitch__is_open_drop_campaign

    def record_is_read(self, campaign):
        if threading.current_thread() is evaluation and not listing_thread.ident:
            # The evaluation has its record. The listing gets its turn here
            # if it can have one: not while the record is being decided on.
            listing_thread.start()
            listed.wait(0.2)
        return is_open(self, campaign)

    monkeypatch.setattr(Twitch, "_Twitch__is_open_drop_campaign", record_is_read)

    assert twitch._Twitch__note_reward_claim_retry("quest-1") is False

    listing_thread.join(timeout=5)
    assert seen == [True]
    assert list(claim_retries(twitch)) == ["quest-1"]


def test_claim_thread_that_could_not_be_started_does_not_block_the_next_one(
    monkeypatch,
):
    class NoThreadLeft(InlineThread):
        def start(self):
            raise RuntimeError("can't start new thread")

    gql, _ = channel_listing(
        watch_group(progress(status="CLAIMABLE", minutes=60, window=open_window()))
    )
    twitch = bare_twitch(gql)
    monkeypatch.setattr(twitch_module, "Thread", NoThreadLeft)

    # The listing itself is not failed by it.
    [campaign], _, _ = twitch._Twitch__get_campaigns_from_channel_id("1")
    assert campaign["timeBasedDrops"][0]["_reward_group"]["claimable"] is True
    assert gql.claims == []

    monkeypatch.setattr(twitch_module, "Thread", InlineThread)
    monkeypatch.setattr(
        Twitch, "_Twitch__drop_variant_entries_from_drop", lambda self, drop: []
    )
    twitch._Twitch__get_campaigns_from_channel_id("1")

    assert gql.claims == ["viewer-1#quest-1#group-watch"]


# The campaign sync between two refreshes of its campaigns (#371).

campaign_module = importlib.import_module(
    "TwitchChannelPointsMiner.classes.entities.Campaign"
)
drop_module = importlib.import_module("TwitchChannelPointsMiner.classes.entities.Drop")


def ordinary_campaign(minutes=0):
    """A Drop campaign as the dashboard, its details and the inventory list
    it."""
    return {
        "id": "campaign-1",
        "game": {"id": "777", "displayName": "Example Game"},
        "name": "Example Campaign",
        "status": "ACTIVE",
        "allow": {"channels": None},
        "startAt": START_AT,
        "endAt": END_AT,
        "timeBasedDrops": [
            {
                "id": "drop-1",
                "name": "Reward",
                "benefitEdges": [{"benefit": {"name": "Skin"}}],
                "requiredMinutesWatched": 100,
                "startAt": START_AT,
                "endAt": END_AT,
                "self": {
                    "hasPreconditionsMet": True,
                    "currentMinutesWatched": minutes,
                    "dropInstanceID": None,
                    "isClaimed": False,
                },
            }
        ],
    }


def entity_state(campaign):
    """All a campaign entity and its drops hold."""
    return {
        **{
            slot: getattr(campaign, slot)
            for slot in Campaign.__slots__
            if slot != "drops"
        },
        "drops": [
            {slot: getattr(drop, slot) for slot in Drop.__slots__}
            for drop in campaign.drops
        ],
    }


class CampaignSyncLoop:
    """The real campaign sync loop, a pass a minute, on a clock the test
    moves. Its first pass refreshes the campaigns, and the next refresh is
    15 minutes on. What a pass asks of Twitch is recorded in `asked`."""

    def __init__(self, monkeypatch, twitch, campaigns_ids=("quest-1",)):
        self.twitch = twitch
        self.now = NOW
        self.seconds = 0
        self.passes = 0
        self.asked = []
        self.dashboard = []
        self.details = {}
        self.inventory = []
        self.before_pass = {}
        # By pass: the loop's campaigns, all they held, and the stream's.
        self.campaigns = []
        self.states = []
        self.stream_campaigns = []
        self.streamer = SimpleNamespace(
            username="quest-channel",
            channel_id="4242",
            is_online=True,
            is_watching=False,
            from_category=True,
            settings=SimpleNamespace(claim_drops=True),
            stream=SimpleNamespace(
                campaigns=[], campaigns_ids=list(campaigns_ids), game=dict(CATEGORY)
            ),
        )
        twitch.running = True
        twitch.prompt_claim_pass_lock = threading.Lock()
        twitch.campaign_details_withheld_streak = 0
        for name, request in list(vars(twitch.gql).items()):
            if callable(request):
                setattr(twitch.gql, name, self.recorded(name, request))
        twitch.gql.post_gql_request_batch_raw = self.campaign_details
        for module in (twitch_module, campaign_module, drop_module):
            monkeypatch.setattr(module, "utc_now", lambda: self.now)
        monkeypatch.setattr(time, "time", lambda: 1_700_000_000 + self.seconds)
        monkeypatch.setattr(time, "monotonic", lambda: 50_000 + self.seconds)
        sync_drop_progress = Twitch._Twitch__sync_drop_progress

        def synced(twitch, campaigns, *args):
            result = sync_drop_progress(twitch, campaigns, *args)
            self.synced = result[0]
            return result

        for name, stand_in in {
            "claim_all_drops_from_inventory": lambda twitch: None,
            "_Twitch__get_drops_dashboard": lambda twitch: self.drops_dashboard(),
            "_Twitch__get_inventory": lambda twitch: self.get_inventory(),
            "_Twitch__save_drop_snapshot_from_dict": lambda *args, **kwargs: None,
            "_Twitch__drop_variant_entries_from_drop": lambda twitch, drop: [],
            "_Twitch__sync_drop_progress": synced,
            "_Twitch__chuncked_sleep": lambda twitch, seconds, chunk_size=3: (
                self.slept()
            ),
        }.items():
            monkeypatch.setattr(Twitch, name, stand_in)

    def recorded(self, name, request):
        def asked(*args, **kwargs):
            self.asked.append((self.passes, name))
            return request(*args, **kwargs)

        return asked

    def drops_dashboard(self):
        self.asked.append((self.passes, "dashboard"))
        twitch_module._drops_dashboard_state.unavailable = False
        return copy.deepcopy(self.dashboard)

    def campaign_details(self, operation, requests):
        self.asked.append((self.passes, "details"))
        return [
            {
                "data": {
                    "user": {
                        "dropCampaign": copy.deepcopy(
                            self.details.get(request["variables"]["dropID"])
                        )
                    }
                }
            }
            for request in requests
        ]

    def get_inventory(self):
        self.asked.append((self.passes, "inventory"))
        return {"dropCampaignsInProgress": copy.deepcopy(self.inventory)}

    def slept(self):
        self.campaigns.append(list(self.synced))
        self.states.append([entity_state(campaign) for campaign in self.synced])
        self.stream_campaigns.append(list(self.streamer.stream.campaigns))
        self.passes += 1
        self.seconds += 60
        self.now += timedelta(seconds=60)
        if self.passes >= self.limit:
            self.twitch.running = False
        else:
            self.before_pass.get(self.passes, lambda: None)()

    def run(self, passes):
        self.limit = passes
        self.twitch.sync_campaigns([self.streamer])
        return self

    def asked_in(self, number):
        return [what for asked_in, what in self.asked if asked_in == number]

    def move_clock_before_pass(self, number, moment):
        self.before_pass[number] = lambda: setattr(self, "now", moment)


def list_on_a_channel(twitch, campaign_id="quest-1"):
    """File the stored campaign as a channel's check does for a campaign
    the channel lists with something to watch for."""
    twitch.advertised_drop_campaigns[campaign_id] = (
        twitch._Twitch__normalize_advertised_campaign(
            with_reward_category(
                twitch.watch_reward_campaigns[campaign_id], game_id=CATEGORY["id"]
            )
        )
    )


OPENS_AT = NOW + timedelta(hours=1)
OPENED = OPENS_AT + timedelta(minutes=1)


def waiting_repeat_twitch(monkeypatch, listed=True):
    """A Twitch that claimed the first repeat of a group, the next to be
    earned from OPENS_AT. The campaign was listed on a channel while the
    first was watched for."""
    twitch, _, _ = claimed_repeat_twitch(
        monkeypatch, OPENS_AT.replace(tzinfo=timezone.utc)
    )
    if listed:
        list_on_a_channel(twitch)
    return twitch


def waiting_repeat_loop(monkeypatch, listed=True):
    return CampaignSyncLoop(monkeypatch, waiting_repeat_twitch(monkeypatch, listed))


def drop_ids(campaigns):
    return [[drop.id for drop in campaign.drops] for campaign in campaigns]


def state_drop_ids(states):
    return [[drop["id"] for drop in state["drops"]] for state in states]


def test_repeat_whose_window_opens_between_refreshes_is_watched_from_the_next_sync(
    monkeypatch, caplog
):
    # The refresh found nothing to watch for and left the campaign out. The
    # next one is 15 minutes away.
    loop = waiting_repeat_loop(monkeypatch)
    loop.twitch.log_drop_checks = True
    loop.move_clock_before_pass(2, OPENED)

    with caplog.at_level(logging.DEBUG):
        loop.run(5)

    assert loop.stream_campaigns[:2] == [[], []]
    for watched in loop.stream_campaigns[2:]:
        [campaign] = watched
        assert campaign.game["id"] == CATEGORY["id"]
        assert [drop.id for drop in campaign.drops] == ["group-watch#1"]
        assert campaign.has_watchable_drops() is True
    # Without a word to Twitch: only the refresh asked it anything.
    assert [asked_in for asked_in, _ in loop.asked] == [0, 0]
    # Added once: the passes after it find the repeat there.
    [campaign] = loop.campaigns[2]
    assert loop.campaigns[4] == [campaign]
    assert loop.campaigns[4][0].drops[0] is campaign.drops[0]
    assert [
        record.getMessage()
        for record in caplog.records
        if "from this sync on" in record.getMessage()
    ] == [
        "[drops-check] reward campaign quest-1: watching for Daily Chest "
        "(repeat 2 of 6) from this sync on; opened since the campaigns were "
        "refreshed"
    ]


def two_group_listing(first, second):
    return convert(
        watch_group(first),
        watch_group(second) | {"id": "group-two", "name": "Weekly Chest"},
    )


def test_repeat_that_opens_is_added_to_the_campaign_the_sync_holds(monkeypatch):
    monkeypatch.setattr(
        Twitch, "_Twitch__drop_variant_entries_from_drop", lambda self, drop: []
    )
    gql, _ = channel_listing()
    twitch = bare_twitch(gql)
    twitch._Twitch__store_listed_watch_rewards(
        [
            two_group_listing(
                progress(
                    status="CLAIMED",
                    minutes=60,
                    grants=1,
                    window=open_window(),
                    next_at=stamp(OPENS_AT),
                ),
                progress(minutes=12, window=open_window()),
            )
        ]
    )
    list_on_a_channel(twitch)
    loop = CampaignSyncLoop(monkeypatch, twitch)
    loop.move_clock_before_pass(1, OPENED)

    loop.run(2)

    assert state_drop_ids(loop.states[0]) == [["group-two#0"]]
    # In the order of the record, which is the order a refresh holds them
    # in, and beside the drop the campaign had: the same object, with the
    # progress the streams' views of it share.
    assert state_drop_ids(loop.states[1]) == [["group-watch#1", "group-two#0"]]
    assert loop.campaigns[1][0] is loop.campaigns[0][0]
    assert loop.campaigns[1][0].drops[1] is loop.stream_campaigns[0][0].drops[0]
    assert drop_ids(loop.stream_campaigns[0]) == [["group-two#0"]]
    assert drop_ids(loop.stream_campaigns[1]) == [["group-watch#1", "group-two#0"]]
    assert loop.asked_in(1) == []


def test_repeat_first_stored_after_the_refresh_reaches_the_stream_of_its_category(
    monkeypatch,
):
    # The refresh knew the first repeat only. The campaign it built says
    # which category each of its drops counts in, and the stream gets the
    # drops that count in its own.
    monkeypatch.setattr(
        Twitch, "_Twitch__drop_variant_entries_from_drop", lambda self, drop: []
    )
    gql, _ = channel_listing()
    twitch = bare_twitch(gql)
    twitch._Twitch__store_listed_watch_rewards(
        [convert(watch_group(progress(minutes=12, window=open_window())))]
    )
    list_on_a_channel(twitch)
    loop = CampaignSyncLoop(monkeypatch, twitch)
    # Twitch counted the repeat by itself, and the next window has begun.
    loop.before_pass[1] = lambda: twitch._Twitch__store_listed_watch_rewards(
        [convert(watch_group(progress(minutes=0, grants=1, window=open_window())))]
    )

    loop.run(2)

    assert drop_ids(loop.stream_campaigns[0]) == [["group-watch#0"]]
    [campaign] = loop.stream_campaigns[1]
    watched_for = {drop.id: drop for drop in campaign.drops}["group-watch#1"]
    assert watched_for in campaign.watchable_drops()
    assert loop.asked_in(1) == []


def refreshed_at(monkeypatch, loop):
    """What a refresh made at the loop's last pass holds, from the records
    the loop's Twitch has."""
    gql, _ = channel_listing()
    twitch = bare_twitch(gql)
    twitch.watch_reward_campaigns = copy.deepcopy(loop.twitch.watch_reward_campaigns)
    twitch.advertised_drop_campaigns = copy.deepcopy(
        loop.twitch.advertised_drop_campaigns
    )
    refresh = CampaignSyncLoop(monkeypatch, twitch)
    refresh.now = loop.now - timedelta(seconds=60)
    refresh.run(1)
    return refresh.states[0]


def test_campaign_added_between_refreshes_is_the_one_a_refresh_builds(monkeypatch):
    loop = waiting_repeat_loop(monkeypatch)
    loop.move_clock_before_pass(1, OPENED)
    loop.run(2)

    assert state_drop_ids(loop.states[1]) == [["group-watch#1"]]
    assert loop.states[1] == refreshed_at(monkeypatch, loop)


def test_repeat_added_to_a_held_campaign_leaves_it_as_a_refresh_builds_it(
    monkeypatch,
):
    monkeypatch.setattr(
        Twitch, "_Twitch__drop_variant_entries_from_drop", lambda self, drop: []
    )
    gql, _ = channel_listing()
    twitch = bare_twitch(gql)
    twitch._Twitch__store_listed_watch_rewards(
        [
            two_group_listing(
                progress(
                    status="CLAIMED",
                    minutes=60,
                    grants=1,
                    window=open_window(),
                    next_at=stamp(OPENS_AT),
                ),
                progress(minutes=12, window=open_window()),
            )
        ]
    )
    list_on_a_channel(twitch)
    loop = CampaignSyncLoop(monkeypatch, twitch)
    loop.move_clock_before_pass(1, OPENED)
    loop.run(2)

    assert state_drop_ids(loop.states[1]) == [["group-watch#1", "group-two#0"]]
    assert loop.states[1] == refreshed_at(monkeypatch, loop)


def test_repeat_added_takes_the_place_of_the_one_before_it_in_the_campaign(
    monkeypatch,
):
    # The refresh built the campaign while the first repeat was watched
    # for. That one was claimed since, and the record has the next in its
    # place; the campaign still holds the first, at the minutes it had.
    monkeypatch.setattr(
        Twitch, "_Twitch__drop_variant_entries_from_drop", lambda self, drop: []
    )
    gql, _ = channel_listing()
    twitch = bare_twitch(gql)
    window = open_window()

    def listed(group_self):
        twitch._Twitch__store_listed_watch_rewards([convert(watch_group(group_self))])

    listed(progress(minutes=50, window=window))
    list_on_a_channel(twitch)
    loop = CampaignSyncLoop(monkeypatch, twitch)
    loop.before_pass[1] = lambda: [
        listed(group_self)
        for group_self in (
            progress(status="CLAIMABLE", minutes=60, grants=1, window=window),
            progress(
                status="CLAIMED",
                minutes=60,
                grants=1,
                window=window,
                next_at=stamp(OPENS_AT),
            ),
        )
    ]
    loop.move_clock_before_pass(2, OPENED)

    loop.run(3)

    assert state_drop_ids(loop.states[1]) == [["group-watch#0"]]
    assert state_drop_ids(loop.states[2]) == [["group-watch#1"]]
    assert drop_ids(loop.stream_campaigns[2]) == [["group-watch#1"]]
    assert loop.asked_in(2) == []
    assert loop.states[2] == refreshed_at(monkeypatch, loop)


def test_campaign_the_dashboard_named_comes_back_without_a_channel_listing_it(
    monkeypatch,
):
    # The refresh took it on the dashboard's word, and would again.
    loop = waiting_repeat_loop(monkeypatch, listed=False)
    loop.dashboard = [listing()]
    loop.move_clock_before_pass(1, OPENED)

    loop.run(2)

    assert [drop_ids(watched) for watched in loop.stream_campaigns] == [
        [],
        [["group-watch#1"]],
    ]
    assert loop.asked_in(1) == []


def test_record_takes_the_place_of_the_campaign_as_twitch_details_it(monkeypatch):
    # Before any channel listed it, the refresh had the campaign from the
    # dashboard, with the details Twitch gives of it as a Drop campaign:
    # filed under a category of its own that no stream is in.
    monkeypatch.setattr(
        Twitch, "_Twitch__drop_variant_entries_from_drop", lambda self, drop: []
    )
    gql, _ = channel_listing()
    twitch = bare_twitch(gql)
    loop = CampaignSyncLoop(monkeypatch, twitch)
    loop.dashboard = [listing()]
    loop.details["quest-1"] = ordinary_campaign() | {
        "id": "quest-1",
        "game": {"id": "509663", "displayName": "Special Events"},
    }

    def channel_lists_it():
        twitch._Twitch__store_listed_watch_rewards(
            [convert(watch_group(progress(minutes=12, window=open_window())))]
        )
        list_on_a_channel(twitch)

    loop.before_pass[1] = channel_lists_it

    loop.run(2)

    assert [campaign.reward_categories for campaign in loop.campaigns[0]] == [[]]
    assert loop.stream_campaigns[0] == []
    [campaign] = loop.campaigns[1]
    assert campaign.reward_categories != []
    assert drop_ids(loop.stream_campaigns[1]) == [["group-watch#0"]]
    assert loop.stream_campaigns[1][0].drops[0].current_minutes_watched == 12
    assert loop.asked_in(1) == []


@pytest.mark.parametrize("why", ["named as closed", "over", "not taken by a refresh"])
def test_campaign_a_refresh_would_not_take_is_not_added_between_refreshes(
    monkeypatch, why
):
    loop = waiting_repeat_loop(monkeypatch, listed=why != "not taken by a refresh")
    opened = OPENED
    if why == "named as closed":
        # By Twitch's account-wide listing, ahead of its end date.
        loop.twitch._Twitch__reward_campaign_named_closed(
            "quest-1", named_open=False, named_closed=True
        )
    elif why == "over":
        loop.twitch.watch_reward_campaigns["quest-1"]["endAt"] = stamp(
            OPENS_AT + timedelta(seconds=30)
        )
    loop.move_clock_before_pass(1, opened)

    loop.run(3)

    assert loop.campaigns == [[], [], []]
    assert loop.stream_campaigns == [[], [], []]


def test_reward_the_account_has_the_badge_of_is_not_added_every_sync(
    monkeypatch, caplog
):
    # The sync takes such a drop out again: added back, it would come and go
    # with every pass.
    loop = waiting_repeat_loop(monkeypatch)
    loop.twitch.log_drop_checks = True
    monkeypatch.setattr(
        Twitch,
        "_Twitch__drop_badge_skip_reason",
        lambda self, campaign, drop: "badge already owned",
    )
    loop.move_clock_before_pass(1, OPENED)

    with caplog.at_level(logging.DEBUG):
        loop.run(3)

    assert loop.campaigns == [[], [], []]
    assert not [record for record in caplog.records if "quest-1" in record.getMessage()]


def drop_campaign_loop(monkeypatch, twitch):
    """A sync loop over a Drop campaign the dashboard names, on a watched
    channel that lists it and the reward campaign. Twitch reports a minute
    more of its drop with every pass."""
    loop = CampaignSyncLoop(monkeypatch, twitch, ("campaign-1", "quest-1"))
    loop.dashboard = [ordinary_campaign()]
    loop.details["campaign-1"] = ordinary_campaign()
    loop.inventory = [ordinary_campaign(minutes=3)]
    loop.streamer.is_watching = True
    twitch.gql.get_drop_current_session = loop.recorded(
        "session",
        lambda channel_id: SimpleNamespace(
            has_session=True,
            drop_id="drop-1",
            current_minutes_watched=3 + loop.passes,
            required_minutes_watched=100,
            errors=[],
        ),
    )
    return loop


def test_drop_campaigns_are_synced_the_same_beside_a_reward_campaign(monkeypatch):
    gql, _ = channel_listing()
    alone = drop_campaign_loop(monkeypatch, bare_twitch(gql)).run(6)

    beside = drop_campaign_loop(monkeypatch, waiting_repeat_twitch(monkeypatch))
    beside.move_clock_before_pass(3, OPENED)
    beside.run(6)

    # The reward campaign came in between two refreshes.
    assert [[campaign.id for campaign in held] for held in beside.campaigns] == (
        [["campaign-1"]] * 3 + [["campaign-1", "quest-1"]] * 3
    )
    # The Drop campaign is the object the refresh built, asked about and
    # updated as it is without a reward campaign.
    assert all(held[0] is beside.campaigns[0][0] for held in beside.campaigns)
    assert [state[0] for state in beside.states] == [state[0] for state in alone.states]
    assert beside.states[5][0]["drops"][0]["current_minutes_watched"] == 8
    assert beside.asked == alone.asked
    assert beside.asked_in(0) == ["dashboard", "details", "inventory"]
    assert all("session" in beside.asked_in(number) for number in range(1, 6))
    assert [
        [campaign.id for campaign in watched] for watched in beside.stream_campaigns
    ] == [["campaign-1"]] * 3 + [["campaign-1", "quest-1"]] * 3


def test_sync_goes_on_when_an_opened_repeat_cannot_be_added(monkeypatch, caplog):
    beside = drop_campaign_loop(monkeypatch, waiting_repeat_twitch(monkeypatch))
    beside.twitch.log_drop_checks = True
    beside.move_clock_before_pass(1, OPENED)

    def fails(self, campaigns, refreshed_ids):
        raise KeyError("timeBasedDrops")

    monkeypatch.setattr(Twitch, "_Twitch__with_opened_watch_reward_drops", fails)

    with caplog.at_level(logging.DEBUG):
        beside.run(3)

    assert [[campaign.id for campaign in held] for held in beside.campaigns] == (
        [["campaign-1"]] * 3
    )
    assert [drop_ids(watched) for watched in beside.stream_campaigns] == (
        [[["drop-1"]]] * 3
    )
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert (
        "[drops-check] unable to add the reward campaign repeats that opened: "
        "'timeBasedDrops'"
    ) in [record.getMessage() for record in caplog.records]
