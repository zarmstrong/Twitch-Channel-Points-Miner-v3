from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from TwitchChannelPointsMiner.TwitchChannelPointsMiner import (
    TwitchChannelPointsMiner,
    _load_daily_report_state,
    _save_daily_report_state,
)
from TwitchChannelPointsMiner.classes.Settings import Events, Settings


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
