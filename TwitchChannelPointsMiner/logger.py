import copy
import json
import logging
import os
import platform
import queue
import re
import sys
import threading
from datetime import datetime
from enum import Enum
from logging.handlers import QueueHandler, QueueListener, TimedRotatingFileHandler
from pathlib import Path

import emoji
import pytz
from colorama import Fore, init

from TwitchChannelPointsMiner.classes.Discord import Discord
from TwitchChannelPointsMiner.classes.Email import Email
from TwitchChannelPointsMiner.classes.Gotify import Gotify
from TwitchChannelPointsMiner.classes.Matrix import Matrix
from TwitchChannelPointsMiner.classes.Ntfy import Ntfy
from TwitchChannelPointsMiner.classes.Pushover import Pushover
from TwitchChannelPointsMiner.classes.Settings import Events
from TwitchChannelPointsMiner.classes.Telegram import Telegram
from TwitchChannelPointsMiner.classes.Webhook import Webhook
from TwitchChannelPointsMiner.utils import remove_emoji


# Fore: BLACK, RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN, WHITE, RESET.
class ColorPalette(object):
    def __init__(self, **kwargs):
        # Init with default values RESET for all and GREEN and RED only for WIN and LOSE bet
        # Then set args from kwargs
        for k in Events:
            setattr(self, str(k), Fore.RESET)
        setattr(self, "BET_WIN", Fore.GREEN)
        setattr(self, "BET_LOSE", Fore.RED)

        for k in kwargs:
            if k.upper() in dir(self) and getattr(self, k.upper()) is not None:
                if kwargs[k] in [
                    Fore.BLACK,
                    Fore.RED,
                    Fore.GREEN,
                    Fore.YELLOW,
                    Fore.BLUE,
                    Fore.MAGENTA,
                    Fore.CYAN,
                    Fore.WHITE,
                    Fore.RESET,
                ]:
                    setattr(self, k.upper(), kwargs[k])
                elif kwargs[k].upper() in [
                    "BLACK",
                    "RED",
                    "GREEN",
                    "YELLOW",
                    "BLUE",
                    "MAGENTA",
                    "CYAN",
                    "WHITE",
                    "RESET",
                ]:
                    setattr(self, k.upper(), getattr(Fore, kwargs[k].upper()))

    def get(self, key):
        color = getattr(self, str(key)) if str(key) in dir(self) else None
        return Fore.RESET if color is None else color


class LoggingFormat(Enum):
    JSON = "json"
    TEXT = "text"


def _logging_format(name, value):
    """Accept a LoggingFormat or its string value, such as "json"."""
    try:
        return LoggingFormat(value.lower() if isinstance(value, str) else value)
    except ValueError:
        raise ValueError(f'{name} must be "text" or "json"') from None


