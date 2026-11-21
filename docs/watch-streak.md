# Watch streaks

[Back to the README](../README.md)

The miner prioritizes a streamer with a pending watch streak so the streak is
not missed. A pending streak can bypass `points_limit` until Twitch confirms the
streak.

Before treating a streak as pending, the miner checks Twitch's milestone for the
current broadcast. A streak already awarded before startup, or on a first visit,
is therefore not farmed again. The first broadcast of a streak advances the
milestone without paying bonus points, so Twitch sends no `WATCH_STREAK` event
for it. While a streak is pending, the miner rechecks the milestone after about
five watched minutes and then every few minutes, so the channel stops holding a
watch slot once the streak is confirmed.

## Logical streams

Twitch counts a broadcast that starts less than 30 minutes after the previous
one ended as the same stream for watch-streak purposes. A claim made on any
broadcast in that chain covers the current broadcast, so the miner does not farm
it again.

The gap is defined by `WATCH_STREAK_STREAM_GAP_SECONDS` (30 minutes) in
`TwitchChannelPointsMiner/constants.py`.

## `streak_only`

Set `streak_only` in `StreamerSettings` to watch a channel only for its watch
streak:

```python
STREAMERS = [
    Streamer(
        "some-channel",
        settings=StreamerSettings(streak_only=True),
    ),
]
```

With `streak_only=True`, each broadcast is watched for at most about 10 minutes.
The channel is left once the streak is claimed, so no extra watch-time points
are farmed there. A counted broadcast needs only a few minutes, so
multi-broadcast streaks advance without camping the whole stream.

Set an integer to override the per-broadcast window in minutes:

```python
StreamerSettings(streak_only=5)
```

The dashboard shows this as `Streak only (5 min)` and keeps the custom window
when you save other settings there. Unticking the box turns the option off.

`streak_only` defaults to `False`.
