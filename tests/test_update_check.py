import logging
import math
import importlib

import pytest

from TwitchChannelPointsMiner.classes.Settings import Events
from TwitchChannelPointsMiner.TwitchChannelPointsMiner import (
    TwitchChannelPointsMiner,
    _is_running_in_container,
    _normalize_update_check_interval,
)
from TwitchChannelPointsMiner import utils

miner_module = importlib.import_module(
    "TwitchChannelPointsMiner.TwitchChannelPointsMiner"
)


@pytest.mark.parametrize("hours", [3, 24, 168, math.inf])
def test_update_check_interval_accepts_whole_hours_from_three(hours):
    assert _normalize_update_check_interval(hours) == hours


@pytest.mark.parametrize("hours", [True, "24"])
def test_update_check_interval_rejects_non_numeric_values(hours):
    with pytest.raises(TypeError):
        _normalize_update_check_interval(hours)


@pytest.mark.parametrize("hours", [-math.inf, 2, 3.5, math.nan])
def test_update_check_interval_rejects_out_of_range_values(hours):
    with pytest.raises(ValueError):
        _normalize_update_check_interval(hours)


def test_check_versions_reads_latest_github_release(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"tag_name": "3.8.0"}

    monkeypatch.setattr(utils.requests, "get", lambda *args, **kwargs: Response())

    current, latest = utils.check_versions()

    assert current != "0.0.0"
    assert latest == "3.8.0"


def test_container_detection_supports_docker_and_container_engines(monkeypatch):
    monkeypatch.setattr(
        miner_module.Path,
        "exists",
        lambda path: str(path) == "/run/.containerenv",
    )

    assert _is_running_in_container() is True


def _miner_for_update_check(interval_hours=3):
    miner = TwitchChannelPointsMiner.__new__(TwitchChannelPointsMiner)
    miner.update_check_enabled = True
    miner.update_check_interval_seconds = interval_hours * 60 * 60
    miner.next_update_check_at = None
    return miner


def test_available_update_forces_alert_and_throttles_to_daily(monkeypatch, caplog):
    miner = _miner_for_update_check()
    monkeypatch.setattr(miner_module, "check_versions", lambda: ("3.7.3", "3.8.0"))
    monkeypatch.setattr(miner_module, "_is_running_in_container", lambda: False)

    with caplog.at_level(logging.INFO):
        assert miner._TwitchChannelPointsMiner__check_for_update(now=100) is True

    record = next(record for record in caplog.records if "Update available" in record.msg)
    assert record.levelno == logging.INFO
    assert record.event is Events.UPDATE_AVAILABLE
    assert record.force_alert is True
    assert "/releases/latest" in record.msg
    assert miner.next_update_check_at == 100 + (24 * 60 * 60)


def test_available_update_gives_docker_latest_image_instructions(monkeypatch, caplog):
    miner = _miner_for_update_check()
    monkeypatch.setattr(miner_module, "check_versions", lambda: ("3.7.3", "3.8.0"))
    monkeypatch.setattr(miner_module, "_is_running_in_container", lambda: True)

    with caplog.at_level(logging.INFO):
        assert miner._TwitchChannelPointsMiner__check_for_update(now=100) is True

    record = next(record for record in caplog.records if "Update available" in record.msg)
    assert "zacharmstrong/twitch-channel-points-miner:latest" in record.msg
    assert "docker compose pull && docker compose up -d" in record.msg
    assert "/releases/latest" not in record.msg


def test_no_update_keeps_configured_interval(monkeypatch):
    miner = _miner_for_update_check()
    monkeypatch.setattr(miner_module, "check_versions", lambda: ("3.7.3", "3.7.3"))

    assert miner._TwitchChannelPointsMiner__check_for_update(now=100) is False
    assert miner.next_update_check_at == 100 + (3 * 60 * 60)


def test_update_check_waits_until_next_scheduled_check(monkeypatch):
    miner = _miner_for_update_check()
    miner.next_update_check_at = 200
    calls = []
    monkeypatch.setattr(miner_module, "check_versions", lambda: calls.append(True))

    assert miner._TwitchChannelPointsMiner__check_for_update(now=199) is False
    assert calls == []


def test_disabled_update_check_never_contacts_github(monkeypatch):
    miner = _miner_for_update_check()
    miner.update_check_enabled = False
    calls = []
    monkeypatch.setattr(miner_module, "check_versions", lambda: calls.append(True))

    assert miner._TwitchChannelPointsMiner__check_for_update(now=100) is False
    assert calls == []
