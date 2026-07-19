import pytest

from TwitchChannelPointsMiner.TwitchChannelPointsMiner import (
    _capture_drop_progress_baseline,
    _drop_progress_report_entries,
)

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


def test_drop_progress_report_entries_only_returns_session_changes():
    unchanged = {
        "item_name": "Unchanged",
        "category": "Game",
        "campaign": "Campaign",
        "current_minutes_watched": 10,
        "status": "in_progress",
    }
    original = {
        "unchanged": unchanged,
        "advanced": {
            "current_minutes_watched": 6,
            "status": "in_progress",
        },
    }
    current = {
        "unchanged": unchanged.copy(),
        "advanced": {
            "item_name": "Reward",
            "category": "Game",
            "campaign": "Campaign",
            "current_minutes_watched": 25,
            "minutes_required": 60,
            "status": "in_progress",
        },
    }

    assert _drop_progress_report_entries(original, current) == [
        {
            "item_name": "Reward",
            "category": "Game",
            "campaign": "Campaign",
            "current_minutes_watched": 25,
            "minutes_required": 60,
            "status": "in_progress",
            "minutes_gained": 19,
        }
    ]


def test_drop_progress_report_entries_includes_status_only_change():
    original = {
        "drop": {"current_minutes_watched": 60, "status": "in_progress"}
    }
    current = {
        "drop": {
            "item_name": "Reward",
            "current_minutes_watched": 60,
            "status": "captured",
        }
    }

    assert _drop_progress_report_entries(original, current)[0]["minutes_gained"] == 0


def test_capture_drop_progress_baseline_scrapes_when_not_preloaded():
    class TwitchStub:
        def __init__(self):
            self.snapshot = {}
            self.reasons = []

        def drop_report_snapshot(self):
            return self.snapshot.copy()

        def scrape_drop_progress_from_inventory(self, reason):
            self.reasons.append(reason)
            self.snapshot = {"drop": {"current_minutes_watched": 25}}

    twitch = TwitchStub()

    assert _capture_drop_progress_baseline(twitch) == {
        "drop": {"current_minutes_watched": 25}
    }
    assert twitch.reasons == ["report_baseline"]


def test_capture_drop_progress_baseline_does_not_repeat_inventory_check():
    class TwitchStub:
        def drop_report_snapshot(self):
            return {}

        def scrape_drop_progress_from_inventory(self, reason):
            raise AssertionError("inventory should not be scraped twice")

    assert _capture_drop_progress_baseline(
        TwitchStub(), inventory_checked=True
    ) == {}
