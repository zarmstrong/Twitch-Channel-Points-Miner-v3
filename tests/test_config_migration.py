import ast
import difflib
import errno
import hashlib
import inspect
import logging
import os
import platform
import re
import stat
import sys
from pathlib import Path

import pytest

from TwitchChannelPointsMiner.config_migration import (
    ANALYTICS_CONFIG_DEFAULTS,
    BET_SETTINGS_DEFAULTS,
    CONFIG_VERSION,
    LOGGER_SETTINGS_DEFAULTS,
    LOGGER_NOTIFICATION_SETTINGS,
    MINE_CONFIG_DEFAULTS,
    MINER_CONFIG_DEFAULTS,
    STREAMER_SETTINGS_DEFAULTS,
    ConfigMigrationError,
    GeneratedConfigError,
    convert_runner,
    convert_runner_source,
    migrate_config,
    migrate_config_source,
)
from TwitchChannelPointsMiner.TwitchChannelPointsMiner import TwitchChannelPointsMiner
from TwitchChannelPointsMiner.classes.entities.Bet import BetSettings
from TwitchChannelPointsMiner.classes.entities.Streamer import StreamerSettings
from TwitchChannelPointsMiner.logger import LoggerSettings, LoggingFormat

RUNNER = """\
from TwitchChannelPointsMiner import TwitchChannelPointsMiner
from TwitchChannelPointsMiner.classes.entities.Streamer import StreamerSettings

miner = TwitchChannelPointsMiner("alice", claim_drops_startup=True)
miner.mine(["channel_one", "channel_two"], followers=True)
miner.analytics(port=5000)
"""


CONFIG = """\
# -*- coding: utf-8 -*-
from TwitchChannelPointsMiner.classes.Settings import Priority
from TwitchChannelPointsMiner.classes.entities.Streamer import StreamerSettings

PRIORITIES = [
    Priority.STREAK,
    Priority.DROPS,
    Priority.ORDER,
]
DEFAULT_SETTINGS = StreamerSettings(
    make_predictions=False,
    claim_drops=False,
)
MINER_CONFIG = {
    "username": "alice",
    "priority": PRIORITIES,
    "streamer_settings": DEFAULT_SETTINGS,
}
STREAMERS = []
MINE_CONFIG = {}
ANALYTICS_CONFIG = None
"""


def test_migrate_config_source_adds_version_settings_and_new_priority_last():
    migrated, old_version, new_version = migrate_config_source(CONFIG)
    namespace = {}

    exec(migrated, namespace)

    assert old_version == 0
    assert new_version == CONFIG_VERSION
    assert namespace["CONFIG_VERSION"] == CONFIG_VERSION
    assert namespace["PRIORITIES"] == [
        namespace["Priority"].STREAK,
        namespace["Priority"].DROPS,
        namespace["Priority"].ORDER,
        namespace["Priority"].FAVORITE,
    ]
    settings = namespace["DEFAULT_SETTINGS"]
    assert settings.make_predictions is False
    assert settings.claim_drops is False
    assert {name for name, _ in STREAMER_SETTINGS_DEFAULTS} == set(settings.__slots__)
    assert settings.follow_raid is True
    assert settings.claim_moments is True
    assert settings.watch_streak is True
    assert settings.favorite is False
    assert settings.points_limit is None
    assert settings.community_goals is False
    assert settings.bet is None
    assert settings.chat is None


def test_migrate_config_source_resolves_aliased_miner_config():
    source = CONFIG.replace(
        'MINER_CONFIG = {\n    "username": "alice",',
        'DEFAULT_CONFIG = {\n    "username": "alice",',
    ).replace(
        "STREAMERS = []",
        "MINER_CONFIG = DEFAULT_CONFIG\nSTREAMERS = []",
    )

    migrated, _, _ = migrate_config_source(source)
    namespace = {}
    exec(migrated, namespace)

    assert namespace["MINER_CONFIG"] is namespace["DEFAULT_CONFIG"]
    assert namespace["DEFAULT_CONFIG"]["priority"][-1] is namespace["Priority"].FAVORITE
    assert namespace["DEFAULT_SETTINGS"].points_limit is None


def test_version_one_migration_adds_missing_options_and_follower_source_last():
    source = """\
CONFIG_VERSION = 1
from TwitchChannelPointsMiner.classes.Settings import StreamerSource
MINER_CONFIG = {
    "username": "alice",
    "streamer_source_priority": [
        StreamerSource.STREAMERS,
        StreamerSource.CATEGORIES,
        StreamerSource.BADGES,
    ],
}
STREAMERS = []
MINE_CONFIG = {"followers": True}
ANALYTICS_CONFIG = None
"""

    migrated, old_version, new_version = migrate_config_source(source)
    namespace = {}
    exec(migrated, namespace)

    assert old_version == 1
    assert new_version == CONFIG_VERSION
    assert namespace["MINER_CONFIG"]["streamer_source_priority"] == [
        namespace["StreamerSource"].STREAMERS,
        namespace["StreamerSource"].CATEGORIES,
        namespace["StreamerSource"].BADGES,
        namespace["StreamerSource"].FOLLOWERS,
        namespace["StreamerSource"].WILDCARD_CATEGORIES,
    ]
    for name, _ in MINER_CONFIG_DEFAULTS:
        assert name in namespace["MINER_CONFIG"]
    assert namespace["MINE_CONFIG"]["followers"] is True
    for name, _ in MINE_CONFIG_DEFAULTS:
        assert name in namespace["MINE_CONFIG"]


def test_version_seven_migration_adds_wildcard_categories_and_source_last():
    source = """\
CONFIG_VERSION = 7
from TwitchChannelPointsMiner.classes.Settings import StreamerSource
MINER_CONFIG = {
    "username": "alice",
    "streamer_source_priority": [
        StreamerSource.STREAMERS,
        StreamerSource.FOLLOWERS,
        StreamerSource.CATEGORIES,
        StreamerSource.BADGES,
    ],
}
STREAMERS = []
MINE_CONFIG = {}
ANALYTICS_CONFIG = None
"""

    migrated, old_version, new_version = migrate_config_source(source)
    namespace = {}
    exec(migrated, namespace)

    assert old_version == 7
    assert new_version == CONFIG_VERSION
    assert namespace["CONFIG_VERSION"] == CONFIG_VERSION
    assert namespace["MINER_CONFIG"]["streamer_source_priority"] == [
        namespace["StreamerSource"].STREAMERS,
        namespace["StreamerSource"].FOLLOWERS,
        namespace["StreamerSource"].CATEGORIES,
        namespace["StreamerSource"].BADGES,
        namespace["StreamerSource"].WILDCARD_CATEGORIES,
    ]
    for name, _ in MINE_CONFIG_DEFAULTS:
        assert name in namespace["MINE_CONFIG"]
    assert namespace["MINE_CONFIG"]["wildcard_categories"] is False
    assert namespace["MINE_CONFIG"]["wildcard_category_limit"] == 10
    assert namespace["MINE_CONFIG"]["wildcard_category_streamer_limit"] == 1
    assert namespace["MINE_CONFIG"]["wildcard_category_pin_active"] is True


def test_version_nine_migration_adds_category_drops_override_streamers():
    source = """\
CONFIG_VERSION = 9
MINER_CONFIG = {"username": "alice"}
STREAMERS = []
MINE_CONFIG = {"drop_pick_stickiness_minutes": 15}
ANALYTICS_CONFIG = None
"""

    migrated, old_version, new_version = migrate_config_source(source)
    namespace = {}
    exec(migrated, namespace)

    assert old_version == 9
    assert new_version == CONFIG_VERSION
    assert namespace["MINE_CONFIG"]["drop_pick_stickiness_minutes"] == 15
    assert namespace["MINE_CONFIG"]["category_drops_override_streamers"] is False


def test_version_ten_migration_adds_text_logging_formats():
    source = """\
CONFIG_VERSION = 10
import logging
from TwitchChannelPointsMiner.logger import ColorPalette, LoggerSettings
LOGGER = LoggerSettings(save=False)
MINER_CONFIG = {"username": "alice", "logger_settings": LOGGER}
STREAMERS = []
MINE_CONFIG = {}
ANALYTICS_CONFIG = None
"""

    migrated, old_version, new_version = migrate_config_source(source)
    namespace = {}
    exec(migrated, namespace)

    assert old_version == 10
    assert new_version == CONFIG_VERSION
    assert 'console_format="text"' in migrated
    assert 'file_format="text"' in migrated
    assert "LoggingFormat" not in migrated
    settings = namespace["LOGGER"]
    assert settings.save is False
    assert settings.console_format is LoggingFormat.TEXT
    assert settings.file_format is LoggingFormat.TEXT


def test_version_eleven_migration_adds_watch_status_interval_minutes():
    source = """\
CONFIG_VERSION = 11
MINER_CONFIG = {"username": "alice"}
STREAMERS = []
MINE_CONFIG = {"drop_pick_stickiness_minutes": 15}
ANALYTICS_CONFIG = None
"""

    migrated, old_version, new_version = migrate_config_source(source)
    namespace = {}
    exec(migrated, namespace)

    assert old_version == 11
    assert new_version == CONFIG_VERSION
    assert namespace["MINE_CONFIG"]["drop_pick_stickiness_minutes"] == 15
    assert namespace["MINE_CONFIG"]["watch_status_interval_minutes"] == 15


def test_version_eleven_migration_keeps_existing_watch_status_interval():
    source = """\
CONFIG_VERSION = 11
MINER_CONFIG = {"username": "alice"}
STREAMERS = []
MINE_CONFIG = {"watch_status_interval_minutes": 0}
ANALYTICS_CONFIG = None
"""

    migrated, _, _ = migrate_config_source(source)
    namespace = {}
    exec(migrated, namespace)

    assert namespace["MINE_CONFIG"]["watch_status_interval_minutes"] == 0


def test_version_seven_migration_appends_wildcard_last_to_custom_priority():
    source = """\
CONFIG_VERSION = 7
from TwitchChannelPointsMiner.classes.Settings import StreamerSource
MINER_CONFIG = {
    "username": "alice",
    "streamer_source_priority": [
        StreamerSource.BADGES,
        StreamerSource.CATEGORIES,
        StreamerSource.STREAMERS,
        StreamerSource.FOLLOWERS,
    ],
}
STREAMERS = []
MINE_CONFIG = {}
ANALYTICS_CONFIG = None
"""

    migrated, _, new_version = migrate_config_source(source)
    namespace = {}
    exec(migrated, namespace)

    assert new_version == CONFIG_VERSION
    assert namespace["MINER_CONFIG"]["streamer_source_priority"] == [
        namespace["StreamerSource"].BADGES,
        namespace["StreamerSource"].CATEGORIES,
        namespace["StreamerSource"].STREAMERS,
        namespace["StreamerSource"].FOLLOWERS,
        namespace["StreamerSource"].WILDCARD_CATEGORIES,
    ]


