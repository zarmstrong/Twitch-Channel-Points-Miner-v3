# Migration

[Back to the README](../README.md)

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

### Persisted data versions

User-owned formats are checked when their corresponding feature starts:

- `config/config.py` uses `CONFIG_VERSION` and is migrated before execution.
- Streamer analytics JSON and `drops_by_category.json` use a root `version` and
  are migrated when analytics is enabled. Pre-migration files are retained as
  `<filename>.v0.bak`.
- Watch-streak state and `drop_badge_catalog.json` already carry independent
  version fields checked by their loaders.
- Cookie JSON keeps Twitch's cookie-dictionary shape and is not wrapped in a
  project schema. Authentication remains responsible for validating it.

Migrations are idempotent and refuse to overwrite an existing backup or consume
a format version newer than the running release.

If conversion fails, any incomplete output is removed, a migration warning is
printed, and the original runner executes unchanged. Correct the legacy file and
remove any partial config before retrying, or create `config/config.py` manually
from [config.example.py](../config.example.py). Set
`TCPM_DISABLE_AUTO_CONVERSION=1` only when you intentionally need to postpone
conversion; the container will continue using the mounted legacy runner.

To convert without starting the miner, run the image's runner with
`--convert-only` while mounting both paths. This writes the converted config but
does not launch mining. Conversion still refuses to replace an existing config.

```sh
docker run --rm \
    -v $(pwd)/config:/usr/src/app/config \
    -v $(pwd)/run.py:/usr/src/app/run.py \
    zacharmstrong/twitch-channel-points-miner --convert-only
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
in [Docker](docker.md#docker). The same per-account filename rule applies inside the
mounted directory. Use a separate cookie and config directory for each account.

The legacy cookie may no longer be accepted by Twitch. If the miner reports an
invalid or unsafe file, remove it and authenticate again. If Twitch rejects a
previously valid saved token, the miner clears the cached login and restarts so
that reauthentication can occur; follow the instructions printed in the logs.
