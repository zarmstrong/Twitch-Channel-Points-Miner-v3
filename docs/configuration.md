# Configuration

[Back to the README](../README.md)

## Configuration sections

| Name | Passed to | Purpose |
|---|---|---|
| `MINER_CONFIG` | `TwitchChannelPointsMiner(...)` | Account, priority, default streamer behavior, logging, notifications, and global runtime options. |
| `STREAMERS` | First argument of `mine(...)` | Ordered streamer logins or `Streamer` objects with per-channel overrides. Use `[]` when relying only on followers or categories. |
| `MINE_CONFIG` | Keyword arguments of `mine(...)` | Followers, blacklist, category discovery, Drops diagnostics, and other mining-session options. Do not include `streamers` here. |
| `ANALYTICS_CONFIG` | `analytics(...)` | Analytics server options. Set to `None` to leave the server disabled. |

### MINER_CONFIG

`MINER_CONFIG` contains account-wide and constructor settings. At minimum, set
`username`. This is also where you configure mining priority, analytics storage,
SSL behavior, logging and notification integrations,
the number of concurrent streams watched, and the default `StreamerSettings`
inherited by channels without overrides. `streams_watched` accepts `1` or `2`
and defaults to `2`. This option allows Twitch Turbo users to limit the miner to
one concurrent viewing session: reports in issue #787 indicate that continuously
mining two channels alongside normal Twitch viewing may contribute to Turbo users
being served ads. Invalid values are logged and replaced with `2`.

`streamer_source_priority` controls which source gets the available viewing
slots first. Its default is configured streamers, followed streamers, ordinary
category discovery, automatic badge campaigns, then the wildcard category
fallback:

```python
streamer_source_priority=[
    StreamerSource.STREAMERS,
    StreamerSource.FOLLOWERS,
    StreamerSource.CATEGORIES,
    StreamerSource.BADGES,
    StreamerSource.WILDCARD_CATEGORIES,
]
```

Move `StreamerSource.BADGES` to the front to prioritize earning badge Drops.
The `priority` rules are applied globally; source priority breaks ties within
each rule. For example, `Priority.DROPS` can reserve a slot for an active
category campaign before `Priority.ORDER` fills remaining slots with configured
streamers.

