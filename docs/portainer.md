# Deploy with Portainer

This guide deploys Twitch Channel Points Miner as a Portainer-managed Docker
container. It is adapted from
[Roman Davydov's original Portainer guide](https://github.com/rdavydov/Twitch-Channel-Points-Miner-v2/wiki/Deploy-Docker-container-in-Portainer)
and updated for the current configuration layout and Docker image.

## Prepare persistent storage

On the Docker host, create a directory for the miner and its persistent data:

```sh
mkdir -p twitch-miner/{analytics,config,cookies,logs}
cp config.example.py twitch-miner/config/config.py
```

If the repository is not cloned on the Docker host, download
[`config.example.py`](../config.example.py) and save it as
`twitch-miner/config/config.py`. Edit that file before deploying the container.
Do not put Twitch credentials directly in the image or Portainer stack.

## Create the container

1. Sign in to Portainer and select the Docker environment.
2. Open **Containers**, then select **Add container**.
3. Enter a container name such as `twitch-miner`.
4. Set **Image** to `zacharmstrong/twitch-channel-points-miner:latest`.
5. Enable both **Interactive** and **TTY** so the first Twitch authentication can
   prompt for input.
6. Publish host port `5000` to container port `5000` if analytics is enabled.
7. Under **Advanced container settings > Volumes**, bind the prepared host
   directories to these container paths:

   | Host path | Container path |
   | --- | --- |
   | `/path/to/twitch-miner/analytics` | `/usr/src/app/analytics` |
   | `/path/to/twitch-miner/config` | `/usr/src/app/config` |
   | `/path/to/twitch-miner/cookies` | `/usr/src/app/cookies` |
   | `/path/to/twitch-miner/logs` | `/usr/src/app/logs` |

8. Under **Env**, add `TZ` with your
   [IANA time zone](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones),
   such as `America/Denver`. The container uses UTC when `TZ` is omitted.
9. Set the restart policy you prefer, such as **Unless stopped**.
10. Select **Deploy the container**.

The legacy image name
`zacharmstrong/twitch-channel-points-miner-v2:latest` receives the same builds,
but new deployments should use the primary image shown above.

## Complete first-time authentication

Open the container, select **Attach**, and follow the Twitch authentication
prompt from the miner's interactive terminal. Authentication data is saved in
the mounted `cookies` directory and survives container replacement.

## Access analytics

Set the analytics host to `0.0.0.0` and port to `5000` in
`config/config.py`. After publishing the port, open:

```text
http://DOCKER_HOST_IP:5000
```

Use the Docker host's IP address, not `127.0.0.1`, when connecting from another
device. Protect the analytics endpoint with a password and a trusted reverse
proxy before exposing it beyond your local network. See
[Analytics security and HTTPS reverse proxy](../README.md#analytics-security-and-https-reverse-proxy).

## Updating

In Portainer, recreate or redeploy the container with **Pull latest image**
enabled. Because configuration, cookies, logs, and analytics are bind-mounted,
replacing the container does not remove them.

If the container does not start, inspect its Portainer logs and confirm that
`config/config.py` exists in the mounted configuration directory.
