import errno
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from TwitchChannelPointsMiner.TwitchChannelPointsMiner import (
    _capture_drop_progress_baseline,
    _drop_progress_report_entries,
)

from TwitchChannelPointsMiner import runner
from TwitchChannelPointsMiner.classes.Settings import Priority, StreamerSource
from TwitchChannelPointsMiner.classes.entities.Streamer import (
    Streamer,
    StreamerSettings,
)
from TwitchChannelPointsMiner.config_migration import CONFIG_VERSION
from TwitchChannelPointsMiner.runner import _config_reload_interval, _load_config


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
    # Windows has no POSIX permission bits; chmod only toggles read-only.
    if os.name == "posix":
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


def test_config_digest_uses_only_config_file(tmp_path):
    config = tmp_path / "config.py"
    config.write_text("configuration", encoding="utf-8")
    original_digest = runner._config_digest(config)
    config.write_text("updated configuration", encoding="utf-8")

    assert runner._config_digest(config) != original_digest


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


def test_config_watcher_preserves_wildcard_defaults_when_keys_missing(
    monkeypatch, caplog
):
    # A hand-edited or older reloaded config can omit the wildcard_* keys
    # entirely. Falling back to `.get(key)` (None) instead of the documented
    # default would silently disable the wildcard_category_limit throttle.
    initial = SimpleNamespace(
        STREAMERS=[],
        MINER_CONFIG={"setting": "same"},
        ANALYTICS_CONFIG=None,
        MINE_CONFIG={"categories": ["one"]},
    )
    updated = SimpleNamespace(
        STREAMERS=[],
        MINER_CONFIG={"setting": "same"},
        ANALYTICS_CONFIG=None,
        MINE_CONFIG={"categories": ["one", "two"]},
    )
    refreshed_with = []
    miner = SimpleNamespace(
        running=True,
        ws_pool=object(),
        add_streamers=lambda _items: None,
        remove_streamers=lambda _items: None,
        refresh_categories=lambda config: refreshed_with.append(dict(config)),
    )
    digests = iter((b"initial", b"first-update"))
    loads = []

    monkeypatch.setattr(runner, "_config_digest", lambda _path: next(digests))
    monkeypatch.setattr(runner.time, "sleep", lambda _interval: None)

    def load(_path):
        loads.append(True)
        miner.running = False
        return updated

    monkeypatch.setattr(runner, "_load_config", load)

    runner._watch_config("config.py", miner, initial, 1)

    assert len(refreshed_with) == 1
    assert refreshed_with[0]["wildcard_categories"] is False
    assert refreshed_with[0]["wildcard_category_limit"] == 10
    assert refreshed_with[0]["wildcard_category_streamer_limit"] == 1
    assert refreshed_with[0]["wildcard_category_pin_active"] is True


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
        lambda config, path, on_miner_ready=None: calls.append((config, path)),
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


@pytest.fixture
def no_legacy_exec(monkeypatch):
    """Record any attempt to replace the runner with run.py, and check after
    the test that there was none. It never raises, so the environment passed
    to execve (which can hold secrets) never shows up in a traceback."""
    launched = []
    monkeypatch.setattr(
        runner.os, "execve", lambda path, argv, env: launched.append(argv[1:])
    )
    yield
    assert launched == [], "the runner ran a legacy runner"


def _run_cli(*args):
    return runner.cli([str(arg) for arg in args])


def test_missing_config_dir_with_run_py_stops_instead_of_running_it(
    tmp_path, no_legacy_exec, capsys
):
    legacy_runner = tmp_path / "run.py"
    legacy_runner.write_text("legacy", encoding="utf-8")

    result = _run_cli(
        "--config-dir", tmp_path / "config", "--legacy-runner", legacy_runner
    )

    error = capsys.readouterr().err
    assert result == 1
    assert "CONFIGURATION UPDATE REQUIRED" in error
    assert "Mount a persistent config directory" in error
    assert "run.py is no longer run directly" in error
    assert "docs/migration.md#migrating-from-runpy" in error


