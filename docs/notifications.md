# Notifications

[Back to the README](../README.md)

Notifications are event-filtered copies of selected log messages. Add provider
objects to `LoggerSettings`; leave unused providers as `None`. Every provider
requires an `events` list, and the same event can be sent through several
providers.

```python
from TwitchChannelPointsMiner.classes.Settings import Events
from TwitchChannelPointsMiner.classes.Telegram import Telegram
from TwitchChannelPointsMiner.logger import LoggerSettings

logger_settings = LoggerSettings(
    save=True,
    auto_clear=True,
    log_retention_days=7,
    telegram=Telegram(
        chat_id=123456789,
        token="YOUR_BOT_TOKEN",
        events=[Events.STREAMER_OFFLINE, Events.DROP_CLAIM],
        message_thread_id=987,
    ),
)
```

With `auto_clear=True` (the default), the active `logs/<username>.log` file is
rolled at local midnight on Windows and Linux. Daily archives use a date suffix,
and `log_retention_days` controls how many are retained (7 by default). Set
`auto_clear=False` to preserve the existing timestamped-file-per-start behavior.

Assign this object to `MINER_CONFIG["logger_settings"]`. The complete
[configuration template](../config.example.py) shows all providers together.
Placeholder values are ignored; use real credentials and never commit them.

