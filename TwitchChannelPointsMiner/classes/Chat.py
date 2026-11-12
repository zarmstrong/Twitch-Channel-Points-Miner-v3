import logging
import socket
import ssl
import time
from enum import Enum, auto
from functools import partial
from threading import Lock, Thread

import irc.connection
from irc.bot import SingleServerIRCBot

from TwitchChannelPointsMiner.classes.Settings import Events, Settings
from TwitchChannelPointsMiner.constants import IRC, IRC_PORT, IRC_TLS_PORT

logger = logging.getLogger(__name__)

_tls_probe_lock = Lock()
_tls_supported = None

DEFAULT_GIFT_SUB_MESSAGE = "Thanks for the sub!"
# Twitch rejects chat messages longer than 500 characters.
MAX_CHAT_MESSAGE_LENGTH = 500
# The irc library rejects any line longer than this (RFC 2812).
IRC_MAX_LINE_BYTES = 512
ANONYMOUS_GIFTER_LOGIN = "ananonymousgifter"
SUB_PLAN_NAMES = {
    "1000": "Tier 1",
    "2000": "Tier 2",
    "3000": "Tier 3",
    "Prime": "Prime",
}


class _KeepUnknownPlaceholders(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def format_gift_sub_message(template, gifter, channel, tier):
    """Fill a thank-you template, leaving unknown {placeholders} as typed and
    falling back to the default message if the template is malformed. Line
    breaks are removed so the message can't inject extra IRC commands."""
    values = _KeepUnknownPlaceholders(gifter=gifter, channel=channel, tier=tier)
    try:
        message = str(template).format_map(values)
    except Exception:
        # Any template str.format can't fill, e.g. "{gifter[name]}"
        # (TypeError) as well as "{" (ValueError) or "{0}" (IndexError).
        message = DEFAULT_GIFT_SUB_MESSAGE.format_map(values)
    message = " ".join(message.replace("\r", " ").replace("\n", " ").split())
    message = message[:MAX_CHAT_MESSAGE_LENGTH]
    # The irc library refuses a line over 512 bytes including the
    # "PRIVMSG #channel :" prefix and CRLF, so also fit the UTF-8 length.
    max_bytes = IRC_MAX_LINE_BYTES - len(f"PRIVMSG #{channel} :\r\n".encode("utf-8"))
    encoded = message.encode("utf-8")
    if len(encoded) > max_bytes:
        message = encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip()
    return message


def _irc_tls_context():
    """TLS settings for Twitch IRC: certificate and hostname checks from the
    default context, and never anything older than TLS 1.2."""
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def _irc_tls_available(host=IRC, port=IRC_TLS_PORT, timeout=5):
    """Probe once per process whether a TLS handshake to Twitch's IRC endpoint
    succeeds. The chat OAuth token is otherwise sent over plaintext port 6667 -
    prefer TLS when it's reachable, but keep working on networks that block or
    intercept 6697 by falling back to plaintext."""
    global _tls_supported
    with _tls_probe_lock:
        if _tls_supported is None:
            try:
                context = _irc_tls_context()
                with socket.create_connection((host, port), timeout=timeout) as sock:
                    with context.wrap_socket(sock, server_hostname=host):
                        _tls_supported = True
            except (OSError, ssl.SSLError) as error:
                logger.warning(
                    f"IRC TLS handshake with {host}:{port} failed ({error}), "
                    f"falling back to plaintext IRC on port {IRC_PORT}",
                    extra={"emoji": ":warning:"},
                )
                _tls_supported = False
        return _tls_supported


class ChatPresence(Enum):
    ALWAYS = auto()
    NEVER = auto()
    ONLINE = auto()
    OFFLINE = auto()

    def __str__(self):
        return self.name


class ClientIRC(SingleServerIRCBot):
    def __init__(self, username, token, channel, settings=None):
        self.token = token
        self.settings = settings
        self.channel = "#" + channel
        self.__active = False

        connect_params = {}
        port = IRC_PORT
        if _irc_tls_available():
            port = IRC_TLS_PORT
            context = _irc_tls_context()
            connect_params["connect_factory"] = irc.connection.Factory(
                wrapper=partial(context.wrap_socket, server_hostname=IRC)
            )

        super(ClientIRC, self).__init__(
            [(IRC, port, f"oauth:{token}")], username, username, **connect_params
        )

    def on_welcome(self, client, event):
        # Tags and commands let USERNOTICE (sub/gift) events through; chat
        # messages and mentions keep working the same way with them enabled.
        client.send_raw("CAP REQ :twitch.tv/tags twitch.tv/commands")
        client.join(self.channel)

    def start(self):
        self.__active = True
        self._connect()
        while self.__active:
            try:
                self.reactor.process_once(timeout=0.2)
                time.sleep(0.01)
            except Exception as e:
                # die() closes the socket from another thread while this loop
                # may be inside select.select() on it, so a spurious error here
                # (e.g. a negative file descriptor) is expected on shutdown -
                # only surface it if we weren't asked to stop.
                if self.__active:
                    logger.error(
                        f"Exception raised: {e}. Thread is active: {self.__active}"
                    )

    def die(self, msg="Bye, cruel world!"):
        self.__active = False
        self.connection.disconnect(msg)

    """
    def on_join(self, connection, event):
        logger.info(f"Event: {event}", extra={"emoji": ":speech_balloon:"})
    """

    # """
    def on_pubmsg(self, connection, event):
        msg = event.arguments[0]
        mention = None

        if Settings.disable_at_in_nickname is True:
            mention = f"{self._nickname.lower()}"
        else:
            mention = f"@{self._nickname.lower()}"

        # also self._realname
        # if msg.startswith(f"@{self._nickname}"):
        if mention is not None and mention in msg.lower():
            # nickname!username@nickname.tmi.twitch.tv
            nick = event.source.split("!", 1)[0]
            # chan = event.target

            logger.info(
                f"{nick} at {self.channel} wrote: {msg}",
                extra={"emoji": ":speech_balloon:", "event": Events.CHAT_MENTION},
            )

    @staticmethod
    def _tags(event):
        return {
            tag.get("key"): tag.get("value")
            for tag in (getattr(event, "tags", None) or [])
            if isinstance(tag, dict)
        }

    def on_usernotice(self, connection, event):
        tags = self._tags(event)
        if tags.get("msg-id") != "subgift":
            return
        recipient = (tags.get("msg-param-recipient-user-name") or "").lower()
        if recipient != self._nickname.lower():
            return

        login = (tags.get("login") or "").lower()
        if login == ANONYMOUS_GIFTER_LOGIN:
            gifter = "An Anonymous Gifter"
        else:
            gifter = tags.get("display-name") or tags.get("login") or "someone"
        plan = tags.get("msg-param-sub-plan") or ""
        tier = SUB_PLAN_NAMES.get(plan, plan)
        channel = self.channel.lstrip("#")
        sub = f"a {tier} sub" if tier else "a sub"

        logger.info(
            f"{gifter} gifted you {sub} in {channel}'s chat",
            extra={"emoji": ":gift:", "event": Events.GIFTED_SUB},
        )

        settings = self.settings
        if settings is None or getattr(settings, "gift_sub_thanks", False) is not True:
            return
        template = (
            getattr(settings, "gift_sub_message", None) or DEFAULT_GIFT_SUB_MESSAGE
        )
        message = format_gift_sub_message(template, gifter, channel, tier)
        if not message:
            return
        try:
            connection.privmsg(self.channel, message)
        except Exception as error:
            logger.warning(
                f"Couldn't send the gift sub thank-you in {channel}'s chat: {error}",
                extra={"emoji": ":warning:"},
            )
            return
        logger.info(
            f"Thanked {gifter} in {channel}'s chat: {message}",
            extra={"emoji": ":speech_balloon:"},
        )

    # """


class ThreadChat(Thread):
    def __deepcopy__(self, memo):
        return None

    def __init__(self, username, token, channel):
        super(ThreadChat, self).__init__()

        self.username = username
        self.token = token
        self.channel = channel

        self.chat_irc = None
        self.settings = None

    def run(self):
        self.chat_irc = ClientIRC(
            self.username, self.token, self.channel, settings=self.settings
        )
        logger.info(
            f"Join IRC Chat: {self.channel}", extra={"emoji": ":speech_balloon:"}
        )
        self.chat_irc.start()

    def stop(self):
        if self.chat_irc is not None:
            logger.info(
                f"Leave IRC Chat: {self.channel}", extra={"emoji": ":speech_balloon:"}
            )
            self.chat_irc.die()