def test_migration_preserves_aliased_priority_enum_prefixes():
    source = """\
CONFIG_VERSION = 3
from TwitchChannelPointsMiner.classes.Settings import Priority as P
from TwitchChannelPointsMiner.classes.Settings import StreamerSource as SS
PRIORITIES = [P.ORDER, P.FAVORITE]
SOURCES = [SS.STREAMERS, SS.BADGES]
MINER_CONFIG = {
    "username": "alice",
    "priority": PRIORITIES,
    "streamer_source_priority": SOURCES,
}
STREAMERS = []
MINE_CONFIG = {}
ANALYTICS_CONFIG = None
"""

    migrated, _, _ = migrate_config_source(source)
    namespace = {}
    exec(migrated, namespace)

    assert namespace["PRIORITIES"] == [namespace["P"].ORDER, namespace["P"].FAVORITE]
    assert namespace["SOURCES"] == [
        namespace["SS"].STREAMERS,
        namespace["SS"].BADGES,
        namespace["SS"].FOLLOWERS,
        namespace["SS"].CATEGORIES,
        namespace["SS"].WILDCARD_CATEGORIES,
    ]
    assert migrated.count("P.FAVORITE") == 1
    assert "SS.FOLLOWERS" in migrated
    assert "SS.CATEGORIES" in migrated
    assert "SS.WILDCARD_CATEGORIES" in migrated


def test_version_two_migration_normalizes_logger_settings():
    source = """\
CONFIG_VERSION = 2
import logging
from TwitchChannelPointsMiner.logger import ColorPalette, LoggerSettings
LOGGER = LoggerSettings(save=False, colored=True)
MINER_CONFIG = {"username": "alice", "logger_settings": LOGGER}
STREAMERS = []
MINE_CONFIG = {}
ANALYTICS_CONFIG = None
"""

    migrated, old_version, new_version = migrate_config_source(source)
    namespace = {}
    exec(migrated, namespace)

    assert old_version == 2
    assert new_version == CONFIG_VERSION
    settings = namespace["LOGGER"]
    assert settings.save is False
    assert settings.colored is True
    # emoji is left out so it keeps its platform-dependent runtime default.
    assert {name for name, _ in LOGGER_SETTINGS_DEFAULTS} | set(
        LOGGER_NOTIFICATION_SETTINGS
    ) | {"emoji"} == set(settings.__slots__)
    assert settings.console_level == namespace["logging"].INFO
    assert settings.console_username is False
    assert settings.time_zone is None
    assert settings.date_format == "dd/mm/yy"
    assert settings.telegram is None
    assert settings.gotify is None


def test_logger_settings_with_expanded_kwargs_is_not_modified():
    source = """\
CONFIG_VERSION = 2
from TwitchChannelPointsMiner.logger import LoggerSettings
LOGGER_OVERRIDES = {"save": False}
LOGGER = LoggerSettings(**LOGGER_OVERRIDES)
MINER_CONFIG = {"username": "alice", "logger_settings": LOGGER}
STREAMERS = []
MINE_CONFIG = {}
ANALYTICS_CONFIG = None
"""

    migrated, _, _ = migrate_config_source(source)

    assert "LoggerSettings(**LOGGER_OVERRIDES)" in migrated


def test_multiple_logger_settings_add_required_imports_once():
    source = """\
CONFIG_VERSION = 2
from TwitchChannelPointsMiner.logger import LoggerSettings
FIRST_LOGGER = LoggerSettings(save=False)
SECOND_LOGGER = LoggerSettings(colored=True)
MINER_CONFIG = {"username": "alice", "logger_settings": FIRST_LOGGER}
STREAMERS = []
MINE_CONFIG = {}
ANALYTICS_CONFIG = None
"""

    migrated, _, _ = migrate_config_source(source)
    namespace = {}
    exec(migrated, namespace)

    assert migrated.count("import logging") == 1
    assert (
        migrated.count("from TwitchChannelPointsMiner.logger import ColorPalette") == 1
    )
    assert namespace["FIRST_LOGGER"].save is False
    assert namespace["SECOND_LOGGER"].colored is True


def test_version_three_adds_missing_source_priority_and_comments_notifications():
    source = """\
CONFIG_VERSION = 3
from TwitchChannelPointsMiner.logger import LoggerSettings
LOGGER = LoggerSettings(
    save=False,
)
MINER_CONFIG = {"username": "alice", "logger_settings": LOGGER}
STREAMERS = []
MINE_CONFIG = {}
ANALYTICS_CONFIG = {"port": 6000}
"""

    migrated, old_version, new_version = migrate_config_source(source)
    namespace = {}
    exec(migrated, namespace)

    assert old_version == 3
    assert new_version == CONFIG_VERSION
    assert namespace["MINER_CONFIG"]["streamer_source_priority"] == [
        namespace["StreamerSource"].STREAMERS,
        namespace["StreamerSource"].FOLLOWERS,
        namespace["StreamerSource"].CATEGORIES,
        namespace["StreamerSource"].BADGES,
        namespace["StreamerSource"].WILDCARD_CATEGORIES,
    ]
    for name in LOGGER_NOTIFICATION_SETTINGS:
        assert f"# {name}=" in migrated
    assert namespace["LOGGER"].telegram is None
    assert namespace["ANALYTICS_CONFIG"]["port"] == 6000
    for name, _ in ANALYTICS_CONFIG_DEFAULTS:
        assert name in namespace["ANALYTICS_CONFIG"]


def test_version_four_adds_daily_report_settings_and_email_template():
    source = """\
CONFIG_VERSION = 4
import logging
from TwitchChannelPointsMiner.logger import LoggerSettings
LOGGER = LoggerSettings(
    save=False,
)
MINER_CONFIG = {"username": "alice", "logger_settings": LOGGER}
STREAMERS = []
MINE_CONFIG = {}
ANALYTICS_CONFIG = None
"""

    migrated, old_version, new_version = migrate_config_source(source)
    namespace = {}
    exec(migrated, namespace)

    assert old_version == 4
    assert new_version == CONFIG_VERSION
    assert namespace["CONFIG_VERSION"] == CONFIG_VERSION
    assert namespace["LOGGER"].daily_report is False
    assert namespace["LOGGER"].daily_report_time == "00:00"
    assert "# email=Email(" in migrated
    assert "from TwitchChannelPointsMiner.classes.Email import Email" in migrated


def test_migration_defaults_cover_runtime_configuration_signatures():
    def parameters(callable_object):
        return set(inspect.signature(callable_object).parameters) - {"self"}

    assert {name for name, _ in STREAMER_SETTINGS_DEFAULTS} == parameters(
        StreamerSettings.__init__
    )
    assert {name for name, _ in BET_SETTINGS_DEFAULTS} == parameters(
        BetSettings.__init__
    )
    # emoji is left out so it keeps its platform-dependent runtime default.
    assert {name for name, _ in LOGGER_SETTINGS_DEFAULTS} | set(
        LOGGER_NOTIFICATION_SETTINGS
    ) | {"emoji"} == parameters(LoggerSettings.__init__)
    assert {name for name, _ in MINE_CONFIG_DEFAULTS} == parameters(
        TwitchChannelPointsMiner.mine
    ) - {"streamers"}
    assert {name for name, _ in ANALYTICS_CONFIG_DEFAULTS} == parameters(
        TwitchChannelPointsMiner.analytics
    )
    assert {name for name, _ in MINER_CONFIG_DEFAULTS} | {
        "priority",
        "streamer_source_priority",
    } == parameters(TwitchChannelPointsMiner.__init__) - {
        "username",
        "logger_settings",
        "streamer_settings",
        "gql",
    }


def test_migrate_config_source_is_idempotent():
    migrated, _, _ = migrate_config_source(CONFIG)

    second, old_version, new_version = migrate_config_source(migrated)

    assert second == migrated
    assert old_version == CONFIG_VERSION
    assert new_version == CONFIG_VERSION
    assert second.count("Priority.FAVORITE") == 1


def test_migrate_config_source_handles_single_line_trailing_commas():
    source = """\
from TwitchChannelPointsMiner.classes.Settings import Priority
from TwitchChannelPointsMiner.classes.entities.Streamer import StreamerSettings
PRIORITIES = [Priority.ORDER,]
DEFAULT_SETTINGS = StreamerSettings(watch_streak=False,)
MINER_CONFIG = {"priority": PRIORITIES, "streamer_settings": DEFAULT_SETTINGS}
STREAMERS = []
MINE_CONFIG = {}
ANALYTICS_CONFIG = None
"""

    migrated, _, _ = migrate_config_source(source)
    namespace = {}
    exec(migrated, namespace)

    assert namespace["PRIORITIES"] == [
        namespace["Priority"].ORDER,
        namespace["Priority"].FAVORITE,
    ]
    assert namespace["DEFAULT_SETTINGS"].watch_streak is False
    assert namespace["DEFAULT_SETTINGS"].points_limit is None


def test_migrate_config_source_adds_separator_after_nested_final_setting():
    source = """\
from TwitchChannelPointsMiner.classes.Settings import Priority
from TwitchChannelPointsMiner.classes.entities.Bet import BetSettings
from TwitchChannelPointsMiner.classes.entities.Streamer import StreamerSettings
DEFAULT_SETTINGS = StreamerSettings(
    bet=BetSettings(
        max_points=1234,
    )
)
MINER_CONFIG = {
    "priority": [Priority.ORDER],
    "streamer_settings": DEFAULT_SETTINGS,
}
STREAMERS = []
MINE_CONFIG = {}
ANALYTICS_CONFIG = None
"""

    migrated, _, _ = migrate_config_source(source)
    namespace = {}
    exec(migrated, namespace)

    assert namespace["DEFAULT_SETTINGS"].bet.max_points == 1234
    assert namespace["DEFAULT_SETTINGS"].points_limit is None


def test_migrate_config_source_leaves_expanded_streamer_settings_untouched():
    source = """\
from TwitchChannelPointsMiner.classes.Settings import Priority
from TwitchChannelPointsMiner.classes.entities.Streamer import StreamerSettings
STREAMER_OVERRIDES = {"favorite": True, "points_limit": 50000}
DEFAULT_SETTINGS = StreamerSettings(**STREAMER_OVERRIDES)
MINER_CONFIG = {
    "priority": [Priority.ORDER],
    "streamer_settings": DEFAULT_SETTINGS,
}
STREAMERS = []
MINE_CONFIG = {}
ANALYTICS_CONFIG = None
"""

    migrated, _, _ = migrate_config_source(source)
    namespace = {}
    exec(migrated, namespace)

    settings = namespace["DEFAULT_SETTINGS"]
    assert settings.favorite is True
    assert settings.points_limit == 50000
    assert "StreamerSettings(**STREAMER_OVERRIDES)" in migrated
    assert namespace["MINER_CONFIG"]["priority"] == [
        namespace["Priority"].ORDER,
        namespace["Priority"].FAVORITE,
    ]


