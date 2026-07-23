import hashlib
import inspect
import stat

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
    convert_runner,
    convert_runner_source,
    migrate_config,
    migrate_config_source,
)
from TwitchChannelPointsMiner.TwitchChannelPointsMiner import TwitchChannelPointsMiner
from TwitchChannelPointsMiner.classes.entities.Bet import BetSettings
from TwitchChannelPointsMiner.classes.entities.Streamer import StreamerSettings
from TwitchChannelPointsMiner.logger import LoggerSettings


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
    assert namespace["DEFAULT_CONFIG"]["priority"][-1] is namespace[
        "Priority"
    ].FAVORITE
    assert namespace["DEFAULT_SETTINGS"].points_limit is None


def test_version_one_migration_adds_missing_options_and_follower_source_last():
    source = '''\
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
'''

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
    ]
    for name, _ in MINER_CONFIG_DEFAULTS:
        assert name in namespace["MINER_CONFIG"]
    assert namespace["MINE_CONFIG"]["followers"] is True
    for name, _ in MINE_CONFIG_DEFAULTS:
        assert name in namespace["MINE_CONFIG"]


def test_migration_preserves_aliased_priority_enum_prefixes():
    source = '''\
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
'''

    migrated, _, _ = migrate_config_source(source)
    namespace = {}
    exec(migrated, namespace)

    assert namespace["PRIORITIES"] == [namespace["P"].ORDER, namespace["P"].FAVORITE]
    assert namespace["SOURCES"] == [
        namespace["SS"].STREAMERS,
        namespace["SS"].BADGES,
        namespace["SS"].FOLLOWERS,
        namespace["SS"].CATEGORIES,
    ]
    assert migrated.count("P.FAVORITE") == 1
    assert "SS.FOLLOWERS" in migrated
    assert "SS.CATEGORIES" in migrated


def test_version_two_migration_normalizes_logger_settings():
    source = '''\
CONFIG_VERSION = 2
import logging
from TwitchChannelPointsMiner.logger import ColorPalette, LoggerSettings
LOGGER = LoggerSettings(save=False, colored=True)
MINER_CONFIG = {"username": "alice", "logger_settings": LOGGER}
STREAMERS = []
MINE_CONFIG = {}
ANALYTICS_CONFIG = None
'''

    migrated, old_version, new_version = migrate_config_source(source)
    namespace = {}
    exec(migrated, namespace)

    assert old_version == 2
    assert new_version == CONFIG_VERSION
    settings = namespace["LOGGER"]
    assert settings.save is False
    assert settings.colored is True
    assert {name for name, _ in LOGGER_SETTINGS_DEFAULTS} | set(
        LOGGER_NOTIFICATION_SETTINGS
    ) == set(settings.__slots__)
    assert settings.console_level == namespace["logging"].INFO
    assert settings.console_username is False
    assert settings.time_zone is None
    assert settings.date_format == "dd/mm/yy"
    assert settings.telegram is None
    assert settings.gotify is None


def test_logger_settings_with_expanded_kwargs_is_not_modified():
    source = '''\
CONFIG_VERSION = 2
from TwitchChannelPointsMiner.logger import LoggerSettings
LOGGER_OVERRIDES = {"save": False}
LOGGER = LoggerSettings(**LOGGER_OVERRIDES)
MINER_CONFIG = {"username": "alice", "logger_settings": LOGGER}
STREAMERS = []
MINE_CONFIG = {}
ANALYTICS_CONFIG = None
'''

    migrated, _, _ = migrate_config_source(source)

    assert "LoggerSettings(**LOGGER_OVERRIDES)" in migrated


def test_multiple_logger_settings_add_required_imports_once():
    source = '''\
CONFIG_VERSION = 2
from TwitchChannelPointsMiner.logger import LoggerSettings
FIRST_LOGGER = LoggerSettings(save=False)
SECOND_LOGGER = LoggerSettings(colored=True)
MINER_CONFIG = {"username": "alice", "logger_settings": FIRST_LOGGER}
STREAMERS = []
MINE_CONFIG = {}
ANALYTICS_CONFIG = None
'''

    migrated, _, _ = migrate_config_source(source)
    namespace = {}
    exec(migrated, namespace)

    assert migrated.count("import logging") == 1
    assert (
        migrated.count(
            "from TwitchChannelPointsMiner.logger import ColorPalette"
        )
        == 1
    )
    assert namespace["FIRST_LOGGER"].save is False
    assert namespace["SECOND_LOGGER"].colored is True


def test_version_three_adds_missing_source_priority_and_comments_notifications():
    source = '''\
CONFIG_VERSION = 3
from TwitchChannelPointsMiner.logger import LoggerSettings
LOGGER = LoggerSettings(
    save=False,
)
MINER_CONFIG = {"username": "alice", "logger_settings": LOGGER}
STREAMERS = []
MINE_CONFIG = {}
ANALYTICS_CONFIG = {"port": 6000}
'''

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
    ]
    for name in LOGGER_NOTIFICATION_SETTINGS:
        assert f"# {name}=" in migrated
    assert namespace["LOGGER"].telegram is None
    assert namespace["ANALYTICS_CONFIG"]["port"] == 6000
    for name, _ in ANALYTICS_CONFIG_DEFAULTS:
        assert name in namespace["ANALYTICS_CONFIG"]


