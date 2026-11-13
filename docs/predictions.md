# Predictions

[Back to the README](../README.md)

## BetSettings

| Key | Type | Default | Description |
|---|---|---|---|
| `strategy` | Strategy | `SMART` | Prediction outcome-selection strategy. |
| `percentage` | int | `5` | Percentage of the current balance to place. |
| `percentage_gap` | int | `20` | Percentage-point gap used by the SMART strategy. |
| `max_points` | int | `50000` | Maximum calculated prediction amount. |
| `minimum_points` | int | `0` | Balance that must be exceeded before placing a prediction. |
| `stealth_mode` | bool | `False` | Keep a large prediction just below the current highest prediction. |
| `filter_condition` | FilterCondition or None | `None` | Optional condition that must pass before placing a prediction. |
| `delay_mode` | DelayMode | `FROM_END` | How prediction timing is calculated. |
| `delay` | float | `6` | Timing value interpreted according to `delay_mode`. |

### Bet strategy

- **MOST_VOTED**: Select the option most voted based on users count
- **HIGH_ODDS**: Select the option with the highest odds
- **PERCENTAGE**: Select the option with the highest percentage based on odds (It's the same that show Twitch) - Should be the same as select LOWEST_ODDS
- **SMART_MONEY**: Select the option with the highest points placed.
- **SMART**: If the majority in percent chose an option, then follow the other users, otherwise select the option with the highest odds
- **NUMBER_1**: Always select the 1st option, BLUE side if there are only two options
- **NUMBER_2**: Always select the 2nd option, PINK side if there are only two options
- **NUMBER_3**: Always select the 3rd option
- **NUMBER_4**: Always select the 4th option
- **NUMBER_5**: Always select the 5th option
- **NUMBER_6**: Always select the 6th option
- **NUMBER_7**: Always select the 7th option
- **NUMBER_8**: Always select the 8th option

![Screenshot](https://raw.githubusercontent.com/Tkd-Alex/Twitch-Channel-Points-Miner-v2/master/assets/prediction.png)

Here is a concrete example:

- **MOST_VOTED**: 21 users selected **“over 7.5”**, compared with 9 for “under 7.5.”
- **HIGH_ODDS**: The highest odd is 2.27 on **'over 7.5'** vs 1.79 on 'under 7.5'
- **PERCENTAGE**: The highest percentage is 56% for **'under 7.5'**
- **SMART**: Calculate the percentage based on the users. The percentages are: 'over 7.5': 70% and 'under 7.5': 30%. If the difference between the two percentages is higher than `percentage_gap` select the highest percentage, else the highest odds.

With `percentage_gap=20`, the 40-point difference exceeds the threshold, so the
miner selects “over 7.5.”
### FilterCondition

| Key | Type | Default | Description |
|---|---|---|---|
| `by` | OutcomeKeys | `None` | Prediction value to evaluate. |
| `where` | Condition | `None` | Comparison operator. |
| `value` | number | `None` | Comparison target. |

Allowed values for `by` are:
- `PERCENTAGE_USERS` (no sum) [Would never want a sum as it'd always be 100%]
- `ODDS_PERCENTAGE` (no sum) [Doesn't make sense to sum odds]
- `ODDS` (no sum) [Doesn't make sense to sum odds]
- `DECISION_USERS` (no sum)
- `DECISION_POINTS` (no sum)
- `TOP_POINTS` (no sum) [Doesn't make sense to the top points of both sides]
- `TOTAL_USERS` (sum)
- `TOTAL_POINTS` (sum)

Allowed values for `where` are: `GT, LT, GTE, LTE`

#### Example

Require more than 200 total participants:

```python
FilterCondition(by=OutcomeKeys.TOTAL_USERS, where=Condition.GT, value=200)
```

Require the selected outcome's odds to be at least 1.3:

```python
FilterCondition(by=OutcomeKeys.ODDS, where=Condition.GTE, value=1.3)
```

Require the largest prediction to be below 2,000 points:

```python
FilterCondition(by=OutcomeKeys.TOP_POINTS, where=Condition.LT, value=2000)
```

### DelayMode

- **FROM_START**: Will wait `delay` seconds from when the bet was opened
- **FROM_END**: Wait until `delay` seconds remain in the prediction window
- **PERCENTAGE**: Will place the bet when `delay` percent of the set timer is elapsed

Here's a concrete example. Let's suppose we have a bet that is opened with a timer of 10 minutes:

- **FROM_START** with `delay=20`: The bet will be placed 20s after the bet is opened
- **FROM_END** with `delay=20`: The bet will be placed 20s before the end of the bet (so 9mins 40s after the bet is opened)
- **PERCENTAGE** with `delay=0.2`: The bet will be placed when the timer went down by 20% (so 2mins after the bet is opened)