class LoggerSettings:
    __slots__ = [
        "save",
        "less",
        "console_format",
        "console_level",
        "console_username",
        "time_zone",
        "file_level",
        "date_format",
        "file_format",
        "emoji",
        "colored",
        "color_palette",
        "auto_clear",
        "log_retention_days",
        "telegram",
        "discord",
        "webhook",
        "matrix",
        "pushover",
        "gotify",
        "ntfy",
        "email",
        "daily_report",
        "daily_report_time",
        "username",
    ]

    def __init__(
        self,
        save: bool = True,
        less: bool = False,
        console_format: LoggingFormat | str = LoggingFormat.TEXT,
        console_level: int = logging.INFO,
        console_username: bool = False,
        time_zone: str or None = None,
        date_format: str = "dd/mm/yy",
        file_format: LoggingFormat | str = LoggingFormat.TEXT,
        file_level: int = logging.DEBUG,
        emoji: bool = platform.system() != "Windows",
        colored: bool = False,
        color_palette: ColorPalette = ColorPalette(),
        auto_clear: bool = True,
        log_retention_days: int = 7,
        telegram: Telegram or None = None,
        discord: Discord or None = None,
        webhook: Webhook or None = None,
        matrix: Matrix or None = None,
        pushover: Pushover or None = None,
        gotify: Gotify or None = None,
        ntfy: Ntfy or None = None,
        email: Email or None = None,
        daily_report: bool = False,
        daily_report_time: str = "00:00",
        username: str or None = None,
    ):
        self.save = save
        self.less = less
        self.console_format = _logging_format("console_format", console_format)
        self.console_level = console_level
        self.console_username = console_username
        self.time_zone = time_zone
        if not isinstance(date_format, str):
            raise TypeError("date_format must be a string")
        date_parts = date_format.replace("-", "/").replace(".", "/").split("/")
        if sorted(date_parts) != ["dd", "mm", "yy"] and sorted(date_parts) != [
            "dd",
            "mm",
            "yyyy",
        ]:
            raise ValueError(
                "date_format must contain dd, mm, and yy or yyyy, separated by /, -, or ."
            )
        self.date_format = date_format
        self.file_format = _logging_format("file_format", file_format)
        self.file_level = file_level
        self.emoji = emoji
        self.colored = colored
        self.color_palette = color_palette
        self.auto_clear = auto_clear
        if (
            isinstance(log_retention_days, bool)
            or not isinstance(log_retention_days, int)
            or log_retention_days < 1
        ):
            raise ValueError("log_retention_days must be a positive integer")
        self.log_retention_days = log_retention_days
        self.telegram = telegram
        self.discord = discord
        self.webhook = webhook
        self.matrix = matrix
        self.pushover = pushover
        self.gotify = gotify
        self.ntfy = ntfy
        self.email = email
        self.daily_report = daily_report
        try:
            datetime.strptime(daily_report_time, "%H:%M")
        except (TypeError, ValueError) as error:
            raise ValueError(
                "daily_report_time must use 24-hour HH:MM format"
            ) from error
        self.daily_report_time = daily_report_time
        self.username = username


class JsonFormatter(logging.Formatter):
    """
    Formatter that outputs JSON strings after parsing the LogRecord.

    @param dict fmt_dict: Key: logging format attribute pairs. Defaults to {"message": "message"}.
    @param str time_format: time.strftime() format string. Default: "%Y-%m-%dT%H:%M:%S"
    @param str msec_format: Microsecond formatting. Appended at the end. Default: "%s.%03dZ"
    """

    def __init__(
        self,
        fmt_dict: dict = None,
        time_format: str = "%Y-%m-%dT%H:%M:%S",
        msec_format: str = "%s.%03dZ",
    ):
        super().__init__()
        self.fmt_dict = fmt_dict if fmt_dict is not None else {"message": "message"}
        self.default_time_format = time_format
        self.default_msec_format = msec_format
        self.datefmt = None

    def usesTime(self) -> bool:
        """
        Overwritten to look for the attribute in the format dict values instead of the fmt string.
        """
        return "asctime" in self.fmt_dict.values()

    def formatMessage(self, record) -> dict:
        """
        Overwritten to return a dictionary of the relevant LogRecord attributes instead of a string.
        KeyError is raised if an unknown attribute is provided in the fmt_dict.
        """
        return {
            fmt_key: record.__dict__[fmt_val]
            for fmt_key, fmt_val in self.fmt_dict.items()
        }

    def format(self, record) -> str:
        """
        Mostly the same as the parent's class method, the difference being that a dict is manipulated and dumped as JSON
        instead of a string.
        """
        record.message = record.getMessage()

        if self.usesTime():
            record.asctime = self.formatTime(record, self.datefmt)

        message_dict = self.formatMessage(record)

        if record.exc_info:
            # Cache the traceback text to avoid converting it multiple times
            # (it's constant anyway)
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)

        if record.exc_text:
            message_dict["exc_info"] = record.exc_text

        if record.stack_info:
            message_dict["stack_info"] = self.formatStack(record.stack_info)

        return json.dumps(message_dict, default=str)