def test_migration_leaves_per_streamer_overrides_sparse():
    source = """\
from TwitchChannelPointsMiner.classes.Settings import Priority
from TwitchChannelPointsMiner.classes.entities.Bet import BetSettings, Strategy
from TwitchChannelPointsMiner.classes.entities.Streamer import Streamer, StreamerSettings
DEFAULT_SETTINGS = StreamerSettings(
    make_predictions=True,
    bet=BetSettings(strategy=Strategy.SMART),
)
MINER_CONFIG = {
    "username": "alice",
    "priority": [Priority.ORDER],
    "streamer_settings": DEFAULT_SETTINGS,
}
STREAMERS = [
    Streamer(
        "channel",
        settings=StreamerSettings(
            make_predictions=False,
            follow_raid=True,
            bet=BetSettings(max_points=1234),
        ),
    ),
]
MINE_CONFIG = {}
ANALYTICS_CONFIG = None
"""

    migrated, _, _ = migrate_config_source(source)
    namespace = {}
    exec(migrated, namespace)

    global_settings = namespace["DEFAULT_SETTINGS"]
    override = namespace["STREAMERS"][0].settings
    assert global_settings.points_limit is None
    assert global_settings.bet.minimum_points is None
    assert override.make_predictions is False
    assert override.follow_raid is True
    assert override.claim_moments is None
    assert override.favorite is None
    assert override.points_limit is None
    assert override.bet.max_points == 1234
    assert override.bet.minimum_points is None
    assert (
        "StreamerSettings(\n            make_predictions=False,\n            follow_raid=True,\n            bet=BetSettings(max_points=1234),\n        )"
        in migrated
    )


def test_migration_does_not_override_computed_dictionary_keys():
    source = """\
CONFIG_VERSION = 3
USERNAME = "username"
PASSWORD = "password"
BLACKLIST = "blacklist"
PORT = "port"
MINER_CONFIG = {USERNAME: "alice", PASSWORD: "secret"}
STREAMERS = []
MINE_CONFIG = {BLACKLIST: ["blocked"]}
ANALYTICS_CONFIG = {PORT: 6000}
"""

    migrated, _, _ = migrate_config_source(source)
    namespace = {}
    exec(migrated, namespace)

    assert namespace["MINER_CONFIG"] == {"username": "alice"}
    assert "secret" not in migrated
    assert namespace["MINE_CONFIG"] == {"blacklist": ["blocked"]}
    assert namespace["ANALYTICS_CONFIG"] == {"port": 6000}


def test_migrate_config_backs_up_existing_file_and_preserves_mode(tmp_path):
    config = tmp_path / "config.py"
    config.write_text(CONFIG, encoding="utf-8")
    config.chmod(0o640)

    assert migrate_config(config) is True

    backup = tmp_path / "config.py.v0.bak"
    assert backup.read_text(encoding="utf-8") == CONFIG
    # Windows has no POSIX permission bits; chmod only toggles read-only.
    if os.name == "posix":
        assert stat.S_IMODE(config.stat().st_mode) == 0o640
        assert stat.S_IMODE(backup.stat().st_mode) == 0o640
    assert migrate_config(config) is False


def test_migrate_current_config_wipes_twitch_password_from_file_and_backup(tmp_path):
    config = tmp_path / "config.py"
    config.write_text(
        f"""\
CONFIG_VERSION = {CONFIG_VERSION}
MINER_CONFIG = {{
    "username": "alice",
    "password": "twitch-secret",
}}
STREAMERS = []
MINE_CONFIG = {{}}
ANALYTICS_CONFIG = {{"password": "analytics-secret"}}
""",
        encoding="utf-8",
    )

    assert migrate_config(config) is True

    migrated = config.read_text(encoding="utf-8")
    backup = (tmp_path / f"config.py.v{CONFIG_VERSION}.bak").read_text(encoding="utf-8")
    assert "twitch-secret" not in migrated
    assert "twitch-secret" not in backup
    assert '"password": "analytics-secret"' in migrated
    assert migrate_config(config) is False


def test_migrate_config_sanitizes_matching_interrupted_backup(tmp_path):
    config = tmp_path / "config.py"
    source = f"""\
CONFIG_VERSION = {CONFIG_VERSION}
MINER_CONFIG = {{"username": "alice", "password": "twitch-secret"}}
STREAMERS = []
MINE_CONFIG = {{}}
ANALYTICS_CONFIG = None
"""
    backup = tmp_path / f"config.py.v{CONFIG_VERSION}.bak"
    config.write_text(source, encoding="utf-8")
    backup.write_text(source, encoding="utf-8")

    assert migrate_config(config) is True
    assert "twitch-secret" not in config.read_text(encoding="utf-8")
    assert "twitch-secret" not in backup.read_text(encoding="utf-8")


def test_migrate_config_reuses_matching_backup_from_interrupted_migration(tmp_path):
    config = tmp_path / "config.py"
    source = "CONFIG_VERSION = 3\n" + CONFIG
    backup = tmp_path / "config.py.v3.bak"
    config.write_text(source, encoding="utf-8")
    backup.write_text(source, encoding="utf-8")

    assert migrate_config(config) is True
    assert backup.read_text(encoding="utf-8") == source
    assert f"CONFIG_VERSION = {CONFIG_VERSION}" in config.read_text(encoding="utf-8")


def test_migrate_config_rejects_conflicting_existing_backup(tmp_path):
    config = tmp_path / "config.py"
    source = "CONFIG_VERSION = 3\n" + CONFIG
    backup = tmp_path / "config.py.v3.bak"
    config.write_text(source, encoding="utf-8")
    backup.write_text("different configuration", encoding="utf-8")

    with pytest.raises(ConfigMigrationError, match="do not match"):
        migrate_config(config)

    assert config.read_text(encoding="utf-8") == source


def test_migrate_config_recovers_invalid_previous_migration_from_backup(tmp_path):
    config = tmp_path / "config.py"
    backup = tmp_path / "config.py.v0.bak"
    corrupted = "CONFIG_VERSION = 1\nMINER_CONFIG = {invalid syntax}\n"
    config.write_text(corrupted, encoding="utf-8")
    backup.write_text(CONFIG, encoding="utf-8")

    assert migrate_config(config) is True

    recovered = config.read_text(encoding="utf-8")
    namespace = {}
    exec(recovered, namespace)
    assert namespace["CONFIG_VERSION"] == CONFIG_VERSION
    assert namespace["DEFAULT_SETTINGS"].points_limit is None
    assert backup.read_text(encoding="utf-8") == CONFIG


def test_migrate_config_recovers_a_file_only_the_compiler_rejects(tmp_path):
    # Older migrations could write CONFIG_VERSION above a __future__ import.
    # ast.parse accepts that file and compile() rejects it; it's invalid
    # input, not invalid output, so it's still recovered from the backup.
    config = tmp_path / "config.py"
    backup = tmp_path / "config.py.v0.bak"
    config.write_text(
        "CONFIG_VERSION = 1\nfrom __future__ import annotations\nMINER_CONFIG = {}\n",
        encoding="utf-8",
    )
    backup.write_text(CONFIG, encoding="utf-8")

    assert migrate_config(config) is True

    assert config.read_text(encoding="utf-8") == migrate_config_source(CONFIG)[0]
    assert backup.read_text(encoding="utf-8") == CONFIG


def test_migrate_config_rewrites_invalid_file_from_current_version_backup(tmp_path):
    config = tmp_path / "config.py"
    backup = tmp_path / "config.py.v0.bak"
    current, _, _ = migrate_config_source(CONFIG)
    config.write_text(
        "CONFIG_VERSION = 4\nMINER_CONFIG = {invalid syntax}\n", encoding="utf-8"
    )
    backup.write_text(current, encoding="utf-8")

    assert migrate_config(config) is True
    assert config.read_text(encoding="utf-8") == current


def test_migrate_config_does_not_follow_predictable_temporary_symlink(tmp_path):
    config = tmp_path / "config.py"
    config.write_text(CONFIG, encoding="utf-8")
    external = tmp_path / "external.py"
    external.write_text("do not overwrite", encoding="utf-8")
    predictable = tmp_path / "config.py.migrating"
    try:
        predictable.symlink_to(external)
    except OSError:
        pytest.skip("Creating symlinks is not supported in this environment")

    assert migrate_config(config) is True
    assert external.read_text(encoding="utf-8") == "do not overwrite"
    assert predictable.is_symlink()


def test_migrate_version_four_config_on_direct_bind_mount(tmp_path, monkeypatch):
    config = tmp_path / "config.py"
    source = "CONFIG_VERSION = 4\n" + CONFIG
    config.write_text(source, encoding="utf-8")
    real_replace = os.replace

    def reject_replacing_bind_mount(source_path, destination_path):
        if Path(destination_path) == config:
            raise OSError(errno.EBUSY, "Device or resource busy")
        return real_replace(source_path, destination_path)

    monkeypatch.setattr(
        "TwitchChannelPointsMiner.config_migration.os.replace",
        reject_replacing_bind_mount,
    )

    assert migrate_config(config) is True
    assert f"CONFIG_VERSION = {CONFIG_VERSION}" in config.read_text(encoding="utf-8")
    assert (tmp_path / "config.py.v4.bak").read_text(encoding="utf-8") == source
    assert not list(tmp_path.glob(".config.py.*.migrating"))


def test_migrate_config_rejects_future_version():
    future = CONFIG.replace(
        "# -*- coding: utf-8 -*-",
        f"# -*- coding: utf-8 -*-\nCONFIG_VERSION = {CONFIG_VERSION + 1}",
    )

    with pytest.raises(ConfigMigrationError, match="unsupported CONFIG_VERSION"):
        migrate_config_source(future)


def test_migrate_config_rejects_symlink_without_reading_target(tmp_path):
    target = tmp_path / "external.py"
    target.write_text(CONFIG, encoding="utf-8")
    config = tmp_path / "config.py"
    try:
        config.symlink_to(target)
    except OSError:
        pytest.skip("Creating symlinks is not supported in this environment")

    with pytest.raises(ConfigMigrationError, match="symlinked configuration"):
        migrate_config(config)

    assert target.read_text(encoding="utf-8") == CONFIG
    assert not (tmp_path / "config.py.v0.bak").exists()


def test_convert_runner_source_preserves_configuration_expressions():
    converted = convert_runner_source(RUNNER)
    namespace = {}

    exec(converted, namespace)

    assert namespace["MINER_CONFIG"] == {
        "username": "alice",
        "claim_drops_startup": True,
        "streamer_source_priority": [
            namespace["StreamerSource"].STREAMERS,
            namespace["StreamerSource"].FOLLOWERS,
            namespace["StreamerSource"].CATEGORIES,
            namespace["StreamerSource"].BADGES,
            namespace["StreamerSource"].WILDCARD_CATEGORIES,
        ],
    }
    assert namespace["STREAMERS"] == ["channel_one", "channel_two"]
    assert namespace["MINE_CONFIG"] == {"followers": True}
    assert namespace["ANALYTICS_CONFIG"] == {"port": 5000}


def test_convert_runner_source_defaults_optional_sections():
    converted = convert_runner_source("""\
from TwitchChannelPointsMiner import TwitchChannelPointsMiner

miner = TwitchChannelPointsMiner("alice")
miner.mine()
""")
    namespace = {}

    exec(converted, namespace)

    assert namespace["STREAMERS"] == []
    assert namespace["MINE_CONFIG"] == {}
    assert namespace["ANALYTICS_CONFIG"] is None


