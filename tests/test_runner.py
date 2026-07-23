import pytest
from types import SimpleNamespace

from TwitchChannelPointsMiner.TwitchChannelPointsMiner import (
    _capture_drop_progress_baseline,
    _drop_progress_report_entries,
)

from TwitchChannelPointsMiner import runner
from TwitchChannelPointsMiner.classes.Settings import Priority
from TwitchChannelPointsMiner.classes.entities.Streamer import Streamer, StreamerSettings
from TwitchChannelPointsMiner.config_migration import CONFIG_VERSION
from TwitchChannelPointsMiner.runner import _load_config


def test_main_creates_default_config_for_fresh_install(tmp_path, monkeypatch, capsys):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    template = tmp_path / "config.example.py"
    template.write_text("# default configuration\n", encoding="utf-8")
    monkeypatch.setattr(runner, "DEFAULT_CONFIG_TEMPLATE", template)

    result = runner.main(
        [
            "--config-dir",
            str(config_dir),
            "--legacy-runner",
            str(tmp_path / "run.py"),
        ]
    )

    assert result == 0
    assert (config_dir / "config.py").read_text(encoding="utf-8") == (
        "# default configuration\n"
    )
    assert (config_dir / "config.py").stat().st_mode & 0o777 == 0o600
    output = capsys.readouterr().out
    assert "Created default configuration" in output
    assert "restart the container" in output


def test_streamer_settings_snapshot_ignores_volatile_runtime_state():
    first = Streamer("one", settings=StreamerSettings(favorite=True))
    second = Streamer("one", settings=StreamerSettings(favorite=True))

    assert runner._streamer_settings_snapshot(
        first
    ) == runner._streamer_settings_snapshot(second)


def test_config_digest_tolerates_temporarily_unreadable_overrides(
    tmp_path, monkeypatch
):
    config = tmp_path / "config.py"
    override = tmp_path / "web-config.json"
    config.write_text("configuration", encoding="utf-8")
    override.write_text("{}", encoding="utf-8")
    original_read_bytes = type(config).read_bytes

    def read_bytes(path):
        if path == override:
            raise OSError("temporarily unavailable")
        return original_read_bytes(path)

    monkeypatch.setattr(type(config), "read_bytes", read_bytes)

    unavailable_digest = runner._config_digest(config)
    monkeypatch.setattr(type(config), "read_bytes", original_read_bytes)

    assert runner._config_digest(config) != unavailable_digest


def test_config_watcher_refreshes_restart_snapshots(monkeypatch, caplog):
    initial = SimpleNamespace(
        STREAMERS=[Streamer("one", settings=StreamerSettings(favorite=False))],
        MINER_CONFIG={"setting": "before"},
        ANALYTICS_CONFIG=None,
        MINE_CONFIG={"categories": []},
    )
    updated = SimpleNamespace(
        STREAMERS=[Streamer("one", settings=StreamerSettings(favorite=False))],
        MINER_CONFIG={"setting": "after"},
        ANALYTICS_CONFIG=None,
        MINE_CONFIG={"categories": []},
    )
    miner = SimpleNamespace(
        running=True,
        ws_pool=object(),
        add_streamers=lambda _items: None,
        remove_streamers=lambda _items: None,
        refresh_categories=lambda _config: None,
    )
    digests = iter((b"initial", b"first-update", b"second-update"))
    loads = []

    monkeypatch.setattr(runner, "_config_digest", lambda _path: next(digests))
    monkeypatch.setattr(runner.time, "sleep", lambda _interval: None)

    def load(_path):
        loads.append(True)
        if len(loads) == 2:
            miner.running = False
        return updated

    monkeypatch.setattr(runner, "_load_config", load)

    with caplog.at_level("WARNING", logger=runner.logger.name):
        runner._watch_config("config.py", miner, initial, 1)

    warnings = [
        record
        for record in caplog.records
        if "require a restart" in record.getMessage()
    ]
    assert len(warnings) == 1


