# Docker

[Back to the README](../README.md)

## Docker Hub
Official Docker images are on https://hub.docker.com/r/zacharmstrong/twitch-channel-points-miner for `linux/amd64` and `linux/arm64`. The same tags are also published to the legacy https://hub.docker.com/r/zacharmstrong/twitch-channel-points-miner-v2 repository so existing installations continue to receive updates.

The ARM64 image can run in compatible 64-bit Android Linux environments where
the device reports `aarch64` or `arm64-v8a`. Android itself is not a supported
Docker host, and users are responsible for providing a suitable container,
chroot, or Linux compatibility layer. Older 32-bit ARMv7 devices are not
supported.

Maintainers building or publishing images from source should follow the
[Docker image build guide](../BUILD.md#docker-images).

The image reads `/usr/src/app/config/config.py`. Create and mount a host `config`
directory. When that directory is empty on first launch, the container copies
the bundled [config.example.py](../config.example.py) to `config/config.py` and
exits. Customize the new file, then start the container again. If you normally
use restart policies like `restart: unless-stopped` or `--restart unless-stopped`,
run the initial seed launch with restarts disabled (`restart: "no"` or
`--restart no`) so the container stays stopped while you edit `config/config.py`.
After editing, restore your preferred restart policy before launching the
container again. The container entrypoint starts the stable runner on
subsequent launches.

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
    image: zacharmstrong/twitch-channel-points-miner
    restart: unless-stopped
    stop_grace_period: 60s
    stdin_open: true
    tty: true
    environment:
      - TERM=xterm-256color
      - TZ=America/Denver
    volumes:
      - ./analytics:/usr/src/app/analytics
      - ./cookies:/usr/src/app/cookies
      - ./logs:/usr/src/app/logs
      - ./config:/usr/src/app/config
    ports:
      - "54455:54455"
```

**Example with docker run:**
```sh
docker run \
    --restart unless-stopped \
    --stop-timeout 60 \
    -e TZ=America/Denver \
    -v $(pwd)/analytics:/usr/src/app/analytics \
    -v $(pwd)/cookies:/usr/src/app/cookies \
    -v $(pwd)/logs:/usr/src/app/logs \
    -v $(pwd)/config:/usr/src/app/config \
    -p 54455:54455 \
    zacharmstrong/twitch-channel-points-miner
```

Set `TZ` to your [IANA time zone](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones),
for example `America/New_York`, `Europe/London`, or `Asia/Tokyo`. This controls
the local time used in container logs and timestamps. If `TZ` is omitted, the
container uses UTC.

`$(pwd)` does not work in Windows Command Prompt. Use absolute host paths there,
such as `/path/to/cookies:/usr/src/app/cookies`.

On Windows, use absolute host paths. For example, mount
`C:\Absolute\Path\config` at `/usr/src/app/config`.

With `"android_login": True`, the miner can't take a pasted sign-in when nobody
is attached to the container. Set the `TWITCH_ANDROID_LOGIN` environment variable
to the redirect URL instead (for example `- TWITCH_ANDROID_LOGIN=https://www.twitch.tv/#access_token=...`
under `environment:`, or `-e TWITCH_ANDROID_LOGIN=...` with `docker run`). It is
only read when `cookies/<username>-android.pkl` doesn't exist yet. See
[Android login](settings.md#android-login).

## Graceful Docker shutdowns

The miner handles Docker's normal `SIGTERM` stop signal and writes its
graceful-shutdown report after stopping its worker threads. This applies to
`docker stop`, `docker restart`, and normal Docker Compose stop, restart, down,
and recreate operations.

Docker's default stop grace period is commonly only 10 seconds. Shutdown can
take longer while chat, WebSocket, campaign-sync, and badge-catalog workers
finish. Configure at least 60 seconds so Docker does not send `SIGKILL` before
the report is written:

```yaml
services:
  miner:
    stop_grace_period: 60s
    restart: unless-stopped
```

For `docker run`, use the equivalent `--stop-timeout 60` and
`--restart unless-stopped` options. The restart policy controls recovery after
process or host restarts; it does not replace the stop grace period.

An immediate `docker kill`, an out-of-memory kill, a host crash, or power loss
cannot run shutdown code and therefore cannot produce an end report. Reports
are stored in the mounted `logs` directory, so keep that directory persistent.

For a non-default container path, set `TCPM_CONFIG_DIR` and mount the host
directory at the same location. Users upgrading from the legacy runner should
follow [Migrating from run.py](migration.md#migrating-from-runpy) once; new installations do
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
    -p 54456:54455 \
    zacharmstrong/twitch-channel-points-miner
```

```sh
docker run --name user2 -it \
    -v $(pwd)/user2/config:/usr/src/app/config \
    -v $(pwd)/user2/cookies:/usr/src/app/cookies \
    -v $(pwd)/user2/logs:/usr/src/app/logs \
    -v $(pwd)/user2/analytics:/usr/src/app/analytics \
    -p 54457:54455 \
    zacharmstrong/twitch-channel-points-miner
```

## Portainer

See the [Portainer deployment guide](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/wiki/Deploy-with-Portainer)
for persistent storage, container settings, first-time authentication, analytics,
and updates.
