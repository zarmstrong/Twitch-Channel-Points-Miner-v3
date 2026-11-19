# Traefik + Authelia

[Back to the README](../README.md)

The miner can run the analytics dashboard with Authelia as its only
authentication layer. Traefik terminates TLS and forwards authenticated
requests to the miner; the miner's own HTTP Basic authentication is disabled
with `trust_proxy_auth=True`.

## Topology

```text
browser -> Traefik (TLS + Authelia forwardAuth) -> twitch-miner:54455
```

The miner port must not be published directly. Put Traefik, Authelia and the
miner on the same internal Docker network and expose only Traefik's HTTPS
entrypoint.

## Miner configuration

```python
ANALYTICS_CONFIG = {
    "host": "0.0.0.0",
    "port": 54455,
    "trust_proxy_auth": True,
}
```

Do not set `username` or `password`. `trust_proxy_auth=True` disables the
built-in Basic authentication and enables the Config tab without an analytics
password. Anything that can reach port `54455` directly has full access to
analytics data, logs and configuration, so keep that port on the internal
network only.

## Docker Compose labels

```yaml
services:
  twitch-miner:
    # ...
    networks:
      - proxy
    # Do not add a ports: mapping for 54455.
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.twitch-miner.rule=Host(`miner.example.com`)"
      - "traefik.http.routers.twitch-miner.entrypoints=websecure"
      - "traefik.http.routers.twitch-miner.tls.certresolver=letsencrypt"
      - "traefik.http.routers.twitch-miner.middlewares=authelia@file"
      - "traefik.http.services.twitch-miner.loadbalancer.server.port=54455"

networks:
  proxy:
    external: true
```

## Traefik middleware

```yaml
http:
  middlewares:
    authelia:
      forwardAuth:
        address: "http://authelia:9091/api/verify?rd=https://auth.example.com/"
        trustForwardHeader: true
        authResponseHeaders:
          - Remote-User
          - Remote-Groups
          - Remote-Email
          - Remote-Name
```

Traefik keeps the browser's `Host` header by default, which is what the miner
expects. It also forwards the `X-Forwarded-*` headers.

## Authelia access control

Protect the miner's hostname with the policy you want:

```yaml
access_control:
  default_policy: deny
  rules:
    - domain: "miner.example.com"
      policy: two_factor
```

## Restarting from the dashboard

The Config tab's **Restart now** button posts to `/config/restart` and schedules
an in-process restart. With `trust_proxy_auth=True`, the request passes the
config gate; same-origin POSTs pass the cross-site check through Traefik.

## Verifying the setup

- Open `https://miner.example.com/` while signed out and confirm Authelia
  redirects you to its portal.
- Confirm `http://127.0.0.1:54455/` is unreachable from the host when the miner
  runs in Docker without a published port.
- Confirm the Config tab loads after signing in through Authelia.

## Troubleshooting

| Symptom | Check |
|---|---|
| `403 Unrecognized Host header` | Bind the miner to `0.0.0.0`, or keep `trust_proxy_auth=True` so the loopback Host guard is bypassed. |
| `403 Cross-site request rejected` | Make sure Traefik forwards the original `Host` (default) or `X-Forwarded-Host`; the browser's `Origin` must match. |
| `401` after enabling the proxy | Remove the analytics `username`/`password`; `trust_proxy_auth=True` is meant to replace them, not combine with a second login. |
| Config tab is disabled | Set `trust_proxy_auth=True` and leave the password unset. |
| Authelia redirect loop | Check the Authelia `session` cookie domain and the forwardAuth `rd` URL. |

## Security notes

Authelia is the only gate. The miner does not verify that a request actually
came through Traefik, and the Host guard is not a substitute for network
isolation. In particular, do not rely on DNS rebinding protection when built-in
authentication is disabled: keep port `54455` unreachable from browsers except
through Traefik. If the port is reachable from an untrusted network, add a
shared proxy secret or a network policy before enabling `trust_proxy_auth`.

If you run the miner and Traefik directly on the host instead of Docker, bind
the miner to `127.0.0.1` and make sure nothing else on the host can reach port
`54455`. A browser on the same host can still reach loopback, so Docker network
isolation is the safer setup.
