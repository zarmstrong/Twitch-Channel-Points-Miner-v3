# Settings

[Back to the README](../README.md)

Most settings are documented inline in [config.example.py](../config.example.py).
The `priority` option controls which eligible streamers receive the available
watch slots. It accepts one `Priority` value or an ordered list. Include at least
one fallback such as `ORDER`, `POINTS_ASCENDING`, or `POINTS_DESCENDING`; using
only `STREAK`, for example, leaves no selection rule after available streaks have
been handled.

Available values:
 - `STREAK` - Catch the watch streak from all streamers
 - `FAVORITE` - Prioritize streamers with `StreamerSettings(favorite=True)`
 - `DROPS` - Claim all drops from streamers with drops tags enabled
 - `SUBSCRIBED` - Prioritize streamers you're subscribed to (higher subscription tiers are mined first)
 - `ORDER` - Following the order of the list
 - `POINTS_ASCENDING` - On top the streamers with the lowest points
 - `POINTS_DESCENDING` - On top the streamers with the highest points

Priorities can be combined in order. Avoid contradictory fallback rules such as
`ORDER` and `POINTS_ASCENDING` in the same list.

Set `points_limit` in `StreamerSettings` to stop watching a streamer once its
channel-points balance reaches that value. A pending watch streak can temporarily
bypass the limit so its reward is not missed. Set `favorite=True` per streamer
and include `Priority.FAVORITE` to reserve watch slots for favorites first.

## Android login

By default the miner logs in as Twitch's TV app. Twitch withholds some Drops
data from that app: the Drops dashboard and campaign details come back empty,
so the miner works from your inventory, channel listings and a shared campaign
list instead. Set `"android_login": True` in `MINER_CONFIG` (or tick **Log in as
the Android app** in the dashboard's **Twitch login** box) to log in as Twitch's
Android app, which gets the full dashboard and campaign details, including
channel allow-lists. Watch time, drop progress, drop claims and channel points
have been verified to work under this login; predictions, raids and moments use
the same requests but have not yet been tested under it.

The Android app has no activation code, so the first start asks you to sign
in through a browser:

1. While signed in to Twitch as your miner account, open
   `https://id.twitch.tv/oauth2/authorize?client_id=kd1unb4b3q4t58fwlpcbzcbnm76a8fp&response_type=token&redirect_uri=https://www.twitch.tv/`
2. Twitch approves its own app without showing a page and redirects to
   `https://www.twitch.tv/#access_token=...`, then hides that address. Copy it
   from your browser history, or from the `Location` header of the `authorize`
   request in the developer tools' **Network** tab (turn on **Preserve log**
   before opening the link).
3. Paste it at the miner's prompt. A bare token also works.

Without a console to paste into (Docker, the Windows app, or a background
service), put the redirect URL in the `TWITCH_ANDROID_LOGIN` environment
variable instead. The miner checks that the token was issued to the Android app
and belongs to the configured username, then saves it as
`cookies/<username>-android.pkl`. The TV login's `cookies/<username>.pkl` is
kept, so switching back is a matter of turning the setting off. If Twitch later
rejects the saved token, the miner deletes that cookie file and asks again, so
replace an old `TWITCH_ANDROID_LOGIN` value with a fresh redirect URL.

> [!WARNING]
> This token is a full-access login to your Twitch account that does not
> expire. Keep the cookie file and the environment variable private. To revoke
> it, delete the cookie file and send the token to
> `https://id.twitch.tv/oauth2/revoke` with `client_id=kd1unb4b3q4t58fwlpcbzcbnm76a8fp`,
> or change your Twitch password.

Like the TV login, this impersonates an official Twitch app. Twitch could change
or block it at any time.

