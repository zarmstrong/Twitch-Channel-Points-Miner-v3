![Twitch Channel Points Miner - v2](https://raw.githubusercontent.com/rdavydov/Twitch-Channel-Points-Miner-v2/master/assets/banner.png)
<p align="center">
<a href="https://github.com/rdavydov/Twitch-Channel-Points-Miner-v2/releases"><img alt="Latest Version" src="https://img.shields.io/github/v/release/rdavydov/Twitch-Channel-Points-Miner-v2?style=flat&color=white&logo=github&logoColor=white"></a>
<a href="https://github.com/rdavydov/Twitch-Channel-Points-Miner-v2/stargazers"><img alt="GitHub Repo stars" src="https://img.shields.io/github/stars/rdavydov/Twitch-Channel-Points-Miner-v2?style=flat&color=limegreen&logo=github&logoColor=white"></a>
<a href='https://github.com/MShawon/github-clone-count-badge'><img alt='GitHub Traffic' src='https://img.shields.io/badge/dynamic/json?style=flat&color=blue&label=views&query=count&url=https://gist.githubusercontent.com/rdavydov/ad9a3c6a8d9c322f9a6b62781ea94a93/raw/traffic.json&logo=github&logoColor=white'></a>
<a href='https://github.com/MShawon/github-clone-count-badge'><img alt='GitHub Clones' src='https://img.shields.io/badge/dynamic/json?style=flat&color=purple&label=clones&query=count&url=https://gist.githubusercontent.com/rdavydov/fed04b31a250ad522d9ea6547ce87f95/raw/clone.json&logo=github&logoColor=white'></a>
<a href="https://github.com/rdavydov/Twitch-Channel-Points-Miner-v2/blob/master/LICENSE"><img alt="License" src="https://img.shields.io/github/license/rdavydov/Twitch-Channel-Points-Miner-v2?style=flat&color=black&logo=unlicense&logoColor=white"></a>
<a href="https://github.com/rdavydov/Twitch-Channel-Points-Miner-v2"><img alt="GitHub last commit" src="https://img.shields.io/github/last-commit/rdavydov/Twitch-Channel-Points-Miner-v2?style=flat&color=lightyellow&logo=github&logoColor=white"></a>
</p>

<p align="center">
<a href="https://hub.docker.com/r/rdavidoff/twitch-channel-points-miner-v2"><img alt="Docker Version" src="https://img.shields.io/docker/v/rdavidoff/twitch-channel-points-miner-v2?style=flat&color=white&logo=docker&logoColor=white&label=release"></a>
<a href="https://hub.docker.com/r/rdavidoff/twitch-channel-points-miner-v2"><img alt="Docker Stars" src="https://img.shields.io/docker/stars/rdavidoff/twitch-channel-points-miner-v2?style=flat&color=limegreen&logo=docker&logoColor=white&label=stars"></a>
<a href="https://hub.docker.com/r/rdavidoff/twitch-channel-points-miner-v2"><img alt="Docker Pulls" src="https://img.shields.io/docker/pulls/rdavidoff/twitch-channel-points-miner-v2?style=flat&color=blue&logo=docker&logoColor=white&label=pulls"></a>
<a href="https://hub.docker.com/r/rdavidoff/twitch-channel-points-miner-v2"><img alt="Docker Images Size AMD64" src="https://img.shields.io/docker/image-size/rdavidoff/twitch-channel-points-miner-v2/latest?arch=amd64&label=AMD64 image size&style=flat&color=purple&logo=amd&logoColor=white"></a>
<a href="https://hub.docker.com/r/rdavidoff/twitch-channel-points-miner-v2"><img alt="Docker Images Size ARM64" src="https://img.shields.io/docker/image-size/rdavidoff/twitch-channel-points-miner-v2/latest?arch=arm64&label=ARM64 image size&style=flat&color=black&logo=arm&logoColor=white"></a>
<a href="https://hub.docker.com/r/rdavidoff/twitch-channel-points-miner-v2"><img alt="Docker Images Size ARMv7" src="https://img.shields.io/docker/image-size/rdavidoff/twitch-channel-points-miner-v2/latest?arch=arm&label=ARMv7 image size&style=flat&color=lightyellow&logo=arm&logoColor=white"></a>
</p>


<h1 align="center">https://github.com/rdavydov/Twitch-Channel-Points-Miner-v2</h1>

**Credits**
- Main idea: https://github.com/gottagofaster236/Twitch-Channel-Points-Miner
- ~~Bet system (Selenium): https://github.com/ClementRoyer/TwitchAutoCollect-AutoBet~~
- Based on: https://github.com/Tkd-Alex/Twitch-Channel-Points-Miner-v2

> A simple script that will watch a stream for you and earn the channel points.

> It can wait for a streamer to go live (+_450 points_ when the stream starts), it will automatically click the bonus button (_+50 points_), and it will follow raids (_+250 points_).

Read more about the channel points [here](https://help.twitch.tv/s/article/channel-points-guide).

# README Contents

1. 🤝 [Community](#community)
2. 🚀 [Features](#features)
3. 🧐 [How to use](#how-to-use)
    - [Configuration file](#configuration-file)
    - [Configuration sections](#configuration-sections)
        - [MINER_CONFIG](#miner_config)
        - [STREAMERS](#streamers)
        - [MINE_CONFIG](#mine_config)
        - [Category-based Drops](#category-based-drops)
            - [How category discovery works](#how-category-discovery-works)
            - [Category configuration reference](#category-configuration-reference)
            - [Category input formats](#category-input-formats)
            - [Channel selection and sorting](#channel-selection-and-sorting)
            - [Campaign filtering and ordering](#campaign-filtering-and-ordering)
            - [Refresh behavior](#refresh-behavior)
            - [Category logging and analytics](#category-logging-and-analytics)
            - [Category examples](#category-examples)
            - [Category troubleshooting](#category-troubleshooting)
        - [ANALYTICS_CONFIG](#analytics_config)
    - [Local installation](#local-installation)
    - [Starting the miner](#starting-the-miner)
        - [Configuration reload limitations](#configuration-reload-limitations)
    - [Docker](#docker)
        - [Docker Hub](#docker-hub)
        - [Portainer](#portainer)
    - [Replit](#replit)
    - [Limits](#limits)
4. 🔧 [Settings](#settings)
    - [LoggerSettings](#loggersettings)
        - [Color Palette](#color-palette)
        - [Telegram](#telegram)
        - [Discord](#discord)
        - [Generic Webhook](#generic-webhook)
        - [Events](#events)
    - [StreamerSettings](#streamersettings)
    - [BetSettings](#betsettings)
        - [Bet strategy](#bet-strategy)
        - [FilterCondition](#filtercondition)
            - [Example](#example)
        - [DelayMode](#delaymode)
5. 📈 [Analytics](#analytics)
    - [Analytics security and HTTPS reverse proxy](#analytics-security-and-https-reverse-proxy)
    - [Enabling analytics storage](#enabling-analytics-storage)
6. 🧾 [Logs](#logs)
    - [Full format](#full-format)
    - [Compact format](#compact-format)
    - [Graceful-shutdown report](#graceful-shutdown-report)
7. 🔄 [Migrating from run.py](#migrating-from-runpy)
    - [Automatic Docker migration](#automatic-docker-migration)
    - [What converts automatically](#what-converts-automatically)
8. 🍪 [Legacy cookie migration (optional)](#legacy-cookie-migration-optional)
9. 🪟 [Windows](#windows)
10. 📱 [Termux](#termux)
11. ⚠️ [Disclaimer](#disclaimer)

## Community
If you want to help with this project, please leave a star 🌟 and share it with your friends! 😎

If you want to offer me a coffee, I would be grateful! ❤️

|                                                                                                                                                                                                                                                                                                           |                                               |
|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|-----------------------------------------------|
|<a href="https://bitcoin.org" target="_blank"><img src="https://dynamic-assets.coinbase.com/e785e0181f1a23a30d9476038d9be91e9f6c63959b538eabbc51a1abc8898940383291eede695c3b8dfaa1829a9b57f5a2d0a16b0523580346c6b8fab67af14b/asset_icons/b57ac673f06a4b0338a596817eb0a50ce16e2059f327dc117744449a47915cb2.png" alt="Donate BTC" height="16" width="16"></a>|`bc1qq49mvgda2zw4f9kta0a85xztwuxewqwac5eckd` _(<a href="https://bitcoin.org" target="_blank">BTC</a>)_|
|<a href="https://dogechain.info" target="_blank"><img src="https://dynamic-assets.coinbase.com/3803f30367bb3972e192cd3fdd2230cd37e6d468eab12575a859229b20f12ff9c994d2c86ccd7bf9bc258e9bd5e46c5254283182f70caf4bd02cc4f8e3890d82/asset_icons/1597d628dd19b7885433a2ac2d7de6ad196c519aeab4bfe679706aacbf1df78a.png" alt="Donate DOGE" height="16" width="16"></a>|`DAKzncwKkpfPCm1xVU7u2pConpXwX7HS3D` _(<a href="https://dogechain.info" target="_blank">DOGE</a>)_|
|<a href="https://www.donationalerts.com/r/rdavydov" target="_blank"><img src="https://www.donationalerts.com/static/donations/dist/favicon.ico" alt="Donate via DonationAlerts" height="16" width="16"></a>|https://www.donationalerts.com/r/rdavydov|
|<a href="https://boosty.to/rdavydov/donate" target="_blank"><img src="https://static.boosty.to/static/favicon.png?v=11" alt="Donate via Boosty" height="16" width="16"></a>|https://boosty.to/rdavydov/donate|

If you have any issues or you want to contribute, you are welcome! But please read the [CONTRIBUTING.md](https://github.com/rdavydov/Twitch-Channel-Points-Miner-v2/blob/master/CONTRIBUTING.md) file. Contributors working on code can find setup, test commands, and suite conventions in the [testing guide](tests/README.md).

## Features

- Automatically watches eligible channels and claims channel-point bonuses.
- Waits for configured streamers to go live and follows eligible raids.
- Prioritizes channels by watch streaks, Drops, subscriptions, configured order,
  or channel-point balance.
- Discovers live channels from configured game categories and active Drop
  campaigns.
- Tracks Drop progress and claims available Drop rewards and Moments.
- Participates in channel-point predictions with configurable strategies,
  spending limits, filters, and timing.
- Supports per-streamer settings, follower imports, and blacklists.
- Can join IRC chat and notify you when your username is mentioned.
- Sends selected events through Telegram, Discord, Matrix, Pushover, Gotify, or a
  generic webhook.
- Provides colorized console logs, rotating log files, compact logging, and a
  graceful-shutdown report.
- Includes an optional analytics server for point history, Drops, and log viewing.
- Supports persistent Docker configuration with automatic legacy-runner
  conversion and limited live configuration reloads.

## How to use

The miner uses a persistent Python configuration file at `config/config.py`.
The stable application runner creates the miner, starts analytics when
configured, watches the file for supported changes, and starts mining.

Start with the maintained template:

```sh
mkdir -p config
cp config.example.py config/config.py
```

Edit `config/config.py` and replace all placeholder usernames, passwords, webhook
URLs, and notification credentials. Remove or set optional integrations to
`None` when you do not use them. Never commit the populated file or share it in
logs or issue reports.

### Configuration file

A minimal configuration looks like this:

```python
from TwitchChannelPointsMiner.classes.Settings import FollowersOrder

MINER_CONFIG = {
    "username": "your-twitch-username",
    # Omit "password" to enter it interactively when authentication is needed.
    "enable_analytics": False,
}

STREAMERS = ["streamer1", "streamer2"]

MINE_CONFIG = {
    "followers": False,
    "followers_order": FollowersOrder.ASC,
    "blacklist": [],
    "categories": [],
}

# Set to a dictionary to start the analytics server, or leave as None.
ANALYTICS_CONFIG = None
```

The file is normal Python, so import any enums or settings classes used by its
values. See [config.example.py](config.example.py) for the complete annotated
configuration, including logger integrations, per-streamer settings, predictions,
categories, and analytics.

### Configuration sections

| Name | Passed to | Purpose |
|---|---|---|
| `MINER_CONFIG` | `TwitchChannelPointsMiner(...)` | Account, priority, default streamer behavior, logging, notifications, and global runtime options. |
| `STREAMERS` | First argument of `mine(...)` | Ordered streamer logins or `Streamer` objects with per-channel overrides. Use `[]` when relying only on followers or categories. |
| `MINE_CONFIG` | Keyword arguments of `mine(...)` | Followers, blacklist, category discovery, Drops diagnostics, and other mining-session options. Do not include `streamers` here. |
| `ANALYTICS_CONFIG` | `analytics(...)` | Analytics server options. Set to `None` to leave the server disabled. |

#### MINER_CONFIG

`MINER_CONFIG` contains account-wide and constructor settings. At minimum, set
`username`. This is also where you configure the optional password, mining
priority, analytics storage, SSL behavior, logging and notification integrations,
and the default `StreamerSettings` inherited by channels without overrides.

See [Settings](#settings) for the available priority, logger, streamer, and bet
objects. Keep credentials and integration tokens only in your private
`config/config.py`.

#### STREAMERS

`STREAMERS` is the ordered list of fixed channels. Each entry can be a lowercase
login string or a `Streamer(...)` object when that channel needs custom
`StreamerSettings`. Use an empty list when mining exclusively from followed
channels or category discovery.

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

#### MINE_CONFIG

`MINE_CONFIG` contains the keyword arguments for the mining session. Use it to
combine followed channels with `STREAMERS`, apply a blacklist, configure follower
ordering, discover channels by category, and control category/Drop diagnostics.
The streamer list itself belongs in `STREAMERS`, not in this dictionary.

#### Category-based Drops

Use `categories` to mine Drops for games without maintaining a fixed list of
streamer usernames. The miner finds configured games with active, incomplete
Drop campaigns, discovers eligible live channels, and adds those channels to the
normal mining session. Categories can be used by themselves or together with
`STREAMERS` and `MINE_CONFIG["followers"] = True`.

Import the category enums (and `ChatPresence` if you want a category-specific
chat policy) before using the examples below:

```python
import logging

from TwitchChannelPointsMiner.classes.Chat import ChatPresence
from TwitchChannelPointsMiner.classes.Settings import (
    CategoryCampaignOrder,
    CategorySort,
)

MINE_CONFIG = {
    "categories": [
        "pokemon-go",
        "warframe",
    ],
    "category_limit": 5,
    "category_sort": CategorySort.VIEWERS_DESC,
    "category_campaign_order": CategoryCampaignOrder.EXPIRATION,
    "category_chat": ChatPresence.NEVER,
    "category_log_level": logging.INFO,
    "category_refresh_interval_hours": 6,
}
```

##### How category discovery works

At startup, the miner:

1. Reads the configured category values and checks current Drop campaigns.
2. Keeps categories that have an active campaign with an incomplete, eligible
   time-based Drop.
3. Resolves each category to its Twitch game and searches for live channels.
4. Applies the Drops tag, campaign-channel, limit, and sorting rules described
   below.
5. Loads the discovered users as category streamers and subscribes to the same
   relevant Twitch events used for explicitly configured streamers.

If no configured category has an active incomplete campaign, category discovery
is skipped. Explicit `streamers` and downloaded followers are unaffected.

Discovered category channels use the miner's default `StreamerSettings`, except
that `category_chat` can override their chat presence. The blacklist also applies
to discovered channels. If a username is both explicitly configured and found by
category discovery, it remains explicitly configured and keeps its explicit
settings.

##### Category configuration reference

| Option | Default | Description |
|---|---:|---|
| `categories` | `[]` | Category names, slugs, URLs, or category/streamer selectors to discover. |
| `category_drops_enabled` | `True` | Require the live channel to have a `DropsEnabled` tag. This also affects campaign eligibility checks. |
| `category_limit` | `30` | Maximum channels returned by a normal category search. Values below 1 behave as 1. Restricted campaign discovery may select up to 20 live channels per campaign instead. |
| `category_sort` | `CategorySort.VIEWERS_DESC` | Determines how normal category search results are selected and ordered. Enum values and equivalent strings are accepted. |
| `category_campaign_order` | `CategoryCampaignOrder.ORDER` | Preserves configured category order or prioritizes the nearest viable campaign deadline. |
| `category_chat` | `None` | Chat policy for category-discovered streamers. `None` inherits the default streamer setting. |
| `category_log_level` | `logging.INFO` | Severity used for category discovery and refresh messages. |
| `category_refresh_interval_hours` | `6` | Hours between campaign/channel refreshes. Positive values have a 30-minute minimum; `0` disables refresh. |
| `track_category_streamer_points` | `False` | Include point earn/spend events from category-only streamers in balance tracking and analytics. Explicit streamers are always tracked. |
| `drop_item_art` | `False` | Store and display Drop item artwork URLs in Drops analytics. |
| `print_open_drop_campaigns_on_load` | `False` | Log all open Drop campaigns during startup. Useful for checking category names and campaign dates. |
| `scrape_drop_progress_on_load` | `False` | Read inventory Drop progress immediately at startup. |
| `log_drop_checks` | `False` | Enable verbose Drop GraphQL/inventory diagnostics. Leave disabled unless troubleshooting. |

##### Category input formats

The preferred input is the lowercase slug from a Twitch directory URL:

```python
categories=[
    "pokemon-go",  # Preferred Twitch slug
    "Pokémon GO",  # Twitch display name
    "https://www.twitch.tv/directory/category/pokemon-go?filter=drops",
    "https://twitchdrops.app/game/pokemon-go",
]
```

Category matching is case- and accent-insensitive, so `Pokémon GO`, `Pokemon GO`,
and `pokemon-go` resolve to the same category. A Twitch URL is useful when the
display name is ambiguous. A twitchdrops.app game URL is useful when its slug
differs from Twitch's slug.

To require one particular live streamer, append its login after a pipe (`|`):

```python
categories=[
    "Call of Duty: Warzone|streamer-name",
    "[Pokémon GO]|[@another-streamer]",
]
```

The optional brackets and leading `@` are removed. The pipe is the separator so
punctuation in names such as `Call of Duty: Warzone` remains safe. The forced
streamer is accepted only when they are live in the requested category and, when
`category_drops_enabled=True`, have the `DropsEnabled` tag. A forced selector is
not supported on a URL; use the category name or slug form.

##### Channel selection and sorting

`category_sort` accepts a `CategorySort` member or its string value:

| Value | Selection behavior |
|---|---|
| `ORDER` | Keep Twitch's API order. |
| `VIEWERS_DESC` | Highest viewer count first. This is the default. |
| `VIEWERS_ASC` | Lowest viewer count first. |
| `STARTED_AT_DESC` | Most recently started streams first. |
| `STARTED_AT_ASC` | Longest-running streams first. |
| `RANDOM` | Shuffle the eligible candidates. |

For `ORDER` and `VIEWERS_DESC`, the miner requests enough eligible results to
fill `category_limit`. Other sorts inspect a wider candidate window—up to three
times the limit, capped at 300—before selecting the final results. An invalid
sort value falls back to `VIEWERS_DESC`.

Twitch restricts effective watching capacity; adding many discovered channels
does not mean all of them accrue progress simultaneously. Use a modest
`category_limit`, campaign ordering, and the project's normal priority settings
to keep the active set useful. See [Limits](#limits).

##### Campaign filtering and ordering

With `category_drops_enabled=True`, the miner requires a Drops-enabled live
channel and filters categories using eligible Drop campaign information. Set it
to `False` only when you intentionally want live channels from a category even
without the Drops tag.

`CategoryCampaignOrder.ORDER` preserves the order in `categories`.
`CategoryCampaignOrder.EXPIRATION` prioritizes categories whose viable campaigns
expire soonest, helping time-sensitive Drops get selected first.

If Twitch's campaign sources do not list any campaign for a configured game, the
miner uses that game's `twitchdrops.app/game/<category>` page as a third-priority
fallback. Restricted campaign channel lists are checked in full for live users
while the campaign is active. Shared logins eligible for multiple active
campaigns are checked first. The miner selects up to 20 live channels per
restricted campaign and checks remaining channel lists in standby batches only
when a campaign has not reached that target.

##### Refresh behavior

The miner periodically rechecks configured categories for new campaigns and live
replacement streamers. `category_refresh_interval_hours` defaults to 6 hours,
has a minimum of 30 minutes for any positive value, and adds a random delay of
20 seconds to 5 minutes. During active restricted-campaign discovery, the miner
may temporarily refresh as often as every five minutes to work through standby
channel batches. Set the value to `0` to disable all periodic category checks.

Refreshes add newly discovered streamers but do not remove channels that were
loaded earlier. Restart the miner to fully discard stale or removed category
streamers. In Docker/config mode, changing `MINE_CONFIG["categories"]` triggers
discovery after the configuration watcher detects the edit; other category
options require a restart. See [Configuration reload limitations](#configuration-reload-limitations).

##### Category logging and analytics

`category_log_level` controls category discovery and refresh records separately
from unrelated logs. For example, `logging.DEBUG` lets category diagnostics pass
an `INFO` console threshold without turning on global debug output. Use
`log_drop_checks=True` only for deeper Drop API/inventory debugging because it
can produce substantially more output.

By default, point balance events from category-only streamers are ignored to
avoid noisy or misleading analytics across transient channels. Set
`track_category_streamer_points=True` to include them. A streamer that is also in
the explicit `streamers` list is always tracked. `drop_item_art=True` enriches the
Drops analytics table with item artwork, while `scrape_drop_progress_on_load=True`
populates current inventory progress at startup.

##### Category examples

Prioritize the closest-expiring configured Drop campaigns, preferring
lower-viewer channels and never joining their chats:

```python
MINE_CONFIG = {
    "categories": ["warframe", "diablo-iv", "the-elder-scrolls-online"],
    "category_limit": 3,
    "category_sort": CategorySort.VIEWERS_ASC,
    "category_campaign_order": CategoryCampaignOrder.EXPIRATION,
    "category_chat": ChatPresence.NEVER,
    "category_drops_enabled": True,
}
```

Mix permanent streamers, followers, and category discovery:

```python
STREAMERS = ["favorite-streamer"]
MINE_CONFIG = {
    "followers": True,
    "blacklist": ["blocked-streamer"],
    "categories": ["arc-raiders", "warframe|preferred-streamer"],
    "category_limit": 5,
    "track_category_streamer_points": True,
}
```

##### Category troubleshooting

| Symptom | What to check |
|---|---|
| No category streamers are loaded | Confirm the game has an active, incomplete campaign and try its Twitch directory URL. Enable `print_open_drop_campaigns_on_load=True` to inspect detected campaigns. |
| A live channel is skipped | With `category_drops_enabled=True`, it must have the `DropsEnabled` tag. Restricted campaigns may also require the channel to be on the campaign's allowlist. |
| A forced streamer is skipped | Confirm the login is live, streaming the requested game, and has the Drops tag when required. |
| The chosen channels are unexpected | Check `category_sort`, `category_limit`, category order, and `CategoryCampaignOrder`. Restricted campaign allowlists take precedence over a normal category search. |
| A removed category still affects selection | Refresh only adds streamers; restart the miner to remove already-loaded category channels. |
| Category messages are missing | Set `category_log_level=logging.INFO` or `logging.DEBUG` and ensure the logger/console threshold permits that level. |
| Analytics omit channel point changes | Set `track_category_streamer_points=True`; it defaults to `False` for category-only channels. |

#### ANALYTICS_CONFIG

Set `ANALYTICS_CONFIG` to `None` when the web server is not needed. Otherwise,
provide the keyword arguments for `analytics(...)`, such as `host`, `port`,
`refresh`, `days_ago`, `password`, and `log_poll_interval`. Analytics storage
also requires `MINER_CONFIG["enable_analytics"] = True`. See [Analytics](#analytics)
for configuration and security guidance.

### Local installation

Clone the repository, create an isolated environment, install dependencies, and
copy the configuration template. Python 3.11 through 3.13 is supported.

```sh
git clone https://github.com/rdavydov/Twitch-Channel-Points-Miner-v2
cd Twitch-Channel-Points-Miner-v2
python3 -m venv venv
source venv/bin/activate  # Windows PowerShell: .\venv\Scripts\Activate.ps1
pip install -r requirements.txt
mkdir -p config
cp config.example.py config/config.py
```

Edit `config/config.py`, then continue to [Starting the miner](#starting-the-miner).

### Starting the miner

When running a clone, point the stable runner at the local configuration
directory:

```sh
python -m TwitchChannelPointsMiner.runner --config-dir ./config
```

Docker images already use this runner and default to `/usr/src/app/config`, so no
command override is needed. On first authentication the process may require an
interactive terminal. Cookies, logs, and analytics data are stored separately
from the configuration and should be persisted as described in [Docker](#docker).

The runner checks `config/config.py` every five seconds. Set
`TCPM_CONFIG_RELOAD_SECONDS` to change the interval; the minimum is one second.

#### Configuration reload limitations

- New entries in `STREAMERS` are applied without a restart. Removing a streamer
  does not remove its loaded state, chat connection, or PubSub subscriptions;
  restart the miner to remove it completely.
- Changes to `MINE_CONFIG["categories"]` trigger discovery after the watcher
  detects the edit. Previously discovered channels remain loaded until restart.
- Changes to existing streamer settings, `MINER_CONFIG`, `ANALYTICS_CONFIG`, and
  non-category `MINE_CONFIG` options require a restart.
- Invalid configuration is logged and ignored. The watcher retries after the
  file changes again.

### Docker

#### Docker Hub
Official Docker images are on https://hub.docker.com/r/rdavidoff/twitch-channel-points-miner-v2 for `linux/amd64`, `linux/arm64` and `linux/arm/v7`.

The image reads `/usr/src/app/config/config.py`. Create a host `config` directory,
copy [config.example.py](config.example.py) to `config/config.py`, customize it,
and mount the directory. The container entrypoint starts the stable runner.

Persist these directories on the host:

- `analytics`: analytics history and Drop data
- `cookies`: saved Twitch authentication
- `logs`: miner log files
- `config`: the user-owned configuration

**Example using docker-compose:**

```yml
version: "3.9"

services:
  miner:
    image: rdavidoff/twitch-channel-points-miner-v2
    stdin_open: true
    tty: true
    environment:
      - TERM=xterm-256color
    volumes:
      - ./analytics:/usr/src/app/analytics
      - ./cookies:/usr/src/app/cookies
      - ./logs:/usr/src/app/logs
      - ./config:/usr/src/app/config
    ports:
      - "5000:5000"
```

**Example with docker run:**
```sh
docker run \
    -v $(pwd)/analytics:/usr/src/app/analytics \
    -v $(pwd)/cookies:/usr/src/app/cookies \
    -v $(pwd)/logs:/usr/src/app/logs \
    -v $(pwd)/config:/usr/src/app/config \
    -p 5000:5000 \
    rdavidoff/twitch-channel-points-miner-v2
```

`$(pwd)` does not work in Windows Command Prompt. Use absolute host paths there,
such as `/path/to/cookies:/usr/src/app/cookies`.

On Windows, use absolute host paths. For example, mount
`C:\Absolute\Path\config` at `/usr/src/app/config`.

For a non-default container path, set `TCPM_CONFIG_DIR` and mount the host
directory at the same location. Users upgrading from the legacy runner should
follow [Migrating from run.py](#migrating-from-runpy) once; new installations do
not need a legacy file.

Directories that are not mounted are created inside the container and their data
is lost when the container is removed. On first authentication, run the container
interactively with `-it` so you can complete the Twitch login.

For multiple accounts, give each container separate config, cookie, log, and
analytics directories, and use a different host port for each analytics server:

```sh
docker run --name user1 -it \
    -v $(pwd)/user1/config:/usr/src/app/config \
    -v $(pwd)/user1/cookies:/usr/src/app/cookies \
    -v $(pwd)/user1/logs:/usr/src/app/logs \
    -v $(pwd)/user1/analytics:/usr/src/app/analytics \
    -p 5001:5000 \
    rdavidoff/twitch-channel-points-miner-v2
```

```sh
docker run --name user2 -it \
    -v $(pwd)/user2/config:/usr/src/app/config \
    -v $(pwd)/user2/cookies:/usr/src/app/cookies \
    -v $(pwd)/user2/logs:/usr/src/app/logs \
    -v $(pwd)/user2/analytics:/usr/src/app/analytics \
    -p 5002:5000 \
    rdavidoff/twitch-channel-points-miner-v2
```

#### Portainer

[Link](https://github.com/rdavydov/Twitch-Channel-Points-Miner-v2/wiki/Deploy-Docker-container-in-Portainer) to the illustrated guide on how to deploy a Docker container in Portainer.

### Replit

Official Repl: https://replit.com/@rdavydov/Twitch-Channel-Points-Miner-v2

Provided "as is" with no support. Testing purposes only. Updates may be delayed.

### Limits
_**Twitch has a limit - you can't watch more than two channels at one time. We take the first two streamers from the list as they have the highest priority.**_

Make sure to write the streamers array in order of priority from left to right. If you use `followers=True` you can choose to download the followers sorted by follow date (ASC or DESC).

## Settings
Most settings are documented inline in [config.example.py](config.example.py).
The `priority` option controls which eligible streamers receive the available
watch slots. It accepts one `Priority` value or an ordered list. Include at least
one fallback such as `ORDER`, `POINTS_ASCENDING`, or `POINTS_DESCENDING`; using
only `STREAK`, for example, leaves no selection rule after available streaks have
been handled.

Available values:
 - `STREAK` - Catch the watch streak from all streamers
 - `DROPS` - Claim all drops from streamers with drops tags enabled
 - `SUBSCRIBED` - Prioritize streamers you're subscribed to (higher subscription tiers are mined first)
 - `ORDER` - Following the order of the list
 - `POINTS_ASCENDING` - On top the streamers with the lowest points
 - `POINTS_DESCENDING` - On top the streamers with the highest points

Priorities can be combined in order. Avoid contradictory fallback rules such as
`ORDER` and `POINTS_ASCENDING` in the same list.

### LoggerSettings

| Key | Type | Default | Description |
|---|---|---|---|
| `save` | bool | `True` | Save logs to rotating files. |
| `less` | bool | `False` | Use compact log formatting and shorter messages. |
| `console_level` | logging level | `logging.INFO` | Minimum level written to the console. |
| `console_username` | bool | `False` | Include the account username in console and notification messages. |
| `time_zone` | str or None | `None` | Log time zone, such as `America/Denver`. |
| `file_level` | logging level | `logging.DEBUG` | Minimum level written to log files. |
| `emoji` | bool | Platform-dependent | Enable emoji; defaults to disabled on Windows and enabled elsewhere. |
| `colored` | bool | `False` | Enable colored console output. |
| `auto_clear` | bool | `True` | Rotate logs daily and retain seven backups. |
| `color_palette` | ColorPalette | Default palette | Customize colors by event. |
| `telegram` | Telegram or None | `None` | Send selected events through Telegram. |
| `discord` | Discord or None | `None` | Send selected events through Discord. |
| `webhook` | Webhook or None | `None` | Send selected events to a generic HTTP endpoint. |
| `matrix` | Matrix or None | `None` | Send selected events to a Matrix room. |
| `pushover` | Pushover or None | `None` | Send selected events through Pushover. |
| `gotify` | Gotify or None | `None` | Send selected events to a Gotify server. |

#### Color Palette
`ColorPalette` customizes console colors by event name. Unspecified events use
`Fore.RESET`, while prediction wins and losses default to green and red. Values
may be Colorama constants or case-insensitive color names: `BLACK`, `RED`,
`GREEN`, `YELLOW`, `BLUE`, `MAGENTA`, `CYAN`, `WHITE`, or `RESET`.
```python
from colorama import Fore
ColorPalette(
    STREAMER_ONLINE = Fore.GREEN,
    STREAMER_OFFLINE = Fore.RED,
    GAIN_FOR_RAID = Fore.YELLOW,
    GAIN_FOR_CLAIM = Fore.YELLOW,
    GAIN_FOR_WATCH = Fore.YELLOW,
    GAIN_FOR_WATCH_STREAK = Fore.YELLOW,
    BET_WIN = Fore.GREEN,
    BET_LOSE = Fore.RED,
    BET_REFUND = Fore.RESET,
    BET_FILTERS = Fore.MAGENTA,
    BET_GENERAL = Fore.BLUE,
    BET_FAILED = Fore.RED,
)
```

#### Telegram
To receive selected log events through Telegram, configure a `Telegram`
instance; otherwise omit the option or set it to `None`.
1. Create a bot with [@BotFather](https://t.me/botfather)
2. Get your `chat_id` with [@getmyid_bot](https://t.me/getmyid_bot)

| Key | Type | Default | Description |
|---|---|---|---|
| `chat_id` | int | Required | Numeric Telegram chat ID. |
| `token` | str | Required | Bot token issued by BotFather. |
| `events` | list | Required | Events to send. |
| `disable_notification` | bool | `False` | Send without sound or vibration when enabled. |

```python
Telegram(
    chat_id=123456789,  # Replace with your numeric chat ID.
    token="YOUR_BOT_TOKEN",
    events=[Events.STREAMER_ONLINE, Events.STREAMER_OFFLINE,
                    Events.BET_LOSE, Events.CHAT_MENTION],
    disable_notification=True,
)
```

#### Discord
To receive selected log events through Discord, configure a `Discord` instance;
otherwise omit the option or set it to `None`.
1. Go to the Server you want to receive updates
2. Click "Edit Channel"
3. Click "Integrations"
4. Click "Webhooks"
5. Click "New Webhook"
6. Name it if you want
7. Click on "Copy Webhook URL"


| Key | Type | Default | Description |
|---|---|---|---|
| `webhook_api` | str | Required | Discord webhook URL. |
| `events` | list | Required | Events to send. |

```python
Discord(
   webhook_api="https://discord.com/api/webhooks/YOUR_WEBHOOK_ID/YOUR_WEBHOOK_TOKEN",
   events=[Events.STREAMER_ONLINE, Events.STREAMER_OFFLINE,
                    Events.BET_LOSE, Events.CHAT_MENTION],
)
```

#### Generic Webhook
Use `Webhook` to send selected events to an HTTP endpoint.

| Key | Type | Default | Description |
|---|---|---|---|
| `endpoint` | str | Required | Destination URL. |
| `method` | str | Required | `POST` or `GET`. |
| `events` | list | Required | Events to send. |

```python
Webhook(
   endpoint="https://example.com/webhook",
   method="GET",
   events=[Events.STREAMER_ONLINE, Events.STREAMER_OFFLINE,
                    Events.BET_LOSE, Events.CHAT_MENTION],
)
```


#### Events
 - `STREAMER_ONLINE`
 - `STREAMER_OFFLINE`
 - `GAIN_FOR_RAID`
 - `GAIN_FOR_CLAIM`
 - `GAIN_FOR_WATCH`
 - `GAIN_FOR_WATCH_STREAK`
 - `BET_WIN`
 - `BET_LOSE`
 - `BET_REFUND`
 - `BET_FILTERS`
 - `BET_GENERAL`
 - `BET_FAILED`
 - `BET_START`
 - `BONUS_CLAIM`
 - `MOMENT_CLAIM`
 - `JOIN_RAID`
 - `DROP_CLAIM`
 - `DROP_STATUS`
 - `CHAT_MENTION`
 - `CONFIGURATION`

### StreamerSettings

| Key | Type | Default | Description |
|---|---|---|---|
| `make_predictions` | bool | `True` | Participate in channel-point predictions. |
| `follow_raid` | bool | `True` | Follow eligible raids. |
| `claim_drops` | bool | `True` | Accumulate Drop watch progress and claim available Drops. |
| `claim_moments` | bool | `True` | Claim available [Moments](https://help.twitch.tv/s/article/moments). |
| `watch_streak` | bool | `True` | Prioritize available watch-streak rewards. |
| `community_goals` | bool | `False` | Contribute the maximum allowed points to community goals. |
| `bet` | BetSettings | Default settings | Configure prediction strategy, limits, filters, and timing. |
| `chat` | ChatPresence | `ONLINE` | Control when the miner joins IRC chat. |

Allowed values for `chat` are:
- `ALWAYS` Join in IRC chat and never leave
- `NEVER` Never join IRC chat
- `ONLINE` Participate in IRC chat while the streamer is online (leave when offline)
- `OFFLINE` Participate in IRC chat while the streamer is offline (leave when online)

### BetSettings

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

#### Bet strategy

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
#### FilterCondition

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

##### Example

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

#### DelayMode

- **FROM_START**: Will wait `delay` seconds from when the bet was opened
- **FROM_END**: Wait until `delay` seconds remain in the prediction window
- **PERCENTAGE**: Will place the bet when `delay` percent of the set timer is elapsed

Here's a concrete example. Let's suppose we have a bet that is opened with a timer of 10 minutes:

- **FROM_START** with `delay=20`: The bet will be placed 20s after the bet is opened
- **FROM_END** with `delay=20`: The bet will be placed 20s before the end of the bet (so 9mins 40s after the bet is opened)
- **PERCENTAGE** with `delay=0.2`: The bet will be placed when the timer went down by 20% (so 2mins after the bet is opened)

## Analytics

The optional analytics server provides a browser-based view of channel-point
history, Drops, and miner logs. The points chart includes tooltips with the
balance, timestamp, and gain or spend reason. Annotations highlight predictions,
watch streaks, and other significant changes, and can be disabled in the page.
The interface also includes light and dark themes.

| Light theme | Dark theme |
| ----------- | ---------- |
| ![Light theme](https://raw.githubusercontent.com/Tkd-Alex/Twitch-Channel-Points-Miner-v2/master/assets/chart-analytics-light.png) | ![Dark theme](https://raw.githubusercontent.com/Tkd-Alex/Twitch-Channel-Points-Miner-v2/master/assets/chart-analytics-dark.png) |

Enable storage with `MINER_CONFIG["enable_analytics"] = True` and configure the
server through `ANALYTICS_CONFIG`. The chart refreshes every `refresh` minutes.
The log viewer polls every `log_poll_interval` seconds (default `5`; accepted
range `1` to `180`), and `days_ago` controls the chart's initial time range.

For access from another machine, bind to `0.0.0.0` and provide a strong
`password`; remote binds without a password are rejected. Sign in with the
configured Twitch username and analytics password. The built-in server uses
plain HTTP, so expose it only on a trusted network or behind an HTTPS reverse
proxy. Background and design context are available in [issue #96](https://github.com/Tkd-Alex/Twitch-Channel-Points-Miner-v2/issues/96).
```python
MINER_CONFIG = {
    "username": "your-twitch-username",
    "enable_analytics": True,
}

ANALYTICS_CONFIG = {
    "host": "127.0.0.1",
    "port": 5000,
    "refresh": 5,
    "days_ago": 7,
    "log_poll_interval": 5,
}
```

### Analytics security and HTTPS reverse proxy

The analytics server contains account activity and miner logs. Its built-in authentication uses HTTP Basic authentication, which does not encrypt credentials or response data. Binding it to `0.0.0.0` exposes it to every reachable network interface; use a strong, unique analytics password and do not expose port 5000 directly to the internet.

For remote access, keep the miner bound to loopback and terminate HTTPS in a reverse proxy. A minimal nginx location looks like this:

```nginx
server {
    listen 443 ssl;
    server_name analytics.example.com;

    ssl_certificate /etc/letsencrypt/live/analytics.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/analytics.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }
}
```

Set `ANALYTICS_CONFIG={"host": "127.0.0.1", "password": "a-strong-password"}`
behind that proxy. Restrict the proxy further with a firewall, VPN, or IP
allowlist where possible. nginx and certificate provisioning must be configured
for your operating system and domain; the example only shows the relevant proxy
boundary.

### Enabling analytics storage

Disabling analytics reduces memory and disk use because the miner does not create
or update analytics JSON files.

Set `MINER_CONFIG["enable_analytics"]` to `True` when using the analytics server.
Otherwise leave it `False` (the default).

## Logs

Logging is controlled by `LoggerSettings` in `MINER_CONFIG`. The default full
format includes timestamps, levels, function names, streamer state, point gains,
Drop activity, and prediction decisions. Set `less=True` for shorter console
messages while retaining the important event details.

### Full format

```text
14/07/26 12:00:00 - INFO - [run]: 💣 Start session: 'session-id'
14/07/26 12:00:02 - INFO - [set_online]: 🥳 Streamer(username=streamer-name, channel_id=0000000, channel_points=12000) is Online!
14/07/26 12:05:00 - INFO - [on_message]: 🚀 +10 → Streamer(username=streamer-name, channel_id=0000000, channel_points=12010) - Reason: WATCH.
14/07/26 12:06:00 - INFO - [claim_bonus]: 🎁 Claiming the bonus for Streamer(username=streamer-name, channel_id=0000000, channel_points=12010)!
```

### Compact format

```text
14/07 12:00:00 - 💣 Start session: 'session-id'
14/07 12:00:02 - 🥳 streamer-name (12k points) is Online!
14/07 12:05:00 - 🚀 +10 → streamer-name (12k points) - Reason: WATCH.
14/07 12:06:00 - 🎁 Claiming the bonus for streamer-name (12k points)!
```

### Graceful-shutdown report

During a controlled shutdown—such as `Ctrl+C` or Docker's normal stop signal—the
miner summarizes session duration, the log path, prediction results, and points
gained or spent per streamer and event type. A hard kill, host crash, or power
loss cannot produce this report.

```text
14/07/26 18:00:00 - 🛑 Ending session: 'session-id'
14/07/26 18:00:00 - 📄 Logs file: /path/to/logs/username.timestamp.log
14/07/26 18:00:00 - ⌛ Duration 6:00:00
14/07/26 18:00:00 - 🤖 streamer-name (15k points), Total points gained: 3k
14/07/26 18:00:00 - 💰 CLAIM(4 times, 200 gained), WATCH(24 times, 240 gained)
```

## Migrating from run.py

Existing Docker users can let the image convert a conventional legacy runner on
first startup. The conversion preserves imports and expressions while splitting
the old calls into the four current configuration values.

### Automatic Docker migration

1. Back up the existing `run.py` and container configuration.
2. Create an empty, persistent host directory named `config`.
3. During the first upgraded start, mount both the directory and the old runner:

   ```yaml
   volumes:
     - ./config:/usr/src/app/config
     - ./run.py:/usr/src/app/run.py
   ```

4. Recreate the container. When `/usr/src/app/config/config.py` is absent, the
   entrypoint converts `/usr/src/app/run.py`, validates the result, and starts
   with the new configuration.
5. Check the startup output, inspect `config/config.py`, and confirm the account,
   streamers, mining options, notifications, and analytics server behave as
   expected.
6. Remove the `run.py` mount and recreate the container again. Keep only the
   persistent config directory for future upgrades.

After a successful conversion, the converter renames the legacy runner to
`run.py.bak` so it cannot be mistaken for the active configuration. It refuses
to overwrite either an existing `config/config.py` or `run.py.bak`. The new
configuration has owner-only permissions, and `config/.converted-from-run-py`
records the source path and SHA-256 hash.

### What converts automatically

The legacy file must contain exactly one `TwitchChannelPointsMiner(...)` call,
exactly one `.mine(...)` call, and no more than one `.analytics(...)` call. The
converter maps them as follows:

| Legacy expression | New configuration value |
|---|---|
| Arguments to `TwitchChannelPointsMiner(...)` | `MINER_CONFIG` |
| First positional argument to `.mine(...)` | `STREAMERS` |
| Keyword arguments to `.mine(...)` | `MINE_CONFIG` |
| Arguments to `.analytics(...)` | `ANALYTICS_CONFIG`, or `None` when absent |

Imports needed by enums and settings objects are retained. Expanded `**kwargs`,
extra positional arguments, multiple miner/mine calls, or syntax errors cannot
be converted safely.

If conversion fails, any incomplete output is removed, a migration warning is
printed, and the original runner executes unchanged. Correct the legacy file and
remove any partial config before retrying, or create `config/config.py` manually
from [config.example.py](config.example.py). Set
`TCPM_DISABLE_AUTO_CONVERSION=1` only when you intentionally need to postpone
conversion; the container will continue using the mounted legacy runner.

To convert without starting the miner, run the image's runner with
`--convert-only` while mounting both paths. This writes the converted config but
does not launch mining. Conversion still refuses to replace an existing config.

```sh
docker run --rm \
    -v $(pwd)/config:/usr/src/app/config \
    -v $(pwd)/run.py:/usr/src/app/run.py \
    rdavidoff/twitch-channel-points-miner-v2 --convert-only
```

## Legacy cookie migration (optional)

This section applies only if you are upgrading from an older version or the
original miner and already have a working `twitch-cookies.pkl`. New users can
skip it—the miner creates the account cookie file after normal authentication.

The current account file is `cookies/<twitch-username>.pkl`, where the filename
must match `MINER_CONFIG["username"]`. Despite the historical `.pkl` extension,
new files contain validated JSON rather than Python pickle data.

To reuse an old cookie:

1. Stop the miner.
2. Back up the old cookie file. Treat it like a password: it contains an
   authentication token and must not be committed, shared, or included in logs.
3. Create the persistent `cookies` directory if it does not exist.
4. Copy and rename the old file to match the configured Twitch login:

   ```sh
   mkdir -p cookies
   cp /path/to/twitch-cookies.pkl cookies/your-twitch-username.pkl
   ```

5. Start the miner normally. If the file contains the supported legacy
   data-only cookie list, the miner loads it with a restricted parser, validates
   its structure, rewrites it as JSON, and limits its permissions to the owner.

For Docker, the host directory must be mounted at `/usr/src/app/cookies` as shown
in [Docker](#docker). The same per-account filename rule applies inside the
mounted directory. Use a separate cookie and config directory for each account.

The legacy cookie may no longer be accepted by Twitch. If the miner reports an
invalid or unsafe file, remove it and authenticate again. If Twitch rejects a
previously valid saved token, the miner clears the cached login and restarts so
that reauthentication can occur; follow the instructions printed in the logs.

## Windows
Windows users who encounter terminal rendering problems can disable emoji with
`LoggerSettings(emoji=False)` inside `MINER_CONFIG["logger_settings"]`.

Other useful info can be found here:
- https://github.com/gottagofaster236/Twitch-Channel-Points-Miner/issues/31
- https://github.com/Tkd-Alex/Twitch-Channel-Points-Miner-v2/issues/55

You can also follow this [video tutorial](https://www.youtube.com/watch?v=0VkM7NOZkuA).

## Termux
**1. Upgrade packages**
```
pkg upgrade
```

**2. Install packages to Termux**
```
pkg install python git libcrypt ndk-sysroot clang zlib binutils tur-repo
LDFLAGS="-L${PREFIX}/lib/" CFLAGS="-I${PREFIX}/include/" pip install --upgrade wheel
```
Note: `pkg install tur-repo` will basically enable the [user repository](https://github.com/termux-user-repository/tur) _(Very similar to Arch AUR)_ and `python-pandas` pre-compiled package comes exactly from this repository.

**3. Install pandas**
```
pkg install python-pandas
```

**4. Clone this repository**

`git clone https://github.com/rdavydov/Twitch-Channel-Points-Miner-v2`

**5. Go to the miner's directory**

`cd Twitch-Channel-Points-Miner-v2`

**6. Create and edit the configuration**

```sh
mkdir -p config
cp config.example.py config/config.py
nano config/config.py
```

**7. Install packages**
```
pip install -r requirements.txt
pip install Twitch-Channel-Points-Miner-v2
```

**8. Run the miner!**

`python -m TwitchChannelPointsMiner.runner --config-dir ./config`

Read more at [#92](https://github.com/Tkd-Alex/Twitch-Channel-Points-Miner-v2/issues/92) [#76](https://github.com/Tkd-Alex/Twitch-Channel-Points-Miner-v2/issues/76)

**Note**

⚠️ Installation of `pandas` and its build dependencies can take a long time.

## Disclaimer
This project comes with no guarantee or warranty. You are responsible for whatever happens from using this project. It is possible to get soft or hard banned by using this project if you are not careful. This is a personal project and is in no way affiliated with Twitch.