class CategoryConsoleFilter(logging.Filter):
    """Allow category records to bypass the console's global level."""

    def __init__(self, global_level):
        super().__init__()
        self.global_level = global_level

    def filter(self, record):
        return record.levelno >= self.global_level or getattr(
            record, "category_log", False
        )


class SensitiveUrlFilter(logging.Filter):
    """Redact signed Twitch playback credentials from every log record.

    Third-party loggers such as urllib3 log each request line at DEBUG, which
    for stream segment polling includes the usher playlist's ``sig`` and
    ``token`` query values and the signed media playlist and segment paths.
    Tracebacks and stack info are redacted too, since request exceptions
    usually quote the full URL.
    """

    PATTERNS = (
        (re.compile(r"([?&](?:sig|token)=)[^&\s\"']+"), r"\1<redacted>"),
        (re.compile(r"(/v1/(?:playlist|segment)/)[^\s\"']+"), r"\1<redacted>"),
    )

    _formatter = logging.Formatter()

    @classmethod
    def redact(cls, text):
        if not text or ("=" not in text and "/v1/" not in text):
            return text
        for pattern, replacement in cls.PATTERNS:
            text = pattern.sub(replacement, text)
        return text

    def filter(self, record):
        try:
            message = record.getMessage()
        except Exception:
            message = None
        if message is not None:
            redacted = self.redact(message)
            if redacted != message:
                record.msg = redacted
                record.args = None

        if record.exc_info and not record.exc_text:
            # Formatters reuse exc_text instead of re-formatting exc_info, so
            # formatting the traceback here lets it be redacted.
            record.exc_text = self._formatter.formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self.redact(record.exc_text)
        if record.stack_info:
            record.stack_info = self.redact(record.stack_info)
        return True


class FileFormatter(logging.Formatter):
    def __init__(self, *, fmt, settings: LoggerSettings, datefmt=None):
        self.settings = settings
        self.timezone = None
        if settings.time_zone:
            try:
                self.timezone = pytz.timezone(settings.time_zone)
                logging.info(f"File logger time zone set to: {self.timezone}")
            except pytz.UnknownTimeZoneError:
                logging.error(f"File logger: invalid time zone: {settings.time_zone}")
        logging.Formatter.__init__(self, fmt=fmt, datefmt=datefmt)

    def formatTime(self, record, datefmt=None):
        if self.timezone:
            dt = datetime.fromtimestamp(record.created, self.timezone)
        else:
            dt = datetime.fromtimestamp(record.created)
        return dt.strftime(datefmt or self.default_time_format)


class GlobalFormatter(logging.Formatter):
    def __init__(self, *, fmt, settings: LoggerSettings, datefmt=None):
        self.settings = settings
        self.timezone = None
        if settings.time_zone:
            try:
                self.timezone = pytz.timezone(settings.time_zone)
                logging.info(f"Console logger time zone set to: {self.timezone}")
            except pytz.UnknownTimeZoneError:
                logging.error(
                    f"Console logger: invalid time zone: {settings.time_zone}"
                )
        logging.Formatter.__init__(self, fmt=fmt, datefmt=datefmt)

    def formatTime(self, record, datefmt=None):
        if self.timezone:
            dt = datetime.fromtimestamp(record.created, self.timezone)
        else:
            dt = datetime.fromtimestamp(record.created)
        return dt.strftime(datefmt or self.default_time_format)

    def format(self, record):
        # Decorate a copy so other handlers never see emoji, username or color
        # changes regardless of the order the queue listener calls them in.
        record = logging.makeLogRecord(record.__dict__)
        record.msg = decorate_message(record, self.settings)
        record.args = None

        if hasattr(record, "event") and self.settings.colored is True:
            record.msg = f"{self.settings.color_palette.get(record.event)}{record.msg}{Fore.RESET}"

        return super().format(record)