Thanks to [klept0](https://github.com/klept0) for testing this login live and
implementing it.

## LoggerSettings

| Key | Type | Default | Description |
|---|---|---|---|
| `save` | bool | `True` | Save logs to rotating files. |
| `less` | bool | `False` | Use compact log formatting and shorter messages. |
| `console_format` | str or `LoggingFormat` | `"text"` | Console log format: `"text"` or `"json"`. JSON writes one object per line with an ISO 8601 timestamp (offset included), the account `username`, and the `event` name when present. `emoji`, `colored`, and `date_format` only affect `"text"`; `less` also shortens the messages themselves, so it changes the JSON `message` field too. |
| `console_level` | logging level | `logging.INFO` | Minimum level written to the console. Notifications are not affected; each notifier's `events` list decides what it sends. |
| `console_username` | bool | `False` | Include the account username in console and notification messages. |
| `time_zone` | str or None | `None` | Log time zone, such as `America/Denver`. |
| `date_format` | str | `"dd/mm/yy"` | Date format used in logs and analytics. Use `dd`, `mm`, and `yy` or `yyyy`, separated by `/`, `-`, or `.`. |
| `file_format` | str or `LoggingFormat` | `"text"` | Log file format: `"text"` or `"json"`, with the same fields as the console. `date_format` only affects `"text"`. The dashboard log viewer shows JSON lines as written. |
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
| `email` | Email or None | `None` | Send selected events through an SMTP server. |
| `ntfy` | Ntfy or None | `None` | Publish selected events to an ntfy topic. |
| `apprise` | Apprise or None | `None` | Send selected events to any service Apprise supports. |
| `daily_report` | bool | `False` | Generate a daily channel-points and Drop activity report. |
| `daily_report_time` | str | `"00:00"` | Local delivery time for the daily report, in 24-hour `HH:MM` format. |

### Color Palette
`ColorPalette` customizes console colors by event name. Unspecified events use
`Fore.RESET`, while prediction wins and losses default to green and red. The
*Watching for points* status line has its own `WATCH_STATUS` key, which
defaults to green; it is a console and file log event only and is never sent
to notifiers. The palette applies only with `colored=True`; with
`colored=False` the *Watching for points* line is still green on the console,
as in earlier releases. Values
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

## Events

Import `Events` from `TwitchChannelPointsMiner.classes.Settings` and select any
of these values in each provider's `events` list.

| Event | Sent when |
|---|---|
| `UPDATE_AVAILABLE` | The update checker finds a newer release. This alert is forced through every configured provider when update checking is enabled. |
| `DAILY_REPORT` | The scheduled point and Drop activity summary is generated. Requires `daily_report=True`. |
| `STREAMER_ONLINE` | A tracked streamer goes live. |
| `STREAMER_OFFLINE` | A tracked streamer goes offline. |
| `GAIN_FOR_RAID` | Channel points are earned for joining a raid. |
| `GAIN_FOR_CLAIM` | Channel points are earned from a claimed bonus. |
| `GAIN_FOR_WATCH` | Channel points are earned for watch time. |
| `GAIN_FOR_WATCH_STREAK` | Channel points are earned from a watch-streak reward. |
| `BET_WIN` | A prediction wins and its result is credited. |
| `BET_LOSE` | A prediction loses. |
| `BET_REFUND` | A prediction is canceled or refunded. |
| `BET_FILTERS` | A prediction is skipped because a configured filter does not match. |
| `BET_GENERAL` | General prediction decisions or status messages are produced. |
| `BET_FAILED` | A prediction cannot be placed. |
| `BET_START` | A prediction window opens. |
| `BONUS_CLAIM` | The periodic channel-points bonus is claimed. |
| `MOMENT_CLAIM` | A Twitch Moment is claimed. |
| `JOIN_RAID` | The miner follows a raid to another channel. |
| `DROP_CLAIM` | A completed Drop reward is claimed. |
| `DROP_STATUS` | Drop discovery, eligibility, or progress status is reported. This can be verbose. |
| `DROP_LINK_REQUIRED` | A finished Drop can't be claimed because the campaign's game account isn't linked to Twitch. Sent once per campaign per run (so again after a restart) with the link URL, and once more when its Drops have been claimed. |
| `CHAT_MENTION` | The configured username is mentioned in joined chat. Chat presence must be enabled. |
| `GIFTED_SUB` | Someone gifts this account a sub in a joined chat. Chat presence must be enabled. Anonymous gifts are reported as "An Anonymous Gifter". |
| `CONFIGURATION` | Startup, reload, authentication, or other configuration information requires attention. Critical authentication alerts may be forced through configured providers. |
| `WATCH_STATUS` | Never sent. It only names the *Watching for points* status line for `ColorPalette`; adding it to a notifier's `events` has no effect. |

## StreamerSettings

| Key | Type | Default | Description |
|---|---|---|---|
| `make_predictions` | bool | `True` | Participate in channel-point predictions. |
| `follow_raid` | bool | `True` | Follow eligible raids. |
| `claim_drops` | bool | `True` | Accumulate Drop watch progress and claim available Drops. |
| `claim_moments` | bool | `True` | Claim available [Moments](https://help.twitch.tv/s/article/moments). |
| `watch_streak` | bool | `True` | Prioritize available watch-streak rewards. |
| `favorite` | bool | `False` | Prioritize this streamer when `Priority.FAVORITE` is configured. |
| `points_limit` | int or None | `None` | Stop watching at this balance, except for an eligible pending watch streak. |
| `community_goals` | bool | `False` | Contribute the maximum allowed points to community goals. |
| `bet` | BetSettings | Default settings | Configure prediction strategy, limits, filters, and timing. |
| `chat` | ChatPresence | `ONLINE` | Control when the miner joins IRC chat. |
| `gift_sub_thanks` | bool | `False` | Send a thank-you message in chat when someone gifts this account a sub there. Requires chat presence. |
| `gift_sub_message` | str | `"Thanks for the sub!"` | Thank-you text. Optionally add placeholders `{gifter}`, `{channel}` or `{tier}`, for example `"Thanks for the {tier} sub, {gifter}!"`. |

Allowed values for `chat` are:
- `ALWAYS` Join in IRC chat and never leave
- `NEVER` Never join IRC chat
- `ONLINE` Participate in IRC chat while the streamer is online (leave when offline)
- `OFFLINE` Participate in IRC chat while the streamer is offline (leave when online)

The miner connects to Twitch IRC over TLS on port 6697. If the TLS handshake fails, it skips that chat and tries TLS again the next time it joins. It never falls back to plaintext port 6667 on its own, because that sends your chat OAuth token unencrypted. On a network that blocks port 6697, set the environment variable `TCPM_ALLOW_PLAINTEXT_IRC=1` to allow plaintext IRC.

Gifted subs are only seen in chats the miner has joined, so `chat` must not be `NEVER` and the gift must arrive while the miner is in that chat. Check the channel's chat rules before turning on `gift_sub_thanks`: emote-only, followers-only or subscriber-only modes and bot rules can block or ban automated messages.