def test_convert_runner_source_preserves_supporting_imports_only():
    converted = convert_runner_source("""\
import logging
from TwitchChannelPointsMiner import TwitchChannelPointsMiner
from TwitchChannelPointsMiner.classes.Settings import Priority

miner = TwitchChannelPointsMiner("alice", priority=[Priority.ORDER])
miner.mine([])
""")

    assert "import logging" in converted
    assert "from TwitchChannelPointsMiner.classes.Settings import Priority" in converted
    assert (
        "from TwitchChannelPointsMiner import TwitchChannelPointsMiner" not in converted
    )

    namespace = {}
    exec(converted, namespace)
    assert namespace["MINER_CONFIG"]["priority"] == [namespace["Priority"].ORDER]


def test_convert_runner_source_preserves_explicit_streamer_source_priority():
    converted = convert_runner_source("""\
from TwitchChannelPointsMiner import TwitchChannelPointsMiner
from TwitchChannelPointsMiner.classes.Settings import StreamerSource

miner = TwitchChannelPointsMiner(
    "alice", streamer_source_priority=[StreamerSource.FOLLOWERS]
)
miner.mine([])
""")

    namespace = {}
    exec(converted, namespace)

    assert namespace["MINER_CONFIG"]["streamer_source_priority"] == [
        namespace["StreamerSource"].FOLLOWERS
    ]


def test_convert_runner_source_rejects_configuration_with_missing_imports():
    source = """\
from TwitchChannelPointsMiner import TwitchChannelPointsMiner

miner = TwitchChannelPointsMiner("alice")
miner.mine([], category_sort=CategorySort.VIEWERS_DESC)
"""

    with pytest.raises(
        ConfigMigrationError,
        match=r"not imported: CategorySort",
    ):
        convert_runner_source(source)


@pytest.mark.parametrize(
    "source",
    [
        "miner.mine([])",
        "miner = TwitchChannelPointsMiner('a')",
        "a = TwitchChannelPointsMiner('a')\nb = TwitchChannelPointsMiner('b')\na.mine([])",
        "miner = TwitchChannelPointsMiner('a')\nminer.mine([], [], [])",
    ],
)
def test_convert_runner_source_rejects_ambiguous_inputs(source):
    with pytest.raises(ConfigMigrationError):
        convert_runner_source(source)


def test_convert_runner_source_reports_invalid_python():
    with pytest.raises(ConfigMigrationError, match="Cannot parse broken.py"):
        convert_runner_source("miner.mine([", source_name="broken.py")


@pytest.mark.parametrize(
    "source",
    [
        "miner = TwitchChannelPointsMiner(**settings)\nminer.mine([])",
        "miner = TwitchChannelPointsMiner('alice')\nminer.mine([], **settings)",
    ],
)
def test_convert_runner_source_rejects_expanded_keyword_arguments(source):
    with pytest.raises(ConfigMigrationError, match=r"Expanded \*\*kwargs"):
        convert_runner_source(source)


def test_convert_runner_writes_private_config_and_audit_marker(tmp_path):
    runner = tmp_path / "run.py"
    config = tmp_path / "config.py"
    # newline="\n" keeps the bytes on disk equal to RUNNER on every platform,
    # so the marker's sha256 of the file matches the hash computed below.
    runner.write_text(RUNNER, encoding="utf-8", newline="\n")

    result = convert_runner(runner, config)

    assert result == config
    # Windows has no POSIX permission bits; chmod only toggles read-only.
    if os.name == "posix":
        assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert not runner.exists()
    assert runner.with_name("run.py.bak").read_text(encoding="utf-8") == RUNNER
    marker = (tmp_path / ".converted-from-run-py").read_text(encoding="utf-8")
    assert f"source={runner.resolve()}" in marker
    assert hashlib.sha256(RUNNER.encode()).hexdigest() in marker


def test_convert_runner_refuses_to_overwrite_existing_config(tmp_path):
    runner = tmp_path / "run.py"
    config = tmp_path / "config.py"
    runner.write_text(RUNNER, encoding="utf-8")
    config.write_text("existing = True", encoding="utf-8")

    with pytest.raises(ConfigMigrationError, match="Refusing to overwrite"):
        convert_runner(runner, config)

    assert config.read_text(encoding="utf-8") == "existing = True"


def test_convert_runner_refuses_to_overwrite_existing_backup(tmp_path):
    runner = tmp_path / "run.py"
    backup = tmp_path / "run.py.bak"
    config = tmp_path / "config.py"
    runner.write_text(RUNNER, encoding="utf-8")
    backup.write_text("existing backup", encoding="utf-8")

    with pytest.raises(ConfigMigrationError, match="Refusing to overwrite"):
        convert_runner(runner, config)

    assert runner.read_text(encoding="utf-8") == RUNNER
    assert backup.read_text(encoding="utf-8") == "existing backup"
    assert not config.exists()


def test_convert_runner_removes_temporary_file_after_failed_replace(
    tmp_path, monkeypatch
):
    runner = tmp_path / "run.py"
    config = tmp_path / "config" / "config.py"
    runner.write_text(RUNNER, encoding="utf-8")

    def fail_replace(source, destination):
        raise OSError("replace failed")

    monkeypatch.setattr(
        "TwitchChannelPointsMiner.config_migration.os.replace", fail_replace
    )

    with pytest.raises(OSError, match="replace failed"):
        convert_runner(runner, config)

    assert not config.exists()
    assert not config.with_name("config.py.migrating").exists()
    assert not config.with_name(".converted-from-run-py").exists()


def test_migrate_config_accepts_utf8_bom(tmp_path):
    config = tmp_path / "config.py"
    current, _, _ = migrate_config_source(CONFIG)
    config.write_text("﻿" + current, encoding="utf-8")

    migrate_config(config)

    source = config.read_text(encoding="utf-8")
    assert not source.startswith("﻿")
    namespace = {}
    exec(source, namespace)
    assert namespace["CONFIG_VERSION"] == CONFIG_VERSION


@pytest.mark.parametrize(
    "existing_backup", [None, "# from an earlier migration\n"], ids=["none", "differs"]
)
def test_bom_only_change_leaves_backups_alone(tmp_path, existing_backup):
    config = tmp_path / "config.py"
    current, _, _ = migrate_config_source(CONFIG)
    config.write_text("\ufeff" + current, encoding="utf-8")
    backup = tmp_path / f"config.py.v{CONFIG_VERSION}.bak"
    if existing_backup is not None:
        backup.write_text(existing_backup + current, encoding="utf-8")

    assert migrate_config(config) is True

    assert config.read_text(encoding="utf-8") == current
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(
        ["config.py"] + ([backup.name] if existing_backup is not None else [])
    )
    if existing_backup is not None:
        assert backup.read_text(encoding="utf-8") == existing_backup + current


def test_bom_on_an_old_config_still_migrates_with_a_backup(tmp_path):
    config = tmp_path / "config.py"
    config.write_text("\ufeff" + CONFIG, encoding="utf-8")

    assert migrate_config(config) is True

    _, old_version, _ = migrate_config_source(CONFIG)
    backup = tmp_path / f"config.py.v{old_version}.bak"
    assert backup.read_text(encoding="utf-8") == CONFIG
    assert not config.read_text(encoding="utf-8").startswith("\ufeff")


def test_migrate_config_recovers_from_highest_numbered_backup(tmp_path):
    config = tmp_path / "config.py"
    current, _, _ = migrate_config_source(CONFIG)
    config.write_text(
        "CONFIG_VERSION = 4\nMINER_CONFIG = {invalid syntax}\n", encoding="utf-8"
    )
    (tmp_path / "config.py.v9.bak").write_text(CONFIG, encoding="utf-8")
    (tmp_path / "config.py.v11.bak").write_text(
        "# newest\n" + current,
        encoding="utf-8",
    )

    assert migrate_config(config) is True
    assert "# newest" in config.read_text(encoding="utf-8")


def test_migration_uses_utf8_byte_columns_for_non_ascii_before_edit():
    source = """\
CONFIG_VERSION = 10
MINER_CONFIG = {"username": "é", "logger_settings": None}
STREAMERS = []
MINE_CONFIG = {}
ANALYTICS_CONFIG = None
"""

    migrated, old_version, new_version = migrate_config_source(source)
    namespace = {}
    exec(migrated, namespace)

    assert old_version == 10
    assert new_version == CONFIG_VERSION
    assert namespace["CONFIG_VERSION"] == CONFIG_VERSION
    assert namespace["MINER_CONFIG"]["username"] == "é"
    assert "logger_settings" in namespace["MINER_CONFIG"]


def test_migration_removes_password_after_non_ascii_text_on_same_line():
    source = f"""\
CONFIG_VERSION = {CONFIG_VERSION}
MINER_CONFIG = {{"username": "é", "password": "secret"}}
STREAMERS = []
MINE_CONFIG = {{}}
ANALYTICS_CONFIG = None
"""

    migrated, old_version, new_version = migrate_config_source(source)
    namespace = {}
    exec(migrated, namespace)

    assert old_version == CONFIG_VERSION
    assert new_version == CONFIG_VERSION
    assert "password" not in migrated
    assert namespace["MINER_CONFIG"] == {"username": "é"}


def test_migration_counts_non_ascii_comment_on_multiline_dict_line():
    source = """\
# café \U0001f600
CONFIG_VERSION = 9
MINER_CONFIG = {"username": "é"}
STREAMERS = []
MINE_CONFIG = {
    "drop_pick_stickiness_minutes": 15,  # éé
}
ANALYTICS_CONFIG = None
"""

    migrated, old_version, new_version = migrate_config_source(source)
    namespace = {}
    exec(migrated, namespace)

    assert old_version == 9
    assert new_version == CONFIG_VERSION
    assert namespace["MINE_CONFIG"]["drop_pick_stickiness_minutes"] == 15
    assert namespace["MINE_CONFIG"]["category_drops_override_streamers"] is False


def test_migration_handles_form_feed_and_line_separator_in_comments():
    source = '# form\x0cfeed and   separator\nCONFIG_VERSION = 9\n# vertical\x0btab\nMINER_CONFIG = {"username": "alice"}\nSTREAMERS = []\nMINE_CONFIG = {"drop_pick_stickiness_minutes": 15}\nANALYTICS_CONFIG = None\n'

    migrated, old_version, new_version = migrate_config_source(source)
    namespace = {}
    exec(migrated, namespace)

    assert old_version == 9
    assert new_version == CONFIG_VERSION
    assert namespace["MINER_CONFIG"]["username"] == "alice"


# LoggerSettings computes this default from platform.system() when logger.py
# is imported, so a migrated config gets it by leaving emoji out.
RUNTIME_EMOJI_DEFAULT = (
    inspect.signature(LoggerSettings.__init__).parameters["emoji"].default
)