def test_missing_config_dir_without_run_py_says_to_create_it(
    tmp_path, no_legacy_exec, capsys
):
    result = _run_cli(
        "--config-dir",
        tmp_path / "config",
        "--legacy-runner",
        tmp_path / "run.py",
    )

    error = capsys.readouterr().err
    assert result == 1
    assert "does not exist. Create it" in error
    assert "Legacy runner not found" not in error


def test_disabled_conversion_stops_and_leaves_run_py_alone(
    tmp_path, monkeypatch, no_legacy_exec, capsys
):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    legacy_runner = tmp_path / "run.py"
    legacy_runner.write_text("legacy", encoding="utf-8")
    monkeypatch.setenv("TCPM_DISABLE_AUTO_CONVERSION", "1")
    monkeypatch.setattr(
        runner,
        "convert_runner",
        lambda *args: pytest.fail("converted although conversion is disabled"),
    )

    result = _run_cli("--config-dir", config_dir, "--legacy-runner", legacy_runner)

    error = capsys.readouterr().err
    assert result == 1
    assert "TCPM_DISABLE_AUTO_CONVERSION=1" in error
    assert legacy_runner.read_text(encoding="utf-8") == "legacy"
    assert not (config_dir / "config.py").exists()


@pytest.mark.parametrize("convert_only", [False, True])
def test_failed_conversion_stops_and_removes_the_partial_config(
    tmp_path, no_legacy_exec, capsys, convert_only
):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    legacy_runner = tmp_path / "run.py"
    # Not a conventional runner: no TwitchChannelPointsMiner(...) call.
    legacy_runner.write_text("print('hello')\n", encoding="utf-8")
    args = ["--config-dir", config_dir, "--legacy-runner", legacy_runner]

    result = _run_cli(*args, *(["--convert-only"] if convert_only else []))

    error = capsys.readouterr().err
    assert result == 1
    assert f"Couldn't convert {legacy_runner}" in error
    assert "write" in error and "config.example.py" in error
    assert legacy_runner.is_file()
    assert not (config_dir / "config.py").exists()
    assert not (config_dir / ".converted-from-run-py").exists()


def test_failed_load_after_conversion_names_the_kept_original(
    tmp_path, monkeypatch, no_legacy_exec, capsys
):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    legacy_runner = tmp_path / "run.py"
    legacy_runner.write_text("legacy", encoding="utf-8")

    def convert(source, destination):
        destination.write_text("broken = (\n", encoding="utf-8")
        source.rename(source.with_name("run.py.bak"))

    def load(path):
        raise RuntimeError(f"Unable to migrate configuration {path}")

    monkeypatch.setattr(runner, "convert_runner", convert)
    monkeypatch.setattr(runner, "_load_config", load)

    result = _run_cli("--config-dir", config_dir, "--legacy-runner", legacy_runner)

    error = capsys.readouterr().err
    assert result == 1
    assert f"Your original file was restored as {legacy_runner}" in error
    assert legacy_runner.read_text(encoding="utf-8") == "legacy"
    assert not (tmp_path / "run.py.bak").exists()
    assert not (config_dir / "config.py").exists()


def test_failed_load_names_the_backup_when_it_cannot_be_restored(
    tmp_path, monkeypatch, no_legacy_exec, capsys
):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    legacy_runner = tmp_path / "run.py"
    legacy_runner.write_text("legacy", encoding="utf-8")

    def convert(source, destination):
        destination.write_text("broken = (\n", encoding="utf-8")
        source.rename(source.with_name("run.py.bak"))

    def load(path):
        raise RuntimeError("bad")

    real_rename = Path.rename

    def rename(self, target):
        if self.name == "run.py.bak":
            raise OSError("read-only")
        return real_rename(self, target)

    monkeypatch.setattr(runner, "convert_runner", convert)
    monkeypatch.setattr(runner, "_load_config", load)
    monkeypatch.setattr(Path, "rename", rename)

    result = _run_cli("--config-dir", config_dir, "--legacy-runner", legacy_runner)

    error = capsys.readouterr().err
    assert result == 1
    assert f"Your original file is kept as {tmp_path / 'run.py.bak'}" in error
    assert "rename it back to run.py" in error


