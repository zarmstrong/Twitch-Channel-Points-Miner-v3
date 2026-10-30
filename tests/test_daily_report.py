import threading
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from TwitchChannelPointsMiner.TwitchChannelPointsMiner import (
    TwitchChannelPointsMiner,
    _load_daily_report_state,
    _save_daily_report_state,
)
from TwitchChannelPointsMiner.classes.Settings import Events, Settings
from TwitchChannelPointsMiner.classes.entities.Streamer import Streamer
from TwitchChannelPointsMiner.logger import LoggerSettings, LoggingFormat


def _miner():
    miner = TwitchChannelPointsMiner.__new__(TwitchChannelPointsMiner)
    miner.streamers = [
        SimpleNamespace(username="alice", channel_points=1250),
        SimpleNamespace(username="bob", channel_points=500),
    ]
    miner.daily_report_streamers = {"alice": 1000, "bob": 550}
    miner.daily_report_drop_progress = {}
    miner.daily_report_date = date(2026, 7, 20)
    miner.daily_report_state_path = "/unused/daily-report.json"
    miner.daily_report_retired_balances = {}
    miner.daily_report_lock = threading.Lock()
    miner.daily_report_saved_state = None
    miner.daily_report_save_failed = False
    miner.twitch = SimpleNamespace(drop_report_snapshot=MagicMock(return_value={}))
    return miner


def _report(miner, now):
    logger_path = "TwitchChannelPointsMiner.TwitchChannelPointsMiner.logger.info"
    save_path = (
        "TwitchChannelPointsMiner.TwitchChannelPointsMiner._save_daily_report_state"
    )
    with patch(logger_path) as info, patch(save_path):
        miner._TwitchChannelPointsMiner__send_daily_report_if_due(now)
    return info.call_args.args[0] if info.called else None


def test_daily_report_emits_event_and_advances_baseline():
    miner = _miner()
    Settings.logger = SimpleNamespace(daily_report=True, daily_report_time="09:00")

    logger_path = "TwitchChannelPointsMiner.TwitchChannelPointsMiner.logger.info"
    save_path = (
        "TwitchChannelPointsMiner.TwitchChannelPointsMiner._save_daily_report_state"
    )
    with patch(logger_path) as info, patch(save_path) as save:
        miner._TwitchChannelPointsMiner__send_daily_report_if_due(
            datetime(2026, 7, 21, 9, 1)
        )

    message = info.call_args.args[0]
    assert "alice: +250 channel points" in message
    assert "bob: -50 channel points" in message
    assert info.call_args.kwargs["extra"]["event"] is Events.DAILY_REPORT
    assert miner.daily_report_date == date(2026, 7, 21)
    assert miner.daily_report_streamers == {"alice": 1250, "bob": 500}
    miner.twitch.drop_report_snapshot.assert_called_once_with()
    save.assert_called_once()


def test_daily_report_omits_unchanged_streamers_and_includes_drop_context():
    miner = _miner()
    miner.streamers[1].channel_points = 550
    miner.daily_report_drop_progress = {
        "drop": {
            "category": "Warframe",
            "campaign": "TennoCon 2026",
            "item_name": "Cool Cat",
            "current_minutes_watched": 10,
            "status": "in_progress",
        }
    }
    miner.twitch.drop_report_snapshot.return_value = {
        "drop": {
            "category": "Warframe",
            "campaign": "TennoCon 2026",
            "item_name": "Cool Cat",
            "current_minutes_watched": 66,
            "minutes_required": 120,
            "status": "in_progress",
        }
    }
    Settings.logger = SimpleNamespace(daily_report=True, daily_report_time="09:00")

    logger_path = "TwitchChannelPointsMiner.TwitchChannelPointsMiner.logger.info"
    save_path = (
        "TwitchChannelPointsMiner.TwitchChannelPointsMiner._save_daily_report_state"
    )
    with patch(logger_path) as info, patch(save_path):
        miner._TwitchChannelPointsMiner__send_daily_report_if_due(
            datetime(2026, 7, 21, 9, 1)
        )

    message = info.call_args.args[0]
    assert "alice: +250 channel points" in message
    assert "bob:" not in message
    assert "- Warframe — TennoCon 2026 — Cool Cat: +56m of 120m, in progress" in message


