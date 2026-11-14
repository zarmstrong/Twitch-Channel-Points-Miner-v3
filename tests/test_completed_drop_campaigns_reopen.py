import logging

from TwitchChannelPointsMiner.classes.Twitch import Twitch


def bare_twitch():
    twitch = object.__new__(Twitch)
    twitch.completed_drop_campaigns = set()
    twitch.log_drop_checks = False
    return twitch


def in_progress_campaign(campaign_id, drops):
    return {"id": campaign_id, "timeBasedDrops": drops}


def drop(drop_id, claimed=False, minutes=0, required=10):
    return {
        "id": drop_id,
        "requiredMinutesWatched": required,
        "self": {
            "dropInstanceID": f"instance-{drop_id}",
            "isClaimed": claimed,
            "currentMinutesWatched": minutes,
        },
    }


def merge(twitch, inventory):
    return twitch._Twitch__merge_completed_drop_campaigns(inventory)


def test_campaign_reopened_by_new_unfinished_drop_leaves_completed_set():
    twitch = bare_twitch()
    finished = in_progress_campaign("campaign-1", [drop("d1", claimed=True)])
    merge(twitch, {"dropCampaignsInProgress": [finished]})
    assert "campaign-1" in twitch.completed_drop_campaigns

    reopened = in_progress_campaign(
        "campaign-1", [drop("d1", claimed=True), drop("d2", minutes=3)]
    )
    merge(twitch, {"dropCampaignsInProgress": [reopened]})

    assert "campaign-1" not in twitch.completed_drop_campaigns


def test_campaign_in_completed_rewards_stays_completed_despite_open_drop():
    twitch = bare_twitch()
    inventory = {
        "completedRewardCampaigns": [{"id": "campaign-1"}],
        "dropCampaignsInProgress": [
            in_progress_campaign("campaign-1", [drop("d2", minutes=3)])
        ],
    }
    merge(twitch, inventory)

    assert "campaign-1" in twitch.completed_drop_campaigns


def test_campaign_left_in_progress_with_all_drops_done_stays_completed():
    twitch = bare_twitch()
    inventory = {
        "dropCampaignsInProgress": [
            in_progress_campaign(
                "campaign-1", [drop("d1", claimed=True), drop("d2", minutes=10)]
            )
        ]
    }
    merge(twitch, inventory)

    assert "campaign-1" in twitch.completed_drop_campaigns


def test_merge_replaces_set_object_and_leaves_old_one_unchanged(caplog):
    twitch = bare_twitch()
    twitch.completed_drop_campaigns = {"campaign-1", "campaign-2"}
    old_set = twitch.completed_drop_campaigns
    reopened = in_progress_campaign("campaign-1", [drop("d2", minutes=1)])
    caplog.set_level(logging.DEBUG)
    twitch.log_drop_checks = True

    merge(twitch, {"dropCampaignsInProgress": [reopened]})

    assert twitch.completed_drop_campaigns is not old_set
    assert old_set == {"campaign-1", "campaign-2"}
    assert twitch.completed_drop_campaigns == {"campaign-2"}
    assert "campaign-1" in caplog.text


def test_merge_without_reopened_campaigns_keeps_contents():
    twitch = bare_twitch()
    twitch.completed_drop_campaigns = {"old-campaign"}

    merge(twitch, {"completedRewardCampaigns": [{"id": "new-campaign"}]})

    assert twitch.completed_drop_campaigns == {"old-campaign", "new-campaign"}
