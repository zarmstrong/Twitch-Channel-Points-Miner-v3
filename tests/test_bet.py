import pytest

from TwitchChannelPointsMiner.classes.entities.Bet import (
    Bet,
    BetSettings,
    Condition,
    FilterCondition,
    OutcomeKeys,
    Strategy,
)


def outcomes():
    return [
        {"id": "a", "title": "Alpha", "color": "BLUE"},
        {"id": "b", "title": "Beta", "color": "PINK"},
    ]


def update_payload():
    return [
        {
            "total_users": "30",
            "total_points": "300",
            "top_predictors": [{"points": 80}, {"points": 120}],
        },
        {
            "total_users": "70",
            "total_points": "700",
            "top_predictors": [{"points": 250}],
        },
    ]


def make_bet(strategy=Strategy.MOST_VOTED, **settings):
    bet_settings = BetSettings(strategy=strategy, percentage=10, max_points=500, **settings)
    bet = Bet(outcomes(), bet_settings)
    bet.update_outcomes(update_payload())
    return bet


def test_default_settings_fill_only_missing_values():
    settings = BetSettings(percentage=15, stealth_mode=True)

    settings.default()

    assert settings.strategy is Strategy.SMART
    assert settings.percentage == 15
    assert settings.percentage_gap == 20
    assert settings.stealth_mode is True


def test_update_outcomes_calculates_totals_percentages_and_odds():
    bet = make_bet()

    assert bet.total_users == 100
    assert bet.total_points == 1000
    assert bet.outcomes[0][OutcomeKeys.PERCENTAGE_USERS] == 30
    assert bet.outcomes[1][OutcomeKeys.ODDS] == pytest.approx(1.43)
    assert bet.outcomes[0][OutcomeKeys.TOP_POINTS] == 120


@pytest.mark.parametrize(
    ("strategy", "choice"),
    [
        (Strategy.MOST_VOTED, 1),
        (Strategy.HIGH_ODDS, 0),
        (Strategy.PERCENTAGE, 1),
        (Strategy.SMART_MONEY, 1),
        (Strategy.NUMBER_1, 0),
        (Strategy.NUMBER_2, 1),
    ],
)
def test_calculate_selects_expected_outcome(strategy, choice):
    decision = make_bet(strategy).calculate(balance=10_000)

    assert decision == {
        "choice": choice,
        "amount": 500,
        "id": outcomes()[choice]["id"],
    }


def test_calculate_uses_percentage_when_below_maximum():
    decision = make_bet().calculate(balance=2_000)

    assert decision["amount"] == 200


@pytest.mark.parametrize(
    ("condition", "threshold", "skipped"),
    [(Condition.GT, 90, False), (Condition.GT, 110, True), (Condition.LTE, 100, False)],
)
def test_filter_conditions(condition, threshold, skipped):
    filter_condition = FilterCondition(
        by=OutcomeKeys.TOTAL_USERS, where=condition, value=threshold
    )
    bet = make_bet(filter_condition=filter_condition)
    bet.calculate(balance=1_000)

    assert bet.skip() == (skipped, 100)