def decorate_message(record, settings):
    """Return the record's message with emoji and the username prefix applied."""
    message = record.getMessage()
    if hasattr(record, "emoji") and settings.emoji is True:
        message = emoji.emojize(f"{record.emoji}  {message.strip()}", language="alias")

    if settings.emoji is False:
        if "\u2192" in message:
            message = message.replace("\u2192", "-->")

        # With the update of Stream class, the Stream Title may contain emoji
        # Full remove using a method from utils.
        message = remove_emoji(message)

    return (settings.username or "") + message


class NotificationOnlyFilter(logging.Filter):
    """Keep records meant only for notifiers out of the console and log files."""

    def filter(self, record):
        return not getattr(record, "notification_only", False)


class NotificationHandler(logging.Handler):
    """Send event records to the configured notification providers.

    Notifications are independent of the console and file formatters, so
    changing how log lines are rendered never changes what gets sent.
    """

    # Each configured provider, with a check for the example values from
    # config.example.py that mean it was never set up.
    PLACEHOLDERS = {
        "telegram": lambda notifier: notifier.chat_id == 123456789,
        "discord": lambda notifier: notifier.webhook_api
        == "https://discord.com/api/webhooks/0123456789/0a1B2c3D4e5F6g7H8i9J",
        "webhook": lambda notifier: notifier.endpoint == "https://example.com/webhook",
        "matrix": lambda notifier: notifier.room_id == "..."
        or not notifier.access_token,
        "pushover": lambda notifier: notifier.userkey == "YOUR-ACCOUNT-TOKEN"
        or notifier.token == "YOUR-APPLICATION-TOKEN",
        "gotify": lambda notifier: notifier.endpoint
        == "https://example.com/message?token=TOKEN",
        "ntfy": lambda notifier: notifier.topic == "YOUR_NTFY_TOPIC",
        "email": lambda notifier: notifier.host == "smtp.example.com",
    }

    def __init__(self, settings: LoggerSettings, level=logging.NOTSET):
        super().__init__(level)
        self.settings = settings
        # Last failure reason of each provider that is currently failing, so
        # an outage is reported once rather than on every event.
        self._failures = {}

    def has_providers(self):
        """Return True when at least one provider is set up to send."""
        return any(
            getattr(self.settings, provider, None) is not None
            and not is_placeholder(getattr(self.settings, provider))
            for provider, is_placeholder in self.PLACEHOLDERS.items()
        )

    def emit(self, record):
        if not hasattr(record, "event") or not self.has_providers():
            return
        # Problems are logged through the queue rather than handleError(),
        # which prints the traceback to stderr without redaction. Only the
        # exception type is kept, since errors can quote webhook or token URLs.
        try:
            message = decorate_message(record, self.settings)
        except Exception as error:
            logging.getLogger(__name__).warning(
                f"Unable to build the notification message: {type(error).__name__}"
            )
            return
        for provider in self.PLACEHOLDERS:
            try:
                result = self._dispatch(provider, record, message)
            except Exception as error:
                # One failing provider must not stop the others.
                result = (False, f"The notifier raised {type(error).__name__}.")
            self._report_result(provider, result)

    def _dispatch(self, provider, record, message):
        notifier = getattr(self.settings, provider, None)
        if (
            notifier is None
            or getattr(record, f"skip_{provider}", False)
            or self.PLACEHOLDERS[provider](notifier)
        ):
            return None
        return self._send(notifier, record, message)

    def _report_result(self, provider, result):
        # Notifiers catch their own delivery errors and return a reason that
        # is already safe to show, without URLs or credentials.
        if not (isinstance(result, tuple) and len(result) == 2):
            return
        success, reason = result
        log = logging.getLogger(__name__)
        if success:
            if self._failures.pop(provider, None) is not None:
                log.info(f"Notifications to {provider} are being delivered again")
        elif provider not in self._failures:
            self._failures[provider] = reason
            log.warning(
                f"Notifications to {provider} are failing: {reason} "
                "Further failures are not logged until one gets through."
            )
        elif self._failures[provider] != reason:
            self._failures[provider] = reason
            log.debug(f"Notifications to {provider} are still failing: {reason}")

    @staticmethod
    def _send(notifier, record, message):
        """Send a record, optionally bypassing a notifier's event allowlist."""
        event = str(record.event)
        force_alert = getattr(record, "force_alert", False)
        added = force_alert and event not in notifier.events
        if not added and event not in notifier.events:
            return
        if added:
            notifier.events.append(event)
        try:
            return notifier.send(message, record.event)
        finally:
            if added:
                notifier.events.remove(event)