def test_daily_report_suppresses_report_when_nothing_changed():
    miner = _miner()
    miner.streamers[0].channel_points = 1000
    miner.streamers[1].channel_points = 550
    Settings.logger = SimpleNamespace(daily_report=True, daily_report_time="09:00")

    logger_path = "TwitchChannelPointsMiner.TwitchChannelPointsMiner.logger.info"
    save_path = (
        "TwitchChannelPointsMiner.TwitchChannelPointsMiner._save_daily_report_state"
    )
    with patch(logger_path) as info, patch(save_path) as save:
        miner._TwitchChannelPointsMiner__send_daily_report_if_due(
            datetime(2026, 7, 21, 9, 1)
        )

    info.assert_not_called()
    assert miner.daily_report_date == date(2026, 7, 21)
    save.assert_called_once()


def test_daily_report_waits_until_scheduled_time():
    miner = _miner()
    Settings.logger = SimpleNamespace(daily_report=True, daily_report_time="09:00")

    with patch("TwitchChannelPointsMiner.TwitchChannelPointsMiner.logger.info") as info:
        miner._TwitchChannelPointsMiner__send_daily_report_if_due(
            datetime(2026, 7, 21, 8, 59)
        )

    info.assert_not_called()


def test_daily_report_state_survives_reload(tmp_path):
    path = tmp_path / "daily-report.json"
    drops = {"reward": {"current_minutes_watched": 30}}

    _save_daily_report_state(
        path,
        date(2026, 7, 20),
        {"alice": 1000},
        drops,
    )

    assert _load_daily_report_state(path) == {
        "date": date(2026, 7, 20),
        "streamers": {"alice": 1000},
        "drop_progress": drops,
        "latest_drop_progress": {},
        "retired_balances": {},
    }


def test_invalid_daily_report_state_starts_fresh(tmp_path):
    path = tmp_path / "daily-report.json"
    path.write_text("not json", encoding="utf-8")

    assert _load_daily_report_state(path) is None


def test_malformed_drop_progress_state_starts_fresh(tmp_path):
    path = tmp_path / "daily-report.json"
    path.write_text(
        '{"version": 1, "streamers": {}, "drop_progress": []}',
        encoding="utf-8",
    )

    assert _load_daily_report_state(path) is None


def test_daily_report_includes_drop_progress_from_before_a_restart(
    tmp_path, monkeypatch
):
    # The drop finished and was claimed before the restart, so the startup
    # inventory check of the next session doesn't record it again.
    finished = {
        "category": "HITMAN World of Assassination",
        "campaign": "The Host Drop #1",
        "item_name": "Royal Purple Banana",
        "drop_id": "banana",
        "current_minutes_watched": 60,
        "minutes_required": 60,
        "status": "captured",
    }
    unchanged = {
        "category": "Albion Online",
        "item_name": "Old Chest",
        "drop_id": "chest",
        "current_minutes_watched": 240,
        "status": "captured",
    }
    monkeypatch.chdir(tmp_path)
    # The miner below sets Settings.logger; restore it for later tests.
    monkeypatch.setattr(
        Settings,
        "logger",
        SimpleNamespace(daily_report=True, daily_report_time="00:00"),
    )
    before = _miner()
    before.daily_report_state_path = (
        tmp_path / "logs" / ".state" / ("daily_report.alice.json")
    )
    before.daily_report_drop_progress = {
        "id|banana|HITMAN World of Assassination": {
            **finished,
            "current_minutes_watched": 0,
            "status": "in_progress",
        },
        "id|chest|Albion Online": unchanged,
    }
    before.twitch.drop_report_snapshot.return_value = {
        "id|banana|HITMAN World of Assassination": finished,
        "id|chest|Albion Online": unchanged,
    }
    before._TwitchChannelPointsMiner__save_daily_report_progress()

    with patch(
        "TwitchChannelPointsMiner.TwitchChannelPointsMiner.configure_loggers",
        return_value=(None, MagicMock()),
    ):
        after = TwitchChannelPointsMiner(
            "alice", logger_settings=LoggerSettings(daily_report=True)
        )

    # Only progress made since the last report is restored.
    assert after.twitch.drop_report_snapshot() == {
        "id|banana|HITMAN World of Assassination": finished
    }
    after.streamers = [SimpleNamespace(username="alice", channel_points=1000)]
    message = _report(after, datetime(2026, 7, 21, 0, 1))

    assert (
        "- HITMAN World of Assassination — The Host Drop #1 — "
        "Royal Purple Banana: +60m of 60m, captured"
    ) in message
    assert "Old Chest" not in message