| Provider | Destination | Class |
|---|---|---|
| Telegram | Bot chat | `Telegram` |
| Discord | Channel webhook | `Discord` |
| Generic webhook | HTTP endpoint | `Webhook` |
| Matrix | Matrix room | `Matrix` |
| Pushover | Pushover account or group | `Pushover` |
| Gotify | Self-hosted Gotify application | `Gotify` |
| Email | Addresses reached through SMTP | `Email` |
| ntfy | ntfy topic (hosted or self-hosted) | `Ntfy` |
| Apprise | Any of 100+ services, one URL each (not in the Windows executable, see [Apprise](#apprise)) | `Apprise` |

Telegram, Discord and Pushover split a message that is too long to send in
one piece into consecutive messages, breaking at line ends where possible. A
notification is sent as at most ten messages; the tenth says how many more
were left out. On Telegram and Pushover only the first message alerts as
configured: later ones are sent silently on Telegram and at quiet priority on
Pushover. Discord posts every message normally.

Delivery failures are logged and do not stop mining. Enable
`console_username` when several accounts share a destination. The analytics
dashboard can send a test message for each enabled provider.

## Telegram

Create a bot with [@BotFather](https://t.me/botfather), then obtain the numeric
destination `chat_id` (for example with [@getmyid_bot](https://t.me/getmyid_bot)).

| Key | Type | Default | Description |
|---|---|---|---|
| `chat_id` | int | Required | Numeric Telegram chat ID. |
| `token` | str | Required | Bot token issued by BotFather. |
| `events` | list | Required | Events to send. |
| `disable_notification` | bool | `False` | Send without sound or vibration. Later messages of a split notification are always silent. |
| `message_thread_id` | int or None | `None` | Optional Telegram forum topic ID for supergroup topics. |

```python
Telegram(
    chat_id=123456789,
    token="YOUR_BOT_TOKEN",
    events=[Events.STREAMER_ONLINE, Events.CHAT_MENTION],
    disable_notification=True,
    message_thread_id=987,
)
```

## Discord

In the destination channel, open **Edit Channel > Integrations > Webhooks**,
create a webhook, and copy its URL.

| Key | Type | Default | Description |
|---|---|---|---|
| `webhook_api` | str | Required | Discord webhook URL. |
| `events` | list | Required | Events to send. |

```python
Discord(
    webhook_api="https://discord.com/api/webhooks/ID/TOKEN",
    events=[Events.STREAMER_OFFLINE, Events.BET_LOSE],
)
```

## Generic Webhook

`Webhook` sends `event_name` and `message` as URL query parameters for `GET`, or
as form fields in the request body for `POST`.

| Key | Type | Default | Description |
|---|---|---|---|
| `endpoint` | str | Required | Destination URL. |
| `method` | str | Required | `POST` or `GET`. |
| `events` | list | Required | Events to send. |
| `timeout` | float | `10` | Request timeout in seconds. |

```python
Webhook(
    endpoint="https://example.com/webhook",
    method="POST",
    events=[Events.DROP_CLAIM, Events.CONFIGURATION],
    timeout=5,
)
```

## ntfy

`Ntfy` publishes plain-text notifications to [ntfy](https://ntfy.sh/). The
default server is `https://ntfy.sh`; set `server_url` for a self-hosted server.
Topic names act like passwords when access control is not enabled, so use a
hard-to-guess topic and do not commit it. Protected topics can use an access
token.

| Key | Type | Default | Description |
|---|---|---|---|
| `topic` | str | Required | Destination topic name. |
| `events` | list | Required | Events to send. |
| `server_url` | str | `"https://ntfy.sh"` | Hosted or self-hosted ntfy server URL. |
| `token` | str or None | `None` | Optional ntfy access token. |
| `priority` | int or None | `None` | Optional ntfy priority from 1 through 5. |
| `tags` | list, tuple, str, or None | `None` | Optional ntfy tags. |
| `timeout` | float | `10` | Request timeout in seconds. |

```python
from TwitchChannelPointsMiner.classes.Ntfy import Ntfy

Ntfy(
    topic="YOUR_NTFY_TOPIC",
    token="YOUR_ACCESS_TOKEN",
    priority=3,
    tags=["twitch"],
    events=[Events.STREAMER_OFFLINE, Events.DROP_CLAIM],
)
```

## Apprise

`Apprise` sends notifications through [Apprise](https://github.com/caronc/apprise),
which supports more than 100 services. Each destination is one URL, such as
`discord://`, `tgram://` or `ntfy://`; see the
[Apprise wiki](https://github.com/caronc/apprise/wiki) for every format.

Apprise is an optional package, so whether you can use this notifier depends
on how you run the miner:

| How you run the miner | Apprise |
|---|---|
| [Docker](docker.md#docker) | Included. Nothing to install. |
| [Source checkout](../README.md#source-checkout-quick-start) | Install it with `pip install -r requirements-optional.txt`. |
| [Windows executable](windows.md#windows) | Not available. Use one of the built-in providers above (Telegram, Discord, ntfy and the others), or run the miner from a source checkout or Docker. |

Apprise is not part of `requirements.txt` because it brings about 20 more
packages, one of which is compiled. On platforms without prebuilt packages,
that would slow down or break every install, even for people who never use
Apprise. When the package is missing, the miner keeps running and logs one
warning at the first notification. The warning says how to fix it.

The **Config** tab of the desktop window and web dashboard does not list
Apprise. Add it to `config/config.py` by hand; saving from the Config tab
keeps it.

| Key | Type | Default | Description |
|---|---|---|---|
| `urls` | str or list | Required | One or more Apprise service URLs. |
| `events` | list | Required | Events to send. |
| `title` | str | `"Twitch Channel Points Miner"` | Notification title. `{event}` is replaced with the event name, such as `DROP_CLAIM`. |

```python
from TwitchChannelPointsMiner.classes.Apprise import Apprise

Apprise(
    urls=["discord://WEBHOOK_ID/WEBHOOK_TOKEN", "tgram://BOT_TOKEN/CHAT_ID"],
    events=[Events.DROP_CLAIM, Events.STREAMER_OFFLINE],
)
```

### Custom Discord embeds

Apprise's `discord://` URL can send a custom embed instead of plain text. Point
its `template` option at a JSON file that holds the full webhook payload.
Apprise fills in the `{{...}}` tokens before sending.

```json
{
  "username": "{{botname}}",
  "avatar_url": "https://example.com/avatar.png",
  "embeds": [{
    "title": "{{app_title}}",
    "description": "<:pog:123456789012345678> {{app_body}}",
    "color": {{color}},
    "thumbnail": {"url": "https://example.com/thumbnail.png"},
    "footer": {"text": "Twitch Channel Points Miner"}
  }]
}
```

```python
Apprise(
    urls=[
        "discord://WEBHOOK_ID/WEBHOOK_TOKEN"
        "?template=/path/to/discord.json&:color=9520895&:botname=Points%20Miner"
    ],
    events=[Events.DROP_CLAIM, Events.STREAMER_ONLINE],
    title="{event}",
)
```

- `{{app_body}}` is the miner's message. `{{app_title}}` is the `title` value,
  so `title="{event}"` puts the event name in the embed title.
- Add your own tokens to the URL as `:name=value` and use them as `{{name}}`.
  Above, `:color` sets the embed colour as a decimal number (`9520895` is
  `#9146FF`) and `:botname` sets the webhook's display name.
- Custom server emoji use Discord's `<:name:id>` syntax.
- Every event uses the same template. Tested with Apprise 2.1.0; see the
  [Apprise Discord wiki](https://github.com/caronc/apprise/wiki/Notify_discord)
  for all options.

## Matrix

Matrix logs in when configured, then sends plain-text messages to the room.

| Key | Type | Default | Description |
|---|---|---|---|
| `username` | str | Required | Username accepted by the homeserver. |
| `password` | str | Required | Matrix account password. |
| `homeserver` | str | Required | Hostname without `https://`, such as `matrix.org`. |
| `room_id` | str | Required | Destination room ID, such as `!roomid:matrix.org`. |
| `events` | list | Required | Events to send. |

```python
Matrix(
    username="twitch_miner",
    password="YOUR_MATRIX_PASSWORD",
    homeserver="matrix.org",
    room_id="!roomid:matrix.org",
    events=[Events.STREAMER_OFFLINE, Events.DROP_CLAIM],
)
```

The account must already be able to post in the room. Authentication failure is
logged at startup and disables Matrix delivery for that run.

## Pushover

Create an application at [Pushover](https://pushover.net/), then use the account
user key and application API token.

| Key | Type | Default | Description |
|---|---|---|---|
| `userkey` | str | Required | Pushover user or group key. |
| `token` | str | Required | Application API token. |
| `priority` | int | Required | [Pushover message priority](https://pushover.net/api#priority). Later messages of a split notification go at quiet priority (-1), or -2 when the configured priority is -2 or lower. |
| `sound` | str | Required | [Pushover sound name](https://pushover.net/api#sounds). |
| `events` | list | Required | Events to send. |

```python
Pushover(
    userkey="YOUR_USER_KEY",
    token="YOUR_APPLICATION_TOKEN",
    priority=0,
    sound="pushover",
    events=[Events.CHAT_MENTION, Events.DROP_CLAIM],
)
```

## Gotify

Create an application in Gotify and use its message endpoint, including the
application token.

| Key | Type | Default | Description |
|---|---|---|---|
| `endpoint` | str | Required | Message URL, such as `https://gotify.example/message?token=TOKEN`. |
| `priority` | int | Required | Priority passed to Gotify. |
| `events` | list | Required | Events to send. |

```python
Gotify(
    endpoint="https://gotify.example/message?token=YOUR_TOKEN",
    priority=8,
    events=[Events.STREAMER_OFFLINE, Events.BET_LOSE],
)
```

## Email / SMTP

Email uses Python's built-in SMTP support and accepts one recipient or a list or
tuple of recipients.

| Key | Type | Default | Description |
|---|---|---|---|
| `host` | str | Required | SMTP server hostname. |
| `port` | int | Required | SMTP server port. |
| `sender` | str | Required | Address placed in the `From` header. |
| `recipients` | str, list, or tuple | Required | One recipient or a list/tuple of recipients. |
| `events` | list | Required | Events to send. |
| `username` | str or None | `None` | SMTP login username. |
| `password` | str or None | `None` | SMTP login password. |
| `use_ssl` | bool | `False` | Use implicit TLS from connection start. |
| `starttls` | bool | `True` | Upgrade a plain connection with STARTTLS. |
| `timeout` | float | `15` | Connection timeout in seconds. |

```python
Email(
    host="smtp.example.com",
    port=587,
    username="miner@example.com",
    password="YOUR_SMTP_PASSWORD",
    sender="miner@example.com",
    recipients=["you@example.com"],
    events=[Events.DAILY_REPORT, Events.CONFIGURATION],
    starttls=True,
)
```

`use_ssl` and `starttls` cannot both be enabled. Use `use_ssl=True,
starttls=False` for implicit TLS, commonly on port 465. For scheduled summaries,
subscribe to `DAILY_REPORT` and set `daily_report=True`. Report progress is saved
per account under `logs/.state/`, including Drop progress and the points of
channels added or removed during the day, so restarting the miner doesn't drop
them from the next report. The report also lists finished Drops that Twitch
won't deliver until the campaign's game account is linked, with the link URL,
every day until the account is linked. An overdue report is sent after startup
finishes refreshing streamer balances.
