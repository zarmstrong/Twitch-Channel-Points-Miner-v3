import threading

from TwitchChannelPointsMiner.classes.entities.Bet import Bet
from TwitchChannelPointsMiner.classes.entities.Streamer import Streamer
from TwitchChannelPointsMiner.classes.Settings import Settings
from TwitchChannelPointsMiner.utils import float_round, millify


def _points(value):
    """A points amount from a PubSub message, or None when it isn't one."""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


class EventPrediction(object):
    __slots__ = [
        "streamer",
        "event_id",
        "title",
        "created_at",
        "prediction_window_seconds",
        "status",
        "result",
        "box_fillable",
        "bet_confirmed",
        "bet_placed",
        "bet",
        "confirmed_outcome_id",
        "confirmed_points",
        "miner_bet_requested",
        "miner_bet_answered",
        "placed_by_hand",
        "wager_unknown",
        "state_lock",
    ]

    def __init__(
        self,
        streamer: Streamer,
        event_id,
        title,
        created_at,
        prediction_window_seconds,
        status,
        outcomes,
    ):
        self.streamer = streamer

        self.event_id = event_id
        self.title = title.strip()
        self.created_at = created_at
        self.prediction_window_seconds = prediction_window_seconds
        self.status = status
        self.result: dict = {"string": "", "type": None, "gained": 0}

        self.box_fillable = False
        self.bet_confirmed = False
        self.bet_placed = False
        self.bet = Bet(outcomes, streamer.settings.bet)
        # What Twitch says the account bet, from predictions-user-v1. The bet
        # may have been placed by hand, before the miner decided anything or
        # on another outcome than the miner's decision.
        self.confirmed_outcome_id = None
        self.confirmed_points = None
        # The miner sent its own bet (set before the request), and
        # bet_placed: Twitch accepted it.
        self.miner_bet_requested = False
        # Clear while the miner's bet request waits for Twitch's answer.
        self.miner_bet_answered = threading.Event()
        self.miner_bet_answered.set()
        self.placed_by_hand = False
        # Points were added to the bet without a new total.
        self.wager_unknown = False
        # The websocket thread confirms bets while the miner's timer thread
        # places its own.
        self.state_lock = threading.Lock()

    def __repr__(self):
        return f"EventPrediction(event_id={self.event_id}, streamer={self.streamer}, title={self.title})"

    def __str__(self):
        return (
            f"EventPrediction: {self.streamer} - {self.title}"
            if Settings.logger.less
            else self.__repr__()
        )

    def elapsed(self, timestamp):
        return float_round((timestamp - self.created_at).total_seconds())

    def closing_bet_after(self, timestamp):
        return float_round(self.prediction_window_seconds - self.elapsed(timestamp))

    def print_recap(self) -> str:
        return f"{self}\n\t\t{self.bet}\n\t\tResult: {self.result['string']}"

    def confirm_bet(self, prediction, addition=False):
        """Record the bet a predictions-user-v1 ``prediction`` reports.

        ``addition``: the message reports points added to the bet
        (prediction-updated)."""

        if not isinstance(prediction, dict):
            prediction = {}
        outcome_id = prediction.get("outcome_id")
        points = _points(prediction.get("points"))
        with self.state_lock:
            if not self.bet_confirmed and not self.miner_bet_requested:
                # Twitch confirmed a bet before the miner sent one: at least
                # part of it was placed by hand.
                self.placed_by_hand = True
            self.bet_confirmed = True
            if isinstance(outcome_id, str) and outcome_id:
                self.confirmed_outcome_id = outcome_id
            if points is not None:
                # The bet's total so far.
                self.confirmed_points = points
                self.wager_unknown = False
            elif addition:
                # Points were added, but how many isn't known.
                self.confirmed_points = None
                self.wager_unknown = True

    def begin_miner_bet(self):
        """The miner is about to send its own bet. Returns what was known of
        the bet before it, for miner_bet_accepted."""

        with self.state_lock:
            self.miner_bet_requested = True
            self.miner_bet_answered.clear()
            return (self.bet_confirmed, self.confirmed_points)

    def miner_bet_accepted(self, before, amount):
        """Twitch accepted the miner's bet of ``amount`` points; ``before``
        is what begin_miner_bet returned."""

        confirmed_before, points_before = before
        with self.state_lock:
            self.bet_placed = True
            if (
                confirmed_before
                and points_before is not None
                and self.confirmed_points is not None
                and _points(amount) is not None
            ):
                # The miner added to a bet Twitch had already confirmed. A
                # total can only grow: one from before the addition, or from
                # while it was being made, is at least this much now.
                self.confirmed_points = max(
                    self.confirmed_points, points_before + amount
                )

    def miner_bet_settled(self):
        """Twitch answered the miner's bet request (or it failed)."""

        self.miner_bet_answered.set()

    def miner_bet_pending(self):
        """The miner's own bet request is waiting for Twitch's answer."""

        return not self.miner_bet_answered.is_set()

    def wait_for_miner_bet(self):
        """Wait for Twitch's answer to the miner's bet request, if one is
        being made: until then it isn't known whether the miner's decision
        is part of the bet. The request always ends: each attempt has a
        timeout."""

        self.miner_bet_answered.wait()

    def _miner_choice(self):
        """The miner's decision, if Twitch accepted the bet it placed."""

        choice = self.bet.decision.get("choice")
        if (
            self.bet_placed
            and isinstance(choice, int)
            and not isinstance(choice, bool)
            and 0 <= choice < len(self.bet.outcomes)
        ):
            return choice
        return None

    def placed_choice(self):
        """The index in ``bet.outcomes`` of the outcome the account bet on,
        or None when it isn't known."""

        if self.confirmed_outcome_id is not None:
            for index, outcome in enumerate(self.bet.outcomes):
                if (
                    isinstance(outcome, dict)
                    and outcome.get("id") == self.confirmed_outcome_id
                ):
                    return index
            # Twitch named an outcome this event doesn't list: the miner's
            # decision may be another one.
            return None
        return self._miner_choice()

    def placed_points(self):
        """The points the account bet, or None when they aren't known."""

        if self.confirmed_points is not None:
            return self.confirmed_points
        choice = self._miner_choice()
        if (
            self.placed_by_hand
            or self.wager_unknown
            or choice is None
            or choice != self.placed_choice()
        ):
            # The miner's amount is the whole bet only when the miner alone
            # placed it.
            return None
        return _points(self.bet.decision.get("amount"))

    def parse_result(self, result) -> dict:
        """The points of a prediction-result ``result``: ``placed`` (the
        wager), ``won`` (the payout, or for a refund the points returned) and
        ``gained`` (the net change), each None when it isn't known."""

        result_type = result["type"]
        payout = _points(result.get("points_won"))
        placed = self.placed_points()

        points = {}
        if result_type == "REFUND":
            # A refund returns the wager.
            refunded = payout if payout is not None else placed
            points["placed"] = refunded
            points["won"] = refunded
            points["gained"] = 0
        elif result_type == "LOSE":
            points["placed"] = placed
            points["won"] = 0
            points["gained"] = None if placed is None else -placed
        else:
            points["placed"] = placed
            points["won"] = payout
            points["gained"] = (
                None if placed is None or payout is None else payout - placed
            )
        points["prefix"] = (
            "+" if points["gained"] is not None and points["gained"] >= 0 else ""
        )

        action = (
            "Lost"
            if result_type == "LOSE"
            else ("Refunded" if result_type == "REFUND" else "Gained")
        )

        amount = (
            f"{points['prefix']}{millify(points['gained'])}"
            if points["gained"] is not None
            else "unknown (the amounts aren't known)"
        )
        self.result = {
            "string": f"{result_type}, {action}: {amount}",
            "type": result_type,
            "gained": points["gained"],
        }

        return points
