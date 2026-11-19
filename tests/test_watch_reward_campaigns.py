"""Reward campaigns (quests) earned by watching (#348).

The payloads follow what Twitch sent for a real sitewide campaign: no game,
one repeating watch-time group, subscription groups, and a final group Twitch
reports no progress on.
"""

import copy
import logging
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest

from TwitchChannelPointsMiner.classes.entities.Campaign import (
    Campaign,
    CampaignChannelScope,
    campaign_channel_scope,
    campaign_from_channel_progress,
)
from TwitchChannelPointsMiner.classes.entities.Drop import Drop, utc_now
from TwitchChannelPointsMiner.classes.entities.RewardCampaign import (
    campaign_from_reward_groups,
    is_watch_reward_campaign,
    is_watch_reward_listing,
    reward_category_views,
    with_claim_instance_ids,
    with_reward_category,
)
from TwitchChannelPointsMiner.classes import Twitch as twitch_module
from TwitchChannelPointsMiner.TwitchChannelPointsMiner import (
    TwitchChannelPointsMiner,
)
from TwitchChannelPointsMiner.classes.Twitch import (
    WATCH_REWARD_FINISHED_REFRESH_SECONDS,
    WATCH_REWARD_PROGRESS_REFRESH_SECONDS,
    WATCH_REWARD_UNLISTED_SECONDS,
    Twitch,
)

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
    assert drop["name"] == "Daily Chest (0/6 earned)"
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

    assert twitch.claim_drop(claimable(0)) is True
    # The same repeat again, before the listing catches up: not re-sent.
    assert twitch.claim_drop(claimable(0)) is True
    assert sent == [instance_id]

    assert twitch.claim_drop(claimable(1)) is True
    assert sent == [instance_id, instance_id]
    assert twitch.accepted_drop_ids >= {"group-watch#0", "group-watch#1"}


def test_accepted_claim_does_not_hide_the_next_repeat_under_the_same_drop_id():
    # Twitch may count a repeat before its claim: the claimed drop and the
    # next repeat then both read "1 granted".
    twitch = bare_twitch()
    twitch.accepted_drop_ids = frozenset({"group-watch#1", "other-drop"})

    # Still waiting to be claimed (the listing lags the claim): kept hidden.
    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(status="CLAIMABLE", minutes=60, grants=1)))
    )
    assert twitch.accepted_drop_ids == {"group-watch#1", "other-drop"}

    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(minutes=0, grants=1)))
    )
    assert twitch.accepted_drop_ids == {"other-drop"}


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
        silent_channels={"1", "2", "3"},
    )
    directory_calls = []

    evaluate(
        monkeypatch,
        twitch,
        [listing()],
        requested=("other-game",),
        channels=("1", "2", "3"),
        directory_calls=directory_calls,
    )

    # The configured category first, two channels in each, four in all.
    assert directory_calls == [("slug-888", False), ("slug-777", False)]
    assert listing_calls == ["1", "2", "1", "2"]


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


def test_reopened_repeat_is_claimed_again_under_the_same_drop_id(monkeypatch):
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
    # under the same drop ID, and is claimable again within the hour.
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
    twitch._Twitch__claim_watch_reward_drops(stored)
    assert len(gql.claims) == 1

    # A listing that still reports it claimable brings the claim back.
    twitch._Twitch__get_campaigns_from_channel_id("1")
    assert len(gql.claims) == 2


def test_error_while_claiming_does_not_fail_the_channel_listing(monkeypatch):
    gql, _ = channel_listing(watch_group(progress(status="CLAIMABLE", minutes=60)))
    twitch = bare_twitch(gql)

    def broken_claim(self, campaign):
        raise OSError("analytics disk full")

    monkeypatch.setattr(Twitch, "_Twitch__claim_watch_reward_drops", broken_claim)

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


def test_accepted_claims_are_reopened_under_the_lock_the_record_is_stored_under(
    monkeypatch,
):
    # Reopening after the lock is released lets a newer listing be stored
    # and claimed in between, and its accepted claim be forgotten.
    twitch = bare_twitch()
    reopen = Twitch._Twitch__reopen_watch_reward_drops
    held = []

    def reopen_checking_the_lock(self, stored):
        held.append(twitch_module._watch_reward_lock(self).locked())
        return reopen(self, stored)

    monkeypatch.setattr(
        Twitch, "_Twitch__reopen_watch_reward_drops", reopen_checking_the_lock
    )

    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(minutes=30, window=open_window())))
    )

    assert held == [True]


def test_claim_answered_after_the_next_repeat_was_listed_does_not_hide_it(
    monkeypatch,
):
    # Twitch counts the repeat before its claim, so the next repeat has the
    # claimed one's drop ID. Another thread stores it while the claim request
    # is on its way: nothing is accepted yet for it to reopen.
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
    assert "group-watch#1" not in twitch.accepted_drop_ids

    # The repeat stored meanwhile is claimed in its turn.
    twitch._Twitch__get_campaigns_from_channel_id("1")
    assert gql.claims == ["viewer-1#quest-1#group-watch"] * 2