def test_miner_no_longer_reads_the_legacy_runner_notice():
    source = (Path(runner.__file__).with_name("TwitchChannelPointsMiner.py")).read_text(
        encoding="utf-8"
    )

    assert "TCPM_LEGACY_CONFIG_NOTICE" not in source
    assert "TCPM_LEGACY_CONFIG_NOTICE" not in Path(runner.__file__).read_text(
        encoding="utf-8"
    )


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
        f"""\
CONFIG_VERSION = {CONFIG_VERSION}
MINER_CONFIG = {{"username": "alice", **{{"password": "secret"}}}}
STREAMERS = []
MINE_CONFIG = {{}}
ANALYTICS_CONFIG = None
""",
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
        f"""\
CONFIG_VERSION = {CONFIG_VERSION + 1}
MINER_CONFIG = {{}}
STREAMERS = []
MINE_CONFIG = {{}}
ANALYTICS_CONFIG = None
""",
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
        f"""\
CONFIG_VERSION = {CONFIG_VERSION}
MINER_CONFIG = {{}}
STREAMERS = []
MINE_CONFIG = {{}}
ANALYTICS_CONFIG = None
""",
        encoding="utf-8",
    )
    (tmp_path / "web-config.json").write_text("{broken", encoding="utf-8")

    with pytest.raises(
        RuntimeError, match="Unable to migrate dashboard configuration"
    ) as raised:
        _load_config(config)

    assert raised.value.__cause__.__class__.__name__ == "ConfigEditError"


def test_load_config_migrates_legacy_dashboard_overrides(tmp_path):
    config = tmp_path / "config.py"
    config.write_text(
        f"""\
CONFIG_VERSION = {CONFIG_VERSION}
from TwitchChannelPointsMiner.classes.Settings import StreamerSource
MINER_CONFIG = {{"streamer_source_priority": [StreamerSource.STREAMERS]}}
STREAMERS = ["one"]
MINE_CONFIG = {{"followers": False, "categories": []}}
ANALYTICS_CONFIG = None
""",
        encoding="utf-8",
    )
    legacy = tmp_path / "web-config.json"
    legacy.write_text('{"sources": {"followers": true}}', encoding="utf-8")

    loaded = _load_config(config)

    assert loaded.MINE_CONFIG["followers"] is True
    assert StreamerSource.FOLLOWERS in loaded.MINER_CONFIG["streamer_source_priority"]
    assert not legacy.exists()
    assert (tmp_path / ".web-config.json.migrated.bak").is_file()


def test_load_config_sanitizes_dashboard_override_io_errors(tmp_path, monkeypatch):
    config = tmp_path / "config.py"
    config.write_text(
        f"""\
CONFIG_VERSION = {CONFIG_VERSION}
MINER_CONFIG = {{}}
STREAMERS = []
MINE_CONFIG = {{}}
ANALYTICS_CONFIG = None
""",
        encoding="utf-8",
    )

    def fail_migration(_path):
        raise OSError("permission denied: /private/config/web-config.json")

    monkeypatch.setattr(runner, "migrate_web_config", fail_migration)

    with pytest.raises(
        RuntimeError, match="Unable to migrate dashboard configuration"
    ) as raised:
        _load_config(config)

    assert "/private/config" not in str(raised.value)
    assert raised.value.__cause__.__class__.__name__ == "OSError"


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
    ] == ["STREAMERS", "FOLLOWERS", "CATEGORIES", "BADGES", "WILDCARD_CATEGORIES"]


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


def test_drop_progress_report_entries_ignores_zero_watch_capture():
    original = {
        "drop": {
            "current_minutes_watched": 0,
            "status": "in_progress",
        }
    }
    current = {
        "drop": {
            "item_name": "Subscriber Reward",
            "current_minutes_watched": 0,
            "status": "captured",
        }
    }

    assert _drop_progress_report_entries(original, current) == []


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


