# Category-based Drops

[Back to the README](../README.md)

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

## How category discovery works

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

## Category configuration reference

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
| `drop_progress_stall_minutes` | `10` | Rotate to another eligible category channel when authenticated Drop progress remains unchanged for this many minutes. The minimum is `5`; `0` disables stall detection. |
| `drop_pick_stickiness_minutes` | `15` | Keep watching the currently picked drop channel unless another eligible campaign's deadline is at least this many minutes closer, the channel goes offline, its campaign finishes/ends, or the in-progress drop can no longer complete before its campaign deadline. While the in-progress drop can still finish in time, the pick is held even past the margin. `0` disables stickiness (always switch to the soonest-expiring campaign). |
| `watch_status_interval_minutes` | `15` | The *Watching for points* status line is logged only when the watched streams, their watch reasons, or their Drop campaigns change. An unchanged line is repeated after this many minutes as a sign of life; `0` logs it only on change. Repeats in between are logged at `DEBUG`, so a file log at `file_level=logging.DEBUG` keeps the full timeline. |
| `category_drops_override_streamers` | `False` | Twitch counts Drops progress on only one channel at a time, so the miner watches a single Drops stream (category, wildcard, badge, or a configured/followed streamer with an active Drop), chosen by `streamer_source_priority`, and fills the other slot with a points-only stream. When `True`, a configured or followed streamer's Drops for a game that is not in `categories` rank below every discovered Drops stream, even if `STREAMERS`/`FOLLOWERS` rank higher. |
| `drop_badge_catalog` | `True` | On startup, classify rewards from the shared Drops gist against the shared Twitch badge catalog and persist the result in the config directory. |
| `drop_badge_refresh_interval_hours` | `1` | Hours between checks for new campaigns. Positive values have a one-hour minimum; `0` keeps the startup check but disables periodic checks. |
| `auto_mine_badge_drops` | `False` | Automatically add live channels for active, unearned watch-time badge campaigns. |
| `badge_drop_streamer_limit` | `1` | Live candidates added for each eligible all-channel badge campaign. Accepts `1` or `2`; restricted campaigns use their listed channels. |
| `track_category_streamer_points` | `False` | Include point earn/spend events from category-only streamers in balance tracking and analytics. Explicit streamers are always tracked. |
| `drop_item_art` | `False` | Store and display Drop item artwork URLs in Drops analytics. |
| `print_open_drop_campaigns_on_load` | `False` | Log all open Drop campaigns during startup. Useful for checking category names and campaign dates. |
| `scrape_drop_progress_on_load` | `False` | Read inventory Drop progress immediately at startup. Required for accurate per-session Drop progress in the graceful-shutdown report. |
| `log_drop_checks` | `False` | Enable verbose Drop GraphQL/inventory diagnostics. Leave disabled unless troubleshooting. |

## Category input formats

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

