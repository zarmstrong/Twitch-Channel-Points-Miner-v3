import logging
import os
import platform
import queue
import re
import sys
from datetime import datetime
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


class LoggerSettings:
    __slots__ = [
        "save",
        "less",
        "console_level",
        "console_username",
        "time_zone",
        "date_format",
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
    ):
        self.save = save
        self.less = less
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


class NotificationHandler(logging.Handler):
    """Send event records to the configured notification providers.

    Notifications are independent of the console and file formatters, so
    changing how log lines are rendered never changes what gets sent.
    """

    PROVIDERS = (
        "telegram",
        "discord",
        "webhook",
        "matrix",
        "pushover",
        "gotify",
        "ntfy",
        "email",
    )

    def __init__(self, settings: LoggerSettings, level=logging.NOTSET):
        super().__init__(level)
        self.settings = settings

    def emit(self, record):
        if not hasattr(record, "event") or not any(
            getattr(self.settings, provider, None) is not None
            for provider in self.PROVIDERS
        ):
            return
        try:
            message = decorate_message(record, self.settings)
        except Exception as error:
            self._report_failure("notification", error)
            return
        for provider in self.PROVIDERS:
            try:
                getattr(self, provider)(record, message)
            except Exception as error:
                # One failing provider must not stop the others.
                self._report_failure(provider, error)

    @staticmethod
    def _report_failure(provider, error):
        # Log through the queue rather than handleError(), which prints the
        # traceback to stderr without redaction. Only the exception type is
        # kept, since provider errors can quote webhook or bot-token URLs.
        logging.getLogger(__name__).warning(
            f"Unable to send {provider} notification: {type(error).__name__}"
        )

    def telegram(self, record, message):
        skip_telegram = getattr(record, "skip_telegram", False)

        if (
            self.settings.telegram is not None
            and not skip_telegram
            and self.settings.telegram.chat_id != 123456789
        ):
            self._send(self.settings.telegram, record, message)

    def discord(self, record, message):
        skip_discord = getattr(record, "skip_discord", False)

        if (
            self.settings.discord is not None
            and not skip_discord
            and self.settings.discord.webhook_api
            != "https://discord.com/api/webhooks/0123456789/0a1B2c3D4e5F6g7H8i9J"
        ):
            self._send(self.settings.discord, record, message)

    def webhook(self, record, message):
        skip_webhook = getattr(record, "skip_webhook", False)

        if (
            self.settings.webhook is not None
            and not skip_webhook
            and self.settings.webhook.endpoint != "https://example.com/webhook"
        ):
            self._send(self.settings.webhook, record, message)

    def matrix(self, record, message):
        skip_matrix = getattr(record, "skip_matrix", False)

        if (
            self.settings.matrix is not None
            and not skip_matrix
            and self.settings.matrix.room_id != "..."
            and self.settings.matrix.access_token
        ):
            self._send(self.settings.matrix, record, message)

    def pushover(self, record, message):
        skip_pushover = getattr(record, "skip_pushover", False)

        if (
            self.settings.pushover is not None
            and not skip_pushover
            and self.settings.pushover.userkey != "YOUR-ACCOUNT-TOKEN"
            and self.settings.pushover.token != "YOUR-APPLICATION-TOKEN"
        ):
            self._send(self.settings.pushover, record, message)

    def gotify(self, record, message):
        skip_gotify = getattr(record, "skip_gotify", False)

        if (
            self.settings.gotify is not None
            and not skip_gotify
            and self.settings.gotify.endpoint
            != "https://example.com/message?token=TOKEN"
        ):
            self._send(self.settings.gotify, record, message)

    def email(self, record, message):
        skip_email = getattr(record, "skip_email", False)
        if (
            self.settings.email is not None
            and not skip_email
            and self.settings.email.host != "smtp.example.com"
        ):
            self._send(self.settings.email, record, message)

    def ntfy(self, record, message):
        skip_ntfy = getattr(record, "skip_ntfy", False)
        if (
            self.settings.ntfy is not None
            and not skip_ntfy
            and self.settings.ntfy.topic != "YOUR_NTFY_TOPIC"
        ):
            self._send(self.settings.ntfy, record, message)

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
            result = notifier.send(message, record.event)
        finally:
            if added:
                notifier.events.remove(event)

        # Notifiers catch their own delivery errors and return a reason that
        # is already safe to show, without URLs or credentials.
        if isinstance(result, tuple) and len(result) == 2 and not result[0]:
            logging.getLogger(__name__).warning(f"Notification not sent: {result[1]}")


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

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG)
    console_handler.addFilter(CategoryConsoleFilter(settings.console_level))
    console_handler.setFormatter(
        GlobalFormatter(
            fmt=(
                "%(asctime)s - %(levelname)s - [%(funcName)s]: %(message)s"
                if settings.less is False
                else "%(asctime)s - %(message)s"
            ),
            datefmt=f"{log_date_format} %H:%M:%S",
            settings=settings,
        )
    )

    # Each provider's event list decides what is sent, so a quieter console
    # never silences notifications.
    notification_handler = NotificationHandler(settings)

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

        file_handler.setFormatter(
            FileFormatter(
                fmt="%(asctime)s - %(levelname)s - %(name)s - [%(funcName)s]: %(message)s",
                datefmt=f"{log_date_format} %H:%M:%S",
                settings=settings,
            )
        )
        file_handler.setLevel(settings.file_level)

        # Add logger handlers to the logger queue and start the process
        queue_listener = QueueListener(
            logger_queue,
            file_handler,
            console_handler,
            notification_handler,
            respect_handler_level=True,
        )
        queue_listener.start()
        return logs_file, queue_listener
    else:
        queue_listener = QueueListener(
            logger_queue,
            console_handler,
            notification_handler,
            respect_handler_level=True,
        )
        queue_listener.start()
        return None, queue_listener
