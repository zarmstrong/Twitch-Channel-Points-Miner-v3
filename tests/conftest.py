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
