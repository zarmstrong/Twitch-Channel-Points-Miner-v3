"""A prediction the account bet on by hand, before or instead of the miner's
own decision (#404)."""

import json
import threading
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


def _credited(streamer, reason, amount):
    """community-points-user-v1's credit of a payout or refund."""

    streamer.update_history(reason, amount)


def _result_line(logger):
    (line,) = [
        call.args[0]
        for call in logger.info.call_args_list
        if call.args and "Result:" in call.args[0]
    ]
    return line


UNKNOWN = "unknown (the amounts aren't known)"


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
    _credited(streamer, "PREDICTION", 120)
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
    _credited(streamer, "PREDICTION", 500)
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

    # No points on either message: the miner's amount is the bet.
    _send(streamer, event, "prediction-made", {"outcome_id": "outcome-yes"})
    _credited(streamer, "PREDICTION", 250)
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
    assert streamer.history == {"PREDICTION": {"counter": 1, "amount": 150}}


@pytest.mark.parametrize("points_won", [40, None], ids=["payout", "no-payout"])
def test_a_refund_replaces_its_credit_with_a_prediction(
    streamer, event, logger, points_won
):
    _send(
        streamer, event, "prediction-made", {"outcome_id": "outcome-no", "points": 40}
    )
    _credited(streamer, "REFUND", 40)
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
    _credited(streamer, "PREDICTION", 120)
    _send(
        streamer,
        event,
        "prediction-result",
        {"outcome_id": "outcome-no", "result": {"type": "WIN", "points_won": None}},
    )

    assert _result_line(logger).endswith(f"Result: WIN, Gained: {UNKNOWN}")
    # The payout credit, less the wager.
    assert streamer.history == {"PREDICTION": {"counter": 1, "amount": 70}}


def test_a_refund_of_an_unknown_wager_uses_the_refunded_points(streamer, event, logger):
    _send(streamer, event, "prediction-made", {"outcome_id": "outcome-no"})
    _credited(streamer, "REFUND", 40)
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


def test_a_refund_with_no_amounts_is_left_as_credited(streamer, event, logger):
    _send(streamer, event, "prediction-made", {"outcome_id": "outcome-no"})
    _credited(streamer, "REFUND", 40)
    _send(
        streamer,
        event,
        "prediction-result",
        {"outcome_id": "outcome-no", "result": {"type": "REFUND", "points_won": None}},
    )

    logger.error.assert_not_called()
    assert _result_line(logger).endswith("Result: REFUND, Refunded: +0")
    assert streamer.history == {"REFUND": {"counter": 1, "amount": 40}}


class _WaitSignal(threading.Event):
    """miner_bet_answered that says when a result starts waiting on it."""

    def __init__(self):
        super().__init__()
        self.waiting = threading.Event()
        self.set()

    def wait(self, timeout=None):
        self.waiting.set()
        return super().wait(timeout)


def _while_the_request_waits(streamer, event, deliver, answer):
    """A request function for _miner_bets: on the websocket's thread,
    ``deliver()`` sends messages; the request answers ``answer`` (raises it)
    only once a result waits for that answer."""

    event.miner_bet_answered = _WaitSignal()
    threads = []

    def request(event_id, outcome_id, amount):
        thread = threading.Thread(target=deliver, daemon=True)
        threads.append(thread)
        thread.start()
        assert event.miner_bet_answered.waiting.wait(5)
        if isinstance(answer, Exception):
            raise answer
        return answer

    return request, threads


def _result_threads():
    return [
        thread
        for thread in threading.enumerate()
        if thread.name.startswith("prediction-result-")
    ]


def _join(threads):
    # The messages' threads, then any result waiting for the request.
    for thread in list(threads) + _result_threads():
        thread.join(5)
        assert not thread.is_alive()


def test_a_result_before_the_miners_request_answers_is_the_miners_bet(
    streamer, event, logger
):
    def deliver():
        _send(streamer, event, "prediction-made", {"outcome_id": "outcome-yes"})
        _credited(streamer, "PREDICTION", 250)
        _send(
            streamer,
            event,
            "prediction-result",
            {
                "outcome_id": "outcome-yes",
                "result": {"type": "WIN", "points_won": 250},
            },
        )

    request, threads = _while_the_request_waits(
        streamer, event, deliver, MakePredictionResponse(None)
    )
    _miner_bets(event, {"choice": 0, "amount": 100, "id": "outcome-yes"}, request)
    _join(threads)

    assert event.placed_by_hand is False
    assert _result_line(logger).endswith("Result: WIN, Gained: +150")
    assert streamer.history == {"PREDICTION": {"counter": 1, "amount": 150}}