def test_runtime_emoji_default_depends_on_the_platform():
    assert RUNTIME_EMOJI_DEFAULT is (platform.system() != "Windows")


def test_migration_keeps_platform_dependent_emoji_default():
    source = """\
CONFIG_VERSION = 2
from TwitchChannelPointsMiner.logger import LoggerSettings
LOGGER = LoggerSettings(save=False)
MINER_CONFIG = {"username": "alice", "logger_settings": LOGGER}
STREAMERS = []
MINE_CONFIG = {}
ANALYTICS_CONFIG = None
"""

    migrated, _, _ = migrate_config_source(source)
    namespace = {}
    exec(migrated, namespace)

    assert "emoji" not in migrated
    assert "import platform" not in migrated
    assert namespace["LOGGER"].emoji is RUNTIME_EMOJI_DEFAULT


def test_migration_keeps_explicit_emoji_and_existing_platform_import():
    source = """\
CONFIG_VERSION = 2
import platform
from TwitchChannelPointsMiner.logger import LoggerSettings
FIRST = LoggerSettings(emoji=True)
SECOND = LoggerSettings(save=False)
MINER_CONFIG = {"username": "alice", "logger_settings": FIRST}
STREAMERS = []
MINE_CONFIG = {}
ANALYTICS_CONFIG = None
"""

    migrated, _, _ = migrate_config_source(source)
    namespace = {}
    exec(migrated, namespace)

    assert migrated.count("import platform") == 1
    assert "LoggerSettings(emoji=True" in migrated
    assert namespace["FIRST"].emoji is True


@pytest.mark.parametrize(
    "shadow",
    [
        "from sys import platform",
        "import sys as platform",
        'platform = "linux"',
        "def platform():\n    return None",
        "class platform:\n    pass",
        "import sys as _tcpm_platform",
        "_tcpm_platform = None",
        "__import__ = None",
    ],
)
def test_emoji_default_ignores_a_config_name_called_platform(shadow):
    # A config may bind `platform` to something other than the module; the
    # migrated emoji default must not depend on it.
    source = f"""\
CONFIG_VERSION = 2
from TwitchChannelPointsMiner.logger import LoggerSettings
{shadow}
LOGGER = LoggerSettings(save=False)
MINER_CONFIG = {{"username": "alice", "logger_settings": LOGGER}}
STREAMERS = []
MINE_CONFIG = {{}}
ANALYTICS_CONFIG = None
"""

    migrated, _, _ = migrate_config_source(source)
    namespace = {}
    exec(migrated, namespace)

    assert "emoji" not in migrated
    assert namespace["LOGGER"].emoji is RUNTIME_EMOJI_DEFAULT


def test_emoji_default_migration_is_idempotent():
    source = """\
CONFIG_VERSION = 2
from TwitchChannelPointsMiner.logger import LoggerSettings
LOGGER = LoggerSettings(save=False)
MINER_CONFIG = {"username": "alice", "logger_settings": LOGGER}
STREAMERS = []
MINE_CONFIG = {}
ANALYTICS_CONFIG = None
"""

    once, _, _ = migrate_config_source(source)
    twice, _, _ = migrate_config_source(once)

    assert twice == once


def test_emoji_default_works_when_an_import_follows_logger_settings():
    # The migration adds imports after the last top-level import, which here
    # comes after LoggerSettings(...) runs.
    source = """\
CONFIG_VERSION = 2
import logging
from TwitchChannelPointsMiner.logger import ColorPalette, LoggerSettings
LOGGER = LoggerSettings(save=False)
import os
import platform
MINER_CONFIG = {"username": "alice", "logger_settings": LOGGER}
STREAMERS = []
MINE_CONFIG = {}
ANALYTICS_CONFIG = None
"""

    migrated, _, _ = migrate_config_source(source)
    namespace = {}
    exec(migrated, namespace)

    assert "emoji" not in migrated
    assert namespace["LOGGER"].emoji is RUNTIME_EMOJI_DEFAULT


def test_missing_imports_go_above_the_first_statement_that_uses_them():
    # #327: `import logging` used to go after the last top-level import,
    # below the LoggerSettings(...) call that now uses logging.INFO, so the
    # migrated config failed with NameError on every start.
    source = """\
CONFIG_VERSION = 2
from TwitchChannelPointsMiner.logger import LoggerSettings
LOGGER = LoggerSettings(save=False)
import os
MINER_CONFIG = {"username": "alice", "logger_settings": LOGGER}
STREAMERS = []
MINE_CONFIG = {}
ANALYTICS_CONFIG = None
"""

    migrated, _, _ = migrate_config_source(source)
    namespace = {}
    exec(migrated, namespace)

    lines = migrated.splitlines()
    logger_line = next(
        index for index, line in enumerate(lines) if line.startswith("LOGGER = ")
    )
    assert lines.index("import logging") < logger_line
    assert namespace["LOGGER"].console_level == logging.INFO


# What the migration before #327 wrote for the config above: the current
# CONFIG_VERSION, with the imports below the LoggerSettings(...) call using them.
EARLIER_LATE_IMPORT_OUTPUT = f"""\
CONFIG_VERSION = {CONFIG_VERSION}
from TwitchChannelPointsMiner.logger import LoggerSettings
LOGGER = LoggerSettings(save=False, console_level=logging.INFO, color_palette=ColorPalette())
import os
from TwitchChannelPointsMiner.classes.Settings import Priority
import logging
from TwitchChannelPointsMiner.logger import ColorPalette

MINER_CONFIG = {{"username": "alice", "logger_settings": LOGGER, "priority": [Priority.STREAK]}}
STREAMERS = []
MINE_CONFIG = {{}}
ANALYTICS_CONFIG = None
"""


def test_migrate_config_repairs_imports_an_earlier_migration_placed_too_late(
    tmp_path,
):
    # R327-16: that config already has the current CONFIG_VERSION, so without
    # a repair it raised NameError on every start after the upgrade too.
    config = tmp_path / "config.py"
    config.write_text(EARLIER_LATE_IMPORT_OUTPUT, encoding="utf-8")
    with pytest.raises(NameError):
        exec(EARLIER_LATE_IMPORT_OUTPUT, {})

    assert migrate_config(config) is True

    repaired = config.read_text(encoding="utf-8")
    namespace = {}
    exec(compile(repaired, "config.py", "exec", dont_inherit=True), namespace)
    assert namespace["LOGGER"].console_level == logging.INFO
    lines = repaired.splitlines()
    assert lines[2:4] == [
        "import logging",
        "from TwitchChannelPointsMiner.logger import ColorPalette",
    ]
    # Priority was imported above its first use already.
    assert repaired.count("import Priority") == 1
    backup = tmp_path / f"config.py.v{CONFIG_VERSION}.bak"
    assert backup.read_text(encoding="utf-8") == EARLIER_LATE_IMPORT_OUTPUT
    assert migrate_config(config) is False


def test_late_import_repair_ignores_a_function_binding_the_name():
    # R327-19: a function's parameter can't shadow the module's import.
    source = EARLIER_LATE_IMPORT_OUTPUT.replace(
        "ANALYTICS_CONFIG = None\n",
        "ANALYTICS_CONFIG = None\n\ndef helper(logging):\n    return logging\n",
    )

    migrated, _, _ = migrate_config_source(source)

    namespace = {}
    exec(compile(migrated, "config.py", "exec", dont_inherit=True), namespace)
    assert namespace["LOGGER"].console_level == logging.INFO


def test_an_older_config_with_a_late_import_starts_after_one_migration():
    # R327-20: the repair also runs while upgrading an older schema, so the
    # first start after the upgrade works.
    source = EARLIER_LATE_IMPORT_OUTPUT.replace(
        f"CONFIG_VERSION = {CONFIG_VERSION}\n",
        f"CONFIG_VERSION = {CONFIG_VERSION - 1}\n",
    )

    migrated, old_version, new_version = migrate_config_source(source)

    assert (old_version, new_version) == (CONFIG_VERSION - 1, CONFIG_VERSION)
    namespace = {}
    exec(compile(migrated, "config.py", "exec", dont_inherit=True), namespace)
    assert namespace["LOGGER"].console_level == logging.INFO
    assert migrated.count("import logging") == 2
    assert migrate_config_source(migrated)[0] == migrated


@pytest.mark.parametrize(
    ("rebinding", "repaired"),
    [
        # Bound above the use: the config's own value, left alone.
        ("from TwitchChannelPointsMiner.logger import LoggerSettings\n", False),
        # Bound after the use: it can't shadow it, so the import is repaired.
        ("ANALYTICS_CONFIG = None\n", True),
    ],
)
def test_late_import_repair_leaves_a_name_the_config_binds_above_its_use(
    rebinding, repaired
):
    source = EARLIER_LATE_IMPORT_OUTPUT.replace(
        rebinding, f"{rebinding}logging = None\n"
    )

    migrated, _, _ = migrate_config_source(source)

    assert migrated.count("import logging") == (2 if repaired else 1)
    assert migrated.count("import ColorPalette") == 2


_CONFIG_TAIL = (
    'MINER_CONFIG = {"username": "alice"}\n'
    "STREAMERS = []\n"
    "MINE_CONFIG = {}\n"
    "ANALYTICS_CONFIG = None\n"
)


@pytest.mark.parametrize(
    "use",
    [
        # A function body runs only when it's called, after the import.
        "def make_level():\n    return logging.WARNING\n",
        "LEVEL = lambda: logging.WARNING\n",
        "LEVELS = (logging.INFO for _ in (1,))\n",
        # The config's own fallback: moving the import would change LEVEL.
        "try:\n    LEVEL = logging.INFO\nexcept NameError:\n    LEVEL = 30\n",
        "try:\n    LEVEL = logging.INFO\nexcept (ValueError, Exception):\n    LEVEL = 30\n",
        # R327-23: a function the module only defines, or that binds the name
        # itself, and a class with its own import.
        "def make_level():\n    return logging.INFO\nHANDLER = make_level\n",
        "def make_level(logging=None):\n    return logging\nLEVEL = make_level()\n",
        "class Levels:\n    import logging\n    INFO = logging.INFO\n",
    ],
)
def test_late_import_repair_leaves_references_that_dont_run_first(use):
    # R327-21: such a config starts as it is, so it must not be rewritten
    # (a read-only config would then stop the miner from starting).
    source = f"CONFIG_VERSION = {CONFIG_VERSION}\n{use}import logging\n{_CONFIG_TAIL}"

    migrated, _, _ = migrate_config_source(source)

    assert migrated == source


