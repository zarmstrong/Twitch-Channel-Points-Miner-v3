import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from TwitchChannelPointsMiner.classes.WebSocketsPool import WebSocketsPool

POOL_MODULE = "TwitchChannelPointsMiner.classes.WebSocketsPool"


class FakeStreamer:
    def __init__(self, username, channel_id, on_is_online=None):
        self.username = username
        self.channel_id = channel_id
        self.on_is_online = on_is_online
        self.channel_points = 1000
        self.settings = SimpleNamespace(bet=SimpleNamespace(minimum_points=None))
        self.stream_up = 0
        self.calls = []

    @property
    def is_online(self):
        # Lets a test retire or reorder streamers in the middle of a message,
        # as another thread can while on_message runs.
        if self.on_is_online is not None:
            self.on_is_online()
        return True

    def stream_up_elapsed(self):
        return True

    def get_prediction_window(self, prediction_window_seconds):
        return prediction_window_seconds

    def __getattr__(self, name):
        # Record calls to the other Streamer methods on_message uses.
        if name in {
            "set_offline",
            "update_history",
            "add_community_goal",
            "update_community_goal",
            "delete_community_goal",
        }:
            return lambda *args, **kwargs: self.calls.append(name)
        raise AttributeError(name)


class ReorderedAfterLookup(list):
    """A streamer list that another thread reorders as soon as it's read."""

    def __iter__(self):
        snapshot = list(super().__iter__())
        self.reverse()
        return iter(snapshot)


def _websocket(streamers):
    return SimpleNamespace(
        index=0,
        streamers=streamers,
        twitch=MagicMock(),
        events_predictions={},
        last_message_timestamp=None,
        last_message_type_channel=None,
    )


def _pubsub(topic, channel_id, message):
    message = {"server_time": 1700000000.0, **message}
    return json.dumps(
        {
            "type": "MESSAGE",
            "data": {"topic": f"{topic}.{channel_id}", "message": json.dumps(message)},
        }
    )


@pytest.fixture
def logger(monkeypatch):
    logger = MagicMock()
    monkeypatch.setattr(f"{POOL_MODULE}.logger", logger)
    return logger


@pytest.mark.parametrize(
    "change_list",
    [
        pytest.param(lambda streamers: streamers.pop(0), id="shrunk"),
        pytest.param(lambda streamers: streamers.reverse(), id="reordered"),
    ],
)
def test_on_message_keeps_its_streamer_when_the_list_changes_mid_message(
    logger, change_list
):
    # #170: on_message looked the streamer up by position once, then indexed the
    # shared list again. Retiring or reordering streamers in between credited
    # the message to another channel or raised IndexError.
    streamers = []
    first = FakeStreamer("first", "1")
    target = FakeStreamer("target", "2", on_is_online=lambda: change_list(streamers))
    streamers.extend([first, target])

    WebSocketsPool.on_message(
        _websocket(streamers),
        _pubsub("video-playback-by-id", "2", {"type": "stream-down"}),
    )

    assert target.calls == ["set_offline"]
    assert first.calls == []
    logger.error.assert_not_called()


GOAL = {
    "id": "goal-1",
    "title": "Goal",
    "is_in_stock": True,
    "points_contributed": 0,
    "goal_amount": 100,
    "per_stream_maximum_user_contribution": 10,
    "status": "STARTED",
}


@pytest.mark.parametrize(
    ("topic", "message", "twitch_call", "streamer_calls"),
    [
        pytest.param(
            "community-points-user-v1",
            {
                "type": "claim-available",
                "data": {"claim": {"id": "claim-1", "channel_id": "1"}},
            },
            "claim_bonus",
            [],
            id="bonus",
        ),
        pytest.param(
            "video-playback-by-id",
            {"type": "viewcount"},
            "check_streamer_online",
            [],
            id="viewcount",
        ),
        pytest.param(
            "raid",
            {"type": "raid_update_v2", "raid": {"id": "r", "target_login": "x"}},
            "update_raid",
            [],
            id="raid",
        ),
        pytest.param(
            "community-moments-channel-v1",
            {"type": "active", "data": {"moment_id": "m"}},
            "claim_moment",
            [],
            id="moment",
        ),
        pytest.param(
            "community-points-channel-v1",
            {"type": "community-goal-created", "data": {"community_goal": GOAL}},
            "contribute_to_community_goals",
            ["add_community_goal"],
            id="goal-created",
        ),
        pytest.param(
            "community-points-channel-v1",
            {"type": "community-goal-updated", "data": {"community_goal": GOAL}},
            "contribute_to_community_goals",
            ["update_community_goal"],
            id="goal-updated",
        ),
        pytest.param(
            "community-points-channel-v1",
            {"type": "community-goal-deleted", "data": {"community_goal": GOAL}},
            None,
            ["delete_community_goal"],
            id="goal-deleted",
        ),
    ],
)
def test_on_message_acts_on_its_streamer_after_the_list_is_reordered(
    logger, topic, message, twitch_call, streamer_calls
):
    target = FakeStreamer("target", "1")
    other = FakeStreamer("other", "2")
    streamers = ReorderedAfterLookup([target, other])
    websocket = _websocket(streamers)

    WebSocketsPool.on_message(websocket, _pubsub(topic, "1", message))

    # The lookup really did see the list change under it.
    assert streamers[0] is other
    if twitch_call is not None:
        call = getattr(websocket.twitch, twitch_call)
        call.assert_called_once()
        assert call.call_args.args[0] is target
    assert target.calls == streamer_calls
    assert other.calls == []
    logger.error.assert_not_called()


