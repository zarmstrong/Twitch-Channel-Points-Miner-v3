# -*- coding: utf-8 -*-

"""Windows executable entry point for Twitch Channel Points Miner."""

import os
import shutil
import sys
from pathlib import Path

from TwitchChannelPointsMiner.runner import main as runner_main


def application_directory():
    """Return the user-owned directory containing the executable or script."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def bundled_file(name):
    """Return a file bundled by PyInstaller or present in the source checkout."""
    bundle_directory = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return bundle_directory / name


def prepare_config(application_dir):
    """Create the external configuration template on first launch."""
    config_dir = application_dir / "config"
    config_path = config_dir / "config.py"
    if config_path.is_file():
        return config_dir, False

    config_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(bundled_file("config.example.py"), config_path)
    return config_dir, True


def main():
    application_dir = application_directory()
    os.chdir(application_dir)
    config_dir, created = prepare_config(application_dir)
    if created:
        print(f"Created {config_dir / 'config.py'}")
        print(
            "Edit that file with your Twitch account and mining settings, then "
            "run the executable again."
        )
        return 0

    return runner_main(
        [
            "--config-dir",
            str(config_dir),
            "--legacy-runner",
            str(application_dir / "run.py"),
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main())
