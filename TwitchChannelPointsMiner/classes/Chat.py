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


def _irc_tls_available(host=IRC, port=IRC_TLS_PORT, timeout=5):
    """Probe once per process whether a TLS handshake to Twitch's IRC endpoint
    succeeds. The chat OAuth token is otherwise sent over plaintext port 6667 -
    prefer TLS when it's reachable, but keep working on networks that block or
    intercept 6697 by falling back to plaintext."""
    global _tls_supported
    with _tls_probe_lock:
        if _tls_supported is None:
            try:
                context = ssl.create_default_context()
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
    def __init__(self, username, token, channel):
        self.token = token
        self.channel = "#" + channel
        self.__active = False

        connect_params = {}
        port = IRC_PORT
        if _irc_tls_available():
            port = IRC_TLS_PORT
            context = ssl.create_default_context()
            connect_params["connect_factory"] = irc.connection.Factory(
                wrapper=partial(context.wrap_socket, server_hostname=IRC)
            )

        super(ClientIRC, self).__init__(
            [(IRC, port, f"oauth:{token}")], username, username, **connect_params
        )

    def on_welcome(self, client, event):
        client.join(self.channel)

    def start(self):
        self.__active = True
        self._connect()
        while self.__active:
            try:
                self.reactor.process_once(timeout=0.2)
                time.sleep(0.01)
            except Exception as e:
                logger.error(
                    f"Exception raised: {e}. Thread is active: {self.__active}"
                )

    def die(self, msg="Bye, cruel world!"):
        self.connection.disconnect(msg)
        self.__active = False

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

    def run(self):
        self.chat_irc = ClientIRC(self.username, self.token, self.channel)
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