`StreamerSource.WILDCARD_CATEGORIES` is only ever populated once the
preferred `categories` pass leaves capacity idle for the cycle (see
[Wildcard category fallback](categories-and-drops.md#wildcard-category-fallback)) -- reordering it
only changes how it competes for a watch slot against `CATEGORIES`/`BADGES`
*when it has something to offer*, it does not make it activate any earlier.
It defaults to last so it never silently outranks a curated `categories` list
or `auto_mine_badge_drops`; move it earlier only if you deliberately want
wildcard-discovered streams to win watch slots over those sources when both
are eligible at once.

The web config dashboard's "Stream sources" list mirrors this setting: each
row is a source with an on/off checkbox, and rows can be dragged into the
order you want `streamer_source_priority` to end up in. Saving writes both
the enabled sources and their order back to `config.py` in one step.

See [Settings](settings.md#settings) for the available priority, logger, streamer, and bet
objects. Keep credentials and integration tokens only in your private
`config/config.py`.

### STREAMERS

`STREAMERS` is the ordered list of fixed channels. Each entry can be a lowercase
login string or a `Streamer(...)` object when that channel needs custom
`StreamerSettings`. Use an empty list when mining exclusively from followed
channels or category discovery.

List order is a real priority control, not just a display preference:
`Priority.ORDER`'s tie-break and the final watch-slot selection both key off
each streamer's position in this list, so an earlier entry wins a contested
watch slot over a later one when nothing else distinguishes them. The web
config dashboard's "Configured streamers" list can be reordered by dragging
rows (via the handle icon); that writes straight back to this list's order in
`config.py`.

Streamer settings follow this precedence: settings on an individual `Streamer`,
then the default `streamer_settings` in `MINER_CONFIG`, then project defaults.
Plain strings in `STREAMERS` use the configured defaults.

Common streamer-list patterns:

```python
# Fixed list
STREAMERS = ["streamer1", "streamer2"]
MINE_CONFIG = {"followers": False, "categories": []}

# All followed channels
STREAMERS = []
MINE_CONFIG = {"followers": True, "followers_order": FollowersOrder.ASC}

# Fixed list plus followers, excluding selected users
STREAMERS = ["streamer1"]
MINE_CONFIG = {
    "followers": True,
    "followers_order": FollowersOrder.DESC,
    "blacklist": ["user1", "user2"],
}
```

### MINE_CONFIG

`MINE_CONFIG` contains the keyword arguments for the mining session. Use it to
combine followed channels with `STREAMERS`, apply a blacklist, configure follower
ordering, discover channels by category, and control category/Drop diagnostics.
The streamer list itself belongs in `STREAMERS`, not in this dictionary.

## ANALYTICS_CONFIG

Set `ANALYTICS_CONFIG` to `None` when the web server is not needed. Otherwise,
provide the keyword arguments for `analytics(...)`, such as `host`, `port`,
`refresh`, `days_ago`, `password`, and `log_poll_interval`. Analytics storage
also requires `MINER_CONFIG["enable_analytics"] = True`. See [Analytics](analytics.md#analytics)
for configuration and security guidance.

## Runner behavior

Docker, Windows, and source checkouts all use the stable application runner. It
loads `config/config.py`, starts analytics when configured, applies supported
configuration migrations, watches for supported live changes, and starts
mining. Directly constructing `TwitchChannelPointsMiner` and calling `.mine(...)`
is the legacy `run.py` workflow; existing installations can
[migrate from run.py](migration.md#migrating-from-runpy).

The runner checks `config/config.py` every five seconds. Set
`TCPM_CONFIG_RELOAD_SECONDS` to change the interval; the minimum is one second.

At startup, the runner checks `CONFIG_VERSION`. Unversioned configurations are
backed up as `config.py.v0.bak` and upgraded in place. The migration adds missing
global `StreamerSettings` fields with their defaults and appends newly introduced
priority values to the existing priority list without reordering it. A config
created by a newer unsupported release is rejected rather than rewritten. The
obsolete Twitch `MINER_CONFIG["password"]` field is removed from both the active
configuration and any migration backup; Twitch authentication uses the TV
activation flow instead.
Generated Python is parsed before replacement. If an earlier migration left an
invalid file and a versioned backup exists, startup rebuilds the config from that
backup without overwriting it.

### Configuration reload limitations

- New entries in `STREAMERS` are applied without a restart. Removing an
  explicitly configured streamer also removes its loaded state, chat connection,
  and channel PubSub subscriptions. A channel discovered from another enabled
  source remains loaded under that source.
- Changes to `MINE_CONFIG["categories"]` trigger discovery after the watcher
  detects the edit. Previously discovered channels remain loaded until restart.
- Changes to existing streamer settings, `MINER_CONFIG`, `ANALYTICS_CONFIG`, and
  non-category `MINE_CONFIG` options require a restart.
- Invalid configuration is logged and ignored. The watcher retries after the
  file changes again.

## Supervisor (optional)

The [supervisor directory](../supervisor/) provides an optional supervisor for
source checkouts on Windows, Linux, and macOS. It runs
`python -m TwitchChannelPointsMiner.runner --config-dir config`
from the checkout, restarts it on crashes and on a daily schedule, applies
`git` updates, and sends alerts. It reads `config/config.py` like every other
run method. The Windows executable and Docker do not need it.

When an update changes files in `supervisor/`, the supervisor restarts itself
right after updating, so the new supervisor code runs without a manual restart.

```sh
cd Twitch-Channel-Points-Miner-v3
python3 supervisor/minerctl.py install   # checks config/config.py, creates .venv, registers the jobs
python3 supervisor/minerctl.py health    # HEALTHY / WARNING / DOWN
python3 supervisor/minerctl.py start | stop | uninstall
```

On Windows, run `.\supervisor\MinerTask.ps1` from an Administrator PowerShell
in the checkout (`-Action Health`, `-Action Stop`, and `-Action Uninstall` also
work). Optional alerts go in `supervisor/miner-alerts.json`; copy
`supervisor/miner-alerts.example.json` and keep the file private. Logs and
status are written to `supervisor-logs/`.
