import io
import json
import logging
import sys
from contextlib import contextmanager
from datetime import datetime
from logging.handlers import QueueHandler, TimedRotatingFileHandler
from unittest.mock import MagicMock

import pytest
from colorama import Fore

from TwitchChannelPointsMiner import logger as logger_module
from TwitchChannelPointsMiner.classes.Settings import Events
from TwitchChannelPointsMiner.logger import (
    JSON_LOG_FIELDS,
    JsonFormatter,
    LoggerSettings,
    LoggingQueueHandler,
    LoggingFormat,
    SensitiveUrlFilter,
    configure_loggers,
)


@contextmanager
def _configured_loggers(settings, username="test-user"):
    """Run configure_loggers and undo its root logger changes afterwards."""
    root_logger = logging.getLogger()
    existing_handlers = set(root_logger.handlers)
    existing_level = root_logger.level

    log_file, listener = configure_loggers(username, settings)
    try:
        yield log_file, listener
    finally:
        listener.stop()
        for handler in listener.handlers:
            handler.close()
        for handler in set(root_logger.handlers) - existing_handlers:
            root_logger.removeHandler(handler)
            handler.close()
        root_logger.setLevel(existing_level)


def _json_formatter(**settings):
    return JsonFormatter(
        settings=LoggerSettings(**settings),
        username="alice",
        label="Console logger",
    )


