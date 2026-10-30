import threading
import time
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from TwitchChannelPointsMiner.TwitchChannelPointsMiner import (
    DAILY_REPORT_DROP_RETENTION,
    TwitchChannelPointsMiner,
    _load_daily_report_state,
    _recent_drop_progress,
    _save_daily_report_state,
)
from TwitchChannelPointsMiner.classes.Settings import Events, Settings
from TwitchChannelPointsMiner.classes.Twitch import Twitch
from TwitchChannelPointsMiner.logger import LoggingFormat

SAVE_PATH = "TwitchChannelPointsMiner.TwitchChannelPointsMiner._save_daily_report_state"


@pytest.fixture(autouse=True)
def _restore_logger_settings():
    # These tests replace the global logger settings; keep daily_report=True
    # from leaking into later modules' partially built miners.
    original = Settings.logger
    yield
    Settings.logger = original


def _twitch(drop_report_state=None):
    twitch = object.__new__(Twitch)
    twitch.analytics_mutex = threading.RLock()
    twitch.drop_report_state = drop_report_state or {}
    return twitch


def _drop(minutes, status="in_progress", recorded_at=None):
    return {
        "x": round((time.time() if recorded_at is None else recorded_at) * 1000),
        "category": "HITMAN World of Assassination",
        "campaign": "The Host Drop #1",
        "item_name": "Royal Purple Banana",
        "drop_id": "banana",
        "current_minutes_watched": minutes,
        "minutes_required": 60,
        "status": status,
    }


def _send_report(miner, when=datetime(2026, 7, 21, 9, 1)):
    logger_path = "TwitchChannelPointsMiner.TwitchChannelPointsMiner.logger.info"
    with patch(logger_path) as info, patch(SAVE_PATH):
        miner._TwitchChannelPointsMiner__send_daily_report_if_due(when)
    return info.call_args.args[0] if info.called else None


def _miner():
    miner = TwitchChannelPointsMiner.__new__(TwitchChannelPointsMiner)
    miner.streamers = [
        SimpleNamespace(username="alice", channel_points=1250),
        SimpleNamespace(username="bob", channel_points=500),
    ]
    miner.daily_report_streamers = {"alice": 1000, "bob": 550}
    miner.daily_report_drop_progress = {}
    miner.daily_report_drop_progress_latest = {}
    miner.daily_report_retired_streamers = {}
    miner.daily_report_lock = threading.RLock()
    miner.daily_report_state_dirty = False
    miner.daily_report_in_progress = None
    miner.daily_report_date = date(2026, 7, 20)
    miner.daily_report_state_path = "/unused/daily-report.json"
    miner.twitch = SimpleNamespace(drop_report_snapshot=MagicMock(return_value={}))
    return miner


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
        "drop_progress_latest": None,
        "retired_streamers": {},
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


def test_drop_progress_recorded_before_a_restart_reaches_the_next_report(tmp_path):
    # Reproduces #199: a drop finished and was claimed, then the miner
    # restarted before the daily report and the new session no longer had it.
    Settings.logger = SimpleNamespace(daily_report=True, daily_report_time="09:00")
    path = tmp_path / "daily-report.json"
    baseline = {"id|banana|HITMAN": _drop(0)}

    first = _miner()
    first.daily_report_state_path = path
    first.daily_report_drop_progress = baseline
    first.daily_report_drop_progress_latest = baseline
    first.twitch = _twitch({"id|banana|HITMAN": _drop(60, "captured")})
    first._save_daily_report_state_if_changed()

    state = _load_daily_report_state(path)
    restarted = _miner()
    restarted.daily_report_drop_progress = state["drop_progress"]
    restarted.twitch = _twitch()
    restarted.twitch.restore_drop_report_state(
        _recent_drop_progress(state["drop_progress_latest"])
    )

    message = _send_report(restarted)

    assert "Royal Purple Banana: +60m of 60m, captured" in message


def test_drop_report_mutex_is_reentrant(tmp_path, monkeypatch):
    # end() reads the drop state as a signal handler on the main thread, which
    # may be interrupted while already holding the mutex. A timed acquire
    # fails here instead of hanging if it ever stops being reentrant.
    monkeypatch.chdir(tmp_path)
    twitch = Twitch("someone", "agent", gql_factory=MagicMock())

    with twitch.analytics_mutex:
        assert twitch.analytics_mutex.acquire(timeout=1)
        twitch.analytics_mutex.release()


def test_restored_drop_progress_never_overrides_newer_session_progress():
    twitch = _twitch({"drop": _drop(45)})

    twitch.restore_drop_report_state({"drop": _drop(30), "other": _drop(10)})

    snapshot = twitch.drop_report_snapshot()
    assert snapshot["drop"]["current_minutes_watched"] == 45
    assert snapshot["other"]["current_minutes_watched"] == 10