@pytest.mark.parametrize("restart", [False, True])
def test_run_config_flushes_notifications_whenever_mining_ends(
    monkeypatch, tmp_path, restart
):
    import TwitchChannelPointsMiner as package
    from TwitchChannelPointsMiner.classes.Settings import Settings

    # run_config() sets this; restore it after the test.
    monkeypatch.setattr(
        Settings, "config_path", getattr(Settings, "config_path", None), raising=False
    )
    calls = []

    class FakeMiner:
        def __init__(self, **_kwargs):
            self.queue_listener = SimpleNamespace(stop=lambda: calls.append("stop"))
            self.twitch = SimpleNamespace(restart_requested=threading.Event())

        def mine(self, *_args, **_kwargs):
            # The miner stops itself, for example when it cannot clear a
            # cached login, after logging a forced alert.
            if restart:
                self.twitch.restart_requested.set()

    monkeypatch.setattr(package, "TwitchChannelPointsMiner", FakeMiner)
    monkeypatch.setattr(runner, "_watch_config", lambda *_args: None)
    monkeypatch.setattr(runner, "restart_process", lambda: calls.append("restart"))
    config = SimpleNamespace(
        MINER_CONFIG={}, MINE_CONFIG={}, STREAMERS=[], ANALYTICS_CONFIG=None
    )

    runner.run_config(config, tmp_path / "config.py")

    assert calls == (["stop", "restart"] if restart else ["stop"])