## Channel selection and sorting

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
to keep the active set useful. See [Limits](../README.md#limits).

## Campaign filtering and ordering

With `category_drops_enabled=True`, the miner requires a Drops-enabled live
channel and filters categories using eligible Drop campaign information. Set it
to `False` only when you intentionally want live channels from a category even
without the Drops tag.

Campaigns that Twitch reports as completed are excluded from active category
discovery. For badge rewards, the miner also checks the authenticated account's
full available badge inventory. This prevents an already-earned badge campaign
from keeping a category active when the normal Drops inventory omits that
reward.

`CategoryCampaignOrder.ORDER` preserves the order in `categories`.
`CategoryCampaignOrder.EXPIRATION` prioritizes categories whose viable campaigns
expire soonest, helping time-sensitive Drops get selected first. With `ORDER`
selected, the web config dashboard's "Configured categories" list can be
reordered by dragging rows, same as `STREAMERS`.

If Twitch's campaign sources do not list any campaign for a configured game, the
miner uses the shared `twitch-drops.json` gist as a third-priority fallback.
Restricted campaign channel lists are checked in full for live users
while the campaign is active. Shared logins eligible for multiple active
campaigns are checked first. The miner selects up to 20 live channels per
restricted campaign and checks remaining channel lists in standby batches only
when a campaign has not reached that target.

The miner signs in as Twitch's TV client. Twitch does not return the Drops
dashboard or full campaign details to that client, so the miner reads campaigns
from your inventory, the campaigns that live channels list, and the shared
gist. When Twitch withholds every campaign's details, the miner logs one
warning per run.

## Refresh behavior

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
options require a restart. See [Configuration reload limitations](configuration.md#configuration-reload-limitations).

The Drop badge catalog runs independently of category discovery. Its startup
check begins in a background thread after normal miner setup completes, and it
checks hourly thereafter by default. It reads normalized campaign and badge data
from the `twitch-drops.json` and `twitch-badges.json` gists. The game signature
is compared with the saved state, so only new or changed campaign data is
processed. Full Twitch badge records, game snapshots,
classifications, and historical campaign records are stored in
`drop_badge_catalog.json` beside `config.py`. Rewards confirmed by Twitch are
marked `BADGE`; unmatched rewards remain `UNKNOWN` rather than being assumed not
to be badges.

Both gists are published by
[Twitch-Channel-Points-Miner-v3-Scraper](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3-Scraper).
To use your own scraper instance or a mirror, set `TCPM_DROPS_GIST_URL` and
`TCPM_BADGES_GIST_URL` to the raw JSON URLs in your shell or Docker environment.
The defaults remain the shared gists. Leading and trailing spaces are trimmed;
empty values use the defaults, and invalid or non-HTTP(S) URLs are ignored with
a warning. Changing the badge URL refreshes the cached badge catalog on its next
check, even if the old cache has not expired.

Set `auto_mine_badge_drops=True` to use that catalog to add eligible live
channels automatically. The feature only considers active watch-time campaigns,
skips badges already available to the authenticated account, honors `blacklist`,
and uses `category_chat` and `category_sort`. Subscription-only rewards are not
added because watching cannot earn them. Automatically selected streamers always
have `claim_drops=True`. This feature is disabled by default.

## Wildcard category fallback

`wildcard_categories` is a separate opt-in fallback for when `categories`
leaves watch capacity idle this cycle -- either because `categories` is
empty, none of your configured categories currently have an active
incomplete campaign, or one does but produced zero live matching channels
right now (a real campaign with nobody streaming it isn't useful capacity
either). Once that happens, and only then, the miner discovers live channels
for *every other* category with an active incomplete Drop campaign -- filling
otherwise idle watch capacity without you having to enumerate every game
yourself. If a long `categories` list yields at least one live streamer this
cycle, wildcard stands down even if most of the list was campaign-free --
the trigger is "did the preferred pass produce anything to watch," not "was
every single configured category checked off."

Wildcard candidate discovery merges Twitch's account-scoped campaign data with
the bundled external Drops catalog because Twitch can omit public campaigns from
the account response. Twitch remains authoritative for every game it does
expose, so a completed or ineligible Twitch campaign is never resurrected by the
external catalog. Games found only in the external catalog are provisional: a
selected live channel must advertise the matching campaign through Twitch before
the miner permits it into the watch pool. The remaining candidates are ranked by
their earliest incomplete Drop deadline before the wildcard limit and pinning
rules are applied.

```python
MINE_CONFIG = {
    "categories": ["warframe", "diablo-iv"],
    "wildcard_categories": True,
    "wildcard_category_limit": 10,
    "wildcard_category_streamer_limit": 1,
    "wildcard_category_pin_active": True,
}
```

| Option | Default | Description |
|---|---:|---|
| `wildcard_categories` | `False` | Enable the fallback. Only activates once the preferred `categories` pass produces zero live streamers this cycle. |
| `wildcard_category_limit` | `10` | Maximum distinct wildcard categories added per refresh cycle. With `wildcard_category_pin_active=True`, an already-tracked wildcard category doesn't count against this cap, so the effective tracked count can exceed it -- that throttles new additions only, it does not cap the total. |
| `wildcard_category_streamer_limit` | `1` | Live channels pulled per wildcard category. Kept small by default since this is best-effort filler, not a curated list. |
| `wildcard_category_pin_active` | `True` | Keep an already-tracked wildcard category even if a re-sort by campaign expiration pushes it out of the top `wildcard_category_limit`, as long as its campaign is still active. Prevents pointlessly retiring and re-picking a streamer between refreshes. Set to `False` for a strict top-N-by-expiration set every cycle. |

This is its own `StreamerSource.WILDCARD_CATEGORIES` priority tier -- see
[MINER_CONFIG](configuration.md#miner_config) -- independent from `StreamerSource.CATEGORIES`.
It defaults to the lowest priority (after `BADGES`) precisely so that
wildcard-discovered streams never compete for a watch slot ahead of your
curated `categories` list or `auto_mine_badge_drops` the moment they're
found; they only ever fill slots those sources leave unused. Reorder
`streamer_source_priority` if you want it ranked higher.

Wildcard-discovered channels use the miner's default `StreamerSettings` (no
chat by default), honor `blacklist`, and use `category_chat`/`category_sort`
like normal category discovery. Twitch only accrues Drops progress on one of
the (up to two) watched streams regardless of source, so the miner watches at
most one discovered ("not explicitly configured") Drops stream per cycle in
total across `categories` and the wildcard fallback combined -- a preferred-
category pick wins that shared slot over a wildcard one whenever both qualify
in the same cycle, freeing the wildcard candidate's slot for an explicit or
followed streamer instead of wasting it on a second stream that can't accrue
progress anyway.

## Category logging and analytics

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

## Category examples

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

## Category troubleshooting

| Symptom | What to check |
|---|---|
| No category streamers are loaded | Confirm the game has an active, incomplete campaign and try its Twitch directory URL. Enable `print_open_drop_campaigns_on_load=True` to inspect detected campaigns. |
| A live channel is skipped | With `category_drops_enabled=True`, it must have the `DropsEnabled` tag. Restricted campaigns may also require the channel to be on the campaign's allowlist. |
| A forced streamer is skipped | Confirm the login is live, streaming the requested game, and has the Drops tag when required. |
| The chosen channels are unexpected | Check `category_sort`, `category_limit`, category order, and `CategoryCampaignOrder`. Restricted campaign allowlists take precedence over a normal category search. |
| A removed category still affects selection | Refresh only adds streamers; restart the miner to remove already-loaded category channels. |
| Category messages are missing | Set `category_log_level=logging.INFO` or `logging.DEBUG` and ensure the logger/console threshold permits that level. |
| Analytics omit channel point changes | Set `track_category_streamer_points=True`; it defaults to `False` for category-only channels. |
| Wildcard categories never activate | They only run once the preferred `categories` pass produces zero live streamers this cycle -- a long `categories` list with even one currently-live match will keep wildcard standing down. Confirm `wildcard_categories=True`, and check whether any configured category currently has a live matching channel, e.g. via `print_open_drop_campaigns_on_load=True` and `category_log_level=logging.DEBUG`. |
| A wildcard channel keeps getting swapped out | Set `wildcard_category_pin_active=True` (the default) so an already-tracked wildcard category isn't dropped just for falling out of the top `wildcard_category_limit` on a re-sort. |

A channel eligible for both `auto_mine_badge_drops` and
`wildcard_categories` at once is claimed by whichever source
`streamer_source()` checks first -- badges, then wildcard, then ordinary
category discovery -- independent of `streamer_source_priority`, which only
governs watch-slot arbitration once a streamer already has a source.
