# Logs

[Back to the README](../README.md)

Logging is controlled by `LoggerSettings` in `MINER_CONFIG`. The default full
format includes timestamps, levels, function names, streamer state, point gains,
Drop activity, and prediction decisions. Set `less=True` for shorter console
messages while retaining the important event details.

## Full format

```text
14/07/26 12:00:00 - INFO - [run]: 💣 Start session: 'session-id'
14/07/26 12:00:02 - INFO - [set_online]: 🥳 Streamer(username=streamer-name, channel_id=0000000, channel_points=12000) is Online!
14/07/26 12:05:00 - INFO - [on_message]: 🚀 +10 → Streamer(username=streamer-name, channel_id=0000000, channel_points=12010) - Reason: WATCH.
14/07/26 12:06:00 - INFO - [claim_bonus]: 🎁 Claiming the bonus for Streamer(username=streamer-name, channel_id=0000000, channel_points=12010)!
```

## Compact format

```text
14/07 12:00:00 - 💣 Start session: 'session-id'
14/07 12:00:02 - 🥳 streamer-name (12k points) is Online!
14/07 12:05:00 - 🚀 +10 → streamer-name (12k points) - Reason: WATCH.
14/07 12:06:00 - 🎁 Claiming the bonus for streamer-name (12k points)!
```

## Graceful-shutdown report

During a controlled shutdown—such as `Ctrl+C` or Docker's normal stop signal—the
miner summarizes session duration, the log path, prediction results, and points
gained or spent per streamer and event type. It also reports Drop rewards whose
progress changed during the session, including watched minutes, minutes gained,
required minutes, and final status. Enable `scrape_drop_progress_on_load` to
capture the startup baseline required for this part of the report. When it is
disabled, the miner does not make an implicit startup inventory request or
estimate Drop gains from an incomplete baseline. Docker users should configure
the [recommended stop grace period](docker.md#graceful-docker-shutdowns). A hard kill,
host crash, or power loss cannot produce this report.

```text
14/07/26 18:00:00 - 🛑 Ending session: 'session-id'
14/07/26 18:00:00 - 📄 Logs file: /path/to/logs/username.timestamp.log
14/07/26 18:00:00 - ⌛ Duration 6:00:00
14/07/26 18:00:00 - 🤖 streamer-name (15k points), Total points gained: 3k
14/07/26 18:00:00 - 💰 CLAIM(4 times, 200 gained), WATCH(24 times, 240 gained)
14/07/26 18:00:00 - 🎁 Drop progress gained this session (1 reward):
14/07/26 18:00:00 - ⏳ Game - Campaign - Reward: 25/60m (+19m), in progress
```