def test_restored_drop_progress_yields_to_this_sessions_updates():
    from TwitchChannelPointsMiner.classes.Twitch import Twitch

    twitch = Twitch.__new__(Twitch)
    twitch.analytics_mutex = threading.Lock()
    twitch.drop_report_state = {"drop": {"current_minutes_watched": 40}}

    twitch.restore_drop_report_state(
        {
            "drop": {"current_minutes_watched": 30},
            "other": {"current_minutes_watched": 10},
        }
    )

    assert twitch.drop_report_snapshot() == {
        "drop": {"current_minutes_watched": 40},
        "other": {"current_minutes_watched": 10},
    }


def test_daily_report_counts_channels_added_mid_day():
    miner = _miner()
    Settings.logger = SimpleNamespace(daily_report=True, daily_report_time="09:00")
    added = Streamer("willerz")
    added.channel_points = 3000
    miner._TwitchChannelPointsMiner__add_daily_report_streamer(added)
    miner.streamers.append(added)
    added.channel_points = 3450

    message = _report(miner, datetime(2026, 7, 21, 9, 1))

    assert "willerz: +450 channel points" in message
    assert miner.daily_report_streamers["willerz"] == 3450


def test_daily_report_keeps_gains_of_channels_retired_mid_day():
    miner = _miner()
    Settings.logger = SimpleNamespace(daily_report=True, daily_report_time="09:00")
    retired = Streamer("willerz")
    retired.channel_points = 3000
    miner._TwitchChannelPointsMiner__add_daily_report_streamer(retired)
    retired.channel_points = 3450
    miner._TwitchChannelPointsMiner__retire_daily_report_streamer(retired)

    message = _report(miner, datetime(2026, 7, 21, 9, 1))

    assert "willerz: +450 channel points" in message
    assert "willerz" not in miner.daily_report_streamers
    assert miner.daily_report_retired_balances == {}


def test_daily_report_counts_a_re_added_channel_since_it_was_first_added():
    # Like a restart, the report covers the balance change since the channel
    # was first seen after the last report, including the untracked gap.
    miner = _miner()
    Settings.logger = SimpleNamespace(daily_report=True, daily_report_time="09:00")
    channel = Streamer("willerz")
    channel.channel_points = 3000
    miner._TwitchChannelPointsMiner__add_daily_report_streamer(channel)
    channel.channel_points = 3100
    miner._TwitchChannelPointsMiner__retire_daily_report_streamer(channel)
    channel.channel_points = 3200
    miner._TwitchChannelPointsMiner__add_daily_report_streamer(channel)
    miner.streamers.append(channel)
    channel.channel_points = 3250

    message = _report(miner, datetime(2026, 7, 21, 9, 1))

    assert "willerz: +250 channel points" in message
    assert miner.daily_report_retired_balances == {}


def test_daily_report_counts_a_channel_being_retired_once():
    # The badge thread retires a channel before it leaves self.streamers, so
    # the report can see it both as tracked and as retired.
    miner = _miner()
    Settings.logger = SimpleNamespace(daily_report=True, daily_report_time="09:00")
    channel = Streamer("willerz")
    channel.channel_points = 3000
    miner._TwitchChannelPointsMiner__add_daily_report_streamer(channel)
    miner.streamers.append(channel)
    channel.channel_points = 3450
    miner._TwitchChannelPointsMiner__retire_daily_report_streamer(channel)

    message = _report(miner, datetime(2026, 7, 21, 9, 1))

    assert message.count("willerz") == 1
    assert "willerz: +450 channel points" in message


def test_removing_a_configured_channel_keeps_its_gains_for_the_report():
    miner = _miner()
    Settings.logger = SimpleNamespace(daily_report=True, daily_report_time="09:00")
    bob = Streamer("bob", explicitly_configured=True)
    bob.channel_points = 600
    miner.streamers[1] = bob
    miner.running = True
    miner.config_reload_lock = threading.Lock()
    miner.original_streamers = [1000, 550]
    miner.ws_pool = SimpleNamespace(remove_streamer_topics=lambda streamer: None)

    miner.remove_streamers(["bob"])
    message = _report(miner, datetime(2026, 7, 21, 9, 1))

    assert [streamer.username for streamer in miner.streamers] == ["alice"]
    assert "bob: +50 channel points" in message


def test_retired_channel_balances_survive_reload(tmp_path):
    path = tmp_path / "daily-report.json"
    latest = {"reward": {"current_minutes_watched": 45}}

    assert _save_daily_report_state(
        path,
        date(2026, 7, 20),
        {"alice": 1000},
        {},
        latest_drop_progress=latest,
        retired_balances={"willerz": 3450},
    )

    state = _load_daily_report_state(path)
    assert state["latest_drop_progress"] == latest
    assert state["retired_balances"] == {"willerz": 3450}


