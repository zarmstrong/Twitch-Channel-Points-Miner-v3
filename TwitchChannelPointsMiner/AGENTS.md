# AGENTS.md: the miner package

Read the root [AGENTS.md](../AGENTS.md) first. This file covers what is easy to get wrong inside `TwitchChannelPointsMiner/`.

## Threads and shared state

The miner runs many threads against the same objects: the main loop in `TwitchChannelPointsMiner.run()`, the minute watcher (`Twitch.send_minute_watched_events()`), campaign sync, the Drop badge catalog, category discovery, one thread per PubSub websocket plus its ping thread, one IRC thread per joined chat, the config watcher in `runner.py`, and the analytics server.

- `miner.streamers`, `miner.original_streamers` and `WebSocketsPool.ws` are shared by reference. Retirement and config reloads change them under `config_reload_lock` (an `RLock`). Other threads read them without that lock, so iterate a snapshot (`list(self.streamers)`) and re-check that an item is still present before acting on it.
- `streamers` and `original_streamers` are parallel lists. Read them as pairs only while holding `config_reload_lock`.
- Locks and fields that tests may not set up are read with `getattr(self, "name", default)`, and some locks are created lazily, because many tests build `Twitch` with `object.__new__` and skip `__init__`. Follow that pattern when you add state that other code reads.
- Every new attribute on a class with `__slots__` needs a slot entry.
- Results that must not leak between threads, such as the latest Drops dashboard outcome, are thread-local (`_drops_dashboard_state` in `classes/Twitch.py`).
- Prefer replacing a shared container with a new one over mutating it in place when other threads iterate it.

## Configuration files

`config.py` is the user's own Python file. Never rewrite it wholesale.

- `config_editor.py` changes one node at a time: it finds the node with `ast` and splices bytes into the file, keeping the user's comments and formatting. `ast` column offsets are UTF-8 byte offsets, so work on `source.encode("utf-8")` (see `_byte_offset` and `_insert_config_item`), and split lines only on `\n`, `\r\n` and `\r`.
- Writes go through a temporary file and `os.replace`, with an in-place fallback for Docker bind mounts that reject the replace (`EBUSY`).
- `config_migration.py` upgrades older files to `CONFIG_VERSION`. Each migration keeps a `config.py.v<N>.bak` backup with the miner password removed, and re-parses its output before writing. A change to a config default usually needs a matching migration and a test in `tests/test_config_migration.py`.
- Settings the dashboard manages without code edits live in the web-config overrides file next to `config.py` (`_overrides_path`).

## Dashboard authentication

`classes/AnalyticsServer.py` serves the dashboard and its Config tab.

- `require_authentication()` guards every route except an allowlist of static asset extensions. Page templates in the same folder still go through it.
- The Config tab needs a configured analytics password. Without one, `/config` routes are refused.
- The Windows desktop app passes a per-process token in `TCPM_SHELL_ANALYTICS_TOKEN` (`SHELL_BYPASS_TOKEN_ENV_VAR`). The server exchanges it for a cookie so the embedded window skips the Basic Auth prompt. Compare secrets with `secrets.compare_digest` (see the helper near the top of the file).
- Any new route that reads account data or changes the config must stay behind these checks.

## Secrets

- Never log tokens, passwords, cookie contents, webhook URLs or full request URLs. Notifier failures go through `format_request_failure()` in `classes/NotificationError.py`, which describes the failure without the URL.
- Login sessions are pickled to `cookies/<username>.pkl` (`<username>-android.pkl` for the Android login). Treat those files as credentials.
- Examples and docs use placeholders only.

## Notifiers

Each provider in `classes/` implements `send(message, event)` and returns a `(success, reason)` tuple: `(False, reason)` instead of raising. Multi-part messages go through `send_parts()`. The dashboard's notifier forms and test button read the metadata at the top of `config_editor.py`. Keep it in step when a constructor argument becomes required. See the root AGENTS.md for the `skip_<provider>` rule.

## Tests and docs

Tests live in [tests/](../tests/AGENTS.md). User docs are in [docs/](../docs/) and the [README](../README.md). Update them when you change behaviour users can see.
