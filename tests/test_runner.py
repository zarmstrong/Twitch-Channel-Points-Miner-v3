import pytest

from TwitchChannelPointsMiner import runner
from TwitchChannelPointsMiner.runner import _load_config


def test_load_config_reports_missing_import(tmp_path):
    config = tmp_path / "config.py"
    config.write_text(
        '''\
MINER_CONFIG = {}
STREAMERS = []
MINE_CONFIG = {"category_sort": CategorySort.VIEWERS_DESC}
ANALYTICS_CONFIG = None
''',
        encoding="utf-8",
    )

    with pytest.raises(
        RuntimeError,
        match=r"CategorySort is used but not defined.*required import",
    ) as raised:
        _load_config(config)

    assert isinstance(raised.value.__cause__, NameError)


def test_load_config_accepts_imported_configuration_names(tmp_path):
    config = tmp_path / "config.py"
    config.write_text(
        '''\
from TwitchChannelPointsMiner.classes.Settings import CategorySort

MINER_CONFIG = {}
STREAMERS = []
MINE_CONFIG = {"category_sort": CategorySort.VIEWERS_DESC}
ANALYTICS_CONFIG = None
''',
        encoding="utf-8",
    )

    loaded = _load_config(config)

    assert loaded.MINE_CONFIG["category_sort"].name == "VIEWERS_DESC"


def test_restart_process_relaunches_frozen_executable(monkeypatch):
    calls = []
    monkeypatch.setattr(runner.sys, "frozen", True, raising=False)
    monkeypatch.setattr(runner.sys, "executable", "miner.exe")
    monkeypatch.setattr(runner.sys, "argv", ["miner.exe", "--example"])
    monkeypatch.setattr(
        runner.os,
        "execv",
        lambda executable, args: calls.append((executable, args)),
    )

    runner.restart_process()

    assert calls == [("miner.exe", ["miner.exe", "--example"])]
