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


def test_load_config_supports_migrated_config_missing_streamer_source_import(
    tmp_path,
):
    config = tmp_path / "config.py"
    config.write_text(
        '''\
MINER_CONFIG = {
    "streamer_source_priority": [
        StreamerSource.STREAMERS,
        StreamerSource.FOLLOWERS,
    ]
}
STREAMERS = []
MINE_CONFIG = {}
ANALYTICS_CONFIG = None
''',
        encoding="utf-8",
    )
    (tmp_path / ".converted-from-run-py").write_text(
        "source=/usr/src/app/run.py\nsha256=legacy\n", encoding="utf-8"
    )

    loaded = _load_config(config)

    assert [
        source.name for source in loaded.MINER_CONFIG["streamer_source_priority"]
    ] == ["STREAMERS", "FOLLOWERS"]


def test_load_config_still_rejects_user_config_missing_streamer_source_import(
    tmp_path,
):
    config = tmp_path / "config.py"
    config.write_text(
        '''\
MINER_CONFIG = {"streamer_source_priority": [StreamerSource.STREAMERS]}
STREAMERS = []
MINE_CONFIG = {}
ANALYTICS_CONFIG = None
''',
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match=r"StreamerSource is used but not defined"):
        _load_config(config)


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
