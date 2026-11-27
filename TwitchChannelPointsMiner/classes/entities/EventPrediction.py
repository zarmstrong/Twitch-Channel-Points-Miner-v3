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
        "wager_unknown",
        "state_lock",
        "confirmations",
        "points_floor",
        "totals_seen",
        "final_points",
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
        # The bet may hold more than any total known: points were added
        # without a new total, or a report can't be placed.  Only a new,
        # larger total or the result's clears it.
        self.wager_unknown = False
        # The websocket thread confirms bets while the miner's timer thread
        # places its own.
        self.state_lock = threading.Lock()
        # predictions-user-v1 messages about the bet so far.
        self.confirmations = 0
        # The bet is at least this many points.
        self.points_floor = 0
        # Every total Twitch reported: one seen again is an older message
        # delivered late (a total only grows).
        self.totals_seen = set()
        # The total on the prediction-result: the bet's final one.
        self.final_points = None

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

    def confirm_bet(self, prediction, addition=False, final=False):
        """Record the bet a predictions-user-v1 ``prediction`` reports.

        ``addition``: the message reports points added to the bet
        (prediction-updated). ``final``: it is the prediction-result."""

        if not isinstance(prediction, dict):
            prediction = {}
        outcome_id = prediction.get("outcome_id")
        points = _points(prediction.get("points"))
        with self.state_lock:
            self.bet_confirmed = True
            self.confirmations += 1
            if isinstance(outcome_id, str) and outcome_id:
                self.confirmed_outcome_id = outcome_id
            if final and points is not None:
                self.final_points = points
            elif points is None:
                if addition:
                    # Points were added, but how many isn't known.
                    self.wager_unknown = True
            elif points in self.totals_seen:
                # Reported before: nothing newer.
                pass
            elif points >= self.points_floor:
                # The bet's total so far.
                self.totals_seen.add(points)
                self.confirmed_points = points
                self.points_floor = points
                self.wager_unknown = False
            else:
                # Less than the bet is known to hold, and never reported: a
                # part of the bet (placed by hand) the miner didn't know of.
                self.totals_seen.add(points)
                self.wager_unknown = True

    def _known_points(self):
        return None if self.wager_unknown else self.confirmed_points

    def begin_miner_bet(self):
        """The miner is about to send its own bet. Returns what was known of
        the bet before it, for miner_bet_accepted."""

        with self.state_lock:
            self.miner_bet_requested = True
            self.miner_bet_answered.clear()
            return (self.bet_confirmed, self._known_points(), self.confirmations)

    def miner_bet_accepted(self, before, amount):
        """Twitch accepted the miner's bet of ``amount`` points; ``before``
        is what begin_miner_bet returned."""

        confirmed_before, points_before, confirmations_before = before
        amount = _points(amount)
        # The bet's total before the miner's points, None when unknown.
        base = points_before if confirmed_before else 0
        expected = None if base is None or amount is None else base + amount
        with self.state_lock:
            self.bet_placed = True
            known = self._known_points()
            if self.confirmations == confirmations_before or (
                # Reported again during the request, with nothing added.
                confirmed_before
                and points_before is not None
                and known == points_before
            ):
                if confirmed_before:
                    # The miner added to a bet Twitch had confirmed.
                    if expected is None:
                        self.wager_unknown = True
                    else:
                        self.confirmed_points = expected
                        self.wager_unknown = False
                        # As if Twitch had reported it: its own report of
                        # this total, delivered later, adds nothing.
                        self.totals_seen.add(expected)
            elif expected is None or known != expected:
                # Twitch reported the bet while the request was made: by
                # hand, or the miner's points, or both. Only a total of
                # exactly the earlier total plus the miner's points says
                # which; otherwise the wager is unknown until a new total.
                self.wager_unknown = True
            if amount is not None:
                self.points_floor = max(
                    self.points_floor, expected if expected is not None else amount
                )

    def abandon_miner_bet(self):
        """Stop waiting for the answer to the miner's bet request (the miner
        is stopping): whether its points are part of the bet stays
        unknown."""

        with self.state_lock:
            if self.miner_bet_answered.is_set():
                return
            self.wager_unknown = True
            self.miner_bet_answered.set()

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

        with self.state_lock:
            if self.final_points is not None:
                return self.final_points
            if self.wager_unknown:
                return None
            # Only a total Twitch reported says what the bet holds: even the
            # miner's own amount may share the bet with points placed by
            # hand that Twitch hasn't reported yet.
            return self.confirmed_points

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