class NotificationQueueHandler(QueueHandler):
    """Hand event records to the notification thread."""

    def emit(self, record):
        if hasattr(record, "event"):
            super().emit(record)

    def prepare(self, record):
        # NotificationHandler builds its own message, so the record needs no
        # flattening. A copy keeps the two threads from sharing one record.
        return copy.copy(record)


class LoggingQueueListener(QueueListener):
    """Write log records, and send notifications on a thread of their own.

    A slow or unreachable notifier then never delays console or file output.
    stop() drains both queues, so alerts logged before shutdown are still sent,
    but waits at most NOTIFICATION_SHUTDOWN_TIMEOUT seconds for notifiers.
    """

    NOTIFICATION_SHUTDOWN_TIMEOUT = 30

    def __init__(self, logger_queue, *handlers, settings, respect_handler_level=True):
        self.notification_listener = None
        self._stop_lock = threading.Lock()
        self._stopped = False
        notification_handler = NotificationHandler(settings)
        if notification_handler.has_providers():
            # Logger settings changes restart the miner, so a notification
            # thread is only needed when a provider is set up at startup.
            notification_queue = queue.Queue(-1)
            self.notification_listener = QueueListener(
                notification_queue, notification_handler
            )
            handlers = (*handlers, NotificationQueueHandler(notification_queue))
        super().__init__(
            logger_queue, *handlers, respect_handler_level=respect_handler_level
        )

    def start(self):
        if self.notification_listener is not None:
            self.notification_listener.start()
        super().start()

    def stop(self):
        # Safe to call more than once: the miner's end() and the runner can
        # both stop the listener.
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
            # Stop the log listener first, so every record it hands over is
            # queued before the notification thread drains and exits.
            super().stop()
            if self.notification_listener is not None:
                self._stop_notifications()
            # Problems reported while the notification queue drained were
            # logged after the log listener stopped; write them out now.
            while True:
                try:
                    record = self.dequeue(False)
                except queue.Empty:
                    break
                if record is not self._sentinel:
                    self.handle(record)

    def _stop_notifications(self):
        # QueueListener.stop() joins without a timeout, so an unreachable
        # notifier with a backlog could hold up shutdown indefinitely.
        listener = self.notification_listener
        listener.enqueue_sentinel()
        listener._thread.join(self.NOTIFICATION_SHUTDOWN_TIMEOUT)
        if listener._thread.is_alive():
            logging.getLogger(__name__).warning(
                "Stopped waiting for notifications after "
                f"{self.NOTIFICATION_SHUTDOWN_TIMEOUT} seconds; alerts still "
                "queued were not sent."
            )
        listener._thread = None


# Logging extra for records that should reach the console and log files but
# no notifier, such as status lines that are summarized separately.
SKIP_NOTIFICATIONS = {
    f"skip_{provider}": True for provider in NotificationHandler.PLACEHOLDERS
}


