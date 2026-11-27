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

    def confirm_bet(self, prediction):
        """Record the bet a predictions-user-v1 ``prediction`` reports."""

        self.bet_confirmed = True
        if not isinstance(prediction, dict):
            return
        outcome_id = prediction.get("outcome_id")
        if isinstance(outcome_id, str) and outcome_id:
            self.confirmed_outcome_id = outcome_id
        points = _points(prediction.get("points"))
        if points is not None:
            self.confirmed_points = points

    def placed_choice(self):
        """The index in ``bet.outcomes`` of the outcome the account bet on,
        or None when it isn't known."""

        outcomes = self.bet.outcomes
        if self.confirmed_outcome_id is not None:
            for index, outcome in enumerate(outcomes):
                if (
                    isinstance(outcome, dict)
                    and outcome.get("id") == self.confirmed_outcome_id
                ):
                    return index
            # Twitch named an outcome this event doesn't list: the miner's
            # decision may be another one.
            return None
        choice = self.bet.decision.get("choice")
        if (
            isinstance(choice, int)
            and not isinstance(choice, bool)
            and 0 <= choice < len(outcomes)
        ):
            return choice
        return None

    def placed_points(self):
        """The points the account bet, or None when they aren't known."""

        if self.confirmed_points is not None:
            return self.confirmed_points
        choice = self.placed_choice()
        if choice is None or choice != self.bet.decision.get("choice"):
            # Not the miner's decision (or none was made): its amount is not
            # what was bet.
            return None
        return _points(self.bet.decision.get("amount"))

    def parse_result(self, result) -> dict:
        result_type = result["type"]

        points = {}
        wager = self.placed_points() if result_type != "REFUND" else 0
        points["placed"] = wager or 0
        points["won"] = (
            result["points_won"]
            if result["points_won"] or result_type == "REFUND"
            else 0
        )
        points["gained"] = (
            points["won"] - points["placed"] if result_type != "REFUND" else 0
        )
        points["prefix"] = "+" if points["gained"] >= 0 else ""

        action = (
            "Lost"
            if result_type == "LOSE"
            else ("Refunded" if result_type == "REFUND" else "Gained")
        )

        amount = (
            f"{points['prefix']}{millify(points['gained'])}"
            if wager is not None
            else "unknown (the bet's points aren't known)"
        )
        self.result = {
            "string": f"{result_type}, {action}: {amount}",
            "type": result_type,
            "gained": points["gained"],
        }

        return points
