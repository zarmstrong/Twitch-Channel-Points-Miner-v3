import io
import logging
import queue
import threading
import time
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import colorama
from colorama import Fore

from TwitchChannelPointsMiner.classes.Settings import Events
from TwitchChannelPointsMiner.logger import (
    GlobalFormatter,
    LoggerSettings,
    LoggingQueueListener,
    SKIP_NOTIFICATIONS,
    NotificationHandler,
    NotificationQueueHandler,
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
    assert (
        "Notifications to telegram are failing: The notifier raised RuntimeError."
        in caplog.text
    )
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
        "Notifications to telegram are failing: Telegram rejected the configured "
        "credentials (HTTP 401)." in caplog.text
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


def test_notification_only_records_skip_console_and_log_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    telegram = _telegram(events=(Events.DROP_STATUS,))
    settings = LoggerSettings(file_level=logging.DEBUG, telegram=telegram)

    with _configured(settings) as listener:
        stream = io.StringIO()
        _console_handler(listener).setStream(stream)
        logger = logging.getLogger("miner")
        logger.info(
            "Drop: 50%", extra={"event": Events.DROP_STATUS, **SKIP_NOTIFICATIONS}
        )
        logger.info(
            "streamer is streaming\nDrop: 50%",
            extra={"event": Events.DROP_STATUS, "notification_only": True},
        )

    telegram.send.assert_called_once_with(
        "streamer is streaming\nDrop: 50%", Events.DROP_STATUS
    )
    log_file = (tmp_path / "logs" / "test-user.log").read_text(encoding="utf-8")
    for output in (stream.getvalue(), log_file):
        assert "Drop: 50%" in output
        assert "streamer is streaming" not in output


def test_skip_notifications_silences_every_provider():
    assert set(SKIP_NOTIFICATIONS) == {
        f"skip_{provider}" for provider in NotificationHandler.PLACEHOLDERS
    }
    notifiers = {}
    for provider in NotificationHandler.PLACEHOLDERS:
        notifier = MagicMock()
        notifier.events = [str(Events.DROP_STATUS)]
        notifiers[provider] = notifier
    settings = LoggerSettings(username="", **notifiers)
    handler = NotificationHandler(settings)

    handler.handle(
        _record("Watching for points", event=Events.DROP_STATUS, **SKIP_NOTIFICATIONS)
    )

    for notifier in notifiers.values():
        notifier.send.assert_not_called()


def test_slow_notifier_does_not_delay_console_output():
    release = threading.Event()
    telegram = _telegram()
    telegram.send.side_effect = lambda *_args: release.wait(5) and (True, None)
    settings = LoggerSettings(save=False, telegram=telegram)

    with _configured(settings) as listener:
        stream = io.StringIO()
        _console_handler(listener).setStream(stream)
        logger = logging.getLogger("miner")
        logger.info("Streamer is online", extra={"event": Events.STREAMER_ONLINE})
        logger.info("next line")

        deadline = time.monotonic() + 2
        while "next line" not in stream.getvalue() and time.monotonic() < deadline:
            time.sleep(0.01)
        console_before_release = stream.getvalue()
        release.set()

    assert "next line" in console_before_release
    # Stopping the listener still delivers the queued notification.
    telegram.send.assert_called_once_with("Streamer is online", Events.STREAMER_ONLINE)


def test_failure_while_draining_at_shutdown_is_still_logged():
    def slow_failure(*_args):
        # Fail after stop() has already stopped the log listener.
        time.sleep(0.2)
        return False, "Telegram rejected the configured credentials (HTTP 401)."

    telegram = _telegram()
    telegram.send.side_effect = slow_failure
    settings = LoggerSettings(save=False, telegram=telegram)

    with _configured(settings) as listener:
        stream = io.StringIO()
        _console_handler(listener).setStream(stream)
        logging.getLogger("miner").info(
            "Final report", extra={"event": Events.STREAMER_ONLINE}
        )

    assert "Notifications to telegram are failing" in stream.getvalue()


def test_an_outage_is_logged_once_until_delivery_recovers(caplog):
    first = "Discord reported a server error (HTTP 502)."
    second = "Discord reported a server error (HTTP 503)."
    telegram = _telegram()
    telegram.send.side_effect = [
        (False, first),
        (False, second),
        RuntimeError("boom"),
        (True, None),
        (False, first),
    ]
    handler = NotificationHandler(LoggerSettings(telegram=telegram, username=""))

    with caplog.at_level(logging.DEBUG, logger="TwitchChannelPointsMiner.logger"):
        for _ in range(5):
            handler.handle(_record(event=Events.STREAMER_ONLINE))

    suffix = " Further failures are not logged until one gets through."
    assert [(r.levelname, r.getMessage()) for r in caplog.records] == [
        ("WARNING", f"Notifications to telegram are failing: {first}{suffix}"),
        ("DEBUG", f"Notifications to telegram are still failing: {second}"),
        (
            "DEBUG",
            "Notifications to telegram are still failing: "
            "The notifier raised RuntimeError.",
        ),
        ("INFO", "Notifications to telegram are being delivered again"),
        ("WARNING", f"Notifications to telegram are failing: {first}{suffix}"),
    ]


def test_message_build_failure_is_worded_clearly(caplog, monkeypatch):
    telegram = _telegram()
    handler = NotificationHandler(LoggerSettings(telegram=telegram, username=""))
    monkeypatch.setattr(
        "TwitchChannelPointsMiner.logger.decorate_message",
        MagicMock(side_effect=TypeError("bad")),
    )

    with caplog.at_level(logging.WARNING):
        handler.handle(_record(event=Events.STREAMER_ONLINE))

    assert caplog.messages == ["Unable to build the notification message: TypeError"]
    telegram.send.assert_not_called()


def test_notification_thread_gets_its_own_copy_of_the_record():
    notification_queue = queue.Queue()
    record = _record(event=Events.STREAMER_ONLINE)

    NotificationQueueHandler(notification_queue).handle(record)
    queued = notification_queue.get_nowait()

    assert queued is not record
    assert queued.event == Events.STREAMER_ONLINE
    assert queued.getMessage() == "Streamer is online"


def test_shutdown_stops_waiting_for_a_stuck_notifier(monkeypatch):
    monkeypatch.setattr(LoggingQueueListener, "NOTIFICATION_SHUTDOWN_TIMEOUT", 0.2)
    release = threading.Event()
    telegram = _telegram()
    telegram.send.side_effect = lambda *_args: release.wait(5) and (True, None)
    settings = LoggerSettings(save=False, telegram=telegram)

    try:
        with _configured(settings) as listener:
            stream = io.StringIO()
            _console_handler(listener).setStream(stream)
            logging.getLogger("miner").info(
                "Streamer is online", extra={"event": Events.STREAMER_ONLINE}
            )
            started = time.monotonic()
        elapsed = time.monotonic() - started
    finally:
        release.set()

    assert elapsed < 2
    assert "Stopped waiting for notifications after 0.2 seconds" in stream.getvalue()


def test_stop_can_be_called_twice():
    settings = LoggerSettings(save=False, telegram=_telegram())

    with _configured(settings) as listener:
        listener.stop()


def test_no_notification_thread_without_a_provider():
    with _configured(LoggerSettings(save=False)) as listener:
        assert listener.notification_listener is None
        assert not any(
            isinstance(handler, NotificationQueueHandler)
            for handler in listener.handlers
        )

    with _configured(LoggerSettings(save=False, telegram=_telegram())) as listener:
        assert listener.notification_listener is not None


def _log_failure(event=Events.CONFIGURATION):
    try:
        raise ValueError("title \U0001f389 \u2192 done")
    except ValueError:
        logging.getLogger("miner").error(
            "The miner stopped", exc_info=True, extra={"event": event}
        )


def test_notification_message_keeps_the_traceback_with_emoji_removed():
    telegram = _telegram(events=(Events.CONFIGURATION,))
    settings = LoggerSettings(save=False, emoji=False, telegram=telegram)

    with _configured(settings):
        _log_failure()

    message, event = telegram.send.call_args.args
    assert event == Events.CONFIGURATION
    assert message.startswith("The miner stopped\nTraceback")
    assert "ValueError: title" in message
    assert "\U0001f389" not in message
    assert "--> done" in message


def test_console_traceback_is_decorated_like_the_message():
    settings = LoggerSettings(save=False, emoji=False, colored=True)

    with _configured(settings) as listener:
        stream = io.StringIO()
        _console_handler(listener).setStream(stream)
        _log_failure(event=Events.BET_WIN)

    output = stream.getvalue()
    assert "ValueError: title" in output
    assert "\U0001f389" not in output
    assert "--> done" in output
    # The event color wraps the traceback as well, then resets.
    assert output.rstrip("\n").endswith(Fore.RESET)
    assert output.count("Traceback") == 1
