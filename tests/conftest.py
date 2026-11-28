from types import SimpleNamespace

import pytest

from TwitchChannelPointsMiner.classes.Settings import Settings
from TwitchChannelPointsMiner.logger import LoggingFormat


@pytest.fixture(autouse=True)
def configured_logger():
    """Entities expect the application to have initialized Settings.logger."""
    previous = getattr(Settings, "logger", None)
    Settings.logger = SimpleNamespace(less=False, console_format=LoggingFormat.TEXT)
    yield
    Settings.logger = previous


@pytest.fixture(autouse=True)
def isolated_history_journal(monkeypatch):
    """A fresh, stopped History journal per test, so a test that runs the
    miner never leaves the process-wide journal writing for later tests."""
    from TwitchChannelPointsMiner.history import journal as journal_module

    fresh = journal_module.Journal()
    monkeypatch.setattr(journal_module, "JOURNAL", fresh)
    yield
    fresh.stop()


@pytest.fixture(autouse=True)
def isolated_prediction_credits():
    """Prediction payout credits waiting for their result are kept per
    streamer name, process-wide: none may carry over to another test."""
    from TwitchChannelPointsMiner.classes import WebSocketsPool

    WebSocketsPool._prediction_credits.clear()
    yield
    WebSocketsPool._prediction_credits.clear()
