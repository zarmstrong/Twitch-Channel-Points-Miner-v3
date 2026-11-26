# Watch streaks

[Back to the README](../README.md)

The miner prioritizes a streamer with a pending watch streak so the streak is
not missed. A pending streak can bypass `points_limit` until Twitch confirms the
streak, for at most `watch_streak_max_minutes` minutes in a watch slot per
broadcast.

Before treating a streak as pending, the miner checks Twitch's milestone for the
current broadcast. A streak already awarded before startup, or on a first visit,
is therefore not farmed again. The first broadcast of a streak advances the
milestone without paying bonus points, so Twitch sends no `WATCH_STREAK` event
for it. While a streak is pending, the miner rechecks the milestone after about
five watched minutes and then every few minutes, so the channel stops holding a
watch slot once the streak is confirmed.

Watched minutes count only time the channel actually holds a watch slot. Time it
spends waiting for a slot doesn't count.

## Unconfirmed streaks

Some streaks are never confirmed. A channel without channel points has no
streak, and watch time that Twitch doesn't credit never advances one. After
`watch_streak_max_minutes` minutes in a watch slot without a confirmation, the
miner stops prioritizing the streak for the rest of that broadcast. These
minutes include time whose minute-watched events Twitch didn't accept, so a
channel that isn't being credited can't keep its slot either. The channel then
competes for a slot like any other, and `points_limit` applies to it again.

Watched minutes and slot time are counted in memory. If the miner restarts while the same
broadcast is live, the count starts again from zero, and so does a `streak_only`
window.

Set it in `MINE_CONFIG`:

```python
MINE_CONFIG = {
    "watch_streak_max_minutes": 20,
}
```

The default is `20`. Values are kept between `11` and `60`; anything outside
that range is clamped, with a warning in the log.

## Logical streams

Twitch counts a broadcast that starts less than 30 minutes after the previous
one ended as the same stream for watch-streak purposes. A claim made on any
broadcast in that chain covers the current broadcast, so the miner does not farm
it again.

The miner measures that gap itself, from the last time it saw the previous
broadcast live to the start time Twitch reports for the new one. It may notice
a broadcast end late, for example when a reconnect drops Twitch's offline
message or the machine was asleep, so it never measures from that moment. The
measured gap is therefore never shorter than the real one. Either end can come
from this machine's clock, and the clock can be corrected between the two, so
only a gap under 26 minutes counts as a continuation: with the host clock within
2 minutes of the real time (as NTP keeps it), a claim is never carried into a
broadcast that really started 30 or more minutes later. A
continuation the miner can't prove is watched again, for at most
`watch_streak_max_minutes`, rather than risking a broken streak.

The gap is defined by `WATCH_STREAK_STREAM_GAP_SECONDS` (30 minutes) and the
clock margin by `WATCH_STREAK_CLOCK_SKEW_SECONDS` (2 minutes) in
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

With `streak_only=True`, each broadcast is watched for at most about 10 minutes,
and never longer than `watch_streak_max_minutes`.
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