@pytest.mark.parametrize("key", ["drop_progress", "latest_drop_progress"])
def test_malformed_drop_progress_entry_starts_fresh(tmp_path, key):
    path = tmp_path / "daily-report.json"
    path.write_text(
        '{"version": 1, "streamers": {}, "%s": {"drop": "x"}}' % key,
        encoding="utf-8",
    )

    assert _load_daily_report_state(path) is None


def test_daily_report_progress_is_saved_only_when_it_changed():
    miner = _miner()
    Settings.logger = SimpleNamespace(daily_report=True, daily_report_time="09:00")
    save_path = (
        "TwitchChannelPointsMiner.TwitchChannelPointsMiner._save_daily_report_state"
    )

    with patch(save_path) as save:
        miner._TwitchChannelPointsMiner__save_daily_report_progress()
        miner._TwitchChannelPointsMiner__save_daily_report_progress()
        miner.twitch.drop_report_snapshot.return_value = {
            "drop": {"current_minutes_watched": 5}
        }
        miner._TwitchChannelPointsMiner__save_daily_report_progress()

    assert save.call_count == 2
    assert save.call_args.args[4] == {"drop": {"current_minutes_watched": 5}}


def test_failed_daily_report_save_is_retried_quietly(tmp_path):
    miner = _miner()
    Settings.logger = SimpleNamespace(daily_report=True, daily_report_time="09:00")
    blocker = tmp_path / "logs"
    blocker.write_text("not a directory", encoding="utf-8")
    miner.daily_report_state_path = blocker / ".state" / "daily_report.alice.json"
    module = "TwitchChannelPointsMiner.TwitchChannelPointsMiner.logger"

    with patch(f"{module}.warning") as warning, patch(f"{module}.debug") as debug:
        miner._TwitchChannelPointsMiner__save_daily_report_progress()
        miner._TwitchChannelPointsMiner__save_daily_report_progress()
    blocker.unlink()
    miner._TwitchChannelPointsMiner__save_daily_report_progress()

    assert warning.call_count == 1
    assert debug.call_count == 1
    assert _load_daily_report_state(miner.daily_report_state_path)["streamers"] == {
        "alice": 1000,
        "bob": 550,
    }


def test_first_start_records_a_baseline_for_every_channel():
    miner = _miner()
    Settings.logger = SimpleNamespace(daily_report=True, daily_report_time="09:00")
    miner.daily_report_date = None
    miner.daily_report_streamers = {}

    with patch(
        "TwitchChannelPointsMiner.TwitchChannelPointsMiner._save_daily_report_state"
    ) as save:
        miner._TwitchChannelPointsMiner__start_daily_report()

    assert miner.daily_report_streamers == {"alice": 1250, "bob": 500}
    assert miner.daily_report_date is not None
    save.assert_called_once()


def test_shutdown_save_gives_up_when_the_report_state_is_busy():
    # end() runs as a signal handler and can interrupt the main thread while
    # it holds the lock; the save must give up instead of hanging.
    miner = _miner()
    Settings.logger = SimpleNamespace(daily_report=True, daily_report_time="09:00")
    save_path = (
        "TwitchChannelPointsMiner.TwitchChannelPointsMiner._save_daily_report_state"
    )

    with (
        miner.daily_report_lock,
        patch(save_path) as save,
        patch(
            "TwitchChannelPointsMiner.TwitchChannelPointsMiner.logger.warning"
        ) as warning,
    ):
        miner._TwitchChannelPointsMiner__save_daily_report_progress(timeout=0.01)

    save.assert_not_called()
    warning.assert_called_once()


def test_drop_report_snapshot_can_reenter_the_analytics_mutex():
    # end() reads the snapshot from a signal handler, possibly while the main
    # thread it interrupted holds the mutex.
    from TwitchChannelPointsMiner.classes.Twitch import Twitch

    twitch = Twitch("alice", "user-agent")
    finished = threading.Event()

    def snapshot_while_holding_the_mutex():
        with twitch.analytics_mutex:
            twitch.drop_report_snapshot()
        finished.set()

    threading.Thread(target=snapshot_while_holding_the_mutex, daemon=True).start()

    assert finished.wait(timeout=5)


@pytest.mark.parametrize(
    "console_format, printed",
    [(LoggingFormat.TEXT, "\n\n"), (LoggingFormat.JSON, "")],
)
def test_report_spacing_only_on_the_text_console(
    monkeypatch, capsys, console_format, printed
):
    monkeypatch.setattr(
        Settings, "logger", SimpleNamespace(console_format=console_format)
    )

    TwitchChannelPointsMiner._print_report_spacing("\n")

    assert capsys.readouterr().out == printed
