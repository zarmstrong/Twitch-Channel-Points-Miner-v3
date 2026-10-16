import io
import logging
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import colorama
from colorama import Fore

from TwitchChannelPointsMiner.classes.Settings import Events
from TwitchChannelPointsMiner.logger import (
    GlobalFormatter,
    LoggerSettings,
    NotificationHandler,
    configure_loggers,
)


def _telegram(events=(Events.STREAMER_ONLINE,)):
    notifier = MagicMock(chat_id=1)
    notifier.events = [str(event) for event in events]
    return notifier


def _record(msg="Streamer is online", level=logging.INFO, **extra):
    record = logging.LogRecord("miner", level, __file__, 1, msg, None, None)
    record.__dict__.update(extra)
    return record


@contextmanager
def _configured(settings, username="test-user"):
    root_logger = logging.getLogger()
    existing_handlers = set(root_logger.handlers)
    existing_level = root_logger.level
    _, listener = configure_loggers(username, settings)
    try:
        yield listener
    finally:
        listener.stop()
        for handler in set(root_logger.handlers) - existing_handlers:
            root_logger.removeHandler(handler)
            handler.close()
        root_logger.setLevel(existing_level)
        if settings.colored:
            # configure_loggers() wraps the standard streams for colors.
            colorama.deinit()


def _console_handler(listener):
    return next(
        handler
        for handler in listener.handlers
        if isinstance(handler, logging.StreamHandler)
        and not isinstance(handler, logging.FileHandler)
    )


def test_notifications_do_not_depend_on_the_console_formatter():
    telegram = _telegram()
    settings = LoggerSettings(save=False, emoji=True, telegram=telegram)

    with _configured(settings) as listener:
        # Any console formatter, such as a JSON one, must keep notifications.
        _console_handler(listener).setFormatter(logging.Formatter("%(message)s"))
        logging.getLogger("miner").info(
            "Streamer is online",
            extra={"event": Events.STREAMER_ONLINE, "emoji": ":tv:"},
        )

    telegram.send.assert_called_once_with(
        "📺  Streamer is online", Events.STREAMER_ONLINE
    )


def test_notification_message_has_username_but_no_console_color():
    telegram = _telegram()
    settings = LoggerSettings(
        save=False, console_username=True, colored=True, emoji=False, telegram=telegram
    )

    with _configured(settings, username="alice") as listener:
        stream = io.StringIO()
        _console_handler(listener).setStream(stream)
        logging.getLogger("miner").info(
            "Streamer is online", extra={"event": Events.STREAMER_ONLINE}
        )

    telegram.send.assert_called_once_with(
        "[alice] Streamer is online", Events.STREAMER_ONLINE
    )
    console_line = stream.getvalue()
    assert console_line.count("[alice] ") == 1
    assert f"{Fore.RESET}[alice] Streamer is online{Fore.RESET}" in console_line


def test_console_formatter_does_not_change_records_seen_by_other_handlers():
    settings = LoggerSettings(
        console_username=True, colored=True, emoji=True, username="[alice] "
    )
    formatter = GlobalFormatter(fmt="%(message)s", settings=settings)
    record = _record("plain %s", event=Events.STREAMER_ONLINE, emoji=":tv:")
    record.args = ("text",)

    assert "[alice] " in formatter.format(record)
    assert record.msg == "plain %s"
    assert record.args == ("text",)


def test_notifications_skip_records_without_an_event():
    telegram = _telegram()
    handler = NotificationHandler(LoggerSettings(telegram=telegram, username=""))

    handler.handle(_record())

    telegram.send.assert_not_called()


def test_notifications_do_not_depend_on_the_console_level():
    telegram = _telegram()
    settings = LoggerSettings(
        save=False, console_level=logging.WARNING, telegram=telegram
    )

    with _configured(settings) as listener:
        stream = io.StringIO()
        _console_handler(listener).setStream(stream)
        logging.getLogger("miner").info(
            "Streamer is online", extra={"event": Events.STREAMER_ONLINE}
        )

    assert stream.getvalue() == ""
    telegram.send.assert_called_once_with("Streamer is online", Events.STREAMER_ONLINE)


def test_failing_provider_does_not_block_the_others(caplog):
    telegram = _telegram()
    telegram.send.side_effect = RuntimeError(
        "POST https://api.telegram.org/botSECRET-TOKEN/sendMessage failed"
    )
    discord = MagicMock(webhook_api="https://discord.example/hook")
    discord.events = [str(Events.STREAMER_ONLINE)]
    handler = NotificationHandler(
        LoggerSettings(telegram=telegram, discord=discord, username="")
    )

    handler.handle(_record(event=Events.STREAMER_ONLINE))

    discord.send.assert_called_once_with("Streamer is online", Events.STREAMER_ONLINE)
    assert "Unable to send telegram notification: RuntimeError" in caplog.text
    assert "SECRET-TOKEN" not in caplog.text


def test_force_alert_sends_an_event_outside_the_allowlist_once():
    telegram = _telegram(events=())
    handler = NotificationHandler(LoggerSettings(telegram=telegram, username=""))

    handler.handle(_record(event=Events.STREAMER_ONLINE, force_alert=True))

    telegram.send.assert_called_once_with("Streamer is online", Events.STREAMER_ONLINE)
    assert telegram.events == []


def test_notifications_skip_decoration_when_no_provider_is_configured():
    handler = NotificationHandler(LoggerSettings(username=""))

    with patch("TwitchChannelPointsMiner.logger.decorate_message") as decorate:
        handler.handle(_record(event=Events.STREAMER_ONLINE))

    decorate.assert_not_called()


def test_delivery_failure_reason_is_logged(caplog):
    telegram = _telegram()
    telegram.send.return_value = (
        False,
        "Telegram rejected the configured credentials (HTTP 401).",
    )
    handler = NotificationHandler(LoggerSettings(telegram=telegram, username=""))

    with caplog.at_level(logging.WARNING):
        handler.handle(_record(event=Events.STREAMER_ONLINE))

    assert (
        "Notification not sent: Telegram rejected the configured credentials "
        "(HTTP 401)." in caplog.text
    )


def test_events_outside_the_allowlist_are_skipped_quietly(caplog):
    telegram = _telegram(events=())
    telegram.send.return_value = (False, "This event is not enabled for Telegram.")
    handler = NotificationHandler(LoggerSettings(telegram=telegram, username=""))

    with caplog.at_level(logging.WARNING):
        handler.handle(_record(event=Events.STREAMER_ONLINE))

    telegram.send.assert_not_called()
    assert caplog.text == ""


def test_successful_delivery_logs_nothing(caplog):
    telegram = _telegram()
    telegram.send.return_value = (True, None)
    handler = NotificationHandler(LoggerSettings(telegram=telegram, username=""))

    with caplog.at_level(logging.WARNING):
        handler.handle(_record(event=Events.STREAMER_ONLINE))

    telegram.send.assert_called_once()
    assert caplog.text == ""