@pytest.mark.parametrize(
    "use",
    [
        # Each of these runs as the module loads, where an earlier migration
        # could have written the reference.
        "LEVEL = (lambda level=logging.INFO: level)()\n",
        "def make_level(level=logging.INFO):\n    return level\nLEVEL = make_level()\n",
        "if True:\n    LEVEL = logging.INFO\n",
        "class Levels:\n    LEVEL = logging.INFO\nLEVEL = Levels.LEVEL\n",
        "LEVEL = [logging.INFO for _ in (1,)][0]\n",
        "try:\n    LEVEL = logging.INFO\nexcept ValueError:\n    LEVEL = 30\n",
        # R327-23: a function body runs when the module calls it.
        "def make_level():\n    return logging.INFO\nLEVEL = make_level()\n",
        "def outer():\n    return inner()\ndef inner():\n    return logging.INFO\n"
        "LEVEL = outer()\n",
        "def deco(function):\n    function.level = logging.INFO\n    return function\n"
        "@deco\ndef f():\n    pass\nLEVEL = f.level\n",
        # A handler can't catch what else or finally raises.
        "try:\n    pass\nexcept NameError:\n    pass\nelse:\n    LEVEL = logging.INFO\n",
        "try:\n    pass\nexcept NameError:\n    pass\nfinally:\n    LEVEL = logging.INFO\n",
    ],
)
def test_late_import_repair_covers_references_that_run_as_the_module_loads(use):
    # R327-22
    source = f"CONFIG_VERSION = {CONFIG_VERSION}\n{use}import logging\n{_CONFIG_TAIL}"
    with pytest.raises(NameError):
        exec(compile(source, "config.py", "exec", dont_inherit=True), {})

    migrated, _, _ = migrate_config_source(source)

    namespace = {}
    exec(compile(migrated, "config.py", "exec", dont_inherit=True), namespace)
    assert namespace["LEVEL"] == logging.INFO


@pytest.mark.skipif(
    sys.version_info >= (3, 14), reason="annotations are evaluated lazily"
)
def test_late_import_repair_covers_an_eager_argument_annotation():
    # R327-23: without the future import, an annotation runs at the def.
    source = (
        f"CONFIG_VERSION = {CONFIG_VERSION}\n"
        "def helper(value: logging.INFO = 0):\n    return value\n"
        "LEVEL = helper.__annotations__['value']\n"
        f"import logging\n{_CONFIG_TAIL}"
    )

    migrated, _, _ = migrate_config_source(source)

    namespace = {}
    exec(compile(migrated, "config.py", "exec", dont_inherit=True), namespace)
    assert namespace["LEVEL"] == logging.INFO


def test_late_import_repair_skips_a_postponed_annotation():
    source = (
        f"from __future__ import annotations\nCONFIG_VERSION = {CONFIG_VERSION}\n"
        "def helper(value: logging.INFO = 0):\n    return value\n"
        f"import logging\n{_CONFIG_TAIL}"
    )

    assert migrate_config_source(source)[0] == source


def test_unversioned_config_keeps_its_docstring_and_future_import_first():
    # #327: CONFIG_VERSION used to go above both, a SyntaxError for the
    # __future__ import and no longer a docstring.
    source = '''\
"""Alice's miner."""
from __future__ import annotations
from TwitchChannelPointsMiner.logger import LoggerSettings
MINER_CONFIG = {"username": "alice", "logger_settings": LoggerSettings(save=False)}
STREAMERS = []
MINE_CONFIG = {}
ANALYTICS_CONFIG = None
'''

    migrated, version, _ = migrate_config_source(source)
    exec(compile(migrated, "config.py", "exec", dont_inherit=True), {})

    assert version == 0
    lines = migrated.splitlines()
    assert lines[:3] == [
        '"""Alice\'s miner."""',
        "from __future__ import annotations",
        f"CONFIG_VERSION = {CONFIG_VERSION}",
    ]
    assert ast.get_docstring(ast.parse(migrated)) == "Alice's miner."


def test_unversioned_config_keeps_its_shebang_and_encoding_lines_first():
    source = """\
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
MINER_CONFIG = {"username": "alice"}
STREAMERS = []
MINE_CONFIG = {}
ANALYTICS_CONFIG = None
"""

    migrated, _, _ = migrate_config_source(source)
    exec(compile(migrated, "config.py", "exec", dont_inherit=True), {})

    lines = migrated.splitlines()
    assert lines[:2] == ["#!/usr/bin/env python3", "# -*- coding: utf-8 -*-"]
    # Imports and CONFIG_VERSION both go below them, not between them.
    assert lines.index(f"CONFIG_VERSION = {CONFIG_VERSION}") > 1
    assert all(not line.startswith(("import ", "from ")) for line in lines[:2])


def test_missing_imports_go_above_a_decorated_function_that_uses_them():
    # R327-03: a decorated def starts at its first decorator, which is where
    # the migrated LoggerSettings(...) runs.
    source = """\
CONFIG_VERSION = 2
from TwitchChannelPointsMiner.logger import LoggerSettings
def keep(settings):
    return lambda function: function
@keep(LoggerSettings(save=False))
def unused():
    pass
import os
MINER_CONFIG = {"username": "alice"}
STREAMERS = []
MINE_CONFIG = {}
ANALYTICS_CONFIG = None
"""

    migrated, _, _ = migrate_config_source(source)

    exec(compile(migrated, "config.py", "exec", dont_inherit=True), {})
    lines = migrated.splitlines()
    assert lines.index("import logging") < lines.index("def keep(settings):")


@pytest.mark.parametrize(
    "header", ['"""Doc."""; ', "from __future__ import annotations; "]
)
def test_migration_inserts_before_a_statement_sharing_the_header_line(header):
    # R327-02: MINER_CONFIG shares the header's last line, so CONFIG_VERSION
    # and the imports it needs go before it on that line, not below it.
    source = (
        header
        + "MINER_CONFIG = {'username': 'alice', 'priority': []}\n"
        + "STREAMERS = []\nMINE_CONFIG = {}\nANALYTICS_CONFIG = None\n"
    )

    migrated, _, _ = migrate_config_source(source)

    namespace = {}
    exec(compile(migrated, "config.py", "exec", dont_inherit=True), namespace)
    assert namespace["CONFIG_VERSION"] == CONFIG_VERSION
    if header.startswith('"""'):
        assert ast.get_docstring(ast.parse(migrated)) == "Doc."


def test_invalid_generated_output_does_not_recover_from_a_backup(tmp_path, monkeypatch):
    # R327-06: when the migration's own output doesn't compile (forced here),
    # the file is still valid, so it must not be replaced by migrating an
    # older backup.
    from TwitchChannelPointsMiner import config_migration

    apply_edits = config_migration._apply_edits
    monkeypatch.setattr(
        config_migration,
        "_apply_edits",
        lambda source, edits: apply_edits(source, edits) + ")\n",
    )
    config = tmp_path / "config.py"
    config.write_text(
        'MINER_CONFIG = {"username": "alice",\n'
        '    "claim_drops_startup": True,\n'
        "}\n"
        "STREAMERS = []\n"
        "MINE_CONFIG = {}\n"
        "ANALYTICS_CONFIG = None\n",
        encoding="utf-8",
    )
    backup = tmp_path / "config.py.v0.bak"
    backup.write_text(CONFIG, encoding="utf-8")
    before = config.read_bytes()
    backup_before = backup.read_bytes()

    with pytest.raises(GeneratedConfigError, match="generated invalid"):
        migrate_config(config)

    assert config.read_bytes() == before
    assert backup.read_bytes() == backup_before
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "config.py",
        "config.py.v0.bak",
    ]


@pytest.mark.parametrize(
    "miner_config",
    [
        '{"username": "alice", "password": ("secret")}',
        '{"username": "alice", "password": ( ( "secret" ) ) }',
        '{\n    "username": "alice",\n    "password": (  # note (\n'
        '        "secret"\n    )\n}',
        '{\n    "username": "alice",\n    "password": ("secret"),\n}',
        '{\n    "username": "alice",\n    "password": (\n        "secret"  #### ) ##\n'
        "    \\\n    )\n}",
    ],
)
def test_a_parenthesized_password_is_removed_at_the_current_version(
    tmp_path, miner_config
):
    # R327-07: a config already at CONFIG_VERSION only gets its password
    # removed. The removal must take the value's parentheses with it, so the
    # result is valid and saved, not refused on every start.
    config = tmp_path / "config.py"
    config.write_text(
        f"CONFIG_VERSION = {CONFIG_VERSION}\n"
        f"MINER_CONFIG = {miner_config}\n"
        "STREAMERS = []\n"
        "MINE_CONFIG = {}\n"
        "ANALYTICS_CONFIG = None\n",
        encoding="utf-8",
    )

    assert migrate_config(config) is True

    migrated = config.read_text(encoding="utf-8")
    assert "secret" not in migrated
    namespace = {}
    exec(compile(migrated, "config.py", "exec", dont_inherit=True), namespace)
    assert namespace["MINER_CONFIG"] == {"username": "alice"}
    backup = tmp_path / f"config.py.v{CONFIG_VERSION}.bak"
    assert "secret" not in backup.read_text(encoding="utf-8")
    # The saved file needs no further edit, so the next start is a no-op.
    assert migrate_config(config) is False


@pytest.mark.parametrize(
    "last_entry, expected",
    [
        ('"password": ("secret")\n', {}),
        ('"password": ("secret"),\n', {}),
        ('"other": ("x")\n', {"other": "x"}),
        ('"other": ( ("x")  # note )\n    )\n', {"other": "x"}),
    ],
)
def test_an_older_config_whose_last_entry_is_parenthesized_migrates(
    last_entry, expected
):
    # Defaults are added after the last entry. Its comma goes after the
    # parentheses (not inside, where it makes a tuple), and it survives the
    # password's removal when the password is that last entry.
    source = (
        "CONFIG_VERSION = 11\n"
        "MINER_CONFIG = {\n"
        '    "username": "alice",\n'
        f"    {last_entry}"
        "}\n"
        "STREAMERS = []\n"
        "MINE_CONFIG = {}\n"
        "ANALYTICS_CONFIG = None\n"
    )

    migrated, _, _ = migrate_config_source(source)

    assert "secret" not in migrated
    namespace = {}
    exec(compile(migrated, "config.py", "exec", dont_inherit=True), namespace)
    config = namespace["MINER_CONFIG"]
    assert config["username"] == "alice"
    assert "password" not in config
    for name, value in expected.items():
        assert config[name] == value
    assert migrate_config_source(migrated)[0] == migrated


@pytest.mark.parametrize(
    "first_lines",
    [
        '"""Doc."""; \\\n    MINER_CONFIG = {"username": "alice"}\n',
        '"""Doc."""; \\\r\n    MINER_CONFIG = {"username": "alice"}\r\n',
        'from __future__ import annotations; \\\n  MINER_CONFIG = {"username": "alice"}\n',
        'import os; \\\n  MINER_CONFIG = {"username": "alice"}\n',
        'CONFIG_VERSION = 5; \\\n  MINER_CONFIG = {"username": "alice"}\n',
        # A backslash joins a blank or comment line to the header's line.
        '"""Doc.""" \\\n\nMINER_CONFIG = {"username": "alice"}\n',
        '"""Doc.""" \\\n# note\nMINER_CONFIG = {"username": "alice"}\n',
        '"""Doc.""" \\\r\n\r\nMINER_CONFIG = {"username": "alice"}\r\n',
        'import os \\\n\nMINER_CONFIG = {"username": "alice"}\n',
        'CONFIG_VERSION = 5 \\\n\nMINER_CONFIG = {"username": "alice"}\n',
        'from __future__ import annotations \\\n\nMINER_CONFIG = {"username": "alice"}\n',
        # A backslash in a comment continues nothing.
        '"""Doc.""" # not \\\nMINER_CONFIG = {"username": "alice"}\n',
    ],
)
def test_a_header_continued_onto_the_next_line_migrates(first_lines):
    # The first statement shares the header's logical line past a backslash
    # continuation; inserted statements must not split that logical line.
    source = first_lines + "STREAMERS = []\nMINE_CONFIG = {}\nANALYTICS_CONFIG = None\n"

    migrated, _, _ = migrate_config_source(source)

    namespace = {}
    exec(compile(migrated, "config.py", "exec", dont_inherit=True), namespace)
    assert namespace["MINER_CONFIG"]["username"] == "alice"
    assert namespace["CONFIG_VERSION"] == CONFIG_VERSION
    assert migrate_config_source(migrated)[0] == migrated


