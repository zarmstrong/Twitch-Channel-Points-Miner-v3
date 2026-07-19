import hashlib
import stat

import pytest

from TwitchChannelPointsMiner.config_migration import (
    CONFIG_VERSION,
    STREAMER_SETTINGS_DEFAULTS,
    ConfigMigrationError,
    convert_runner,
    convert_runner_source,
    migrate_config,
    migrate_config_source,
)


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
