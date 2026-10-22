import logging
import sys
from logging.handlers import TimedRotatingFileHandler

import pytest

from TwitchChannelPointsMiner.logger import (
    LoggerSettings,
    LoggingFormat,
    SensitiveUrlFilter,
    configure_loggers,
)


@pytest.mark.parametrize("value", [True, 0, -1, 1.5, "7"])
def test_log_retention_days_requires_a_positive_integer(value):
    with pytest.raises(ValueError, match="positive integer"):
        LoggerSettings(log_retention_days=value)


def test_logging_formats_default_to_text():
    settings = LoggerSettings()

    assert settings.console_format is LoggingFormat.TEXT
    assert settings.file_format is LoggingFormat.TEXT


@pytest.mark.parametrize(
    "value, expected",
    [
        ("text", LoggingFormat.TEXT),
        ("json", LoggingFormat.JSON),
        ("JSON", LoggingFormat.JSON),
        (LoggingFormat.JSON, LoggingFormat.JSON),
    ],
)
def test_logging_formats_accept_strings_and_enum_values(value, expected):
    settings = LoggerSettings(console_format=value, file_format=value)

    assert settings.console_format is expected
    assert settings.file_format is expected


@pytest.mark.parametrize("name", ["console_format", "file_format"])
@pytest.mark.parametrize("value", ["xml", "", None, 1])
def test_invalid_logging_format_is_rejected(name, value):
    with pytest.raises(ValueError, match=f"{name} must be"):
        LoggerSettings(**{name: value})


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


def _filtered_message(msg, *args):
    record = logging.LogRecord("urllib3.connectionpool", 10, __file__, 1, msg, args, None)
    assert SensitiveUrlFilter().filter(record) is True
    return record.getMessage()


def test_sensitive_url_filter_redacts_usher_signature_and_token():
    message = _filtered_message(
        '%s://%s:%s "%s %s %s" %s %s',
        "https",
        "usher.ttvnw.net",
        443,
        "GET",
        "/api/channel/hls/example.m3u8?sig=abc123&token=%7B%22secret%22%7D",
        "HTTP/1.1",
        200,
        None,
    )

    assert "abc123" not in message
    assert "secret" not in message
    assert "/api/channel/hls/example.m3u8?sig=<redacted>&token=<redacted>" in message


def test_sensitive_url_filter_redacts_signed_playlist_and_segment_paths():
    playlist = _filtered_message('"GET /v1/playlist/CqSIGNED.m3u8 HTTP/1.1" 200 None')
    segment = _filtered_message('"HEAD /s1/v1/segment/CtoSIGNED?dna=XYZ HTTP/1.1" 200 0')

    assert playlist == '"GET /v1/playlist/<redacted> HTTP/1.1" 200 None'
    assert segment == '"HEAD /s1/v1/segment/<redacted> HTTP/1.1" 200 0'


def test_sensitive_url_filter_leaves_other_messages_untouched():
    record = logging.LogRecord(
        "x", 20, __file__, 1, "Watching %s for %s", ("alice", "drops"), None
    )

    SensitiveUrlFilter().filter(record)

    assert record.args == ("alice", "drops")
    assert record.getMessage() == "Watching alice for drops"


def test_configured_loggers_redact_urllib3_request_lines(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = LoggerSettings(file_level=logging.DEBUG)
    root_logger = logging.getLogger()
    existing_handlers = set(root_logger.handlers)
    existing_level = root_logger.level

    log_file, listener = configure_loggers("test-user", settings)
    try:
        logging.getLogger("urllib3.connectionpool").debug(
            '%s://%s:%s "%s %s %s" %s %s',
            "https",
            "usher.ttvnw.net",
            443,
            "GET",
            "/api/channel/hls/example.m3u8?sig=leaked-signature&token=leaked-token",
            "HTTP/1.1",
            200,
            None,
        )
    finally:
        listener.stop()
        for handler in set(root_logger.handlers) - existing_handlers:
            root_logger.removeHandler(handler)
            handler.close()
        root_logger.setLevel(existing_level)

    contents = open(log_file, encoding="utf-8").read()
    assert "sig=<redacted>&token=<redacted>" in contents
    assert "leaked-signature" not in contents
    assert "leaked-token" not in contents


def test_sensitive_url_filter_redacts_tracebacks_and_stack_info():
    url = "https://usher.ttvnw.net/api/channel/hls/x.m3u8?sig=leaked-sig&token=leaked"
    try:
        raise ValueError(f"Max retries exceeded with url: {url}")
    except ValueError:
        record = logging.LogRecord(
            "x", 30, __file__, 1, "Unable to poll", None, sys.exc_info()
        )
    record.stack_info = f"Stack (most recent call last):\n  fetching {url}"

    SensitiveUrlFilter().filter(record)
    formatted = logging.Formatter().format(record)

    assert "ValueError: Max retries exceeded" in formatted
    assert "leaked" not in formatted
    assert "sig=<redacted>&token=<redacted>" in formatted


def test_configured_loggers_redact_exception_tracebacks(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = LoggerSettings(file_level=logging.DEBUG)
    root_logger = logging.getLogger()
    existing_handlers = set(root_logger.handlers)
    existing_level = root_logger.level

    log_file, listener = configure_loggers("test-user", settings)
    try:
        try:
            raise ConnectionError(
                "GET /v1/playlist/SIGNEDPLAYLIST.m3u8?sig=leaked-sig failed"
            )
        except ConnectionError:
            logging.getLogger("segments").warning("Unable to poll", exc_info=True)
    finally:
        listener.stop()
        for handler in set(root_logger.handlers) - existing_handlers:
            root_logger.removeHandler(handler)
            handler.close()
        root_logger.setLevel(existing_level)

    contents = open(log_file, encoding="utf-8").read()
    assert "ConnectionError" in contents
    assert "SIGNEDPLAYLIST" not in contents
    assert "leaked-sig" not in contents