def test_a_dict_whose_first_entry_shares_the_opening_line_migrates():
    # The dashboard writes this layout when it adds to a one-line dict. The
    # added defaults take the closing line's indent plus four spaces.
    source = (
        'MINER_CONFIG = {"username": "alice"}\n'
        "STREAMERS = []\n"
        'MINE_CONFIG = {"categories": [],\n'
        '    "category_sort": "RANDOM",\n'
        "}\n"
        "ANALYTICS_CONFIG = None\n"
    )

    migrated, _, _ = migrate_config_source(source)

    namespace = {}
    exec(compile(migrated, "config.py", "exec", dont_inherit=True), namespace)
    assert namespace["MINE_CONFIG"]["category_sort"] == "RANDOM"
    assert "\n    'blacklist': [],\n" in migrated
    assert migrate_config_source(migrated)[0] == migrated


def test_closing_paren_scan_is_linear_on_a_run_of_hashes():
    # CodeQL: overlapping comment alternatives backtracked exponentially on
    # a long run of `#` with no `)` after it.
    import time

    from TwitchChannelPointsMiner import config_migration

    started = time.perf_counter()
    assert config_migration._CLOSING_PAREN.match("#" * 50000) is None
    assert config_migration._CLOSING_PAREN.match(" #" + "#" * 50000 + "\n") is None
    assert time.perf_counter() - started < 1
    # A `)` inside a comment never counts; the one on the next line does.
    assert config_migration._CLOSING_PAREN.match("  # c )") is None
    match = config_migration._CLOSING_PAREN.match("  # ) ##\n  )")
    assert match is not None and match.end() == len("  # ) ##\n  )")


@pytest.mark.parametrize(
    "entries, expected",
    [
        ('{("password"): "s", "username": "a"}', {"username": "a"}),
        (
            '{"username": "a", ("password") : ("s"), ("x"): 1}',
            {"username": "a", "x": 1},
        ),
        ('{"password": "s", **EXTRA}', {"extra": True}),
        ('{"username": "a", "password": "s"  # a, b\n}', {"username": "a"}),
    ],
)
def test_password_removal_keeps_neighbouring_entries_intact(entries, expected):
    source = (
        f"CONFIG_VERSION = {CONFIG_VERSION}\n"
        'EXTRA = {"extra": True}\n'
        f"MINER_CONFIG = {entries}\n"
    )

    migrated, _, _ = migrate_config_source(source)

    namespace = {}
    exec(compile(migrated, "config.py", "exec", dont_inherit=True), namespace)
    assert namespace["MINER_CONFIG"] == expected


@pytest.mark.parametrize(
    "late_import",
    [
        "import logging",
        "from TwitchChannelPointsMiner.logger import ColorPalette",
        "from TwitchChannelPointsMiner.classes.Settings import Priority",
        "from TwitchChannelPointsMiner.classes.Settings import StreamerSource",
    ],
)
def test_an_import_below_its_first_use_does_not_count_as_present(late_import):
    # R327-08: the config already imports the name, but only below the
    # statement the migration adds it to, so it must be imported above that.
    source = (
        "from TwitchChannelPointsMiner.logger import LoggerSettings\n"
        "LOGGER = LoggerSettings(save=False)\n"
        'MINER_CONFIG = {"username": "alice"}\n'
        f"{late_import}\n"
    )

    migrated, _, _ = migrate_config_source(source)

    namespace = {}
    exec(compile(migrated, "config.py", "exec", dont_inherit=True), namespace)
    assert namespace["MINER_CONFIG"]["username"] == "alice"
    assert namespace["LOGGER"].save is False


def test_defaults_are_not_given_a_name_a_function_annotates_locally():
    # R327-17: an annotation without a value binds nothing in the module, but
    # in a function it makes the name local, so logging.INFO written into
    # that function would raise UnboundLocalError.
    source = (
        "from TwitchChannelPointsMiner.logger import LoggerSettings\n"
        "\n"
        "def make_logger():\n"
        "    settings = LoggerSettings(save=False)\n"
        "    logging: object\n"
        "    return settings\n"
        "\n"
        'MINER_CONFIG = {"username": "alice", "logger_settings": make_logger()}\n'
    )

    migrated, _, _ = migrate_config_source(source)

    assert "logging.INFO" not in migrated
    namespace = {}
    exec(compile(migrated, "config.py", "exec", dont_inherit=True), namespace)
    assert namespace["MINER_CONFIG"]["logger_settings"].save is False


def test_defaults_are_not_given_names_a_factory_binds_locally():
    # R327-10: `import logging` inside the function makes logging local to
    # all of it, so a module-level import can't supply logging.INFO above it.
    # Those defaults are left to LoggerSettings; the others are still added.
    source = (
        "from TwitchChannelPointsMiner.logger import LoggerSettings\n"
        "\n"
        "def make_logger():\n"
        "    settings = LoggerSettings(save=False)\n"
        "    import logging\n"
        "    return settings\n"
        "\n"
        "def make_colors():\n"
        "    ColorPalette = None\n"
        "    return LoggerSettings(save=False)\n"
        "\n"
        'MINER_CONFIG = {"username": "alice", "logger_settings": make_logger()}\n'
        "COLORS = make_colors()\n"
    )

    migrated, _, _ = migrate_config_source(source)

    namespace = {}
    exec(compile(migrated, "config.py", "exec", dont_inherit=True), namespace)
    settings = namespace["MINER_CONFIG"]["logger_settings"]
    assert settings.save is False
    assert settings.less is False
    # Any binding other than a plain top-level import keeps the defaults
    # using that name out of the whole file.
    assert "logging.INFO" not in migrated
    assert "color_palette=" not in migrated
    assert "less=False" in migrated


@pytest.mark.parametrize(
    "binding",
    [
        "match 0:\n        case logging:\n            pass",
        "if False:\n        del logging",
        "for logging in ():\n        pass",
        "try:\n        pass\n    except Exception as logging:\n        pass",
    ],
)
def test_every_local_binding_form_keeps_the_logging_defaults_out(binding):
    # R327-11: a match capture or `del` makes logging local to the whole
    # function too, so logging.INFO would raise UnboundLocalError there.
    source = (
        "import functools\n"
        "from TwitchChannelPointsMiner.logger import LoggerSettings\n"
        "\n"
        "@functools.lru_cache\n"
        "def make_logger():\n"
        "    settings = LoggerSettings(save=False)\n"
        f"    {binding}\n"
        "    return settings\n"
        "\n"
        'MINER_CONFIG = {"username": "alice", "logger_settings": make_logger()}\n'
    )

    migrated, _, _ = migrate_config_source(source)

    namespace = {}
    exec(compile(migrated, "config.py", "exec", dont_inherit=True), namespace)
    assert namespace["MINER_CONFIG"]["logger_settings"].save is False
    assert "logging.INFO" not in migrated.split("MINER_CONFIG")[0]


def test_a_global_declaration_keeps_the_logging_defaults():
    source = (
        "from TwitchChannelPointsMiner.logger import LoggerSettings\n"
        "\n"
        "def make_logger():\n"
        "    global logging\n"
        "    return LoggerSettings(save=False)\n"
        "\n"
        'MINER_CONFIG = {"username": "alice", "logger_settings": make_logger()}\n'
    )

    migrated, _, _ = migrate_config_source(source)

    namespace = {}
    exec(compile(migrated, "config.py", "exec", dont_inherit=True), namespace)
    assert namespace["MINER_CONFIG"]["logger_settings"].save is False
    assert "console_level=logging.INFO" in migrated


@pytest.mark.parametrize(
    "logger_expression, prelude",
    [
        (
            "make_logger()",
            (
                "def make_logger():\n"
                "    return next(LoggerSettings(save=False) for logging in (None,))\n"
            ),
        ),
        ("[LoggerSettings(save=False) for logging in (None,)][0]", ""),
        (
            "Holder.settings",
            (
                "class Holder:\n"
                "    logging = None\n"
                "    settings = LoggerSettings(save=False)\n"
            ),
        ),
        ("[[LoggerSettings(save=False) for _ in (1,)] for logging in (2,)][0][0]", ""),
    ],
)
def test_comprehension_and_class_bindings_keep_the_logging_defaults_out(
    logger_expression, prelude
):
    # R327-12: a comprehension target or a class attribute named logging
    # shadows the module-level import where the LoggerSettings call runs.
    source = (
        "from TwitchChannelPointsMiner.logger import LoggerSettings\n"
        "\n"
        f"{prelude}"
        f'MINER_CONFIG = {{"username": "alice", "logger_settings": {logger_expression}}}\n'
    )

    migrated, _, _ = migrate_config_source(source)

    namespace = {}
    exec(compile(migrated, "config.py", "exec", dont_inherit=True), namespace)
    assert namespace["MINER_CONFIG"]["logger_settings"].save is False


@pytest.mark.skipif(sys.version_info < (3, 12), reason="type parameters need 3.12")
@pytest.mark.parametrize(
    "definition, call",
    [
        (
            "def make_logger[logging]():\n    return LoggerSettings(save=False)\n",
            "make_logger()",
        ),
        (
            "class Make[logging]:\n    settings = LoggerSettings(save=False)\n",
            "Make.settings",
        ),
        ("type Make[logging] = LoggerSettings(save=False)\n", "Make.__value__"),
        ("type Make[ColorPalette] = LoggerSettings(save=False)\n", "Make.__value__"),
    ],
)
def test_type_parameters_keep_the_logging_defaults_out(definition, call):
    # R327-13, R327-14: a type parameter named logging (or ColorPalette)
    # shadows the import inside the def, class or type alias.
    source = (
        "from TwitchChannelPointsMiner.logger import LoggerSettings\n"
        "\n"
        f"{definition}"
        f'MINER_CONFIG = {{"username": "alice", "logger_settings": {call}}}\n'
    )

    migrated, _, _ = migrate_config_source(source)

    namespace = {}
    exec(compile(migrated, "config.py", "exec", dont_inherit=True), namespace)
    assert namespace["MINER_CONFIG"]["logger_settings"].save is False