def test_version_four_adds_daily_report_settings_and_email_template():
    source = '''\
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
'''

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
    assert {name for name, _ in LOGGER_SETTINGS_DEFAULTS} | set(
        LOGGER_NOTIFICATION_SETTINGS
    ) == parameters(LoggerSettings.__init__)
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
    source = '''\
from TwitchChannelPointsMiner.classes.Settings import Priority
from TwitchChannelPointsMiner.classes.entities.Streamer import StreamerSettings
PRIORITIES = [Priority.ORDER,]
DEFAULT_SETTINGS = StreamerSettings(watch_streak=False,)
MINER_CONFIG = {"priority": PRIORITIES, "streamer_settings": DEFAULT_SETTINGS}
STREAMERS = []
MINE_CONFIG = {}
ANALYTICS_CONFIG = None
'''

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
    source = '''\
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
'''

    migrated, _, _ = migrate_config_source(source)
    namespace = {}
    exec(migrated, namespace)

    assert namespace["DEFAULT_SETTINGS"].bet.max_points == 1234
    assert namespace["DEFAULT_SETTINGS"].points_limit is None


def test_migrate_config_source_leaves_expanded_streamer_settings_untouched():
    source = '''\
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
'''

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
    source = '''\
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
'''

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
    assert "StreamerSettings(\n            make_predictions=False,\n            follow_raid=True,\n            bet=BetSettings(max_points=1234),\n        )" in migrated


def test_migration_does_not_override_computed_dictionary_keys():
    source = '''\
CONFIG_VERSION = 3
USERNAME = "username"
PASSWORD = "password"
BLACKLIST = "blacklist"
PORT = "port"
MINER_CONFIG = {USERNAME: "alice", PASSWORD: "secret"}
STREAMERS = []
MINE_CONFIG = {BLACKLIST: ["blocked"]}
ANALYTICS_CONFIG = {PORT: 6000}
'''

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
    assert stat.S_IMODE(config.stat().st_mode) == 0o640
    assert stat.S_IMODE(backup.stat().st_mode) == 0o640
    assert migrate_config(config) is False


def test_migrate_current_config_wipes_twitch_password_from_file_and_backup(tmp_path):
    config = tmp_path / "config.py"
    config.write_text(
        f'''\
CONFIG_VERSION = {CONFIG_VERSION}
MINER_CONFIG = {{
    "username": "alice",
    "password": "twitch-secret",
}}
STREAMERS = []
MINE_CONFIG = {{}}
ANALYTICS_CONFIG = {{"password": "analytics-secret"}}
''',
        encoding="utf-8",
    )

    assert migrate_config(config) is True

    migrated = config.read_text(encoding="utf-8")
    backup = (tmp_path / f"config.py.v{CONFIG_VERSION}.bak").read_text(
        encoding="utf-8"
    )
    assert "twitch-secret" not in migrated
    assert "twitch-secret" not in backup
    assert '"password": "analytics-secret"' in migrated
    assert migrate_config(config) is False


def test_migrate_config_sanitizes_matching_interrupted_backup(tmp_path):
    config = tmp_path / "config.py"
    source = f'''\
CONFIG_VERSION = {CONFIG_VERSION}
MINER_CONFIG = {{"username": "alice", "password": "twitch-secret"}}
STREAMERS = []
MINE_CONFIG = {{}}
ANALYTICS_CONFIG = None
'''
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


def test_migrate_config_rewrites_invalid_file_from_current_version_backup(tmp_path):
    config = tmp_path / "config.py"
    backup = tmp_path / "config.py.v0.bak"
    current, _, _ = migrate_config_source(CONFIG)
    config.write_text("CONFIG_VERSION = 4\nMINER_CONFIG = {invalid syntax}\n", encoding="utf-8")
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
        ],
    }
    assert namespace["STREAMERS"] == ["channel_one", "channel_two"]
    assert namespace["MINE_CONFIG"] == {"followers": True}
    assert namespace["ANALYTICS_CONFIG"] == {"port": 5000}


def test_convert_runner_source_defaults_optional_sections():
    converted = convert_runner_source(
        """\
from TwitchChannelPointsMiner import TwitchChannelPointsMiner

miner = TwitchChannelPointsMiner("alice")
miner.mine()
"""
    )
    namespace = {}

    exec(converted, namespace)

    assert namespace["STREAMERS"] == []
    assert namespace["MINE_CONFIG"] == {}
    assert namespace["ANALYTICS_CONFIG"] is None


def test_convert_runner_source_preserves_supporting_imports_only():
    converted = convert_runner_source(
        """\
import logging
from TwitchChannelPointsMiner import TwitchChannelPointsMiner
from TwitchChannelPointsMiner.classes.Settings import Priority

miner = TwitchChannelPointsMiner("alice", priority=[Priority.ORDER])
miner.mine([])
"""
    )

    assert "import logging" in converted
    assert "from TwitchChannelPointsMiner.classes.Settings import Priority" in converted
    assert (
        "from TwitchChannelPointsMiner import TwitchChannelPointsMiner" not in converted
    )

    namespace = {}
    exec(converted, namespace)
    assert namespace["MINER_CONFIG"]["priority"] == [namespace["Priority"].ORDER]


def test_convert_runner_source_preserves_explicit_streamer_source_priority():
    converted = convert_runner_source(
        """\
from TwitchChannelPointsMiner import TwitchChannelPointsMiner
from TwitchChannelPointsMiner.classes.Settings import StreamerSource

miner = TwitchChannelPointsMiner(
    "alice", streamer_source_priority=[StreamerSource.FOLLOWERS]
)
miner.mine([])
"""
    )

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
    runner.write_text(RUNNER, encoding="utf-8")

    result = convert_runner(runner, config)

    assert result == config
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