def configure_loggers(username, settings):
    if settings.colored is True:
        init(autoreset=True)

    # Queue handler that will handle the logger queue
    logger_queue = queue.Queue(-1)
    queue_handler = QueueHandler(logger_queue)
    # Redact before the record is queued, so no handler ever sees credentials.
    queue_handler.addFilter(SensitiveUrlFilter())
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    # Add the queue handler to the root logger
    # Send log messages to another thread through the queue
    root_logger.addHandler(queue_handler)

    # Adding a username to the format based on settings
    console_username = "" if settings.console_username is False else f"[{username}] "

    settings.username = console_username

    log_date_format = (
        settings.date_format.replace("yyyy", "%Y")
        .replace("yy", "%y")
        .replace("mm", "%m")
        .replace("dd", "%d")
    )

    json_format = {
        "timestamp": "asctime",
        "exc_text": "exc_text",
        "filename": "filename",
        "funcName": "funcName",
        "level": "levelname",
        "lineno": "lineno",
        "module": "module",
        "msecs": "msecs",
        "message": "message",
        "name": "name",
        "pathname": "pathname",
        "process": "process",
        "processName": "processName",
        "relativeCreated": "relativeCreated",
        "stack_info": "stack_info",
        "thread": "thread",
        "threadName": "threadName",
    }

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG)
    console_handler.addFilter(CategoryConsoleFilter(settings.console_level))
    console_handler.addFilter(NotificationOnlyFilter())

    formatter = None
    if settings.console_format == LoggingFormat.TEXT:
        formatter = GlobalFormatter(
            fmt=(
                "%(asctime)s - %(levelname)s - [%(funcName)s]: %(message)s"
                if settings.less is False
                else "%(asctime)s - %(message)s"
            ),
            datefmt=f"{log_date_format} %H:%M:%S",
            settings=settings,
        )
    elif settings.console_format == LoggingFormat.JSON:
        formatter = JsonFormatter(fmt_dict=json_format)
    else:
        raise ValueError("Invalid console format")

    console_handler.setFormatter(formatter)

    if settings.save is True:
        logs_path = os.path.join(Path().absolute(), "logs")
        Path(logs_path).mkdir(parents=True, exist_ok=True)
        if settings.auto_clear is True:
            logs_file = os.path.join(
                logs_path,
                f"{username}.log",
            )
            file_handler = TimedRotatingFileHandler(
                logs_file,
                when="midnight",
                interval=1,
                backupCount=settings.log_retention_days,
                encoding="utf-8",
                delay=False,
            )
        else:
            # Getting time zone from the console_handler's formatter since they are the same
            tz = (
                None
                if console_handler.formatter.timezone is None
                else console_handler.formatter.timezone
            )
            logs_file = os.path.join(
                logs_path,
                f"{username}.{datetime.now(tz).strftime('%Y%m%d-%H%M%S')}.log",
            )
            file_handler = logging.FileHandler(logs_file, "w", "utf-8")

        file_formatter = None
        if settings.file_format == LoggingFormat.TEXT:
            file_formatter = FileFormatter(
                fmt="%(asctime)s - %(levelname)s - %(name)s - [%(funcName)s]: %(message)s",
                datefmt=f"{log_date_format} %H:%M:%S",
                settings=settings,
            )
        elif settings.file_format == LoggingFormat.JSON:
            file_formatter = JsonFormatter(fmt_dict=json_format)
        else:
            raise ValueError("Invalid file format")

        file_handler.setFormatter(file_formatter)
        file_handler.setLevel(settings.file_level)
        file_handler.addFilter(NotificationOnlyFilter())

        # Add logger handlers to the logger queue and start the process
        # Each provider's event list decides what is sent, so a quieter console
        # never silences notifications.
        queue_listener = LoggingQueueListener(
            logger_queue, file_handler, console_handler, settings=settings
        )
        queue_listener.start()
        return logs_file, queue_listener
    else:
        queue_listener = LoggingQueueListener(
            logger_queue, console_handler, settings=settings
        )
        queue_listener.start()
        return None, queue_listener
