import sys
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from TwitchChannelPointsMiner.TwitchChannelPointsMiner import TwitchChannelPointsMiner
from TwitchChannelPointsMiner.classes.Settings import Settings


def _miner(streamer_mutex):
    miner = TwitchChannelPointsMiner.__new__(TwitchChannelPointsMiner)
    miner.running = True
    miner.streamers = [
        SimpleNamespace(
            username="alice",
            irc_chat=None,
            mutex=streamer_mutex,
        )
    ]
    miner.twitch = SimpleNamespace(
        running=True, drop_report_snapshot=MagicMock(return_value={})
    )
    miner.daily_report_lock = threading.RLock()
    miner.daily_report_state_dirty = False
    miner.daily_report_in_progress = False
    miner.daily_report_drop_progress_latest = {}
    miner.drop_badge_catalog_stop_event = MagicMock()
    miner.ws_pool = None
    miner.minute_watcher_thread = None
    miner.sync_campaigns_thread = None
    miner.drop_badge_catalog_thread = None
    miner.queue_listener = MagicMock()
    return miner


def test_end_releases_a_still_locked_streamer_mutex_normally():
    mutex = MagicMock()
    mutex.locked.return_value = True
    mutex.acquire.return_value = True
    miner = _miner(mutex)

    warning_path = "TwitchChannelPointsMiner.TwitchChannelPointsMiner.logger.warning"
    report_path = (
        "TwitchChannelPointsMiner.TwitchChannelPointsMiner."
        "TwitchChannelPointsMiner._TwitchChannelPointsMiner__print_report"
    )
    with patch(warning_path) as warning, patch(report_path):
        with pytest.raises(SystemExit):
            miner.end(None, None)

    mutex.acquire.assert_called_once_with(timeout=30)
    mutex.release.assert_called_once()
    warning.assert_not_called()


def test_end_does_not_hang_forever_when_a_streamer_mutex_never_releases():
    # Reproduces a real report: a save still in progress (e.g. a slow/hung
    # disk write) held streamer.mutex, and a bare acquire() here - unlike
    # every other join in this method, which already tolerates its thread
    # not stopping in time - blocked shutdown (and the whole GUI thread
    # driving it, in the Windows shell) forever. Must time out and continue
    # instead of blocking indefinitely.
    mutex = MagicMock()
    mutex.locked.return_value = True
    mutex.acquire.return_value = False  # Simulates a timed-out acquire.
    miner = _miner(mutex)

    warning_path = "TwitchChannelPointsMiner.TwitchChannelPointsMiner.logger.warning"
    report_path = (
        "TwitchChannelPointsMiner.TwitchChannelPointsMiner."
        "TwitchChannelPointsMiner._TwitchChannelPointsMiner__print_report"
    )
    with patch(warning_path) as warning, patch(report_path) as report:
        with pytest.raises(SystemExit):
            miner.end(None, None)

    mutex.acquire.assert_called_once_with(timeout=30)
    mutex.release.assert_not_called()  # Never acquired - nothing to release.
    warning.assert_called_once()
    # Shutdown must still complete rather than getting stuck on this one
    # unresponsive streamer.
    report.assert_called_once()
    miner.queue_listener.stop.assert_called_once()


def _end_with_daily_report(monkeypatch, miner):
    monkeypatch.setattr(Settings, "logger", SimpleNamespace(daily_report=True))
    warning_path = "TwitchChannelPointsMiner.TwitchChannelPointsMiner.logger.warning"
    report_path = (
        "TwitchChannelPointsMiner.TwitchChannelPointsMiner."
        "TwitchChannelPointsMiner._TwitchChannelPointsMiner__print_report"
    )
    save_path = (
        "TwitchChannelPointsMiner.TwitchChannelPointsMiner._save_daily_report_state"
    )
    with (
        patch(warning_path) as warning,
        patch(report_path) as report,
        patch(save_path) as save,
    ):
        with pytest.raises(SystemExit):
            miner.end(None, None)
    return warning, report, save


def test_end_saves_daily_report_progress_recorded_since_the_last_pass(monkeypatch):
    miner = _miner(MagicMock(locked=MagicMock(return_value=False)))
    miner.streamers[0].channel_points = 25
    miner.daily_report_state_path = "/unused/daily-report.json"
    miner.daily_report_date = None
    miner.daily_report_streamers = {"alice": 10}
    miner.daily_report_drop_progress = None
    miner.daily_report_retired_streamers = {}
    miner.twitch.drop_report_snapshot.return_value = {"drop": {"x": 1}}

    warning, report, save = _end_with_daily_report(monkeypatch, miner)

    # The next session takes alice back if it tracks her again; otherwise her
    # balance so far still reaches the next report.
    assert save.call_args.args[4] == {"drop": {"x": 1}}
    assert save.call_args.args[5] == {"alice": 25}
    warning.assert_not_called()
    report.assert_called_once()


def test_end_still_finishes_when_drop_progress_cannot_be_saved(monkeypatch):
    miner = _miner(MagicMock(locked=MagicMock(return_value=False)))
    miner.daily_report_streamers = {}
    miner.daily_report_retired_streamers = {}
    miner.twitch.drop_report_snapshot.side_effect = RuntimeError("boom")

    warning, report, _ = _end_with_daily_report(monkeypatch, miner)

    # Once before the workers stop and once after.
    assert [call.args[0] for call in warning.call_args_list] == [
        "Unable to save daily-report state",
        "Unable to save daily-report state",
    ]
    report.assert_called_once()
    miner.queue_listener.stop.assert_called_once()


def test_end_saves_before_waiting_on_the_workers(monkeypatch):
    # A container stop may kill the process during the joins below.
    miner = _miner(MagicMock(locked=MagicMock(return_value=False)))
    miner.streamers[0].channel_points = 25
    miner.daily_report_state_path = "/unused/daily-report.json"
    miner.daily_report_date = None
    miner.daily_report_streamers = {"alice": 10}
    miner.daily_report_drop_progress = None
    miner.daily_report_retired_streamers = {}
    saves_before_join = []
    miner.minute_watcher_thread = MagicMock(
        is_alive=MagicMock(return_value=False),
        join=MagicMock(
            side_effect=lambda timeout: saves_before_join.append(
                sys.modules[
                    TwitchChannelPointsMiner.__module__
                ]._save_daily_report_state.call_count
            )
        ),
    )
    miner.minute_watcher_thread.is_alive.side_effect = [True, False]

    _, _, save = _end_with_daily_report(monkeypatch, miner)

    assert saves_before_join == [1]
    assert save.call_args_list[0].args[5] == {"alice": 25}


def test_end_finishes_when_the_daily_report_state_was_never_loaded(monkeypatch):
    # A crash before __init__ loaded the state still runs this shutdown.
    miner = _miner(MagicMock(locked=MagicMock(return_value=False)))
    del miner.daily_report_in_progress

    warning, report, save = _end_with_daily_report(monkeypatch, miner)

    save.assert_not_called()
    assert warning.call_count == 2
    report.assert_called_once()
    miner.drop_badge_catalog_stop_event.set.assert_called_once()
    miner.queue_listener.stop.assert_called_once()