def _json_entries(log_file):
    with open(log_file, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    return [json.loads(line) for line in lines]


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
        (" json ", LoggingFormat.JSON),
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

    with _configured_loggers(settings) as (log_file, listener):
        file_handler = next(
            handler
            for handler in listener.handlers
            if isinstance(handler, TimedRotatingFileHandler)
        )

        assert log_file == str(tmp_path / "logs" / "test-user.log")
        assert file_handler.when == "MIDNIGHT"
        assert file_handler.interval == 24 * 60 * 60
        assert file_handler.backupCount == 3


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

    with _configured_loggers(settings) as (log_file, _):
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

    with _configured_loggers(settings) as (log_file, _):
        try:
            raise ConnectionError(
                "GET /v1/playlist/SIGNEDPLAYLIST.m3u8?sig=leaked-sig failed"
            )
        except ConnectionError:
            logging.getLogger("segments").warning("Unable to poll", exc_info=True)

    contents = open(log_file, encoding="utf-8").read()
    assert "ConnectionError" in contents
    assert "SIGNEDPLAYLIST" not in contents
    assert "leaked-sig" not in contents


def test_json_formatter_writes_valid_json_with_traceback():
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord(
            "miner", logging.ERROR, __file__, 1, "failed %s", ("hard",), sys.exc_info()
        )

    entry = json.loads(_json_formatter().format(record))

    assert entry["level"] == "ERROR"
    assert entry["name"] == "miner"
    assert entry["message"] == "failed hard"
    assert entry["username"] == "alice"
    assert "ValueError: boom" in entry["exc_info"]
    assert set(entry) == set(JSON_LOG_FIELDS) | {"username", "exc_info"}
    # Local paths would otherwise end up in logs pasted into issues.
    assert "pathname" not in entry


def test_json_formatter_timestamp_uses_configured_time_zone():
    record = logging.LogRecord("miner", logging.INFO, __file__, 1, "hi", None, None)
    record.created = 0.0

    entry = json.loads(_json_formatter(time_zone="America/Denver").format(record))

    assert entry["timestamp"] == "1969-12-31T17:00:00.000-07:00"


def test_json_formatter_timestamp_carries_host_offset_without_time_zone():
    record = logging.LogRecord("miner", logging.INFO, __file__, 1, "hi", None, None)
    record.created = 1_700_000_000.25

    moment = datetime.fromisoformat(
        json.loads(_json_formatter().format(record))["timestamp"]
    )

    assert moment.utcoffset() is not None
    assert moment.timestamp() == record.created


def test_json_console_starts_without_auto_clear(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = LoggerSettings(console_format="json", auto_clear=False)

    with _configured_loggers(settings) as (log_file, _):
        assert log_file.startswith(str(tmp_path / "logs" / "test-user."))
        assert log_file.endswith(".log")


def test_json_log_file_is_redacted_and_parseable(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = LoggerSettings(file_format="json", file_level=logging.DEBUG)

    with _configured_loggers(settings) as (log_file, _):
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

    entries = _json_entries(log_file)
    request = next(
        entry for entry in entries if entry["name"] == "urllib3.connectionpool"
    )
    assert "sig=<redacted>&token=<redacted>" in request["message"]
    assert "leaked" not in open(log_file, encoding="utf-8").read()
    assert request["username"] == "test-user"
    assert request["level"] == "DEBUG"


def test_json_log_file_keeps_event_and_non_ascii(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = LoggerSettings(file_format="json")

    with _configured_loggers(settings) as (log_file, _):
        logging.getLogger("miner").info(
            "Zoë is live", extra={"event": Events.STREAMER_ONLINE, "emoji": ":tada:"}
        )

    entry = next(e for e in _json_entries(log_file) if e["name"] == "miner")
    assert entry["event"] == "STREAMER_ONLINE"
    assert entry["message"] == "Zoë is live"
    assert "\\u00eb" not in open(log_file, encoding="utf-8").read()


def test_text_console_uses_date_format(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = LoggerSettings(save=False, date_format="yyyy-mm-dd")

    with _configured_loggers(settings) as (_, listener):
        (console_handler,) = listener.handlers
        assert console_handler.formatter.datefmt == "%Y-%m-%d %H:%M:%S"


def test_json_log_file_carries_traceback_as_exc_info(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = LoggerSettings(file_format="json")

    with _configured_loggers(settings) as (log_file, _):
        try:
            raise ValueError("boom")
        except ValueError:
            logging.getLogger("miner").exception("failed")

    entry = next(e for e in _json_entries(log_file) if e["name"] == "miner")
    assert entry["message"] == "failed"
    assert "ValueError: boom" in entry["exc_info"]


@pytest.mark.parametrize(
    "encoding, errors, escaped",
    [
        ("cp1252", "strict", True),
        ("cp1252", "backslashreplace", True),
        ("ascii", "namereplace", True),
        ("ascii", "replace", True),
        ("ascii", "surrogateescape", True),
        ("utf-8", "strict", False),
        ("UTF8", "strict", False),
        ("cp65001", "strict", False),
        ("utf-8-sig", "strict", False),
    ],
)
def test_json_console_lines_stay_valid_and_lossless_on_any_encoding(
    monkeypatch, encoding, errors, escaped
):
    raw = io.BytesIO()
    stdout = io.TextIOWrapper(raw, encoding=encoding, errors=errors, write_through=True)
    monkeypatch.setattr(sys, "stdout", stdout)
    settings = LoggerSettings(save=False, console_format="json")

    with _configured_loggers(settings) as (_, listener):
        (console_handler,) = listener.handlers
        assert console_handler.formatter.ensure_ascii is escaped
        logging.getLogger("miner").info("Party \U0001f389 Zo\u00eb")

    lines = raw.getvalue().decode(encoding).splitlines()
    line = next(line for line in lines if '"miner"' in line)
    # Escapes such as backslashreplace's \\U0001f389 would not be JSON.
    assert json.loads(line)["message"] == "Party \U0001f389 Zo\u00eb"


def test_json_console_on_a_str_sink_is_not_escaped(monkeypatch):
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    settings = LoggerSettings(save=False, console_format="json")

    with _configured_loggers(settings) as (_, listener):
        (console_handler,) = listener.handlers
        assert console_handler.formatter.ensure_ascii is False
        logging.getLogger("miner").info("Zo\u00eb")

    assert '"message": "Zo\u00eb"' in stdout.getvalue()


def test_json_message_drops_embedded_console_colors():
    record = logging.LogRecord(
        "miner",
        logging.INFO,
        __file__,
        1,
        f"{Fore.GREEN}resumed{Fore.RESET}",
        None,
        None,
    )

    assert json.loads(_json_formatter().format(record))["message"] == "resumed"


def test_json_formatter_leaves_the_record_unchanged():
    record = logging.LogRecord(
        "miner", logging.INFO, __file__, 1, "hi %s", ("x",), None
    )
    before = dict(record.__dict__)

    _json_formatter().format(record)

    assert record.__dict__ == before


@pytest.mark.parametrize(
    "console_format, colored, initialized",
    [("text", True, True), ("json", True, False), ("text", False, False)],
)
def test_colorama_only_wraps_a_colored_text_console(
    monkeypatch, console_format, colored, initialized
):
    init = MagicMock()
    monkeypatch.setattr(logger_module, "init", init)
    settings = LoggerSettings(
        save=False, console_format=console_format, colored=colored
    )

    with _configured_loggers(settings):
        pass

    assert init.called is initialized


def test_logging_formats_assigned_after_construction_are_normalized():
    settings = LoggerSettings(save=False)
    settings.console_format = "json"
    assert settings.console_format is LoggingFormat.JSON

    with _configured_loggers(settings) as (_, listener):
        (console_handler,) = listener.handlers
        assert isinstance(console_handler.formatter, JsonFormatter)

    with pytest.raises(ValueError, match="file_format must be"):
        settings.file_format = "xml"


def test_logger_settings_positional_arguments_keep_their_meaning():
    settings = LoggerSettings(True, False, logging.WARNING, True, None, "dd/mm/yy", 10)

    assert settings.console_level == logging.WARNING
    assert settings.console_username is True
    assert settings.file_level == 10
    assert settings.console_format is LoggingFormat.TEXT
    assert settings.file_format is LoggingFormat.TEXT


@pytest.mark.parametrize(
    "settings, keeps_parts",
    [
        ({}, False),
        ({"console_format": "json"}, True),
        ({"file_format": "json"}, True),
        ({"file_format": "json", "save": False}, False),
    ],
)
def test_queue_keeps_traceback_parts_only_for_json(
    tmp_path, monkeypatch, settings, keeps_parts
):
    monkeypatch.chdir(tmp_path)
    root_logger = logging.getLogger()
    existing_handlers = set(root_logger.handlers)

    with _configured_loggers(LoggerSettings(**settings)):
        (queue_handler,) = set(root_logger.handlers) - existing_handlers

    assert isinstance(queue_handler, QueueHandler)
    assert isinstance(queue_handler, LoggingQueueHandler) is keeps_parts