@pytest.mark.skipif(
    sys.platform == "win32", reason="os.execv does not replace the process on Windows"
)
def test_restart_process_relaunches_module_entrypoint(tmp_path):
    # Like the Docker entrypoint (python -m TwitchChannelPointsMiner.runner):
    # the relaunched process must still run as a module of its package.
    package = tmp_path / "restartpkg"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "entry.py").write_text(
        "import os\n"
        "from TwitchChannelPointsMiner import runner\n"
        "if os.environ.get('RESTARTED') != '1':\n"
        "    os.environ['RESTARTED'] = '1'\n"
        "    runner.restart_process()\n"
        "print('relaunched as', __spec__.name)\n"
    )
    repo_root = Path(runner.__file__).resolve().parent.parent
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(repo_root), str(tmp_path)]),
    }
    environment.pop("RESTARTED", None)

    result = subprocess.run(
        [sys.executable, "-m", "restartpkg.entry"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "relaunched as restartpkg.entry"


def test_config_reload_interval_defaults_to_five_seconds(monkeypatch):
    monkeypatch.delenv("TCPM_CONFIG_RELOAD_SECONDS", raising=False)

    assert _config_reload_interval() == 5.0


def test_config_reload_interval_reads_seconds(monkeypatch):
    monkeypatch.setenv("TCPM_CONFIG_RELOAD_SECONDS", "10")

    assert _config_reload_interval() == 10.0


def test_config_reload_interval_enforces_one_second_minimum(monkeypatch):
    monkeypatch.setenv("TCPM_CONFIG_RELOAD_SECONDS", "0.2")

    assert _config_reload_interval() == 1


@pytest.mark.parametrize("value", ["", "   ", "\t"])
def test_config_reload_interval_treats_blank_as_the_default(monkeypatch, value):
    # e.g. TCPM_CONFIG_RELOAD_SECONDS=${X:-} in a compose file, or a blank
    # .env entry.
    monkeypatch.setenv("TCPM_CONFIG_RELOAD_SECONDS", value)

    assert _config_reload_interval() == 5.0


def test_config_reload_interval_accepts_one_day(monkeypatch):
    monkeypatch.setenv("TCPM_CONFIG_RELOAD_SECONDS", "86400")

    assert _config_reload_interval() == 86400.0


@pytest.mark.parametrize(
    "value", ["5s", "nan", "inf", "-inf", "1e309", "86401", "1e300", "9e15"]
)
def test_config_reload_interval_rejects_invalid_values(monkeypatch, value):
    # 1e300 and 9e15 are finite but make time.sleep() raise OverflowError.
    monkeypatch.setenv("TCPM_CONFIG_RELOAD_SECONDS", value)

    with pytest.raises(RuntimeError) as raised:
        _config_reload_interval()

    assert "TCPM_CONFIG_RELOAD_SECONDS" in str(raised.value)


def test_run_config_rejects_the_interval_before_building_the_miner(monkeypatch):
    import TwitchChannelPointsMiner as package
    from TwitchChannelPointsMiner.runner import run_config

    built = []
    monkeypatch.setattr(
        package, "TwitchChannelPointsMiner", lambda **kwargs: built.append(kwargs)
    )
    monkeypatch.setenv("TCPM_CONFIG_RELOAD_SECONDS", "nan")
    config = SimpleNamespace(
        MINER_CONFIG={"username": "alice"},
        MINE_CONFIG={},
        ANALYTICS_CONFIG=None,
        STREAMERS=[],
    )

    with pytest.raises(RuntimeError):
        run_config(config, Path("config/config.py"))

    assert built == []


def test_cli_reports_an_invalid_interval_and_exits_with_1(monkeypatch, capsys):
    from TwitchChannelPointsMiner import runner

    def fail(argv=None):
        runner._config_reload_interval()

    monkeypatch.setenv("TCPM_CONFIG_RELOAD_SECONDS", "inf")
    monkeypatch.setattr(runner, "main", fail)

    assert runner.cli([]) == 1
    assert "TCPM_CONFIG_RELOAD_SECONDS" in capsys.readouterr().err


def test_miner_without_username_points_at_the_username_setting():
    from unittest.mock import patch

    import importlib

    miner_module = importlib.import_module(
        "TwitchChannelPointsMiner.TwitchChannelPointsMiner"
    )
    TwitchChannelPointsMiner = miner_module.TwitchChannelPointsMiner

    with patch.object(miner_module, "logger") as log:
        with pytest.raises(SystemExit):
            TwitchChannelPointsMiner("")

    assert "run.py" not in log.error.call_args_list[0].args[0]
    assert "username setting" in log.error.call_args_list[0].args[0]


BAD_LEGACY_RUNNER = """\
from TwitchChannelPointsMiner import TwitchChannelPointsMiner
from TwitchChannelPointsMiner.logger import LoggerSettings

miner = TwitchChannelPointsMiner(
    "alice", logger_settings=LoggerSettings(date_format="bad")
)
miner.mine(["channel_one"])
"""


def test_retry_after_a_failed_load_converts_the_corrected_runner(
    tmp_path, monkeypatch, no_legacy_exec, capsys
):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    legacy_runner = tmp_path / "run.py"
    legacy_runner.write_text(BAD_LEGACY_RUNNER, encoding="utf-8")
    args = ("--config-dir", config_dir, "--legacy-runner", legacy_runner)

    assert _run_cli(*args) == 1
    assert "CONFIGURATION UPDATE REQUIRED" in capsys.readouterr().err
    assert sorted(path.name for path in config_dir.iterdir()) == []
    assert legacy_runner.is_file()

    legacy_runner.write_text(
        BAD_LEGACY_RUNNER.replace('date_format="bad"', 'date_format="dd/mm/yy"'),
        encoding="utf-8",
    )

    assert _run_cli(*args, "--convert-only") == 0
    assert (config_dir / "config.py").is_file()


def test_missing_runner_with_a_leftover_backup_stops_instead_of_a_default(
    tmp_path, no_legacy_exec, capsys
):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (tmp_path / "run.py.bak").write_text("legacy", encoding="utf-8")

    result = _run_cli(
        "--config-dir", config_dir, "--legacy-runner", tmp_path / "run.py"
    )

    error = capsys.readouterr().err
    assert result == 1
    assert "Rename run.py.bak back to run.py" in error
    assert not (config_dir / "config.py").exists()


GOOD_LEGACY_RUNNER = BAD_LEGACY_RUNNER.replace(
    'date_format="bad"', 'date_format="dd/mm/yy"'
)


def test_reset_config_after_a_conversion_gets_a_default_despite_the_backup(
    tmp_path, monkeypatch, no_legacy_exec, capsys
):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    legacy_runner = tmp_path / "run.py"
    legacy_runner.write_text(GOOD_LEGACY_RUNNER, encoding="utf-8")
    args = ("--config-dir", config_dir, "--legacy-runner", legacy_runner)
    assert _run_cli(*args, "--convert-only") == 0
    assert (tmp_path / "run.py.bak").is_file()

    # The user deletes config.py to start over from a default one.
    (config_dir / "config.py").unlink()
    template = tmp_path / "config.example.py"
    template.write_text("# default\n", encoding="utf-8")
    monkeypatch.setattr(runner, "DEFAULT_CONFIG_TEMPLATE", template)

    assert _run_cli(*args) == 0
    assert (config_dir / "config.py").read_text(encoding="utf-8") == "# default\n"


def test_existing_backup_beside_run_py_stops_with_how_to_fix_it(
    tmp_path, monkeypatch, no_legacy_exec, capsys
):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    legacy_runner = tmp_path / "run.py"
    legacy_runner.write_text(GOOD_LEGACY_RUNNER, encoding="utf-8")
    (tmp_path / "run.py.bak").write_text("older", encoding="utf-8")
    monkeypatch.setattr(
        runner, "convert_runner", lambda *args: pytest.fail("tried to convert")
    )

    result = _run_cli("--config-dir", config_dir, "--legacy-runner", legacy_runner)

    error = capsys.readouterr().err
    assert result == 1
    assert "Move or delete run.py.bak" in error
    assert "correct it" not in error
    assert (tmp_path / "run.py.bak").read_text(encoding="utf-8") == "older"


def test_bind_mounted_run_py_is_converted_once_and_then_warned_about(
    tmp_path, monkeypatch, no_legacy_exec, capsys
):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    legacy_runner = tmp_path / "run.py"
    legacy_runner.write_text(GOOD_LEGACY_RUNNER, encoding="utf-8")
    real_rename = Path.rename

    def rename(self, target):
        if self == legacy_runner:
            raise OSError(errno.EBUSY, "Device or resource busy")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", rename)
    args = ("--config-dir", config_dir, "--legacy-runner", legacy_runner)

    assert _run_cli(*args, "--convert-only") == 0
    first = capsys.readouterr()
    assert f"Converted {legacy_runner}" in first.out
    assert f"WARNING: {legacy_runner} is ignored" in first.err
    assert legacy_runner.is_file()
    assert runner._load_config(config_dir / "config.py").MINER_CONFIG

    monkeypatch.setattr(
        runner, "convert_runner", lambda *args: pytest.fail("converted again")
    )
    assert _run_cli(*args, "--convert-only") == 0
    assert f"WARNING: {legacy_runner} is ignored" in capsys.readouterr().err


def test_failed_load_puts_the_dashboard_settings_back(tmp_path, no_legacy_exec, capsys):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    legacy_runner = tmp_path / "run.py"
    legacy_runner.write_text(BAD_LEGACY_RUNNER, encoding="utf-8")
    web_config = config_dir / "web-config.json"
    web_config.write_text('{"sources": {"followers": true}}\n', encoding="utf-8")

    result = _run_cli("--config-dir", config_dir, "--legacy-runner", legacy_runner)

    assert result == 1
    assert "CONFIGURATION UPDATE REQUIRED" in capsys.readouterr().err
    assert web_config.read_text(encoding="utf-8") == (
        '{"sources": {"followers": true}}\n'
    )
    assert not (config_dir / ".web-config.json.migrated.bak").exists()


def test_failed_load_warns_when_the_dashboard_settings_cannot_be_restored(
    tmp_path, no_legacy_exec, capsys, monkeypatch
):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    legacy_runner = tmp_path / "run.py"
    legacy_runner.write_text(BAD_LEGACY_RUNNER, encoding="utf-8")
    (config_dir / "web-config.json").write_text(
        '{"sources": {"followers": true}}\n', encoding="utf-8"
    )
    original_rename = Path.rename

    def fail_restore(self, target):
        if self.name == ".web-config.json.migrated.bak":
            raise PermissionError("held by a scanner")
        return original_rename(self, target)

    monkeypatch.setattr(Path, "rename", fail_restore)

    result = _run_cli("--config-dir", config_dir, "--legacy-runner", legacy_runner)

    assert result == 1
    error = capsys.readouterr().err
    assert "Unable to restore the dashboard settings" in error
    assert ".web-config.json.migrated.bak" in error