@pytest.mark.parametrize(
    "answer",
    [
        MakePredictionResponse(Error("NOT_ALLOWED")),
        RetryError("MakePrediction", []),
    ],
    ids=["refused", "request-failed"],
)
def test_a_bet_by_hand_while_the_miners_request_fails_does_not_use_its_amount(
    streamer, event, logger, answer
):
    def deliver():
        _send(streamer, event, "prediction-made", {"outcome_id": "outcome-yes"})
        _send(
            streamer,
            event,
            "prediction-result",
            {
                "outcome_id": "outcome-yes",
                "result": {"type": "LOSE", "points_won": None},
            },
        )

    request, threads = _while_the_request_waits(streamer, event, deliver, answer)
    _miner_bets(event, {"choice": 0, "amount": 300, "id": "outcome-yes"}, request)
    _join(threads)

    line = _result_line(logger)
    assert "300" not in line
    assert line.endswith(f"Result: LOSE, Lost: {UNKNOWN}")
    assert streamer.history == {}


def _blocked_request(answer):
    """A request function for _miner_bets that answers ``answer`` once
    ``release`` is set; ``sent`` is set when it is called."""

    sent = threading.Event()
    release = threading.Event()

    def request(event_id, outcome_id, amount):
        sent.set()
        assert release.wait(5)
        return answer

    return request, sent, release


def test_a_result_waiting_for_the_request_does_not_hold_up_the_websocket(
    streamer, event, logger
):
    request, sent, release = _blocked_request(MakePredictionResponse(None))
    timer = threading.Thread(
        target=_miner_bets,
        args=(event, {"choice": 0, "amount": 100, "id": "outcome-yes"}, request),
        daemon=True,
    )
    timer.start()
    assert sent.wait(5)

    def deliver():
        _send(streamer, event, "prediction-made", {"outcome_id": "outcome-yes"})
        _credited(streamer, "PREDICTION", 250)
        _send(
            streamer,
            event,
            "prediction-result",
            {
                "outcome_id": "outcome-yes",
                "result": {"type": "WIN", "points_won": 250},
            },
        )

    websocket = threading.Thread(target=deliver, daemon=True)
    websocket.start()
    # The websocket's thread goes on to its next message while the request
    # still waits for its answer.
    websocket.join(5)
    assert not websocket.is_alive()
    assert timer.is_alive()
    assert not [
        call for call in logger.info.call_args_list if "Result:" in call.args[0]
    ]

    # However long the request takes, the result counts its answer.
    release.set()
    _join([timer])

    assert _result_line(logger).endswith("Result: WIN, Gained: +150")
    assert streamer.history == {"PREDICTION": {"counter": 1, "amount": 150}}


def test_a_failed_result_report_on_its_own_thread_is_logged(
    streamer, event, logger, monkeypatch
):
    request, sent, release = _blocked_request(MakePredictionResponse(None))
    timer = threading.Thread(
        target=_miner_bets,
        args=(event, {"choice": 0, "amount": 100, "id": "outcome-yes"}, request),
        daemon=True,
    )
    timer.start()
    assert sent.wait(5)
    _send(streamer, event, "prediction-made", {"outcome_id": "outcome-yes"})
    _send(
        streamer,
        event,
        "prediction-result",
        {"outcome_id": "outcome-yes", "result": {"type": "LOSE"}},
    )
    monkeypatch.setattr(
        EventPrediction, "parse_result", MagicMock(side_effect=RuntimeError("boom"))
    )
    release.set()
    _join([timer])

    (call,) = logger.error.call_args_list
    assert "Failed to report the result" in call.args[0]


@pytest.mark.parametrize(
    "update",
    [None, 50, 350],
    ids=["no-update", "update-before-the-addition", "update-with-the-addition"],
)
def test_the_miners_addition_to_a_known_bet_is_counted(streamer, event, logger, update):
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
    _send(
        streamer,
        event,
        "prediction-result",
        {"outcome_id": "outcome-no", "result": {"type": "LOSE", "points_won": None}},
    )

    assert _result_line(logger).endswith("Result: LOSE, Lost: -350")
    assert streamer.history == {"PREDICTION": {"counter": 1, "amount": -350}}


def test_a_total_after_the_miners_addition_is_used(streamer, event, logger):
    _send(
        streamer, event, "prediction-made", {"outcome_id": "outcome-no", "points": 50}
    )
    _miner_bets(
        event,
        {"choice": 1, "amount": 300, "id": "outcome-no"},
        MakePredictionResponse(None),
    )
    # More added by hand afterwards.
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

    assert _result_line(logger).endswith("Result: LOSE, Lost: -400")


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
