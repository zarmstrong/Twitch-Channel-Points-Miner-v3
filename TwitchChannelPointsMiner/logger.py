import codecs
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
        return LoggingFormat(value.strip().lower() if isinstance(value, str) else value)
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
        "date_format",
        "file_format",
        "file_level",
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
        console_level: int = logging.INFO,
        console_username: bool = False,
        time_zone: str or None = None,
        date_format: str = "dd/mm/yy",
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
        # Added last so existing positional calls keep their meaning.
        console_format: LoggingFormat | str = LoggingFormat.TEXT,
        file_format: LoggingFormat | str = LoggingFormat.TEXT,
    ):
        self.save = save
        self.less = less
        self.console_format = console_format
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
        self.file_format = file_format
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

    def __setattr__(self, name, value):
        # Normalize here rather than in __init__ so a format assigned later
        # is a LoggingFormat too, and a typo fails at the assignment.
        if name in ("console_format", "file_format"):
            value = _logging_format(name, value)
        super().__setattr__(name, value)


def _timezone_from_settings(settings: LoggerSettings, label):
    """Return the configured pytz time zone, or None when unset or unknown."""
    if not settings.time_zone:
        return None
    try:
        timezone = pytz.timezone(settings.time_zone)
    except pytz.UnknownTimeZoneError:
        logging.error(f"{label}: invalid time zone: {settings.time_zone}")
        return None
    logging.info(f"{label} time zone set to: {timezone}")
    return timezone


# JSON output key -> LogRecord attribute, shared by the console and file
# handlers so both streams have the same shape.
JSON_LOG_FIELDS = {
    "timestamp": "asctime",
    "filename": "filename",
    "funcName": "funcName",
    "level": "levelname",
    "lineno": "lineno",
    "module": "module",
    "message": "message",
    "name": "name",
    "process": "process",
    "processName": "processName",
    "relativeCreated": "relativeCreated",
    "thread": "thread",
    "threadName": "threadName",
}


_TRACEBACK_FORMATTER = logging.Formatter()


def _ensure_exc_text(record):
    """Format the record's traceback into exc_text once, if it has one."""
    if record.exc_info and not record.exc_text:
        record.exc_text = _TRACEBACK_FORMATTER.formatException(record.exc_info)


def _unflattened_parts(record):
    """Return the record's message, traceback and stack as separate strings."""
    _ensure_exc_text(record)
    return record.getMessage(), record.exc_text, record.stack_info


# Colors that log calls embed in their messages for the text console.
_ANSI_SGR = re.compile(r"\x1b\[[0-9;]*m")


class _TimezoneFormatter(logging.Formatter):
    """Base for the formatters: timestamps in the configured time zone."""

    def __init__(self, *, settings: LoggerSettings, label, fmt=None, datefmt=None):
        self.settings = settings
        self.timezone = _timezone_from_settings(settings, label)
        super().__init__(fmt=fmt, datefmt=datefmt)

    def _moment(self, record):
        """Return the record's creation time in the configured time zone."""
        if self.timezone:
            return datetime.fromtimestamp(record.created, self.timezone)
        return datetime.fromtimestamp(record.created)

    def formatTime(self, record, datefmt=None):
        return self._moment(record).strftime(datefmt or self.default_time_format)


class JsonFormatter(_TimezoneFormatter):
    """Write each record as one JSON object per line.

    The fields come from JSON_LOG_FIELDS. The timestamp is ISO 8601 with
    the offset of the configured time zone (or the host's), so log shippers
    such as Loki or Alloy can parse it without guessing. The account
    ``username`` and, when the record carries one, its ``event`` are added
    so several accounts can share one log stream. ``ensure_ascii`` escapes
    non-ASCII text for a stream that cannot encode it.
    """

    def __init__(
        self,
        *,
        settings: LoggerSettings,
        username: str,
        label: str,
        ensure_ascii: bool = False,
    ):
        super().__init__(settings=settings, label=label)
        self.username = username
        self.ensure_ascii = ensure_ascii

    def formatTime(self, record, datefmt=None):
        moment = self._moment(record)
        if moment.tzinfo is None:
            moment = moment.astimezone()
        return moment.isoformat(timespec="milliseconds")

    def format(self, record):
        # Records from the log queue carry their parts already split; the
        # record itself is left unchanged for the other handlers.
        message, exc_text, stack_info = getattr(
            record, "_unflattened", None
        ) or _unflattened_parts(record)
        computed = {
            "asctime": self.formatTime(record),
            "message": _ANSI_SGR.sub("", message),
        }
        entry = {
            key: computed[attr] if attr in computed else getattr(record, attr)
            for key, attr in JSON_LOG_FIELDS.items()
        }
        entry["username"] = self.username
        if hasattr(record, "event"):
            entry["event"] = str(record.event)
        if exc_text:
            entry["exc_info"] = exc_text
        if stack_info:
            entry["stack_info"] = self.formatStack(stack_info)
        return json.dumps(entry, default=str, ensure_ascii=self.ensure_ascii)


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

        # Formatters reuse exc_text instead of re-formatting exc_info, so
        # formatting the traceback here lets it be redacted.
        _ensure_exc_text(record)
        if record.exc_text:
            record.exc_text = self.redact(record.exc_text)
        if record.stack_info:
            record.stack_info = self.redact(record.stack_info)
        return True


