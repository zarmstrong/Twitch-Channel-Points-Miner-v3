# -*- coding: utf-8 -*-

"""Stable application runner and Docker configuration compatibility layer."""

import argparse
import hashlib
import logging
import os
import shutil
import sys
import threading
import time
import types
from pathlib import Path

from TwitchChannelPointsMiner.config_editor import (
    WEB_CONFIG_FILENAME,
    apply_web_overrides,
)
from TwitchChannelPointsMiner.config_migration import (
    ConfigMigrationError,
    convert_runner,
    migrate_config,
)

DEFAULT_CONFIG_DIR = Path("/usr/src/app/config")
DEFAULT_LEGACY_RUNNER = Path("/usr/src/app/run.py")
DEFAULT_CONFIG_TEMPLATE = Path("/usr/src/app/config.example.py")
logger = logging.getLogger(__name__)


def _load_config(path):
    try:
        migrated = migrate_config(path)
    except (ConfigMigrationError, OSError) as error:
        raise RuntimeError(
            f"Unable to migrate configuration {path}: {error}"
        ) from error
    if migrated:
        logger.info("Migrated configuration schema in %s", path)
    source = path.read_text(encoding="utf-8")
    module = types.ModuleType("twitch_miner_user_config")
    module.__file__ = str(path)
    # Configs generated before StreamerSource was added to the migration output
    # can already contain streamer_source_priority without its import. The audit
    # marker lets us support those generated files without masking mistakes in
    # user-authored configs.
    if path.with_name(".converted-from-run-py").is_file():
        from TwitchChannelPointsMiner.classes.Settings import StreamerSource

        module.StreamerSource = StreamerSource
    try:
        exec(compile(source, str(path), "exec"), module.__dict__)
    except NameError as error:
        traceback = error.__traceback__
        while traceback.tb_next is not None:
            traceback = traceback.tb_next
        if traceback.tb_frame.f_code.co_filename != str(path):
            raise
        name = getattr(error, "name", None) or "A name"
        raise RuntimeError(
            f"Configuration error in {path}: {name} is used but not defined. "
            "Add or correct the required import."
        ) from error
    required = ("MINER_CONFIG", "STREAMERS", "MINE_CONFIG", "ANALYTICS_CONFIG")
    missing = [name for name in required if not hasattr(module, name)]
    if missing:
        raise RuntimeError(f"Configuration is missing: {', '.join(missing)}")
    # Schema migration removes conventional password entries from disk. Keep
    # the runtime guard for dynamically assembled dictionaries that cannot be
    # rewritten safely without executing user configuration first.
    if module.MINER_CONFIG.pop("password", None) is not None:
        logger.warning(
            "Removed obsolete Twitch password from MINER_CONFIG; "
            "authentication uses the TV activation flow."
        )
    return apply_web_overrides(module, path)


def _streamer_username(streamer):
    value = getattr(streamer, "username", streamer)
    return str(value).lower().strip()


def _config_digest(path):
    digest = hashlib.sha256(path.read_bytes())
    web_config_path = path.with_name(WEB_CONFIG_FILENAME)
    if web_config_path.is_file():
        digest.update(web_config_path.read_bytes())
    return digest.digest()