def test_on_message_bets_for_its_streamer_after_the_list_is_reordered(
    monkeypatch, logger
):
    event_prediction = MagicMock()
    event_prediction.closing_bet_after.return_value = 5
    event_prediction_class = MagicMock(return_value=event_prediction)
    timer = MagicMock()
    monkeypatch.setattr(f"{POOL_MODULE}.EventPrediction", event_prediction_class)
    monkeypatch.setattr(f"{POOL_MODULE}.Timer", timer)
    target = FakeStreamer("target", "1")
    other = FakeStreamer("other", "2")
    streamers = ReorderedAfterLookup([target, other])
    websocket = _websocket(streamers)

    WebSocketsPool.on_message(
        websocket,
        _pubsub(
            "predictions-channel-v1",
            "1",
            {
                "type": "event-created",
                "data": {
                    "timestamp": "2026-10-30T12:00:00Z",
                    "event": {
                        "id": "event-1",
                        "status": "ACTIVE",
                        "title": "Who wins?",
                        "created_at": "2026-10-30T12:00:00Z",
                        "prediction_window_seconds": 60,
                        "outcomes": [],
                    },
                },
            },
        ),
    )

    assert streamers[0] is other
    assert event_prediction_class.call_args.args[0] is target
    assert websocket.events_predictions == {"event-1": event_prediction}
    timer.return_value.start.assert_called_once()
    logger.error.assert_not_called()


def test_on_message_records_a_prediction_result_for_its_streamer_after_the_list_is_reordered(
    logger,
):
    event_prediction = MagicMock(
        bet_confirmed=True, result={"type": "WIN", "string": "WIN"}
    )
    event_prediction.parse_result.return_value = {
        "gained": 10,
        "placed": 5,
        "won": 15,
    }
    event_prediction.bet.get_decision.return_value = {"title": "A", "color": "BLUE"}
    event_prediction.bet.decision = {"choice": "A"}
    target = FakeStreamer("target", "1")
    other = FakeStreamer("other", "2")
    streamers = ReorderedAfterLookup([target, other])
    websocket = _websocket(streamers)
    websocket.events_predictions["event-1"] = event_prediction

    WebSocketsPool.on_message(
        websocket,
        _pubsub(
            "predictions-user-v1",
            "99",
            {
                "type": "prediction-result",
                "data": {
                    "timestamp": "2026-10-30T12:05:00Z",
                    "prediction": {
                        "event_id": "event-1",
                        "channel_id": "1",
                        "result": {"type": "WIN", "points_won": 15},
                    },
                },
            },
        ),
    )

    assert streamers[0] is other
    assert target.calls == ["update_history", "update_history"]
    assert other.calls == []
    logger.error.assert_not_called()


def test_on_message_claims_a_bonus_for_the_streamer_on_the_message():
    streamers = [FakeStreamer("first", "1"), FakeStreamer("target", "2")]
    websocket = _websocket(streamers)

    WebSocketsPool.on_message(
        websocket,
        _pubsub(
            "community-points-user-v1",
            "99",
            {
                "type": "claim-available",
                "data": {"claim": {"id": "claim-1", "channel_id": "2"}},
            },
        ),
    )

    websocket.twitch.claim_bonus.assert_called_once_with(streamers[1], "claim-1")


def test_on_message_ignores_a_channel_that_is_no_longer_mined():
    streamers = [FakeStreamer("first", "1")]
    websocket = _websocket(streamers)

    WebSocketsPool.on_message(
        websocket,
        _pubsub(
            "community-points-user-v1",
            "99",
            {
                "type": "claim-available",
                "data": {"claim": {"id": "claim-1", "channel_id": "2"}},
            },
        ),
    )

    websocket.twitch.claim_bonus.assert_not_called()