def test_main_still_converts_existing_legacy_runner(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    legacy_runner = tmp_path / "run.py"
    legacy_runner.write_text("legacy", encoding="utf-8")
    converted = object()
    calls = []
    monkeypatch.setattr(
        runner,
        "convert_runner",
        lambda source, destination: calls.append((source, destination)),
    )
    monkeypatch.setattr(runner, "_load_config", lambda path: converted)
    monkeypatch.setattr(
        runner,
        "run_config",
        lambda config, path: calls.append((config, path)),
    )

    result = runner.main(
        [
            "--config-dir",
            str(config_dir),
            "--legacy-runner",
            str(legacy_runner),
        ]
    )

    config_path = config_dir / "config.py"
    assert result == 0
    assert calls == [
        (legacy_runner, config_path),
        (converted, config_path),
    ]


def test_load_config_reports_missing_import(tmp_path):
    config = tmp_path / "config.py"
    config.write_text(
        """\
MINER_CONFIG = {}
STREAMERS = []
MINE_CONFIG = {"category_sort": CategorySort.VIEWERS_DESC}
ANALYTICS_CONFIG = None
""",
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
        """\
from TwitchChannelPointsMiner.classes.Settings import CategorySort

MINER_CONFIG = {}
STREAMERS = []
MINE_CONFIG = {"category_sort": CategorySort.VIEWERS_DESC}
ANALYTICS_CONFIG = None
""",
        encoding="utf-8",
    )

    loaded = _load_config(config)

    assert loaded.MINE_CONFIG["category_sort"].name == "VIEWERS_DESC"


def test_load_config_discards_dynamically_assembled_twitch_password(tmp_path):
    config = tmp_path / "config.py"
    config.write_text(
        f'''\
CONFIG_VERSION = {CONFIG_VERSION}
MINER_CONFIG = {{"username": "alice", **{{"password": "secret"}}}}
STREAMERS = []
MINE_CONFIG = {{}}
ANALYTICS_CONFIG = None
''',
        encoding="utf-8",
    )

    loaded = _load_config(config)

    assert loaded.MINER_CONFIG == {"username": "alice"}


def test_load_config_migrates_existing_config_before_execution(tmp_path):
    config = tmp_path / "config.py"
    config.write_text(
        """\
from TwitchChannelPointsMiner.classes.Settings import Priority
from TwitchChannelPointsMiner.classes.entities.Streamer import StreamerSettings

MINER_CONFIG = {
    "priority": [Priority.ORDER],
    "streamer_settings": StreamerSettings(watch_streak=False),
}
STREAMERS = []
MINE_CONFIG = {}
ANALYTICS_CONFIG = None
""",
        encoding="utf-8",
    )

    loaded = _load_config(config)

    assert loaded.CONFIG_VERSION == CONFIG_VERSION
    assert loaded.MINER_CONFIG["priority"] == [Priority.ORDER, Priority.FAVORITE]
    assert loaded.MINER_CONFIG["streamer_settings"].points_limit is None
    assert (tmp_path / "config.py.v0.bak").is_file()


def test_load_config_reports_migration_errors_without_executing_config(tmp_path):
    config = tmp_path / "config.py"
    config.write_text(
        f'''\
CONFIG_VERSION = {CONFIG_VERSION + 1}
MINER_CONFIG = {{}}
STREAMERS = []
MINE_CONFIG = {{}}
ANALYTICS_CONFIG = None
''',
        encoding="utf-8",
    )

    with pytest.raises(
        RuntimeError,
        match=r"Unable to migrate configuration .*unsupported CONFIG_VERSION",
    ) as raised:
        _load_config(config)

    assert raised.value.__cause__.__class__.__name__ == "ConfigMigrationError"


def test_load_config_wraps_dashboard_override_errors(tmp_path):
    config = tmp_path / "config.py"
    config.write_text(
        f'''\
CONFIG_VERSION = {CONFIG_VERSION}
MINER_CONFIG = {{}}
STREAMERS = []
MINE_CONFIG = {{}}
ANALYTICS_CONFIG = None
''',
        encoding="utf-8",
    )
    (tmp_path / "web-config.json").write_text("{broken", encoding="utf-8")

    with pytest.raises(
        RuntimeError, match="Unable to apply dashboard-managed configuration"
    ) as raised:
        _load_config(config)

    assert raised.value.__cause__.__class__.__name__ == "ConfigEditError"


def test_cli_reports_runtime_errors_without_traceback(monkeypatch, capsys):
    def fail(argv):
        raise RuntimeError("configuration migration failed")

    monkeypatch.setattr(runner, "main", fail)

    assert runner.cli([]) == 1
    captured = capsys.readouterr()
    assert captured.err == "ERROR: configuration migration failed\n"
    assert "Traceback" not in captured.err


def test_load_config_supports_migrated_config_missing_streamer_source_import(
    tmp_path,
):
    config = tmp_path / "config.py"
    config.write_text(
        """\
MINER_CONFIG = {
    "streamer_source_priority": [
        StreamerSource.STREAMERS,
        StreamerSource.FOLLOWERS,
    ]
}
STREAMERS = []
MINE_CONFIG = {}
ANALYTICS_CONFIG = None
""",
        encoding="utf-8",
    )
    (tmp_path / ".converted-from-run-py").write_text(
        "source=/usr/src/app/run.py\nsha256=legacy\n", encoding="utf-8"
    )

    loaded = _load_config(config)

    assert [
        source.name for source in loaded.MINER_CONFIG["streamer_source_priority"]
    ] == ["STREAMERS", "FOLLOWERS", "CATEGORIES", "BADGES"]


def test_load_config_still_rejects_user_config_missing_streamer_source_import(
    tmp_path,
):
    config = tmp_path / "config.py"
    config.write_text(
        """\
MINER_CONFIG = {"streamer_source_priority": [StreamerSource.STREAMERS]}
STREAMERS = []
MINE_CONFIG = {}
ANALYTICS_CONFIG = None
""",
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
    original = {"drop": {"current_minutes_watched": 60, "status": "in_progress"}}
    current = {
        "drop": {
            "item_name": "Reward",
            "current_minutes_watched": 60,
            "status": "captured",
        }
    }

    assert _drop_progress_report_entries(original, current)[0]["minutes_gained"] == 0


def test_drop_progress_report_entries_ignores_new_zero_progress_reward():
    current = {
        "drop": {
            "item_name": "Reward",
            "current_minutes_watched": 0,
            "status": "in_progress",
        }
    }

    assert _drop_progress_report_entries({}, current) == []


def test_drop_progress_report_entries_requires_complete_baseline():
    current = {
        "drop": {
            "current_minutes_watched": 25,
            "status": "in_progress",
        }
    }

    assert _drop_progress_report_entries(None, current) == []


def test_capture_drop_progress_baseline_skips_disabled_scrape():
    class TwitchStub:
        def drop_report_snapshot(self):
            raise AssertionError("snapshot should not be used without a full scrape")

        def scrape_drop_progress_from_inventory(self, reason):
            raise AssertionError("baseline capture should not trigger a scrape")

    assert _capture_drop_progress_baseline(TwitchStub()) is None


def test_capture_drop_progress_baseline_does_not_repeat_progress_scrape():
    class TwitchStub:
        def drop_report_snapshot(self):
            return {"drop": {"current_minutes_watched": 25}}

        def scrape_drop_progress_from_inventory(self, reason):
            raise AssertionError("inventory should not be scraped twice")

    assert _capture_drop_progress_baseline(TwitchStub(), progress_scraped=True) == {
        "drop": {"current_minutes_watched": 25}
    }