def _freeze(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return tuple(sorted((key, _freeze(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if hasattr(value, "name") and hasattr(value, "value"):
        return (value.__class__.__name__, value.name)
    slots = getattr(value, "__slots__", ())
    if slots:
        return (
            value.__class__.__name__,
            tuple(
                (name, _freeze(getattr(value, name)))
                for name in slots
                if hasattr(value, name)
            ),
        )
    return repr(value)


def _watch_config(path, miner, initial_config, interval):
    from TwitchChannelPointsMiner.classes.Settings import Events

    digest = _config_digest(path)
    known_streamers = {
        _streamer_username(streamer) for streamer in initial_config.STREAMERS
    }
    streamer_snapshot = {
        _streamer_username(streamer): _freeze(streamer)
        for streamer in initial_config.STREAMERS
    }
    live_categories = initial_config.MINE_CONFIG.setdefault("categories", [])
    restart_snapshot = {
        "miner": _freeze(initial_config.MINER_CONFIG),
        "analytics": _freeze(initial_config.ANALYTICS_CONFIG),
        "mine": _freeze(
            {
                key: value
                for key, value in initial_config.MINE_CONFIG.items()
                if key != "categories"
            }
        ),
    }

    while not miner.running:
        time.sleep(0.1)
    while miner.running and miner.ws_pool is None:
        time.sleep(1)
    while miner.running:
        time.sleep(interval)
        try:
            current_digest = _config_digest(path)
            if current_digest == digest:
                continue
            updated = _load_config(path)
            updated_streamers = {
                _streamer_username(streamer): streamer for streamer in updated.STREAMERS
            }
            additions = [
                streamer
                for username, streamer in updated_streamers.items()
                if username not in known_streamers
            ]
            if additions:
                miner.add_streamers(additions)
            removals = set(known_streamers) - set(updated_streamers)
            if removals:
                miner.remove_streamers(removals)
            known_streamers = set(updated_streamers)
            updated_streamer_snapshot = {
                username: _freeze(streamer)
                for username, streamer in updated_streamers.items()
            }
            changed_streamer_settings = any(
                username in streamer_snapshot
                and updated_streamer_snapshot[username] != streamer_snapshot[username]
                for username in updated_streamer_snapshot
            )

            updated_categories = list(updated.MINE_CONFIG.get("categories", []))
            if updated_categories != live_categories:
                live_categories[:] = updated_categories
                miner.refresh_categories(initial_config.MINE_CONFIG)

            updated_restart_snapshot = {
                "miner": _freeze(updated.MINER_CONFIG),
                "analytics": _freeze(updated.ANALYTICS_CONFIG),
                "mine": _freeze(
                    {
                        key: value
                        for key, value in updated.MINE_CONFIG.items()
                        if key != "categories"
                    }
                ),
            }
            if (
                updated_restart_snapshot != restart_snapshot
                or changed_streamer_settings
            ):
                logger.warning(
                    "Configuration reloaded, but constructor, analytics, "
                    "per-streamer, and non-category mining changes require "
                    "a restart.",
                    extra={"event": Events.CONFIGURATION},
                )
            digest = current_digest
            logger.info(
                "Configuration reloaded from %s", path, extra={"emoji": ":repeat:"}
            )
        except Exception as error:
            logger.error(
                f"Unable to reload configuration: {error}",
                extra={"event": Events.CONFIGURATION},
            )


def run_config(config, path):
    from TwitchChannelPointsMiner import TwitchChannelPointsMiner
    from TwitchChannelPointsMiner.classes.Settings import Settings

    config.MINE_CONFIG.setdefault("categories", [])
    Settings.config_path = str(path.parent.resolve())
    miner = TwitchChannelPointsMiner(**config.MINER_CONFIG)
    if config.ANALYTICS_CONFIG is not None:
        miner.analytics(**config.ANALYTICS_CONFIG)
    interval = max(float(os.environ.get("TCPM_CONFIG_RELOAD_SECONDS", "5")), 1)
    watcher = threading.Thread(
        target=_watch_config,
        args=(path, miner, config, interval),
        name="Configuration watcher",
        daemon=True,
    )
    watcher.start()
    miner.mine(config.STREAMERS, **config.MINE_CONFIG)
    if miner.twitch.restart_requested.is_set():
        # Flush the forced authentication alert before replacing the process.
        miner.queue_listener.stop()
        restart_process()


def restart_process():
    """Replace the current process, preserving frozen executable arguments."""
    if getattr(sys, "frozen", False):
        return os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
    return os.execv(sys.executable, [sys.executable, "-u", *sys.argv])


def _run_legacy(runner, reason):
    print(
        f"CONFIGURATION UPDATE REQUIRED: {reason} The existing {runner} will "
        "run unchanged. Mount a persistent config directory at "
        "/usr/src/app/config and recreate the container.",
        file=sys.stderr,
        flush=True,
    )
    if not runner.is_file():
        raise RuntimeError(f"Legacy runner not found: {runner}")
    environment = os.environ.copy()
    environment["TCPM_LEGACY_CONFIG_NOTICE"] = "1"
    os.execve(
        sys.executable,
        [sys.executable, "-u", str(runner)],
        environment,
    )


def _create_default_config(config_path, template=None):
    """Seed a user-owned configuration from the bundled template."""
    template = DEFAULT_CONFIG_TEMPLATE if template is None else template
    shutil.copyfile(template, config_path)
    try:
        config_path.chmod(0o600)
    except OSError:
        # Some mounted filesystems do not support POSIX permissions.
        pass


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=Path(os.environ.get("TCPM_CONFIG_DIR", DEFAULT_CONFIG_DIR)),
    )
    parser.add_argument("--legacy-runner", type=Path, default=DEFAULT_LEGACY_RUNNER)
    parser.add_argument("--convert-only", action="store_true")
    args = parser.parse_args(argv)
    config_path = args.config_dir / "config.py"
    config = None

    if not args.config_dir.is_dir():
        if args.convert_only:
            parser.error(f"configuration directory does not exist: {args.config_dir}")
        return _run_legacy(args.legacy_runner, "The config directory is not mounted.")

    if not config_path.is_file():
        if not args.legacy_runner.is_file():
            try:
                _create_default_config(config_path)
            except OSError as error:
                raise RuntimeError(
                    f"Unable to create default configuration {config_path}: {error}"
                ) from error
            print(f"Created default configuration at {config_path}.", flush=True)
            print(
                "Edit it with your Twitch account and mining settings, then "
                "restart the container.",
                flush=True,
            )
            return 0
        if os.environ.get("TCPM_DISABLE_AUTO_CONVERSION") == "1":
            return _run_legacy(args.legacy_runner, "Automatic conversion is disabled.")
        try:
            convert_runner(args.legacy_runner, config_path)
            config = _load_config(config_path)
            print(f"Converted {args.legacy_runner} to {config_path}.", flush=True)
        except Exception as error:
            for incomplete in (
                config_path,
                args.config_dir / ".converted-from-run-py",
            ):
                try:
                    incomplete.unlink(missing_ok=True)
                except OSError:
                    pass
            return _run_legacy(args.legacy_runner, f"Conversion failed: {error}")

    if args.convert_only:
        return 0
    run_config(config or _load_config(config_path), config_path)
    return 0


def cli(argv=None):
    try:
        return main(argv)
    except RuntimeError as error:
        print(f"ERROR: {error}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(cli())
