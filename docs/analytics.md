# Analytics

[Back to the README](../README.md)

The optional analytics server provides a browser-based view of channel-point
history, Drops, and miner logs. The points chart includes tooltips with the
balance, timestamp, and gain or spend reason. Annotations highlight predictions,
watch streaks, and other significant changes, and can be disabled in the page.
The interface also includes light and dark themes.

The **Config** tab manages explicit streamers, categories and their order,
per-streamer behavior, category discovery, stream sources, logging, daily
reports, and notification providers. Streamer additions/removals and category
list changes are detected by the running miner; controls marked in the page as
restart-required take effect on the next start.

Dashboard changes are written directly to their canonical `STREAMERS`,
`MINER_CONFIG`, and `MINE_CONFIG` values in `config.py`; unrelated source and
comments are preserved. An existing `web-config.json` is imported automatically
and renamed to `.web-config.json.migrated.bak`. Notification
credentials are write-only in the browser: existing values are reported only
as configured, never returned by the API, and blank credential fields retain
the saved value. Protect the Config tab with the same analytics authentication
and HTTPS guidance described below.

| Light theme | Dark theme |
| ----------- | ---------- |
| ![Light theme](https://raw.githubusercontent.com/Tkd-Alex/Twitch-Channel-Points-Miner-v2/master/assets/chart-analytics-light.png) | ![Dark theme](https://raw.githubusercontent.com/Tkd-Alex/Twitch-Channel-Points-Miner-v2/master/assets/chart-analytics-dark.png) |

Enable storage with `MINER_CONFIG["enable_analytics"] = True` and configure the
server through `ANALYTICS_CONFIG`. The chart refreshes every `refresh` minutes.
`port` defaults to `54455` for a newly created `ANALYTICS_CONFIG` (chosen to
avoid the common conflicts on port `5000`); an existing configuration that
already sets `port` keeps that value untouched.
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
    "port": 54455,
    "refresh": 5,
    "days_ago": 7,
    "log_poll_interval": 5,
}
```

## Analytics security and HTTPS reverse proxy

The analytics server contains account activity and miner logs. Its built-in authentication uses HTTP Basic authentication, which does not encrypt credentials or response data. Binding it to `0.0.0.0` exposes it to every reachable network interface; use a strong, unique analytics password and do not expose the analytics port (`54455` by default) directly to the internet.

For remote access, keep the miner bound to loopback and terminate HTTPS in a reverse proxy. A minimal nginx location looks like this:

```nginx
server {
    listen 443 ssl;
    server_name analytics.example.com;

    ssl_certificate /etc/letsencrypt/live/analytics.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/analytics.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:54455;
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

## Enabling analytics storage

Disabling analytics reduces memory and disk use because the miner does not create
or update analytics JSON files.

Set `MINER_CONFIG["enable_analytics"]` to `True` when using the analytics server.
Otherwise leave it `False` (the default).
