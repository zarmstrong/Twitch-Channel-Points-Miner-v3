import hashlib
import stat

import pytest

from TwitchChannelPointsMiner.config_migration import (
    ConfigMigrationError,
    convert_runner,
    convert_runner_source,
)


RUNNER = '''\
from TwitchChannelPointsMiner import TwitchChannelPointsMiner
from TwitchChannelPointsMiner.classes.entities.Streamer import StreamerSettings

miner = TwitchChannelPointsMiner("alice", claim_drops_startup=True)
miner.mine(["channel_one", "channel_two"], followers=True)
miner.analytics(port=5000)
'''


def test_convert_runner_source_preserves_configuration_expressions():
    converted = convert_runner_source(RUNNER)
    namespace = {}

    exec(converted, namespace)

    assert namespace["MINER_CONFIG"] == {
        "username": "alice",
        "claim_drops_startup": True,
    }
    assert namespace["STREAMERS"] == ["channel_one", "channel_two"]
    assert namespace["MINE_CONFIG"] == {"followers": True}
    assert namespace["ANALYTICS_CONFIG"] == {"port": 5000}


def test_convert_runner_source_defaults_optional_sections():
    converted = convert_runner_source(
        '''\
from TwitchChannelPointsMiner import TwitchChannelPointsMiner

miner = TwitchChannelPointsMiner("alice")
miner.mine()
'''
    )
    namespace = {}

    exec(converted, namespace)

    assert namespace["STREAMERS"] == []
    assert namespace["MINE_CONFIG"] == {}
    assert namespace["ANALYTICS_CONFIG"] is None


def test_convert_runner_source_preserves_supporting_imports_only():
    converted = convert_runner_source(
        '''\
import logging
from TwitchChannelPointsMiner import TwitchChannelPointsMiner
from TwitchChannelPointsMiner.classes.Settings import Priority

miner = TwitchChannelPointsMiner("alice", priority=[Priority.ORDER])
miner.mine([])
'''
    )

    assert "import logging" in converted
    assert "from TwitchChannelPointsMiner.classes.Settings import Priority" in converted
    assert "from TwitchChannelPointsMiner import TwitchChannelPointsMiner" not in converted

    namespace = {}
    exec(converted, namespace)
    assert namespace["MINER_CONFIG"]["priority"] == [namespace["Priority"].ORDER]


def test_convert_runner_source_rejects_configuration_with_missing_imports():
    source = '''\
from TwitchChannelPointsMiner import TwitchChannelPointsMiner

miner = TwitchChannelPointsMiner("alice")
miner.mine([], category_sort=CategorySort.VIEWERS_DESC)
'''

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
