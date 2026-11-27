"""A prediction the account bet on by hand, before or instead of the miner's
own decision (#404)."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from TwitchChannelPointsMiner.classes.entities.Bet import BetSettings
from TwitchChannelPointsMiner.classes.entities.EventPrediction import EventPrediction
from TwitchChannelPointsMiner.classes.Settings import Settings
from TwitchChannelPointsMiner.classes.WebSocketsPool import WebSocketsPool

POOL_MODULE = "TwitchChannelPointsMiner.classes.WebSocketsPool"


class RecordingStreamer:
    def __init__(self):
        self.username = "streamer"
        self.channel_id = "1001"
        self.channel_points = 1000
        self.settings = SimpleNamespace(bet=BetSettings().default())
        self.history = []
        self.annotations = []

    def update_history(self, reason_code, earned, counter=1, awarded_at=None):
        self.history.append((reason_code, earned, counter))

    def persistent_annotations(self, event_type, event_text):
        self.annotations.append((event_type, event_text))

    def __str__(self):
        return self.username


def _outcomes():
    return [
        {
            "id": "outcome-yes",
            "title": "Yes",
            "color": "BLUE",
            "total_users": 0,
            "total_points": 0,
            "top_predictors": [],
        },
        {
            "id": "outcome-no",
            "title": "No",
            "color": "PINK",
            "total_users": 0,
            "total_points": 0,
            "top_predictors": [],
        },
    ]


@pytest.fixture
def streamer():
    return RecordingStreamer()


@pytest.fixture
def event(streamer):
    return EventPrediction(
        streamer,
        "event-1",
        "Will it work?",
        datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc),
        120,
        "ACTIVE",
        _outcomes(),
    )


@pytest.fixture
def logger(monkeypatch):
    logger = MagicMock()
    monkeypatch.setattr(f"{POOL_MODULE}.logger", logger)
    return logger


@pytest.fixture(autouse=True)
def analytics(monkeypatch):
    monkeypatch.setattr(Settings, "enable_analytics", True, raising=False)


def _send(streamer, event, message_type, prediction):
    websocket = SimpleNamespace(
        index=0,
        streamers=[streamer],
        twitch=MagicMock(),
        events_predictions={"event-1": event},
        last_message_timestamp=None,
        last_message_type_channel=None,
    )
    message = {
        "server_time": 1700000000.0,
        "type": message_type,
        "data": {
            "timestamp": "2026-11-27T12:00:30Z",
            "prediction": {"event_id": "event-1", "channel_id": "1001", **prediction},
        },
    }
    WebSocketsPool.on_message(
        websocket,
        json.dumps(
            {
                "type": "MESSAGE",
                "data": {
                    "topic": "predictions-user-v1.1001",
                    "message": json.dumps(message),
                },
            }
        ),
    )


def _result_line(logger):
    (line,) = [
        call.args[0]
        for call in logger.info.call_args_list
        if call.args and "Result:" in call.args[0]
    ]
    return line


def test_a_bet_placed_before_the_timer_is_reported_from_the_messages(
    streamer, event, logger
):
    assert event.bet.decision == {}

    _send(
        streamer,
        event,
        "prediction-made",
        {"outcome_id": "outcome-no", "points": 50, "result": None},
    )
    _send(
        streamer,
        event,
        "prediction-result",
        {
            "outcome_id": "outcome-no",
            "points": 50,
            "result": {"type": "WIN", "points_won": 120},
        },
    )

    logger.error.assert_not_called()
    assert streamer.annotations == [
        ("PREDICTION_MADE", "Decision: 1 - Will it work?"),
        ("WIN", "Will it work?"),
    ]
    line = _result_line(logger)
    assert "Decision: 1: No (PINK)" in line
    assert line.endswith("Result: WIN, Gained: +70")
    assert streamer.history == [("PREDICTION", 70, 1), ("PREDICTION", -120, -1)]


def test_a_bet_by_hand_on_another_outcome_is_reported_as_placed(
    streamer, event, logger
):
    # The miner's timer ran after the manual bet and decided on "Yes".
    event.bet.decision = {"choice": 0, "amount": 300, "id": "outcome-yes"}

    _send(streamer, event, "prediction-made", {"outcome_id": "outcome-no"})
    _send(
        streamer,
        event,
        "prediction-result",
        {"outcome_id": "outcome-no", "result": {"type": "LOSE", "points_won": None}},
    )

    logger.error.assert_not_called()
    assert streamer.annotations[0] == ("PREDICTION_MADE", "Decision: 1 - Will it work?")
    line = _result_line(logger)
    assert "Decision: 1: No (PINK)" in line
    # The miner's 300 points were not what was bet, and the messages carry
    # no amount.
    assert "300" not in line
    assert line.endswith("Result: LOSE, Lost: unknown (the bet's points aren't known)")


def test_a_bet_with_nothing_known_about_it_does_not_raise(streamer, event, logger):
    _send(streamer, event, "prediction-made", {})
    _send(
        streamer,
        event,
        "prediction-result",
        {"result": {"type": "LOSE", "points_won": None}},
    )

    logger.error.assert_not_called()
    assert streamer.annotations == [
        ("PREDICTION_MADE", "Will it work?"),
        ("LOSE", "Will it work?"),
    ]
    line = _result_line(logger)
    assert "Decision" not in line
    assert line.endswith("Result: LOSE, Lost: unknown (the bet's points aren't known)")


def test_points_added_later_are_counted(streamer, event, logger):
    _send(
        streamer, event, "prediction-made", {"outcome_id": "outcome-yes", "points": 50}
    )
    _send(
        streamer,
        event,
        "prediction-updated",
        {"outcome_id": "outcome-yes", "points": 80},
    )
    _send(
        streamer,
        event,
        "prediction-result",
        {"outcome_id": "outcome-yes", "result": {"type": "LOSE", "points_won": None}},
    )

    logger.error.assert_not_called()
    assert _result_line(logger).endswith("Result: LOSE, Lost: -80")
    assert streamer.history == [("PREDICTION", -80, 1)]


def test_the_miners_own_bet_is_reported_as_before(streamer, event, logger):
    event.bet.decision = {"choice": 0, "amount": 100, "id": "outcome-yes"}

    # No points on either message: the miner's amount is the bet.
    _send(streamer, event, "prediction-made", {"outcome_id": "outcome-yes"})
    _send(
        streamer,
        event,
        "prediction-result",
        {"outcome_id": "outcome-yes", "result": {"type": "WIN", "points_won": 250}},
    )

    logger.error.assert_not_called()
    assert streamer.annotations[0] == ("PREDICTION_MADE", "Decision: 0 - Will it work?")
    line = _result_line(logger)
    assert "Decision: 0: Yes (BLUE) - Result: WIN, Gained: +150" in line
    assert streamer.history == [("PREDICTION", 150, 1), ("PREDICTION", -250, -1)]


def test_a_refund_is_reported_without_the_miners_decision(streamer, event, logger):
    _send(
        streamer, event, "prediction-made", {"outcome_id": "outcome-no", "points": 40}
    )
    _send(
        streamer,
        event,
        "prediction-result",
        {
            "outcome_id": "outcome-no",
            "points": 40,
            "result": {"type": "REFUND", "points_won": 40},
        },
    )

    logger.error.assert_not_called()
    assert _result_line(logger).endswith("Result: REFUND, Refunded: +0")