def test_next_window_of_a_waiting_repeat_is_an_upcoming_start():
    twitch = bare_twitch()
    twitch.twitchdrops_app_upcoming_starts = {}
    assert twitch.next_upcoming_drop_start() is None

    opens = NOW + timedelta(hours=1)
    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(grants=1, next_at=stamp(opens))))
    )
    assert twitch.next_upcoming_drop_start() == opens

    # Not once the campaign is over.
    twitch.watch_reward_campaigns["quest-1"]["endAt"] = "2020-06-01T00:00:00Z"
    assert twitch.next_upcoming_drop_start() is None


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

    first = ("group-watch#0", "Daily Chest (0/6 earned)")
    assert listed(progress(minutes=59, window=open_window())) == first
    claimable = progress(status="CLAIMABLE", minutes=60, grants=1, window=open_window())
    assert listed(claimable) == first
    # Still waiting to be claimed on the next listing.
    assert listed(claimable) == first
    assert listed(progress(minutes=5, grants=1, window=open_window())) == (
        "group-watch#1",
        "Daily Chest (1/6 earned)",
    )


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
                "Daily Chest (0/6 earned)",
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


def test_reopening_a_repeat_does_not_wait_for_a_claim_in_progress():
    # The claim lock is held for as long as Twitch takes to answer a claim,
    # and the listing is read on the thread that sends the watch events.
    twitch = bare_twitch()
    twitch.accepted_drop_ids = frozenset({"group-watch#1"})
    asked = []
    twitch.claim_drop_lock = SimpleNamespace(
        acquire=lambda blocking=True: asked.append(blocking) or False
    )

    twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(progress(minutes=5, grants=1, window=open_window())))
    )

    assert asked == [False]
    # Left for the next listing.
    assert twitch.accepted_drop_ids == {"group-watch#1"}


def test_finished_group_is_its_last_repeat_not_one_more():
    # Six repeats are watched as #0 to #5 whichever way Twitch counts.
    [earned] = convert(watch_group(progress(status="CLAIMED", grants=6)))[
        "timeBasedDrops"
    ]
    assert (earned["id"], earned["name"]) == (
        "group-watch#5",
        "Daily Chest (6/6 earned)",
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


def test_claimable_repeat_schedules_the_return_for_its_next_window():
    # Claimed during an evaluation, it leaves nothing to watch and so no
    # channel to report the next window: Twitch's word for it is kept.
    twitch = bare_twitch()
    twitch.twitchdrops_app_upcoming_starts = {}
    opens = NOW + timedelta(hours=1)
    claimable = progress(
        status="CLAIMABLE", minutes=60, window=open_window(), next_at=stamp(opens)
    )

    [drop] = twitch._Twitch__remember_watch_reward_campaign(
        convert(watch_group(claimable))
    )["timeBasedDrops"]

    assert drop["_reward_group"]["next_start"] == stamp(opens)
    assert twitch.next_upcoming_drop_start() == opens

    # Without a next eligibility time, the window's end is when to look.
    del claimable["nextEligibleAt"]
    twitch._Twitch__remember_watch_reward_campaign(convert(watch_group(claimable)))
    assert twitch.next_upcoming_drop_start() == NOW + timedelta(hours=23)

    # The last repeat has no next window.
    claimable["grantCount"] = 6
    twitch._Twitch__remember_watch_reward_campaign(convert(watch_group(claimable)))
    assert twitch.next_upcoming_drop_start() is None


def test_category_refresh_is_brought_forward_to_a_start_learned_later(monkeypatch):
    miner = object.__new__(TwitchChannelPointsMiner)
    starts = []
    miner.twitch = SimpleNamespace(
        next_upcoming_drop_start=lambda: starts[0] if starts else None
    )
    refresh_by = miner._TwitchChannelPointsMiner__refresh_by_next_drop_start
    scheduled = time.time() + 6 * 3600

    assert refresh_by(None) is None
    assert refresh_by(scheduled) == scheduled

    # A channel listing reports the next window an hour away.
    starts.append(utc_now() + timedelta(hours=1))
    assert refresh_by(scheduled) == pytest.approx(time.time() + 3605, abs=5)
    # A start further off than the refresh changes nothing.
    assert refresh_by(time.time() + 60) == pytest.approx(time.time() + 60, abs=5)


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


def test_read_for_an_opened_window_is_retried_when_no_channel_answers(monkeypatch):
    twitch, _, _ = evaluation_twitch(
        watch_group(progress(minutes=5, grants=1, window=open_window())),
        silent_channels=("9",),
    )
    twitch.twitchdrops_app_upcoming_starts = {}
    twitch._Twitch__remember_watch_reward_campaign(
        convert(
            watch_group(progress(status="CLAIMABLE", minutes=60, window=open_window()))
        )
    )
    [drop] = twitch.watch_reward_campaigns["quest-1"]["timeBasedDrops"]
    drop["_reward_group"]["next_start"] = stamp(NOW - timedelta(seconds=30))
    # Nothing is retried before a read was made for the window: a campaign
    # none of whose categories is configured never gets one.
    assert twitch.next_upcoming_drop_start() is None
    # Nor after a read made before it opened.
    details = twitch.watch_reward_campaigns["quest-1"]["_watch_reward"]
    details["refresh_attempted_at"] = time.time() - 3600
    assert twitch.next_upcoming_drop_start() is None

    assert evaluate(monkeypatch, twitch, [listing()], channels=("9",))[0] == {}

    retry_in = (twitch.next_upcoming_drop_start() - utc_now()).total_seconds()
    assert retry_in == pytest.approx(WATCH_REWARD_PROGRESS_REFRESH_SECONDS, abs=5)


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
