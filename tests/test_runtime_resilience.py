import importlib
import logging
from threading import Lock
from types import SimpleNamespace

from dateutil import parser

from TwitchChannelPointsMiner.classes.entities.Streamer import Streamer
from TwitchChannelPointsMiner.classes.gql.Errors import RetryError
from TwitchChannelPointsMiner.classes.Twitch import Twitch
from TwitchChannelPointsMiner.utils.Utils import server_time

from tests.test_streams_watched import _run_one_watch_iteration, _watch_streamer
from tests.test_twitch_drop_claim import bare_twitch, campaign_data

twitch_module = importlib.import_module("TwitchChannelPointsMiner.classes.Twitch")


def test_server_time_fallback_is_parseable():
    for data in (None, {}, {"server_time": 1700000000.5}):
        parsed = parser.parse(server_time(data))
        assert parsed.utcoffset().total_seconds() == 0
    assert server_time({"server_time": 1700000000.5}).endswith("Z")


def _offline_streamer():
    streamer = Streamer("example")
    streamer.channel_id = "1"
    return streamer


def test_failed_stream_info_does_not_mark_streamer_online(monkeypatch):
    twitch = object.__new__(Twitch)
    streamer = _offline_streamer()
    monkeypatch.setattr(Twitch, "get_spade_url", lambda self, s: None)
    monkeypatch.setattr(Twitch, "get_stream_info", lambda self, s: None)

    twitch.check_streamer_online(streamer)

    assert streamer.is_online is False


def test_online_streamer_without_spade_url_retries_extraction(monkeypatch):
    twitch = object.__new__(Twitch)
    streamer = _offline_streamer()
    streamer.is_online = True
    calls = []

    def fake_spade(self, target):
        calls.append(target.username)
        target.stream.spade_url = "https://spade.test/example"

    monkeypatch.setattr(Twitch, "get_spade_url", fake_spade)
    monkeypatch.setattr(Twitch, "update_stream", lambda self, s: None)

    twitch.check_streamer_online(streamer)
    twitch.check_streamer_online(streamer)

    assert calls == ["example"]
    assert streamer.stream.spade_url == "https://spade.test/example"


def test_minute_watcher_skips_stream_without_spade_url(monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    broken = _watch_streamer("broken")
    broken.stream.spade_url = None

    posted = _run_one_watch_iteration(
        monkeypatch,
        [broken, _watch_streamer("healthy")],
        streams_watched=2,
    )

    assert posted == ["https://spade.test/healthy"]
    assert "spade URL not loaded yet" in caplog.text
    assert "Exception raised in send minute watched" not in caplog.text


def test_claim_all_drops_skips_drop_with_null_self(monkeypatch):
    twitch = bare_twitch(monkeypatch)
    campaign = campaign_data()
    campaign["timeBasedDrops"][0]["self"] = None
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_inventory",
        lambda self: {"dropCampaignsInProgress": [campaign, None]},
    )

    twitch.claim_all_drops_from_inventory()


def _campaign_payload(campaign_id):
    campaign = campaign_data()
    campaign["id"] = campaign_id
    return campaign