def test_stale_or_malformed_saved_drop_progress_is_not_restored():
    now = time.time()
    expired = now - DAILY_REPORT_DROP_RETENTION.total_seconds() - 60

    restored = _recent_drop_progress(
        {
            "recent": _drop(10, recorded_at=now),
            "expired": _drop(10, recorded_at=expired),
            "no-timestamp": {"current_minutes_watched": 10},
            "not-a-dict": 10,
        },
        now=now,
    )

    assert list(restored) == ["recent"]
    assert _recent_drop_progress(None) == {}


def test_drop_progress_is_saved_only_when_it_changed():
    Settings.logger = SimpleNamespace(daily_report=True)
    miner = _miner()
    miner.daily_report_drop_progress_latest = {"drop": _drop(10, recorded_at=1)}
    miner.twitch = _twitch({"drop": _drop(10, recorded_at=1)})

    with patch(SAVE_PATH) as save:
        miner._save_daily_report_state_if_changed()
        save.assert_not_called()

        miner.twitch.drop_report_state["drop"] = _drop(11, recorded_at=2)
        miner._save_daily_report_state_if_changed()

    save.assert_called_once()
    latest = miner.daily_report_drop_progress_latest
    assert latest["drop"]["current_minutes_watched"] == 11


def test_added_and_retired_streamers_are_saved_together_by_the_next_pass():
    Settings.logger = SimpleNamespace(daily_report=True)
    miner = _miner()
    added = [SimpleNamespace(username=f"c{i}", channel_points=i) for i in range(20)]

    with patch(SAVE_PATH) as save:
        miner._record_daily_report_streamers(added)
        for streamer in added:
            miner._record_daily_report_streamers([streamer])
        miner._record_daily_report_retired_streamers(added[:5])
        save.assert_not_called()

        miner._save_daily_report_state_if_changed()
        miner._save_daily_report_state_if_changed()

    save.assert_called_once()
    assert save.call_args.args[5] == {f"c{i}": i for i in range(5)}


def test_nothing_is_recorded_when_the_daily_report_is_off():
    Settings.logger = SimpleNamespace(daily_report=False)
    miner = _miner()
    miner.twitch = _twitch({"drop": _drop(10)})
    carol = SimpleNamespace(username="carol", channel_points=1)

    with patch(SAVE_PATH) as save:
        miner._save_daily_report_state_if_changed()
        miner._record_daily_report_streamers([carol])
        miner._record_daily_report_retired_streamers(miner.streamers)

    save.assert_not_called()
    assert "carol" not in miner.daily_report_streamers
    assert miner.daily_report_retired_streamers == {}


def test_retired_streamer_gains_are_reported_once():
    Settings.logger = SimpleNamespace(daily_report=True, daily_report_time="09:00")
    miner = _miner()
    miner.streamers[1].channel_points = 550
    willerz = SimpleNamespace(username="willerz", channel_points=1310)
    miner.daily_report_streamers["willerz"] = 1000
    with patch(SAVE_PATH):
        miner._record_daily_report_retired_streamers([willerz])

    message = _send_report(miner)

    assert "willerz: +310 channel points" in message
    assert "bob" not in message
    assert miner.daily_report_retired_streamers == {}
    assert "willerz" not in miner.daily_report_streamers

    miner.streamers[0].channel_points += 5
    assert "willerz" not in _send_report(miner, datetime(2026, 7, 22, 9, 1))


def test_streamer_without_a_baseline_is_not_kept_as_retired():
    Settings.logger = SimpleNamespace(daily_report=True)
    miner = _miner()

    with patch(SAVE_PATH):
        miner._record_daily_report_retired_streamers(
            [SimpleNamespace(username="unknown", channel_points=5)]
        )

    assert miner.daily_report_retired_streamers == {}


def test_streamer_added_mid_day_counts_from_when_it_was_added():
    Settings.logger = SimpleNamespace(daily_report=True, daily_report_time="09:00")
    miner = _miner()
    carol = SimpleNamespace(username="carol", channel_points=100)
    with patch(SAVE_PATH):
        miner._record_daily_report_streamers([carol])
    miner.streamers.append(carol)
    carol.channel_points = 160

    message = _send_report(miner)

    assert "carol: +60 channel points" in message
    assert miner.daily_report_streamers["carol"] == 160


def test_re_added_streamer_keeps_its_baseline_and_is_counted_once():
    Settings.logger = SimpleNamespace(daily_report=True, daily_report_time="09:00")
    miner = _miner()
    miner.streamers[1].channel_points = 550
    dave = SimpleNamespace(username="dave", channel_points=300)
    miner.daily_report_streamers["dave"] = 200
    with patch(SAVE_PATH):
        miner._record_daily_report_retired_streamers([dave])
        miner._record_daily_report_streamers([dave])
    miner.streamers.append(dave)
    dave.channel_points = 350

    message = _send_report(miner)

    assert miner.daily_report_retired_streamers == {}
    assert message.count("dave") == 1
    assert "dave: +150 channel points" in message


