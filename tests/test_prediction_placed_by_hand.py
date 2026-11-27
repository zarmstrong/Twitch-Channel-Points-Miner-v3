"""A prediction the account bet on by hand, before or instead of the miner's
own decision (#404)."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from TwitchChannelPointsMiner.classes.entities.Bet import Bet, BetSettings
from TwitchChannelPointsMiner.classes.gql.data.response.Predictions import (
    Error,
    MakePredictionResponse,
)
from TwitchChannelPointsMiner.classes.gql.Errors import RetryError
from TwitchChannelPointsMiner.classes.Twitch import Twitch
from TwitchChannelPointsMiner.classes.entities.EventPrediction import EventPrediction
from TwitchChannelPointsMiner.classes.Settings import Settings
from TwitchChannelPointsMiner.classes.WebSocketsPool import WebSocketsPool

POOL_MODULE = "TwitchChannelPointsMiner.classes.WebSocketsPool"


class RecordingStreamer:
    def __init__(self):
        self.username = "streamer"
        self.channel_id = "1001"
        self.channel_points = 1000
        bet = BetSettings()
        bet.default()
        self.settings = SimpleNamespace(bet=bet)
        self.history = {}
        self.annotations = []
        self.explicitly_configured = True

    def persistent_series(self, event_type="Watch"):
        pass

    def update_history(self, reason_code, earned, counter=1, awarded_at=None):
        entry = self.history.setdefault(reason_code, {"counter": 0, "amount": 0})
        entry["counter"] += counter
        entry["amount"] += earned

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


def _send(
    streamer,
    event,
    message_type,
    prediction,
    sent="2026-11-27T12:00:30Z",
    server_time=None,
    tracked=True,
):
    """A predictions-user-v1 message about ``event``, sent at ``sent``
    (data.timestamp) or ``server_time`` (data.server_time); ``tracked``:
    the miner holds ``event``."""

    websocket = SimpleNamespace(
        index=0,
        streamers=[streamer],
        twitch=MagicMock(),
        events_predictions={event.event_id: event} if tracked else {},
        last_message_timestamp=None,
        last_message_type_channel=None,
    )
    message = {
        "server_time": 1700000000.0,
        "type": message_type,
        "data": {
            "prediction": {
                "event_id": event.event_id,
                "channel_id": "1001",
                **prediction,
            },
        },
    }
    if sent is not None:
        message["data"]["timestamp"] = sent
    if server_time is not None:
        message["data"]["server_time"] = server_time
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


def _miner_bets(event, decision, response):
    """The miner's timer firing for ``event``: it decides ``decision`` and
    Twitch answers ``response`` (an exception is raised; a function is
    called with the request and returns the answer)."""

    def calculate(bet, balance):
        bet.decision = dict(decision)
        return bet.decision

    twitch = object.__new__(Twitch)
    twitch.gql = MagicMock()
    if isinstance(response, Exception) or callable(response):
        twitch.gql.make_prediction.side_effect = response
    else:
        twitch.gql.make_prediction.return_value = response
    with patch.object(Bet, "calculate", calculate):
        twitch.make_predictions(event)
    return twitch.gql.make_prediction


def _credited(
    streamer,
    event,
    reason,
    amount,
    events=None,
    sent="2026-11-27T12:00:40Z",
    server_time=None,
):
    """community-points-user-v1's credit of a payout or refund, sent at
    ``sent`` (data.timestamp) or ``server_time`` (data.server_time) while
    the miner holds ``events`` (by default only ``event``)."""

    websocket = SimpleNamespace(
        index=0,
        streamers=[streamer],
        twitch=MagicMock(),
        events_predictions=events if events is not None else {"event-1": event},
        last_message_timestamp=None,
        last_message_type_channel=None,
    )
    message = {
        "type": "points-earned",
        "data": {
            "channel_id": "1001",
            "point_gain": {"total_points": amount, "reason_code": reason},
            "balance": {"balance": 2000, "channel_id": "1001"},
        },
    }
    if sent is not None:
        message["data"]["timestamp"] = sent
    if server_time is not None:
        message["data"]["server_time"] = server_time
    WebSocketsPool.on_message(
        websocket,
        json.dumps(
            {
                "type": "MESSAGE",
                "data": {
                    "topic": "community-points-user-v1.1001",
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


UNKNOWN = "unknown (the amounts aren't known)"


def _lose(streamer, event, points=None):
    prediction = {
        "outcome_id": "outcome-no",
        "result": {"type": "LOSE", "points_won": None},
    }
    if points is not None:
        prediction["points"] = points
    _send(streamer, event, "prediction-result", prediction)


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
    _credited(streamer, event, "PREDICTION", 120)
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
        ("PREDICTION", "+120 - PREDICTION"),
        ("WIN", "Will it work?"),
    ]
    line = _result_line(logger)
    assert "Decision: 1: No (PINK)" in line
    assert line.endswith("Result: WIN, Gained: +70")
    assert streamer.history == {"PREDICTION": {"counter": 1, "amount": 70}}


def test_a_bet_by_hand_on_another_outcome_is_reported_as_placed(
    streamer, event, logger
):
    _send(streamer, event, "prediction-made", {"outcome_id": "outcome-no"})
    # The timer runs after the manual bet and decides on "Yes"; Twitch
    # refuses a bet on a second outcome.
    _miner_bets(
        event,
        {"choice": 0, "amount": 300, "id": "outcome-yes"},
        MakePredictionResponse(Error("NOT_ALLOWED")),
    )
    _send(
        streamer,
        event,
        "prediction-result",
        {"outcome_id": "outcome-no", "result": {"type": "LOSE", "points_won": None}},
    )

    assert streamer.annotations[0] == ("PREDICTION_MADE", "Decision: 1 - Will it work?")
    line = _result_line(logger)
    assert "Decision: 1: No (PINK)" in line
    assert "300" not in line
    assert line.endswith(f"Result: LOSE, Lost: {UNKNOWN}")
    # An unknown wager is left out of the session's history.
    assert streamer.history == {}


@pytest.mark.parametrize(
    "answer",
    [
        MakePredictionResponse(Error("INSUFFICIENT_POINTS")),
        RetryError("MakePrediction", []),
    ],
    ids=["refused", "request-failed"],
)
def test_a_bet_by_hand_after_a_failed_miner_bet_does_not_use_its_amount(
    streamer, event, logger, answer
):
    _miner_bets(event, {"choice": 1, "amount": 300, "id": "outcome-no"}, answer)
    assert (event.miner_bet_requested, event.bet_placed) == (True, False)

    _send(streamer, event, "prediction-made", {"outcome_id": "outcome-no"})
    _credited(streamer, event, "PREDICTION", 500)
    _send(
        streamer,
        event,
        "prediction-result",
        {"outcome_id": "outcome-no", "result": {"type": "WIN", "points_won": 500}},
    )

    line = _result_line(logger)
    assert "Decision: 1: No (PINK)" in line
    assert line.endswith(f"Result: WIN, Gained: {UNKNOWN}")
    # The payout credited as PREDICTION is not a net gain: the prediction
    # is left out.
    assert streamer.history == {"PREDICTION": {"counter": 0, "amount": 0}}


def test_a_bet_by_hand_that_the_miner_added_to_does_not_use_its_amount(
    streamer, event, logger
):
    _send(streamer, event, "prediction-made", {"outcome_id": "outcome-no"})
    _miner_bets(
        event,
        {"choice": 1, "amount": 300, "id": "outcome-no"},
        MakePredictionResponse(None),
    )
    assert event.bet_placed is True

    _send(
        streamer,
        event,
        "prediction-result",
        {"outcome_id": "outcome-no", "result": {"type": "LOSE", "points_won": None}},
    )

    assert _result_line(logger).endswith(f"Result: LOSE, Lost: {UNKNOWN}")
    assert streamer.history == {}


def test_a_skipped_miner_bet_is_not_the_bet(streamer, event, logger):
    # Calculated, but below the 10-point minimum: never sent.
    gql = _miner_bets(
        event,
        {"choice": 0, "amount": 5, "id": "outcome-yes"},
        MakePredictionResponse(None),
    )
    gql.assert_not_called()
    assert (event.miner_bet_requested, event.bet_placed) == (False, False)

    _send(streamer, event, "prediction-made", {})
    _send(
        streamer,
        event,
        "prediction-result",
        {"result": {"type": "LOSE", "points_won": None}},
    )

    assert streamer.annotations[0] == ("PREDICTION_MADE", "Will it work?")
    line = _result_line(logger)
    assert "Decision" not in line
    assert line.endswith(f"Result: LOSE, Lost: {UNKNOWN}")


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
    assert line.endswith(f"Result: LOSE, Lost: {UNKNOWN}")
    assert streamer.history == {}


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
    assert streamer.history == {"PREDICTION": {"counter": 1, "amount": -80}}


def test_the_miners_own_bet_is_reported_as_before(streamer, event, logger):
    _miner_bets(
        event,
        {"choice": 0, "amount": 100, "id": "outcome-yes"},
        MakePredictionResponse(None),
    )

    _send(
        streamer,
        event,
        "prediction-made",
        {"outcome_id": "outcome-yes", "points": 100},
    )
    _credited(streamer, event, "PREDICTION", 250)
    _send(
        streamer,
        event,
        "prediction-result",
        {
            "outcome_id": "outcome-yes",
            "points": 100,
            "result": {"type": "WIN", "points_won": 250},
        },
    )

    logger.error.assert_not_called()
    assert streamer.annotations[0] == ("PREDICTION_MADE", "Decision: 0 - Will it work?")
    line = _result_line(logger)
    assert "Decision: 0: Yes (BLUE) - Result: WIN, Gained: +150" in line
    assert streamer.history == {"PREDICTION": {"counter": 1, "amount": 150}}


@pytest.mark.parametrize("points_won", [40, None], ids=["payout", "no-payout"])
def test_a_refund_replaces_its_credit_with_a_prediction(
    streamer, event, logger, points_won
):
    _send(
        streamer, event, "prediction-made", {"outcome_id": "outcome-no", "points": 40}
    )
    _credited(streamer, event, "REFUND", 40)
    _send(
        streamer,
        event,
        "prediction-result",
        {
            "outcome_id": "outcome-no",
            "points": 40,
            "result": {"type": "REFUND", "points_won": points_won},
        },
    )

    logger.error.assert_not_called()
    assert _result_line(logger).endswith("Result: REFUND, Refunded: +0")
    assert streamer.history == {
        "REFUND": {"counter": 0, "amount": 0},
        "PREDICTION": {"counter": 1, "amount": 0},
    }


@pytest.mark.parametrize("miner_first", [False, True], ids=["by-hand", "miner"])
def test_points_added_without_a_total_make_the_wager_unknown(
    streamer, event, logger, miner_first
):
    if miner_first:
        _miner_bets(
            event,
            {"choice": 0, "amount": 100, "id": "outcome-yes"},
            MakePredictionResponse(None),
        )
        _send(streamer, event, "prediction-made", {"outcome_id": "outcome-yes"})
    else:
        _send(
            streamer,
            event,
            "prediction-made",
            {"outcome_id": "outcome-yes", "points": 50},
        )
    _send(streamer, event, "prediction-updated", {"outcome_id": "outcome-yes"})
    _send(
        streamer,
        event,
        "prediction-result",
        {"outcome_id": "outcome-yes", "result": {"type": "LOSE", "points_won": None}},
    )

    assert _result_line(logger).endswith(f"Result: LOSE, Lost: {UNKNOWN}")
    assert streamer.history == {}


def test_a_new_total_after_an_unknown_addition_is_used(streamer, event, logger):
    _send(
        streamer, event, "prediction-made", {"outcome_id": "outcome-yes", "points": 50}
    )
    _send(streamer, event, "prediction-updated", {"outcome_id": "outcome-yes"})
    _send(
        streamer,
        event,
        "prediction-result",
        {
            "outcome_id": "outcome-yes",
            "points": 90,
            "result": {"type": "LOSE", "points_won": None},
        },
    )

    assert _result_line(logger).endswith("Result: LOSE, Lost: -90")
    assert streamer.history == {"PREDICTION": {"counter": 1, "amount": -90}}


def test_a_win_without_its_payout_is_unknown_but_counted(streamer, event, logger):
    _send(
        streamer, event, "prediction-made", {"outcome_id": "outcome-no", "points": 50}
    )
    _credited(streamer, event, "PREDICTION", 120)
    _send(
        streamer,
        event,
        "prediction-result",
        {"outcome_id": "outcome-no", "result": {"type": "WIN", "points_won": None}},
    )

    assert _result_line(logger).endswith(f"Result: WIN, Gained: {UNKNOWN}")
    # The payout credit, less the wager.
    assert streamer.history == {"PREDICTION": {"counter": 1, "amount": 70}}


@pytest.mark.parametrize(
    "credit_first", [True, False], ids=["credit-first", "result-first"]
)
def test_a_win_with_nothing_known_takes_its_credit_back(
    streamer, event, logger, credit_first
):
    _send(streamer, event, "prediction-made", {"outcome_id": "outcome-no"})
    if credit_first:
        _credited(streamer, event, "PREDICTION", 120)
    _send(
        streamer,
        event,
        "prediction-result",
        {"outcome_id": "outcome-no", "result": {"type": "WIN", "points_won": None}},
    )
    if not credit_first:
        _credited(streamer, event, "PREDICTION", 120)

    assert _result_line(logger).endswith(f"Result: WIN, Gained: {UNKNOWN}")
    # Neither the wager nor the payout is known: the gross credit is not a
    # gain.
    assert streamer.history == {"PREDICTION": {"counter": 0, "amount": 0}}


def _next_prediction(streamer):
    return EventPrediction(
        streamer,
        "event-2",
        "And now?",
        datetime(2026, 11, 27, 12, 1, tzinfo=timezone.utc),
        120,
        "ACTIVE",
        _outcomes(),
    )


@pytest.mark.parametrize("reason", ["PREDICTION", "REFUND"])
def test_a_late_credit_goes_to_the_prediction_it_pays_out(
    streamer, event, logger, reason
):
    _send(streamer, event, "prediction-made", {"outcome_id": "outcome-no"})
    result = "WIN" if reason == "PREDICTION" else "REFUND"
    _send(
        streamer,
        event,
        "prediction-result",
        {"outcome_id": "outcome-no", "result": {"type": result, "points_won": None}},
    )
    # Sent with the result, delivered after the next prediction was
    # created.
    _credited(
        streamer,
        event,
        reason,
        120,
        events={"event-1": event, "event-2": _next_prediction(streamer)},
        sent="2026-11-27T12:00:31Z",
    )

    assert streamer.history[reason] == {"counter": 0, "amount": 0}


def test_a_credit_sent_long_after_a_result_is_not_its_payout(streamer, event, logger):
    _send(streamer, event, "prediction-made", {"outcome_id": "outcome-no"})
    _send(
        streamer,
        event,
        "prediction-result",
        {"outcome_id": "outcome-no", "result": {"type": "WIN", "points_won": None}},
    )
    # The payout of a later prediction the miner didn't track (for example,
    # one it skipped for the minimum balance): only the first is known.
    _credited(streamer, event, "PREDICTION", 80, sent="2026-11-27T12:05:00Z")

    assert streamer.history["PREDICTION"] == {"counter": 1, "amount": 80}


def test_a_credit_before_its_prediction_was_known_is_still_taken_back(
    streamer, event, logger
):
    # Delivered on the user topic's websocket before the channel topic's
    # messages registered the prediction.
    _credited(streamer, event, "PREDICTION", 120, events={})
    _send(streamer, event, "prediction-made", {"outcome_id": "outcome-no"})
    _send(
        streamer,
        event,
        "prediction-result",
        {"outcome_id": "outcome-no", "result": {"type": "WIN", "points_won": None}},
    )

    assert streamer.history == {"PREDICTION": {"counter": 0, "amount": 0}}


def test_a_credit_without_a_send_time_is_left_as_credited(streamer, event, logger):
    _send(streamer, event, "prediction-made", {"outcome_id": "outcome-no"})
    _credited(streamer, event, "PREDICTION", 120, sent="not a time")
    _send(
        streamer,
        event,
        "prediction-result",
        {"outcome_id": "outcome-no", "result": {"type": "WIN", "points_won": None}},
    )

    assert _result_line(logger).endswith(f"Result: WIN, Gained: {UNKNOWN}")
    assert streamer.history == {"PREDICTION": {"counter": 1, "amount": 120}}


@pytest.mark.parametrize(
    "result_sent, credit_sent",
    [
        (None, "2026-11-27T12:00:31Z"),
        ("2026-11-27T12:00:30Z", None),
        (None, None),
    ],
    ids=["result", "credit", "both"],
)
def test_a_credit_or_result_without_a_send_time_is_not_matched(
    streamer, event, logger, result_sent, credit_sent
):
    _send(streamer, event, "prediction-made", {"outcome_id": "outcome-no"})
    _send(
        streamer,
        event,
        "prediction-result",
        {"outcome_id": "outcome-no", "result": {"type": "WIN", "points_won": None}},
        sent=result_sent,
    )
    _credited(streamer, event, "PREDICTION", 120, sent=credit_sent)

    assert streamer.history == {"PREDICTION": {"counter": 1, "amount": 120}}


@pytest.mark.parametrize(
    "credit_first", [True, False], ids=["credit-first", "result-first"]
)
def test_a_credit_is_claimed_once(streamer, event, logger, credit_first):
    # A known payout claims its credit...
    if credit_first:
        _credited(streamer, event, "PREDICTION", 120, sent="2026-11-27T12:00:31Z")
    _send(
        streamer,
        event,
        "prediction-made",
        {"outcome_id": "outcome-no", "points": 50},
    )
    _send(
        streamer,
        event,
        "prediction-result",
        {"outcome_id": "outcome-no", "result": {"type": "WIN", "points_won": 120}},
    )
    if not credit_first:
        _credited(streamer, event, "PREDICTION", 120, sent="2026-11-27T12:00:31Z")
    # ...so another result sent close to it can't take it back again.
    other = _next_prediction(streamer)
    _send(streamer, other, "prediction-made", {"outcome_id": "outcome-no"})
    other_result = {
        "outcome_id": "outcome-no",
        "result": {"type": "WIN", "points_won": None},
    }
    _send(
        streamer, other, "prediction-result", other_result, sent="2026-11-27T12:00:35Z"
    )

    assert streamer.history == {"PREDICTION": {"counter": 1, "amount": 70}}


def test_an_untracked_predictions_credit_is_not_taken_for_another(
    streamer, event, logger
):
    untracked = _next_prediction(streamer)
    refund = {"outcome_id": "outcome-no", "result": {"type": "REFUND"}}
    # A refund of a prediction the miner doesn't track...
    _send(
        streamer,
        untracked,
        "prediction-result",
        refund,
        sent="2026-11-27T12:00:25Z",
        tracked=False,
    )
    _credited(streamer, event, "REFUND", 40, sent="2026-11-27T12:00:26Z")
    # ...then the tracked one's, before its own credit.
    _send(streamer, event, "prediction-made", {"outcome_id": "outcome-no"})
    _send(streamer, event, "prediction-result", refund, sent="2026-11-27T12:00:30Z")
    _credited(streamer, event, "REFUND", 80, sent="2026-11-27T12:00:31Z")

    # The untracked refund stays as credited; the tracked one is net 0.
    assert streamer.history == {
        "REFUND": {"counter": 1, "amount": 40},
        "PREDICTION": {"counter": 1, "amount": 0},
    }


@pytest.mark.parametrize(
    "credit_first", [True, False], ids=["credit-first", "result-first"]
)
def test_a_server_time_is_a_send_time(streamer, event, logger, credit_first):
    if credit_first:
        _credited(
            streamer, event, "PREDICTION", 120, sent=None, server_time=1764244831.0
        )
    _send(streamer, event, "prediction-made", {"outcome_id": "outcome-no"})
    _send(
        streamer,
        event,
        "prediction-result",
        {"outcome_id": "outcome-no", "result": {"type": "WIN", "points_won": None}},
        sent=None,
        server_time=1764244830.0,
    )
    if not credit_first:
        _credited(
            streamer, event, "PREDICTION", 120, sent=None, server_time=1764244831.0
        )

    assert streamer.history == {"PREDICTION": {"counter": 0, "amount": 0}}


@pytest.mark.parametrize("wager", [40, None], ids=["known-wager", "unknown-wager"])
@pytest.mark.parametrize(
    "credit_first", [True, False], ids=["credit-first", "result-first"]
)
def test_a_refund_with_nothing_won_is_net_zero(
    streamer, event, logger, wager, credit_first
):
    made = {"outcome_id": "outcome-no"}
    if wager is not None:
        made["points"] = wager
    _send(streamer, event, "prediction-made", made)
    if credit_first:
        _credited(streamer, event, "REFUND", 40, sent="2026-11-27T12:00:31Z")
    _send(
        streamer,
        event,
        "prediction-result",
        {"outcome_id": "outcome-no", "result": {"type": "REFUND", "points_won": 0}},
    )
    if not credit_first:
        _credited(streamer, event, "REFUND", 40, sent="2026-11-27T12:00:31Z")

    assert _result_line(logger).endswith("Result: REFUND, Refunded: +0")
    assert streamer.history == {
        "REFUND": {"counter": 0, "amount": 0},
        "PREDICTION": {"counter": 1, "amount": 0},
    }


def test_overlapping_results_and_credits_are_paired_in_order(streamer, event, logger):
    untracked = _next_prediction(streamer)
    refund = {"outcome_id": "outcome-no", "result": {"type": "REFUND"}}
    _send(
        streamer,
        untracked,
        "prediction-result",
        refund,
        sent="2026-11-27T12:00:20Z",
        tracked=False,
    )
    _send(streamer, event, "prediction-made", {"outcome_id": "outcome-no"})
    _send(streamer, event, "prediction-result", refund, sent="2026-11-27T12:00:25Z")
    # The untracked prediction's credit, then the tracked one's.
    _credited(streamer, event, "REFUND", 40, sent="2026-11-27T12:00:26Z")
    _credited(streamer, event, "REFUND", 80, sent="2026-11-27T12:00:31Z")

    assert streamer.history == {
        "REFUND": {"counter": 1, "amount": 40},
        "PREDICTION": {"counter": 1, "amount": 0},
    }


def test_a_repeated_result_is_handled_once(streamer, event, logger):
    refund = {"outcome_id": "outcome-no", "result": {"type": "REFUND"}}
    _send(streamer, event, "prediction-made", {"outcome_id": "outcome-no"})
    _send(streamer, event, "prediction-result", refund, sent="2026-11-27T12:00:20Z")
    _credited(streamer, event, "REFUND", 80, sent="2026-11-27T12:00:21Z")
    # Delivered again, then another prediction (not tracked) is refunded.
    _send(streamer, event, "prediction-result", refund, sent="2026-11-27T12:00:20Z")
    untracked = _next_prediction(streamer)
    _send(
        streamer,
        untracked,
        "prediction-result",
        refund,
        sent="2026-11-27T12:00:25Z",
        tracked=False,
    )
    _credited(streamer, event, "REFUND", 40, sent="2026-11-27T12:00:26Z")

    # One result line, and only the untracked refund's credit stays.
    assert _result_line(logger).endswith("Result: REFUND, Refunded: +0")
    assert streamer.history == {
        "REFUND": {"counter": 1, "amount": 40},
        "PREDICTION": {"counter": 1, "amount": 0},
    }


def test_a_refund_of_an_unknown_wager_uses_the_refunded_points(streamer, event, logger):
    _send(streamer, event, "prediction-made", {"outcome_id": "outcome-no"})
    _credited(streamer, event, "REFUND", 40)
    _send(
        streamer,
        event,
        "prediction-result",
        {"outcome_id": "outcome-no", "result": {"type": "REFUND", "points_won": 40}},
    )

    assert _result_line(logger).endswith("Result: REFUND, Refunded: +0")
    assert streamer.history == {
        "REFUND": {"counter": 0, "amount": 0},
        "PREDICTION": {"counter": 1, "amount": 0},
    }


def test_a_refund_with_no_amounts_takes_its_credit_back(streamer, event, logger):
    _send(streamer, event, "prediction-made", {"outcome_id": "outcome-no"})
    _credited(streamer, event, "REFUND", 40)
    _send(
        streamer,
        event,
        "prediction-result",
        {"outcome_id": "outcome-no", "result": {"type": "REFUND", "points_won": None}},
    )

    logger.error.assert_not_called()
    assert _result_line(logger).endswith("Result: REFUND, Refunded: +0")
    # The credit is the refund: counted as the prediction, net 0.
    assert streamer.history == {
        "REFUND": {"counter": 0, "amount": 0},
        "PREDICTION": {"counter": 1, "amount": 0},
    }


def test_the_miners_addition_to_a_known_bet_is_counted(streamer, event, logger):
    _send(
        streamer, event, "prediction-made", {"outcome_id": "outcome-no", "points": 50}
    )
    _miner_bets(
        event,
        {"choice": 1, "amount": 300, "id": "outcome-no"},
        MakePredictionResponse(None),
    )
    _lose(streamer, event)

    assert _result_line(logger).endswith("Result: LOSE, Lost: -350")
    assert streamer.history == {"PREDICTION": {"counter": 1, "amount": -350}}


@pytest.mark.parametrize(
    "update",
    [50, 350],
    ids=["update-before-the-addition", "update-with-the-addition"],
)
def test_a_total_reported_while_the_miner_adds_makes_the_wager_unknown(
    streamer, event, logger, update
):
    # 350 may be the miner's 300, or 300 more by hand with the miner's
    # still to come.
    _send(
        streamer, event, "prediction-made", {"outcome_id": "outcome-no", "points": 50}
    )

    def request(event_id, outcome_id, amount):
        if update is not None:
            # Twitch's total, sent while the request is made.
            _send(
                streamer,
                event,
                "prediction-updated",
                {"outcome_id": "outcome-no", "points": update},
            )
        return MakePredictionResponse(None)

    _miner_bets(event, {"choice": 1, "amount": 300, "id": "outcome-no"}, request)
    _lose(streamer, event)

    assert _result_line(logger).endswith(f"Result: LOSE, Lost: {UNKNOWN}")
    assert streamer.history == {}


def test_a_total_after_the_miners_addition_makes_the_wager_unknown(
    streamer, event, logger
):
    _send(
        streamer, event, "prediction-made", {"outcome_id": "outcome-no", "points": 50}
    )
    _miner_bets(
        event,
        {"choice": 1, "amount": 300, "id": "outcome-no"},
        MakePredictionResponse(None),
    )
    # 50 + 350 by hand, reported late: whether the miner's 300 is in it
    # can't be told.
    _send(
        streamer,
        event,
        "prediction-updated",
        {"outcome_id": "outcome-no", "points": 400},
    )
    _send(
        streamer,
        event,
        "prediction-result",
        {"outcome_id": "outcome-no", "result": {"type": "LOSE", "points_won": None}},
    )

    assert _result_line(logger).endswith(f"Result: LOSE, Lost: {UNKNOWN}")


def test_a_refused_addition_to_a_known_bet_is_not_counted(streamer, event, logger):
    _send(
        streamer, event, "prediction-made", {"outcome_id": "outcome-no", "points": 50}
    )
    _miner_bets(
        event,
        {"choice": 1, "amount": 300, "id": "outcome-no"},
        MakePredictionResponse(Error("NOT_ALLOWED")),
    )
    _send(
        streamer,
        event,
        "prediction-result",
        {"outcome_id": "outcome-no", "result": {"type": "LOSE", "points_won": None}},
    )

    assert _result_line(logger).endswith("Result: LOSE, Lost: -50")


def test_a_bet_reported_without_points_while_the_miners_request_is_made_is_unknown(
    streamer, event, logger
):
    def request(event_id, outcome_id, amount):
        # A bet placed by hand before the timer, confirmed only now; or the
        # miner's own: without points, it can't be told which.
        _send(streamer, event, "prediction-made", {"outcome_id": "outcome-no"})
        return MakePredictionResponse(None)

    _miner_bets(event, {"choice": 1, "amount": 300, "id": "outcome-no"}, request)
    _send(
        streamer,
        event,
        "prediction-result",
        {"outcome_id": "outcome-no", "result": {"type": "LOSE", "points_won": None}},
    )

    line = _result_line(logger)
    assert "300" not in line
    assert line.endswith(f"Result: LOSE, Lost: {UNKNOWN}")
    assert streamer.history == {}


@pytest.mark.parametrize(
    "result_points, expected",
    [(None, f"Lost: {UNKNOWN}"), (370, "Lost: -370")],
    ids=["result-without-total", "result-with-total"],
)
def test_points_added_by_hand_while_the_miner_adds_make_the_total_unknown(
    streamer, event, logger, result_points, expected
):
    _send(
        streamer, event, "prediction-made", {"outcome_id": "outcome-no", "points": 50}
    )

    def request(event_id, outcome_id, amount):
        # 20 more added by hand while the miner's 300 are being added.
        _send(
            streamer,
            event,
            "prediction-updated",
            {"outcome_id": "outcome-no", "points": 70},
        )
        return MakePredictionResponse(None)

    _miner_bets(event, {"choice": 1, "amount": 300, "id": "outcome-no"}, request)
    prediction = {
        "outcome_id": "outcome-no",
        "result": {"type": "LOSE", "points_won": None},
    }
    if result_points is not None:
        prediction["points"] = result_points
    _send(streamer, event, "prediction-result", prediction)

    assert _result_line(logger).endswith(f"Result: LOSE, {expected}")


def test_an_older_total_delivered_after_the_miners_addition_makes_the_wager_unknown(
    streamer, event, logger
):
    _send(
        streamer, event, "prediction-made", {"outcome_id": "outcome-no", "points": 50}
    )
    _miner_bets(
        event,
        {"choice": 1, "amount": 300, "id": "outcome-no"},
        MakePredictionResponse(None),
    )
    # A total from before the addition, delivered late.
    _send(
        streamer,
        event,
        "prediction-updated",
        {"outcome_id": "outcome-no", "points": 50},
    )
    _send(
        streamer,
        event,
        "prediction-result",
        {"outcome_id": "outcome-no", "result": {"type": "LOSE", "points_won": None}},
    )

    assert _result_line(logger).endswith(f"Result: LOSE, Lost: {UNKNOWN}")


def test_a_result_is_reported_when_the_earlier_messages_were_missed(
    streamer, event, logger
):
    _credited(streamer, event, "PREDICTION", 120)
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

    assert streamer.annotations == [
        ("PREDICTION", "+120 - PREDICTION"),
        ("WIN", "Will it work?"),
    ]
    line = _result_line(logger)
    assert "Decision: 1: No (PINK)" in line
    assert line.endswith("Result: WIN, Gained: +70")
    assert streamer.history == {"PREDICTION": {"counter": 1, "amount": 70}}


def test_a_bet_by_hand_confirmed_after_the_miners_is_not_the_miners_alone(
    streamer, event, logger
):
    _miner_bets(
        event,
        {"choice": 1, "amount": 300, "id": "outcome-no"},
        MakePredictionResponse(None),
    )
    # A bet of 50 by hand from before the timer, reported only now.
    _send(
        streamer, event, "prediction-made", {"outcome_id": "outcome-no", "points": 50}
    )
    _lose(streamer, event)

    assert _result_line(logger).endswith(f"Result: LOSE, Lost: {UNKNOWN}")
    assert streamer.history == {}


@pytest.mark.parametrize("reported", [50, 300, 350], ids=["less", "equal", "more"])
def test_a_report_after_the_miners_first_bet_does_not_say_what_was_bet(
    streamer, event, logger, reported
):
    _miner_bets(
        event,
        {"choice": 1, "amount": 300, "id": "outcome-no"},
        MakePredictionResponse(None),
    )
    # The miner's own bet, or a bet by hand from before the timer reported
    # only now (300 of the miner's may come on top): Twitch doesn't say.
    _send(
        streamer,
        event,
        "prediction-made",
        {"outcome_id": "outcome-no", "points": reported},
    )
    _lose(streamer, event)

    assert _result_line(logger).endswith(f"Result: LOSE, Lost: {UNKNOWN}")
    assert streamer.history == {}


def test_an_older_total_does_not_clear_an_unknown_addition(streamer, event, logger):
    made = {"outcome_id": "outcome-no", "points": 50}
    _send(streamer, event, "prediction-made", made)
    _send(streamer, event, "prediction-updated", {"outcome_id": "outcome-no"})
    # The first message, delivered again.
    _send(streamer, event, "prediction-made", made)
    _lose(streamer, event)

    assert _result_line(logger).endswith(f"Result: LOSE, Lost: {UNKNOWN}")
    assert streamer.history == {}


def test_the_miners_amount_alone_is_not_the_wager(streamer, event, logger):
    _miner_bets(
        event,
        {"choice": 0, "amount": 100, "id": "outcome-yes"},
        MakePredictionResponse(None),
    )
    # No points on either message: a bet placed by hand before the timer
    # could be part of it.
    _send(streamer, event, "prediction-made", {"outcome_id": "outcome-yes"})
    _credited(streamer, event, "PREDICTION", 250)
    _send(
        streamer,
        event,
        "prediction-result",
        {"outcome_id": "outcome-yes", "result": {"type": "WIN", "points_won": 250}},
    )

    line = _result_line(logger)
    assert "Decision: 0: Yes (BLUE)" in line
    assert line.endswith(f"Result: WIN, Gained: {UNKNOWN}")
    assert streamer.history == {"PREDICTION": {"counter": 0, "amount": 0}}


def test_a_late_report_of_the_miners_addition_does_not_clear_a_later_one(
    streamer, event, logger
):
    _send(
        streamer, event, "prediction-made", {"outcome_id": "outcome-no", "points": 50}
    )
    _miner_bets(
        event,
        {"choice": 1, "amount": 300, "id": "outcome-no"},
        MakePredictionResponse(None),
    )
    # More added by hand, without a total; then Twitch's report of the
    # miner's addition, delivered late.
    _send(streamer, event, "prediction-updated", {"outcome_id": "outcome-no"})
    _send(
        streamer,
        event,
        "prediction-updated",
        {"outcome_id": "outcome-no", "points": 350},
    )
    _lose(streamer, event)

    assert _result_line(logger).endswith(f"Result: LOSE, Lost: {UNKNOWN}")
    assert streamer.history == {}


def test_a_result_while_the_miners_request_is_made_is_reported_at_once(
    streamer, event, logger
):
    _send(
        streamer, event, "prediction-made", {"outcome_id": "outcome-no", "points": 50}
    )
    lines = []

    def request(event_id, outcome_id, amount):
        _lose(streamer, event, points=370)
        # Reported before the request is answered.
        lines.append(_result_line(logger))
        return MakePredictionResponse(None)

    _miner_bets(event, {"choice": 1, "amount": 300, "id": "outcome-no"}, request)

    # The result's own total is the bet's.
    assert lines == [_result_line(logger)]
    assert lines[0].endswith("Result: LOSE, Lost: -370")
    assert streamer.history == {"PREDICTION": {"counter": 1, "amount": -370}}


@pytest.mark.parametrize(
    "answer",
    [
        MakePredictionResponse(None),
        MakePredictionResponse(Error("NOT_ALLOWED")),
        RetryError("MakePrediction", []),
    ],
    ids=["accepted", "refused", "request-failed"],
)
def test_a_result_without_a_total_while_the_miners_request_is_made_is_unknown(
    streamer, event, logger, answer
):
    _send(
        streamer, event, "prediction-made", {"outcome_id": "outcome-no", "points": 50}
    )

    def request(event_id, outcome_id, amount):
        _lose(streamer, event)
        if isinstance(answer, Exception):
            raise answer
        return answer

    _miner_bets(event, {"choice": 1, "amount": 300, "id": "outcome-no"}, request)

    line = _result_line(logger)
    assert "300" not in line
    assert line.endswith(f"Result: LOSE, Lost: {UNKNOWN}")
    assert streamer.history == {}


def test_a_miners_bet_that_failed_without_an_answer_makes_the_wager_unknown(
    streamer, event, logger
):
    _send(
        streamer, event, "prediction-made", {"outcome_id": "outcome-no", "points": 50}
    )
    # Twitch may have accepted the 300 and its answer been lost.
    _miner_bets(
        event,
        {"choice": 1, "amount": 300, "id": "outcome-no"},
        RetryError("MakePrediction", []),
    )
    _lose(streamer, event)

    assert _result_line(logger).endswith(f"Result: LOSE, Lost: {UNKNOWN}")
    assert streamer.history == {}


def test_a_total_from_before_an_unknown_addition_does_not_clear_it(
    streamer, event, logger
):
    _send(
        streamer, event, "prediction-made", {"outcome_id": "outcome-no", "points": 50}
    )
    _send(streamer, event, "prediction-updated", {"outcome_id": "outcome-no"})
    # An earlier update, never delivered before.
    _send(
        streamer,
        event,
        "prediction-updated",
        {"outcome_id": "outcome-no", "points": 90},
    )
    _lose(streamer, event)

    assert _result_line(logger).endswith(f"Result: LOSE, Lost: {UNKNOWN}")
    assert streamer.history == {}