class FileFormatter(_TimezoneFormatter):
    def __init__(self, *, fmt, settings: LoggerSettings, datefmt=None):
        super().__init__(
            settings=settings, label="File logger", fmt=fmt, datefmt=datefmt
        )


class GlobalFormatter(_TimezoneFormatter):
    def __init__(self, *, fmt, settings: LoggerSettings, datefmt=None):
        super().__init__(
            settings=settings, label="Console logger", fmt=fmt, datefmt=datefmt
        )

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


class LoggingQueueHandler(QueueHandler):
    """Queue records for the log thread, keeping what JSON output needs.

    QueueHandler.prepare() flattens the traceback and stack into the
    message, which text output and notifications rely on. JSON output wants
    them as separate fields, so the parts are saved on the queued record
    before that happens. They are already redacted, since filters run first.
    """

    def prepare(self, record):
        parts = _unflattened_parts(record)
        record = super().prepare(record)
        record._unflattened = parts
        return record


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


def _stream_takes_unicode(stream):
    """True when the stream writes any text unchanged.

    A codec other than UTF must escape or drop what it cannot encode, and
    its escapes (backslashreplace's \\U0001f389, for one) are not valid JSON.
    """
    encoding = getattr(stream, "encoding", None)
    if not encoding:
        # A str sink without a codec, such as StringIO.
        return True
    try:
        return codecs.lookup(encoding).name.startswith("utf")
    except LookupError:
        return False


def configure_loggers(username, settings):
    # Only the text console is colored. colorama's autoreset would write a
    # reset code after every JSON line on a terminal, breaking the next one.
    if settings.colored is True and settings.console_format is LoggingFormat.TEXT:
        init(autoreset=True)

    # Queue handler that will handle the logger queue. The stdlib one is
    # enough unless a JSON handler needs the traceback kept apart.
    logger_queue = queue.Queue(-1)
    writes_json = settings.console_format is LoggingFormat.JSON or (
        settings.save is True and settings.file_format is LoggingFormat.JSON
    )
    queue_handler = (LoggingQueueHandler if writes_json else QueueHandler)(logger_queue)
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

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG)
    console_handler.addFilter(CategoryConsoleFilter(settings.console_level))
    console_handler.addFilter(NotificationOnlyFilter())

    if settings.console_format is LoggingFormat.JSON:
        formatter = JsonFormatter(
            settings=settings,
            username=username,
            label="Console logger",
            # A console that is not UTF (Windows cp1252, for one) would drop
            # the line or write escapes that are not JSON, so escape here.
            ensure_ascii=not _stream_takes_unicode(sys.stdout),
        )
    else:
        formatter = GlobalFormatter(
            fmt=(
                "%(asctime)s - %(levelname)s - [%(funcName)s]: %(message)s"
                if settings.less is False
                else "%(asctime)s - %(message)s"
            ),
            datefmt=f"{log_date_format} %H:%M:%S",
            settings=settings,
        )
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
            # Every formatter resolves the same time zone, so reuse the console's.
            tz = console_handler.formatter.timezone
            logs_file = os.path.join(
                logs_path,
                f"{username}.{datetime.now(tz).strftime('%Y%m%d-%H%M%S')}.log",
            )
            file_handler = logging.FileHandler(logs_file, "w", "utf-8")

        if settings.file_format is LoggingFormat.JSON:
            file_formatter = JsonFormatter(
                settings=settings,
                username=username,
                label="File logger",
            )
        else:
            file_formatter = FileFormatter(
                fmt="%(asctime)s - %(levelname)s - %(name)s - [%(funcName)s]: %(message)s",
                datefmt=f"{log_date_format} %H:%M:%S",
                settings=settings,
            )
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