@pytest.mark.parametrize(
    "binding",
    ["logging = None", "import os as logging", "from os import path as logging"],
)
def test_a_module_level_rebinding_keeps_the_logging_defaults_out(binding):
    # The added `import logging` can't be relied on when the config binds
    # logging to something else itself.
    source = (
        f"{binding}\n"
        "from TwitchChannelPointsMiner.logger import LoggerSettings\n"
        "\n"
        'MINER_CONFIG = {"username": "alice", "logger_settings": LoggerSettings()}\n'
        'MINE_CONFIG = {"categories": []}\n'
    )

    migrated, _, _ = migrate_config_source(source)

    namespace = {}
    exec(compile(migrated, "config.py", "exec", dont_inherit=True), namespace)
    assert "logging.INFO" not in migrated
    assert "logging.DEBUG" not in migrated
    assert "'category_log_level'" not in migrated
    assert namespace["MINE_CONFIG"]["categories"] == []


def test_a_rebound_enum_name_is_not_used_for_new_priority_lists():
    source = (
        "def Priority():\n" "    pass\n" "\n" 'MINER_CONFIG = {"username": "alice"}\n'
    )

    migrated, _, _ = migrate_config_source(source)

    namespace = {}
    exec(compile(migrated, "config.py", "exec", dont_inherit=True), namespace)
    assert "priority" not in namespace["MINER_CONFIG"]
    assert len(namespace["MINER_CONFIG"]["streamer_source_priority"]) == 5


def test_no_import_replaces_a_name_the_config_binds_itself():
    # R327-15: logging is the config's own string and every logging default
    # is already set, so the migration must not import logging over it.
    source = (
        "logging = 'alice'\n"
        "from TwitchChannelPointsMiner.logger import LoggerSettings\n"
        "LOGGER = LoggerSettings(console_level=20, file_level=10, color_palette=None)\n"
        'MINER_CONFIG = {"username": logging.upper()}\n'
        "Email = 'mine'\n"
    )

    migrated, _, _ = migrate_config_source(source)

    namespace = {}
    exec(compile(migrated, "config.py", "exec", dont_inherit=True), namespace)
    assert namespace["MINER_CONFIG"]["username"] == "ALICE"
    assert namespace["Email"] == "mine"
    assert "import logging" not in migrated
    assert "import ColorPalette" not in migrated
    assert "import Email" not in migrated


@pytest.mark.parametrize(
    "last_import",
    ["import os;", "import os ;  # comment", "from __future__ import annotations;"],
)
def test_imports_are_added_after_an_import_ending_in_a_semicolon(last_import):
    # R327-18: the `;` stays with its import instead of being left alone on a
    # line after the inserted imports.
    source = f'{last_import}\nMINER_CONFIG = {{"username": "alice"}}\n'

    migrated, _, _ = migrate_config_source(source)

    namespace = {}
    exec(compile(migrated, "config.py", "exec", dont_inherit=True), namespace)
    assert len(namespace["MINER_CONFIG"]["priority"]) == 4


def test_a_priority_imported_from_elsewhere_is_not_used_for_new_lists():
    # The name is bound, but not by the import the default relies on.
    source = (
        "from enum import Enum as Priority\n" 'MINER_CONFIG = {"username": "alice"}\n'
    )

    migrated, _, _ = migrate_config_source(source)

    namespace = {}
    exec(compile(migrated, "config.py", "exec", dont_inherit=True), namespace)
    assert "priority" not in namespace["MINER_CONFIG"]


def test_a_wildcard_from_another_module_keeps_the_dependent_defaults_out():
    source = (
        "from os.path import *\n"
        "from TwitchChannelPointsMiner.logger import LoggerSettings\n"
        'MINER_CONFIG = {"username": "alice", "logger_settings": LoggerSettings()}\n'
    )

    migrated, _, _ = migrate_config_source(source)

    namespace = {}
    exec(compile(migrated, "config.py", "exec", dont_inherit=True), namespace)
    assert "logging.INFO" not in migrated
    assert "priority" not in namespace["MINER_CONFIG"]
    assert namespace["MINER_CONFIG"]["logger_settings"].less is False


def test_a_rebound_priority_warns_that_the_default_list_was_not_added(caplog):
    source = 'def Priority():\n    pass\n\nMINER_CONFIG = {"username": "alice"}\n'

    with caplog.at_level(logging.WARNING):
        migrated, _, _ = migrate_config_source(source)

    assert "'priority'" not in migrated
    assert "may bind Priority itself" in caplog.text
    assert "without FAVORITE" in caplog.text


@pytest.mark.parametrize(
    "value, expected",
    [
        ("[logging.INFO]", True),
        ("(ColorPalette.DEFAULT,)", True),
        ("dict(level=logging.INFO)", True),
        ("logging.INFO", True),
        ("'logging'", False),
        ("[Priority.STREAK]", False),
    ],
)
def test_uses_any_reads_every_name_in_a_default(value, expected):
    from TwitchChannelPointsMiner.config_migration import _uses_any

    assert _uses_any(value, {"logging", "ColorPalette"}) is expected


@pytest.mark.parametrize(
    "last_import", ["import os  # comment", "import os;  # comment"]
)
def test_imports_are_added_below_an_import_with_a_trailing_comment(last_import):
    source = f'{last_import}\nMINER_CONFIG = {{"username": "alice"}}\n'

    migrated, _, _ = migrate_config_source(source)

    lines = migrated.split("\n")
    assert lines[0] == f"CONFIG_VERSION = {CONFIG_VERSION}"
    assert lines[1] == last_import
    assert lines[2].startswith("from TwitchChannelPointsMiner.classes.Settings")
    namespace = {}
    exec(compile(migrated, "config.py", "exec", dont_inherit=True), namespace)
    assert len(namespace["MINER_CONFIG"]["priority"]) == 4


def _example_at_version(version):
    source = (Path(__file__).resolve().parent.parent / "config.example.py").read_text(
        encoding="utf-8"
    )
    replacement = "" if version == 0 else f"CONFIG_VERSION = {version}\n"
    old, count = re.subn(r"^CONFIG_VERSION = \d+\n", replacement, source, flags=re.M)
    assert count == 1
    return old


@pytest.mark.parametrize("version", [0, 5, 9, 11])
def test_config_example_migrates_from_older_versions_by_adding_only_defaults(
    version,
):
    # Golden test: config.example.py migrated from an older version is the
    # same file with CONFIG_VERSION updated and only these lines inserted.
    # The output is the same as before #327's placement changes.
    old = _example_at_version(version)

    migrated, old_version, new_version = migrate_config_source(old)

    assert (old_version, new_version) == (version, CONFIG_VERSION)
    expected_lines = [
        "        username=None,\n",
        "    'blacklist': [],\n",
        "    'track_category_streamer_points': False,\n",
    ]
    if version:
        old = old.replace(
            f"CONFIG_VERSION = {version}\n", f"CONFIG_VERSION = {CONFIG_VERSION}\n"
        )
    else:
        # An unversioned file gets CONFIG_VERSION below its coding line.
        expected_lines.insert(0, f"CONFIG_VERSION = {CONFIG_VERSION}\n")
        assert migrated.split("\n")[:2] == [
            "# -*- coding: utf-8 -*-",
            f"CONFIG_VERSION = {CONFIG_VERSION}",
        ]
    matcher = difflib.SequenceMatcher(
        a=old.splitlines(keepends=True),
        b=migrated.splitlines(keepends=True),
        autojunk=False,
    )
    changes = [opcode for opcode in matcher.get_opcodes() if opcode[0] != "equal"]
    assert {opcode[0] for opcode in changes} == {"insert"}
    inserted = [line for _, _, _, j1, j2 in changes for line in matcher.b[j1:j2]]
    assert inserted == expected_lines
    # Migrating the output again changes nothing.
    assert migrate_config_source(migrated)[0] == migrated


@pytest.mark.parametrize(
    "source",
    [
        CONFIG,
        "#!/usr/bin/env python3\n# -*- coding: utf-8 -*-\n"
        '"""Doc."""\nfrom __future__ import annotations\n'
        'MINER_CONFIG = {"username": "alice"}\n',
        '"""Doc."""; MINER_CONFIG = {"username": "alice"}\n',
        "import os  # comment\n"
        "from TwitchChannelPointsMiner.logger import LoggerSettings\n"
        'MINER_CONFIG = {"username": "alice", "logger_settings": LoggerSettings()}\n'
        'MINE_CONFIG = {"categories": []}\n',
    ],
)
def test_migrating_the_migrations_own_output_is_a_no_op(source):
    migrated, _, _ = migrate_config_source(source)

    assert migrate_config_source(migrated) == (
        migrated,
        CONFIG_VERSION,
        CONFIG_VERSION,
    )


def test_non_ascii_text_before_a_semicolon_shared_header_keeps_offsets():
    # ast columns are UTF-8 byte offsets; the inserts must land on characters.
    source = '"""Dökümän für Ålice."""; MINER_CONFIG = {"username": "ålice"}\n'

    migrated, _, _ = migrate_config_source(source)

    assert migrated.startswith(
        f'"""Dökümän für Ålice."""; CONFIG_VERSION = {CONFIG_VERSION}\n'
    )
    namespace = {}
    exec(compile(migrated, "config.py", "exec", dont_inherit=True), namespace)
    assert namespace["__doc__"] == "Dökümän für Ålice."
    assert namespace["MINER_CONFIG"]["username"] == "ålice"
    assert len(namespace["MINER_CONFIG"]["priority"]) == 4


@pytest.mark.parametrize("newline", ["\r\n", "\r"])
def test_crlf_and_cr_configs_migrate_with_their_header_first(tmp_path, newline):
    source = newline.join(
        [
            "# -*- coding: utf-8 -*-",
            '"""Doc."""',
            "import os  # comment",
            "from TwitchChannelPointsMiner.logger import LoggerSettings",
            'MINER_CONFIG = {"username": "alice", "logger_settings": LoggerSettings()}',
            "STREAMERS = []",
            'MINE_CONFIG = {"categories": []}',
            "ANALYTICS_CONFIG = None",
            "",
        ]
    )
    header = [
        "# -*- coding: utf-8 -*-",
        '"""Doc."""',
        f"CONFIG_VERSION = {CONFIG_VERSION}",
        "import os  # comment",
    ]

    # The source itself: the user's lines keep their line endings.
    migrated, _, _ = migrate_config_source(source)

    assert re.split(r"\r\n|\r|\n", migrated)[:4] == header
    assert f"import os  # comment{newline}" in migrated
    namespace = {}
    exec(compile(migrated, "config.py", "exec", dont_inherit=True), namespace)
    assert namespace["MINER_CONFIG"]["logger_settings"].console_level == logging.INFO
    assert migrate_config_source(migrated)[0] == migrated

    # The file: read with universal newlines, so it is saved with LF endings.
    config = tmp_path / "config.py"
    config.write_bytes(source.encode("utf-8"))

    assert migrate_config(config) is True
    assert config.read_bytes().decode("utf-8").split("\n")[:4] == header
    assert migrate_config(config) is False