def _sync_campaigns_harness(monkeypatch, dashboards, details, passes):
    """Run sync_campaigns for `passes` 60-second passes on a fake clock.

    The real dashboard and campaign-details helpers run against a fake GQL
    client. Each due refresh pops the next item of `dashboards` (a RetryError
    to raise, or the campaign IDs the dashboard lists) and, if the dashboard
    loaded, the next item of `details` (a RetryError, or the IDs to answer).
    """
    twitch = bare_twitch(monkeypatch)
    twitch.running = True
    twitch.prompt_claim_pass_lock = Lock()
    twitch.twitch_login = SimpleNamespace(get_user_id=lambda: "1", username="me")
    clock = {"now": 1_000_000.0}
    refreshes = []
    synced = []

    def get_viewer_drops_dashboard():
        refreshes.append(clock["now"])
        result = dashboards.pop(0)
        if isinstance(result, BaseException):
            raise result
        return SimpleNamespace(
            raw_response={
                "data": {
                    "currentUser": {
                        "dropCampaigns": [
                            _campaign_payload(campaign_id) for campaign_id in result
                        ]
                    }
                }
            }
        )

    def post_gql_request_batch_raw(operation_name, json_data):
        result = details.pop(0)
        if isinstance(result, BaseException):
            raise result
        return [
            {
                "data": {
                    "user": {
                        "dropCampaign": (
                            _campaign_payload(item["variables"]["dropID"])
                            if item["variables"]["dropID"] in result
                            else None
                        )
                    }
                }
            }
            for item in json_data
        ]

    twitch.gql = SimpleNamespace(
        get_viewer_drops_dashboard=get_viewer_drops_dashboard,
        post_gql_request_batch_raw=post_gql_request_batch_raw,
    )
    monkeypatch.setattr(twitch_module.time, "time", lambda: clock["now"])
    monkeypatch.setattr(Twitch, "claim_all_drops_from_inventory", lambda self: None)
    monkeypatch.setattr(
        Twitch, "_Twitch__save_drop_snapshot_from_dict", lambda self, *a, **k: None
    )

    def fake_sync(self, campaigns):
        synced.append(sorted(campaign.id for campaign in campaigns))
        return campaigns

    monkeypatch.setattr(Twitch, "_Twitch__sync_campaigns", fake_sync)
    monkeypatch.setattr(
        Twitch, "_Twitch__check_connection_handler", lambda self, chunk_size: None
    )
    remaining = {"passes": passes}

    def fake_sleep(self, seconds, chunk_size=3):
        clock["now"] += 60
        remaining["passes"] -= 1
        if remaining["passes"] <= 0:
            self.running = False

    monkeypatch.setattr(Twitch, "_Twitch__chuncked_sleep", fake_sleep)
    twitch.sync_campaigns([])
    return refreshes, synced


def _retry_error():
    return RetryError("ViewerDropsDashboard", [])


def test_sync_campaigns_keeps_campaigns_when_dashboard_request_fails(monkeypatch):
    refreshes, synced = _sync_campaigns_harness(
        monkeypatch,
        dashboards=[["campaign-1"], _retry_error(), ["campaign-1"]],
        details=[["campaign-1"], ["campaign-1"]],
        passes=22,
    )

    # Initial refresh, the failed one 15 minutes later, then a retry 5 minutes
    # after the failure instead of another full 15-minute wait.
    assert len(refreshes) == 3
    assert refreshes[1] - refreshes[0] >= 15 * 60
    assert refreshes[2] - refreshes[1] == 5 * 60
    # The failed refresh did not blank the campaigns the passes sync against.
    assert all(ids == ["campaign-1"] for ids in synced)


def test_sync_campaigns_keeps_campaigns_whose_details_request_failed(monkeypatch):
    refreshes, synced = _sync_campaigns_harness(
        monkeypatch,
        dashboards=[["campaign-1", "campaign-2"], ["campaign-1", "campaign-2"]],
        details=[["campaign-1", "campaign-2"], _retry_error()],
        passes=17,
    )

    assert len(refreshes) == 2
    assert synced[-1] == ["campaign-1", "campaign-2"]


def test_sync_campaigns_drops_campaigns_a_loaded_dashboard_no_longer_lists(
    monkeypatch,
):
    refreshes, synced = _sync_campaigns_harness(
        monkeypatch,
        dashboards=[["campaign-1", "campaign-2"], ["campaign-1"]],
        details=[["campaign-1", "campaign-2"], ["campaign-1"]],
        passes=17,
    )

    assert len(refreshes) == 2
    assert synced[0] == ["campaign-1", "campaign-2"]
    assert synced[-1] == ["campaign-1"]


def test_sync_campaigns_survives_unexpected_error(monkeypatch, caplog):
    caplog.set_level(logging.ERROR)
    refreshes, synced = _sync_campaigns_harness(
        monkeypatch,
        dashboards=[TypeError("null field"), ["campaign-1"]],
        details=[["campaign-1"]],
        passes=7,
    )

    assert len(refreshes) == 2
    assert synced[-1] == ["campaign-1"]
    assert "Unexpected error while syncing inventory" in caplog.text
