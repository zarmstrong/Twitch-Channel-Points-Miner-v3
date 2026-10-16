
import json
import logging
import os
import platform
import queue
import re
import sys
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
        console_format: str = LoggingFormat.TEXT,
        console_level: int = logging.INFO,
        console_username: bool = False,
        time_zone: str or None = None,
        date_format: str = "dd/mm/yy",
        file_format: str = LoggingFormat.TEXT,
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


class JsonFormatter(logging.Formatter):
    """
    Formatter that outputs JSON strings after parsing the LogRecord.

    @param dict fmt_dict: Key: logging format attribute pairs. Defaults to {"message": "message"}.
    @param str time_format: time.strftime() format string. Default: "%Y-%m-%dT%H:%M:%S"
    @param str msec_format: Microsecond formatting. Appended at the end. Default: "%s.%03dZ"
    """

    def __init__(self, fmt_dict: dict = None, time_format: str = "%Y-%m-%dT%H:%M:%S", msec_format: str = "%s.%03dZ"):
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
        return {fmt_key: record.__dict__[fmt_val] for fmt_key, fmt_val in self.fmt_dict.items()}

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
        record.emoji_is_present = (
            record.emoji_is_present if hasattr(record, "emoji_is_present") else False
        )
        if (
            hasattr(record, "emoji")
            and self.settings.emoji is True
            and record.emoji_is_present is False
        ):
            record.msg = emoji.emojize(
                f"{record.emoji}  {record.msg.strip()}", language="alias"
            )
            record.emoji_is_present = True

        if self.settings.emoji is False:
            if "\u2192" in record.msg:
                record.msg = record.msg.replace("\u2192", "-->")

            # With the update of Stream class, the Stream Title may contain emoji
            # Full remove using a method from utils.
            record.msg = remove_emoji(record.msg)

        record.msg = self.settings.username + record.msg

        if hasattr(record, "event"):
            self.telegram(record)
            self.discord(record)
            self.webhook(record)
            self.matrix(record)
            self.pushover(record)
            self.gotify(record)
            self.ntfy(record)
            self.email(record)

            if self.settings.colored is True:
                record.msg = f"{self.settings.color_palette.get(record.event)}{record.msg}{Fore.RESET}"

        return super().format(record)

    def telegram(self, record):
        skip_telegram = getattr(record, "skip_telegram", False)

        if (
            self.settings.telegram is not None
            and not skip_telegram
            and self.settings.telegram.chat_id != 123456789
        ):
            self._send(self.settings.telegram, record)

    def discord(self, record):
        skip_discord = getattr(record, "skip_discord", False)

        if (
            self.settings.discord is not None
            and not skip_discord
            and self.settings.discord.webhook_api
            != "https://discord.com/api/webhooks/0123456789/0a1B2c3D4e5F6g7H8i9J"
        ):
            self._send(self.settings.discord, record)

    def webhook(self, record):
        skip_webhook = getattr(record, "skip_webhook", False)

        if (
            self.settings.webhook is not None
            and not skip_webhook
            and self.settings.webhook.endpoint != "https://example.com/webhook"
        ):
            self._send(self.settings.webhook, record)

    def matrix(self, record):
        skip_matrix = getattr(record, "skip_matrix", False)

        if (
            self.settings.matrix is not None
            and not skip_matrix
            and self.settings.matrix.room_id != "..."
            and self.settings.matrix.access_token
        ):
            self._send(self.settings.matrix, record)

    def pushover(self, record):
        skip_pushover = getattr(record, "skip_pushover", False)

        if (
            self.settings.pushover is not None
            and not skip_pushover
            and self.settings.pushover.userkey != "YOUR-ACCOUNT-TOKEN"
            and self.settings.pushover.token != "YOUR-APPLICATION-TOKEN"
        ):
            self._send(self.settings.pushover, record)

    def gotify(self, record):
        skip_gotify = getattr(record, "skip_gotify", False)

        if (
            self.settings.gotify is not None
            and not skip_gotify
            and self.settings.gotify.endpoint
            != "https://example.com/message?token=TOKEN"
        ):
            self._send(self.settings.gotify, record)

    def email(self, record):
        skip_email = getattr(record, "skip_email", False)
        if (
            self.settings.email is not None
            and not skip_email
            and self.settings.email.host != "smtp.example.com"
        ):
            self._send(self.settings.email, record)

    def ntfy(self, record):
        skip_ntfy = getattr(record, "skip_ntfy", False)
        if (
            self.settings.ntfy is not None
            and not skip_ntfy
            and self.settings.ntfy.topic != "YOUR_NTFY_TOPIC"
        ):
            self._send(self.settings.ntfy, record)

    @staticmethod
    def _send(notifier, record):
        """Send a record, optionally bypassing a notifier's event allowlist."""
        event = str(record.event)
        force_alert = getattr(record, "force_alert", False)
        added = force_alert and event not in notifier.events
        if added:
            notifier.events.append(event)
        try:
            notifier.send(record.msg, record.event)
        finally:
            if added:
                notifier.events.remove(event)


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

    formatter = None
    if settings.console_format == LoggingFormat.TEXT:
        formatter = GlobalFormatter(
            fmt=(
                "%(asctime)s - %(levelname)s - [%(funcName)s]: %(message)s"
                if settings.less is False
                else "%(asctime)s - %(message)s"
            ),
            datefmt=(
                "%d/%m/%y %H:%M:%S" if settings.less is False else "%d/%m %H:%M:%S"
            ),
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

        # Add logger handlers to the logger queue and start the process
        queue_listener = QueueListener(
            logger_queue, file_handler, console_handler, respect_handler_level=True
        )
        queue_listener.start()
        return logs_file, queue_listener
    else:
        queue_listener = QueueListener(
            logger_queue, console_handler, respect_handler_level=True
        )
        queue_listener.start()
        return None, queue_listener
