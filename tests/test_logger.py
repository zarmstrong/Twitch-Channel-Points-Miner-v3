import logging
from logging.handlers import TimedRotatingFileHandler

import pytest

from TwitchChannelPointsMiner.logger import LoggerSettings, configure_loggers


@pytest.mark.parametrize("value", [True, 0, -1, 1.5, "7"])
def test_log_retention_days_requires_a_positive_integer(value):
    with pytest.raises(ValueError, match="positive integer"):
        LoggerSettings(log_retention_days=value)


def test_file_logs_rotate_at_midnight_with_configured_retention(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = LoggerSettings(log_retention_days=3)
    root_logger = logging.getLogger()
    existing_handlers = set(root_logger.handlers)
    existing_level = root_logger.level

    log_file, listener = configure_loggers("test-user", settings)
    try:
        file_handler = next(
            handler
            for handler in listener.handlers
            if isinstance(handler, TimedRotatingFileHandler)
        )

        assert log_file == str(tmp_path / "logs" / "test-user.log")
        assert file_handler.when == "MIDNIGHT"
        assert file_handler.interval == 24 * 60 * 60
        assert file_handler.backupCount == 3
    finally:
        listener.stop()
        for handler in set(root_logger.handlers) - existing_handlers:
            root_logger.removeHandler(handler)
        root_logger.setLevel(existing_level)