def test_channels_tracked_again_after_a_restart_are_not_counted_twice():
    Settings.logger = SimpleNamespace(daily_report=True, daily_report_time="09:00")
    miner = _miner()
    # As saved at shutdown: both channels' balances were kept as retired.
    with patch(SAVE_PATH):
        miner._record_daily_report_retired_streamers(miner.streamers)
        # The next session tracks alice again but not bob.
        miner._record_daily_report_streamers(miner.streamers[:1])
    miner.streamers = miner.streamers[:1]
    miner.streamers[0].channel_points = 1300

    message = _send_report(miner)

    assert message.count("alice") == 1
    assert "alice: +300 channel points" in message
    assert "bob: -50 channel points" in message


def test_retired_balances_are_reported_with_no_channel_tracked():
    Settings.logger = SimpleNamespace(daily_report=True, daily_report_time="09:00")
    miner = _miner()
    with patch(SAVE_PATH):
        miner._record_daily_report_retired_streamers(miner.streamers)
    miner.streamers = []

    message = _send_report(miner)

    assert "alice: +250 channel points" in message
    assert "bob: -50 channel points" in message


def test_a_save_that_interrupts_a_report_keeps_the_state_from_before_it():
    # end() runs as a signal handler on the main thread and may interrupt
    # the report midway through updating its state.
    Settings.logger = SimpleNamespace(daily_report=True, daily_report_time="09:00")
    miner = _miner()
    logger_path = "TwitchChannelPointsMiner.TwitchChannelPointsMiner.logger.info"

    def interrupt(*_args, **_kwargs):
        miner._TwitchChannelPointsMiner__save_daily_report_state_safely(retire_all=True)

    with patch(logger_path, side_effect=interrupt), patch(SAVE_PATH) as save:
        miner._TwitchChannelPointsMiner__send_daily_report_if_due(
            datetime(2026, 7, 21, 9, 1)
        )

    # Only the report's own save, with no retired balances from the handler.
    save.assert_called_once()
    assert save.call_args.args[5] == {}
    assert miner.daily_report_in_progress is None


def test_a_save_from_another_thread_waits_for_the_report_then_saves():
    # The Windows shell calls end() from its GUI thread, so the report it
    # coincides with is not interrupted and the exit save must still happen.
    Settings.logger = SimpleNamespace(daily_report=True, daily_report_time="09:00")
    miner = _miner()
    logger_path = "TwitchChannelPointsMiner.TwitchChannelPointsMiner.logger.info"
    shutdown = threading.Thread(
        target=miner._TwitchChannelPointsMiner__save_daily_report_state_safely,
        kwargs={"retire_all": True},
    )
    waited = []

    def start_shutdown(*_args, **_kwargs):
        shutdown.start()
        shutdown.join(timeout=0.2)
        waited.append(shutdown.is_alive())

    with patch(logger_path, side_effect=start_shutdown), patch(SAVE_PATH) as save:
        miner._TwitchChannelPointsMiner__send_daily_report_if_due(
            datetime(2026, 7, 21, 9, 1)
        )
        shutdown.join(timeout=5)

    assert waited == [True]
    assert not shutdown.is_alive()
    # The report's save, then the shutdown's with every balance as retired.
    assert [call.args[5] for call in save.call_args_list] == [
        {},
        {"alice": 1250, "bob": 500},
    ]


def test_restart_state_survives_reload(tmp_path):
    path = tmp_path / "daily-report.json"
    latest = {"drop": _drop(5)}

    _save_daily_report_state(
        path,
        date(2026, 7, 20),
        {"alice": 1000, "willerz": 1000},
        {},
        latest,
        {"willerz": 1310},
    )

    state = _load_daily_report_state(path)
    assert state["retired_streamers"] == {"willerz": 1310}
    assert state["drop_progress_latest"] == latest


@pytest.mark.parametrize("key", ["drop_progress_latest", "retired_streamers"])
def test_malformed_restart_state_starts_fresh(tmp_path, key):
    path = tmp_path / "daily-report.json"
    path.write_text(
        f'{{"version": 1, "streamers": {{}}, "{key}": []}}',
        encoding="utf-8",
    )

    assert _load_daily_report_state(path) is None


def test_unwritable_state_directory_only_warns(tmp_path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("", encoding="utf-8")
    warning_path = "TwitchChannelPointsMiner.TwitchChannelPointsMiner.logger.warning"

    with patch(warning_path) as warning:
        _save_daily_report_state(blocker / "state.json", None, {}, None)

    warning.assert_called_once()


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
