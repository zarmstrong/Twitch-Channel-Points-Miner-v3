import functools
import logging
import socket
import sys
import threading
import ssl
import time
import types
from unittest.mock import MagicMock, call

import irc.client
import pytest

from TwitchChannelPointsMiner.classes import Chat
from TwitchChannelPointsMiner.classes.Chat import ClientIRC, ThreadChat
from TwitchChannelPointsMiner.classes.entities.Streamer import StreamerSettings
from TwitchChannelPointsMiner.classes.Settings import Events
from TwitchChannelPointsMiner.constants import IRC_PORT, IRC_TLS_PORT


@pytest.fixture(autouse=True)
def reset_tls_probe_cache():
    Chat._tls_supported = None
    Chat._tls_failure_warned = False
    Chat._tls_failed_at = None
    Chat._tls_probe_done = None
    yield
    Chat._tls_supported = None
    Chat._tls_failure_warned = False
    Chat._tls_failed_at = None
    Chat._tls_probe_done = None


class FakeSocket:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_irc_tls_available_returns_true_on_successful_handshake(monkeypatch):
    monkeypatch.setattr(
        socket, "create_connection", lambda address, timeout=None: FakeSocket()
    )
    monkeypatch.setattr(
        ssl.SSLContext, "wrap_socket", lambda self, sock, server_hostname=None: sock
    )

    assert Chat._irc_tls_available() is True


def test_irc_tls_available_caches_failure_only_briefly(monkeypatch):
    calls = 0

    def failing_connect(address, timeout=None):
        nonlocal calls
        calls += 1
        raise OSError("connection refused")

    monkeypatch.setattr(socket, "create_connection", failing_connect)

    assert Chat._irc_tls_available() is False
    # Chats retrying during the same outage reuse the failure.
    assert Chat._irc_tls_available() is False
    assert calls == 1
    # Once the failure is older than the cache window, TLS is probed again.
    Chat._tls_failed_at -= Chat.IRC_TLS_FAILURE_CACHE_SECONDS
    assert Chat._irc_tls_available() is False
    assert calls == 2
    assert Chat._tls_supported is None


def test_irc_tls_available_caches_successful_probe(monkeypatch):
    calls = 0

    def succeeding_connect(address, timeout=None):
        nonlocal calls
        calls += 1
        return FakeSocket()

    monkeypatch.setattr(socket, "create_connection", succeeding_connect)
    monkeypatch.setattr(
        ssl.SSLContext, "wrap_socket", lambda self, sock, server_hostname=None: sock
    )

    assert Chat._irc_tls_available() is True
    assert Chat._irc_tls_available() is True
    assert calls == 1


def test_client_irc_uses_tls_port_and_wrapped_factory_when_available(monkeypatch):
    monkeypatch.setattr(Chat, "_irc_tls_available", lambda **_: True)

    client = ClientIRC("someuser", "sometoken", "somechannel")

    server = client.servers.peek()
    assert server.port == IRC_TLS_PORT
    factory = client._SingleServerIRCBot__connect_params["connect_factory"]
    assert isinstance(factory, Chat._StoppableConnectFactory)
    assert factory.wrapper is not None
    assert factory.timeout == Chat.IRC_CONNECT_TIMEOUT_SECONDS


def test_client_irc_uses_plaintext_port_when_explicitly_allowed(monkeypatch):
    monkeypatch.setattr(Chat, "_irc_tls_available", lambda **_: False)
    monkeypatch.setenv("TCPM_ALLOW_PLAINTEXT_IRC", "1")

    client = ClientIRC("someuser", "sometoken", "somechannel")

    server = client.servers.peek()
    assert server.port == IRC_PORT
    # Plaintext gets the same timeout and stop handling, without TLS.
    factory = client._SingleServerIRCBot__connect_params["connect_factory"]
    assert isinstance(factory, Chat._StoppableConnectFactory)
    assert factory.wrapper is None


def test_client_irc_refuses_plaintext_when_tls_unavailable(monkeypatch):
    monkeypatch.setattr(Chat, "_irc_tls_available", lambda **_: False)
    monkeypatch.delenv("TCPM_ALLOW_PLAINTEXT_IRC", raising=False)

    with pytest.raises(Chat.IRCTLSUnavailable):
        ClientIRC("someuser", "sometoken", "somechannel")


@pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
def test_plaintext_irc_allowed_for_truthy_values(monkeypatch, value):
    monkeypatch.setenv("TCPM_ALLOW_PLAINTEXT_IRC", value)

    assert Chat._plaintext_irc_allowed() is True


@pytest.mark.parametrize("value", ["", "0", "no"])
def test_plaintext_irc_not_allowed_for_falsy_values(monkeypatch, value):
    monkeypatch.setenv("TCPM_ALLOW_PLAINTEXT_IRC", value)

    assert Chat._plaintext_irc_allowed() is False


def test_irc_tls_context_requires_tls12_and_verifies_hostname():
    context = Chat._irc_tls_context()

    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED


def _usernotice(**tags):
    return types.SimpleNamespace(
        type="usernotice",
        target="#somechannel",
        arguments=[],
        tags=[{"key": k, "value": v} for k, v in tags.items()],
    )


def _subgift(**overrides):
    tags = {
        "msg-id": "subgift",
        "msg-param-recipient-user-name": "SomeUser",
        "display-name": "Gifter_One",
        "login": "gifter_one",
        "msg-param-sub-plan": "1000",
    }
    tags.update(overrides)
    return _usernotice(**tags)


def _client_with(monkeypatch, **settings_values):
    monkeypatch.setattr(Chat, "_irc_tls_available", lambda **_: False)
    monkeypatch.setenv("TCPM_ALLOW_PLAINTEXT_IRC", "1")
    settings = None
    if settings_values:
        settings = StreamerSettings(**settings_values)
        settings.default()
    return ClientIRC("someuser", "sometoken", "somechannel", settings=settings)


def _gift_records(caplog):
    return [r for r in caplog.records if getattr(r, "event", None) is Events.GIFTED_SUB]


def test_gift_sub_to_us_thanks_when_enabled(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    client = _client_with(monkeypatch, gift_sub_thanks=True)
    connection = MagicMock()

    client.on_usernotice(connection, _subgift())

    records = _gift_records(caplog)
    assert len(records) == 1
    connection.privmsg.assert_called_once_with("#somechannel", "Thanks for the sub!")


def test_gift_sub_logged_but_not_thanked_when_disabled(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    client = _client_with(monkeypatch, gift_sub_thanks=False)
    assert client.settings is not None
    connection = MagicMock()

    client.on_usernotice(connection, _subgift())

    assert len(_gift_records(caplog)) == 1
    connection.privmsg.assert_not_called()


def test_gift_sub_logged_but_not_thanked_without_settings(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    client = _client_with(monkeypatch)
    connection = MagicMock()

    client.on_usernotice(connection, _subgift())

    assert len(_gift_records(caplog)) == 1
    connection.privmsg.assert_not_called()


def test_gift_sub_to_other_recipient_is_ignored(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    client = _client_with(monkeypatch, gift_sub_thanks=True)
    connection = MagicMock()

    client.on_usernotice(
        connection, _subgift(**{"msg-param-recipient-user-name": "someoneelse"})
    )

    assert _gift_records(caplog) == []
    connection.privmsg.assert_not_called()


def test_non_gift_usernotice_is_ignored(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    client = _client_with(monkeypatch, gift_sub_thanks=True)
    connection = MagicMock()

    client.on_usernotice(connection, _subgift(**{"msg-id": "sub"}))

    assert _gift_records(caplog) == []
    connection.privmsg.assert_not_called()


def test_anonymous_gifter_uses_placeholder_name_and_tier(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    client = _client_with(
        monkeypatch,
        gift_sub_thanks=True,
        gift_sub_message="Thanks {gifter} for the {tier} sub in {channel}!",
    )
    connection = MagicMock()

    client.on_usernotice(connection, _subgift(login="ananonymousgifter"))

    connection.privmsg.assert_called_once_with(
        "#somechannel",
        "Thanks An Anonymous Gifter for the Tier 1 sub in somechannel!",
    )


def test_format_gift_sub_message_keeps_unknown_placeholders():
    assert (
        Chat.format_gift_sub_message("Hi {nope} {gifter}", "Bob", "chan", "Tier 1")
        == "Hi {nope} Bob"
    )


def test_format_gift_sub_message_falls_back_on_malformed_template():
    assert (
        Chat.format_gift_sub_message("Thanks {", "Bob", "chan", "Tier 1")
        == "Thanks for the sub!"
    )


def test_format_gift_sub_message_strips_line_breaks():
    message = Chat.format_gift_sub_message(
        "line1\r\nPRIVMSG #x :evil", "Bob", "chan", "Tier 1"
    )
    assert "\r" not in message
    assert "\n" not in message


def test_format_gift_sub_message_is_capped_at_500_characters(monkeypatch):
    # The IRC line limit normally cuts first; lift it to see Twitch's cap.
    monkeypatch.setattr(Chat, "IRC_MAX_LINE_BYTES", 10_000)
    message = Chat.format_gift_sub_message("x" * 600, "Bob", "c", "Tier 1")
    assert len(message) == Chat.MAX_CHAT_MESSAGE_LENGTH == 500


@pytest.mark.parametrize("text", ["x" * 600, "\U0001f381" * 200, "é" * 400])
def test_format_gift_sub_message_fits_the_irc_line_limit(text):
    # irc refuses a PRIVMSG line over 512 bytes, prefix and CRLF included,
    # so a 500-character or multi-byte message must be cut to fit.
    import irc.client

    channel = "a_twenty_five_char_login_"
    message = Chat.format_gift_sub_message(text, "Bob", channel, "Tier 1")
    connection = irc.client.ServerConnection(None)
    sent = []
    connection.send_raw = lambda line: sent.append(connection._prep_message(line))

    connection.privmsg("#" + channel, message)

    assert message
    assert len(sent[0]) <= 512


def test_format_gift_sub_message_falls_back_on_unfillable_field():
    # "{gifter[name]}" raises TypeError rather than ValueError.
    assert (
        Chat.format_gift_sub_message("Thanks {gifter[name]}!", "Bob", "c", "Tier 1")
        == "Thanks for the sub!"
    )


def test_gift_sub_without_plan_reads_naturally(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    client = _client_with(monkeypatch)

    client.on_usernotice(MagicMock(), _subgift(**{"msg-param-sub-plan": ""}))

    assert _gift_records(caplog)[0].getMessage() == (
        "Gifter_One gifted you a sub in somechannel's chat"
    )


def test_privmsg_failure_is_logged_as_warning_not_raised(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    client = _client_with(monkeypatch, gift_sub_thanks=True)
    connection = MagicMock()
    connection.privmsg.side_effect = OSError("socket closed")

    client.on_usernotice(connection, _subgift())

    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "gift sub thank-you" in r.getMessage()
    ]
    assert len(warnings) == 1


def test_on_welcome_requests_tags_before_join(monkeypatch):
    client = _client_with(monkeypatch)
    mock_client = MagicMock()

    client.on_welcome(mock_client, None)

    assert mock_client.send_raw.call_args == call(
        "CAP REQ :twitch.tv/tags twitch.tv/commands"
    )
    mock_client.join.assert_called_once_with("#somechannel")
    assert mock_client.mock_calls == [
        call.send_raw("CAP REQ :twitch.tv/tags twitch.tv/commands"),
        call.join("#somechannel"),
    ]


LOGIN_REFUSED = ":tmi.twitch.tv NOTICE * :Login authentication failed"
BAD_AUTH = ":tmi.twitch.tv NOTICE * :Improperly formatted auth"
WELCOME = ":tmi.twitch.tv 001 someuser :Welcome, GLHF!"
OWN_JOIN = ":someuser!someuser@someuser.tmi.twitch.tv JOIN #somechannel"


def _wired_client(monkeypatch):
    """A ClientIRC whose connection parses raw Twitch lines through the irc
    library's own dispatch, without a socket. The username keeps a capital
    letter, as a config might, while Twitch answers in lowercase."""
    monkeypatch.setattr(Chat, "_irc_tls_available", lambda **_: False)
    monkeypatch.setenv("TCPM_ALLOW_PLAINTEXT_IRC", "1")
    client = ClientIRC("SomeUser", "sometoken", "somechannel")
    connection = client.connection
    # What ServerConnection.connect() would have set up.
    connection.handlers = {}
    connection.real_server_name = ""
    connection.real_nickname = "SomeUser"
    connection.send_raw = MagicMock()
    return client


def _feed(client, *lines):
    for line in lines:
        client.connection._process_line(line)


def _chat_records(caplog, text):
    return [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if text in record.getMessage()
    ]


def test_refused_chat_login_warns_once_per_streak(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    client = _wired_client(monkeypatch)

    _feed(client, LOGIN_REFUSED, LOGIN_REFUSED, BAD_AUTH)

    records = _chat_records(caplog, "Twitch refused the chat login")
    assert [level for level, _ in records] == [
        logging.WARNING,
        logging.DEBUG,
        logging.DEBUG,
    ]
    assert "#somechannel: Login authentication failed" in records[0][1]
    assert "Improperly formatted auth" in records[2][1]
    assert all("sometoken" not in message for _, message in records)
    assert _chat_records(caplog, "Join IRC Chat") == []


def test_join_after_a_refusal_is_announced_and_resets_the_warning(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    client = _wired_client(monkeypatch)

    _feed(client, LOGIN_REFUSED, WELCOME, OWN_JOIN)
    # The connection drops and the next login is refused again.
    _feed(client, LOGIN_REFUSED, WELCOME, OWN_JOIN)

    assert _chat_records(caplog, "Join IRC Chat") == [
        (logging.INFO, "Join IRC Chat: #somechannel"),
        (logging.INFO, "Join IRC Chat: #somechannel"),
    ]
    assert [
        level for level, _ in _chat_records(caplog, "Twitch refused the chat login")
    ] == [logging.WARNING, logging.WARNING]


def test_join_is_announced_once_and_rejoins_log_at_debug(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    client = _wired_client(monkeypatch)

    _feed(client, WELCOME, OWN_JOIN, WELCOME, OWN_JOIN)

    assert _chat_records(caplog, "Join IRC Chat") == [
        (logging.INFO, "Join IRC Chat: #somechannel"),
        (logging.DEBUG, "Join IRC Chat: #somechannel"),
    ]


def test_other_joins_and_notices_are_not_logged(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    client = _wired_client(monkeypatch)

    # Twitch only reports other users' joins after our own join.
    _feed(
        client,
        WELCOME,
        OWN_JOIN,
        ":someoneelse!someoneelse@someoneelse.tmi.twitch.tv JOIN #somechannel",
        ":someuser!someuser@someuser.tmi.twitch.tv JOIN #otherchannel",
        ":tmi.twitch.tv NOTICE * :Some other notice",
        ":tmi.twitch.tv NOTICE #somechannel :Login authentication failed",
    )

    assert _chat_records(caplog, "Join IRC Chat") == [
        (logging.INFO, "Join IRC Chat: #somechannel")
    ]
    assert _chat_records(caplog, "Twitch refused the chat login") == []


def test_thread_chat_run_does_not_announce_the_join_before_connecting(
    monkeypatch, caplog
):
    caplog.set_level(logging.DEBUG)

    class FakeClientIRC:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(Chat, "ClientIRC", FakeClientIRC)
    monkeypatch.setattr(Chat, "_irc_tls_available", lambda **_: True)

    ThreadChat("u", "t", "c").run()

    assert _chat_records(caplog, "Join IRC Chat") == []


def test_thread_chat_run_passes_settings_to_client(monkeypatch):
    created = {}

    class FakeClientIRC:
        def __init__(self, *args, **kwargs):
            created["kwargs"] = kwargs

        def start(self):
            pass

    monkeypatch.setattr(Chat, "ClientIRC", FakeClientIRC)
    monkeypatch.setattr(Chat, "_irc_tls_available", lambda **_: True)
    sentinel = object()
    thread = ThreadChat("u", "t", "c")
    thread.settings = sentinel

    thread.run()

    assert created["kwargs"]["settings"] is sentinel


class _StopAfterWaits:
    """Stands in for ThreadChat._stop_event: records each backoff delay and
    reports a stop after the given number of waits."""

    def __init__(self, stop_after):
        self.delays = []
        self.stop_after = stop_after

    def is_set(self):
        return False

    def wait(self, delay):
        self.delays.append(delay)
        return len(self.delays) >= self.stop_after

    def set(self):
        pass


def test_thread_chat_run_waits_for_tls_without_opt_in(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(Chat, "_irc_tls_available", lambda **_: False)
    monkeypatch.delenv("TCPM_ALLOW_PLAINTEXT_IRC", raising=False)
    thread = ThreadChat("u", "t", "c")
    thread._stop_event = _StopAfterWaits(stop_after=4)

    thread.run()

    assert thread.chat_irc is None
    # Backoff doubles from the initial delay and is capped.
    assert thread._stop_event.delays == [30, 60, 120, 240]
    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "TCPM_ALLOW_PLAINTEXT_IRC" in r.getMessage()
    ]
    # One warning per outage, the retries at DEBUG.
    assert len(warnings) == 1


def test_thread_chat_backoff_is_capped(monkeypatch):
    monkeypatch.setattr(Chat, "_irc_tls_available", lambda **_: False)
    monkeypatch.delenv("TCPM_ALLOW_PLAINTEXT_IRC", raising=False)
    thread = ThreadChat("u", "t", "c")
    thread._stop_event = _StopAfterWaits(stop_after=8)

    thread.run()

    assert max(thread._stop_event.delays) == Chat.IRC_TLS_RETRY_MAX_SECONDS
    assert thread._stop_event.delays[-1] == Chat.IRC_TLS_RETRY_MAX_SECONDS


def test_thread_chat_joins_once_tls_becomes_available(monkeypatch):
    monkeypatch.delenv("TCPM_ALLOW_PLAINTEXT_IRC", raising=False)
    attempts = []
    started = []

    class FlakyClientIRC:
        def __init__(self, *args, **kwargs):
            attempts.append(1)
            if len(attempts) < 3:
                raise Chat.IRCTLSUnavailable("no TLS")

        def start(self):
            started.append(self)

    monkeypatch.setattr(Chat, "ClientIRC", FlakyClientIRC)
    monkeypatch.setattr(Chat, "_irc_tls_available", lambda **_: False)
    thread = ThreadChat("u", "t", "c")
    thread._stop_event = _StopAfterWaits(stop_after=99)

    thread.run()

    assert len(attempts) == 3
    assert started == [thread.chat_irc]
    assert thread._stop_event.delays == [30, 60]


def test_thread_chat_stop_ends_the_tls_wait_promptly(monkeypatch):
    monkeypatch.setattr(Chat, "_irc_tls_available", lambda **_: False)
    monkeypatch.delenv("TCPM_ALLOW_PLAINTEXT_IRC", raising=False)
    thread = ThreadChat("u", "t", "c")
    thread.daemon = True

    thread.start()
    thread.stop()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert thread.chat_irc is None


def test_tls_probe_failure_warns_once_until_a_success(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    outcomes = iter([OSError("down"), OSError("down"), None, OSError("down")])

    def connect(address, timeout=None):
        outcome = next(outcomes)
        if outcome is not None:
            raise outcome
        return FakeSocket()

    monkeypatch.setattr(socket, "create_connection", connect)
    monkeypatch.setattr(
        ssl.SSLContext, "wrap_socket", lambda self, sock, server_hostname=None: sock
    )

    def handshake_warnings():
        return [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "handshake" in r.getMessage()
        ]

    assert Chat._irc_tls_available() is False
    Chat._tls_failed_at -= Chat.IRC_TLS_FAILURE_CACHE_SECONDS
    assert Chat._irc_tls_available() is False
    assert len(handshake_warnings()) == 1
    # Let the cached failure expire so the next call probes again.
    Chat._tls_failed_at -= Chat.IRC_TLS_FAILURE_CACHE_SECONDS
    assert Chat._irc_tls_available() is True
    # A success is cached, so force a fresh probe to model a later outage.
    Chat._tls_supported = None
    assert Chat._irc_tls_available() is False
    assert len(handshake_warnings()) == 2


def test_streamer_rejoins_chat_after_its_thread_ended(monkeypatch):
    from TwitchChannelPointsMiner.classes.Chat import ChatPresence
    from TwitchChannelPointsMiner.classes.entities.Streamer import Streamer

    class EndsAtOnce(ThreadChat):
        def run(self):
            pass

    monkeypatch.setattr(
        "TwitchChannelPointsMiner.classes.entities.Streamer.ThreadChat", EndsAtOnce
    )
    streamer = Streamer(
        "somechannel", settings=StreamerSettings(chat=ChatPresence.ALWAYS)
    )
    first = EndsAtOnce("u", "t", "somechannel")
    streamer.irc_chat = first

    streamer.toggle_chat()
    first.join(timeout=5)
    # Before the fix, the second join called start() on the finished thread
    # and raised "threads can only be started once".
    streamer.toggle_chat()
    streamer.irc_chat.join(timeout=5)

    assert streamer.irc_chat is not first
    assert streamer.irc_chat.ident is not None
    assert streamer.irc_chat.username == "u"
    assert streamer.irc_chat.token == "t"


def test_stop_during_client_construction_prevents_the_join(monkeypatch):
    # stop() arrives while ClientIRC is still being built (e.g. the TLS
    # probe is in flight), before run() has published chat_irc.
    monkeypatch.setattr(Chat, "_irc_tls_available", lambda **_: True)
    constructing = threading.Event()
    release = threading.Event()
    started = []

    class SlowClientIRC:
        def __init__(self, *args, **kwargs):
            constructing.set()
            assert release.wait(timeout=5)

        def start(self):
            started.append(self)

    monkeypatch.setattr(Chat, "ClientIRC", SlowClientIRC)
    thread = ThreadChat("u", "t", "c")
    thread.daemon = True
    thread.start()
    assert constructing.wait(timeout=5)

    thread.stop()
    release.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert started == []
    assert thread.chat_irc is None


def test_client_irc_die_before_start_prevents_the_connect(monkeypatch):
    monkeypatch.setattr(Chat, "_irc_tls_available", lambda **_: True)
    client = ClientIRC("someuser", "sometoken", "somechannel")
    connects = []
    monkeypatch.setattr(client, "_connect", lambda: connects.append(1))

    client.die()
    # Run start() in a thread: if die() were ignored it would loop forever.
    runner = threading.Thread(target=client.start, daemon=True)
    runner.start()
    runner.join(timeout=2)
    returned = not runner.is_alive()
    if not returned:
        client.die()

    assert returned
    assert connects == []


def test_client_irc_start_disconnects_when_its_loop_ends(monkeypatch):
    # die() can run between start() going active and _connect(), before
    # there is a connection to close; start() must close what it opened.
    monkeypatch.setattr(Chat, "_irc_tls_available", lambda **_: True)
    client = ClientIRC("someuser", "sometoken", "somechannel")
    connection = MagicMock()
    monkeypatch.setattr(client, "connection", connection)
    monkeypatch.setattr(client, "_connect", lambda: client.die())

    client.start()

    assert connection.disconnect.call_count == 2


def test_stopped_waiter_leaves_without_waiting_for_another_probe(monkeypatch):
    # One chat's probe is in flight (holding the probe lock); a second chat
    # that is stopped while waiting for it must exit promptly, without
    # running a probe of its own.
    in_probe = threading.Event()
    release_probe = threading.Event()
    calls = []

    def slow_connect(address, timeout=None):
        calls.append(address)
        in_probe.set()
        assert release_probe.wait(timeout=10)
        raise OSError("down")

    monkeypatch.setattr(socket, "create_connection", slow_connect)
    monkeypatch.delenv("TCPM_ALLOW_PLAINTEXT_IRC", raising=False)
    prober = threading.Thread(target=Chat._irc_tls_available, daemon=True)
    prober.start()
    assert in_probe.wait(timeout=5)

    waiter = ThreadChat("u", "t", "c")
    waiter.daemon = True
    waiter.start()
    time.sleep(0.3)
    waiter.stop()
    waiter.join(timeout=2)
    stopped_promptly = not waiter.is_alive()
    release_probe.set()
    prober.join(timeout=5)

    assert stopped_promptly
    assert len(calls) == 1
    assert waiter.chat_irc is None


def test_stopping_the_chat_that_owns_a_blocked_probe_exits_promptly(monkeypatch):
    # The probe itself is stuck (e.g. a DNS lookup that ignores the socket
    # timeout). The chat waiting on it must still leave when stopped, and
    # the stuck probe must not keep the process alive.
    in_probe = threading.Event()
    release_probe = threading.Event()

    def stuck_connect(address, timeout=None):
        in_probe.set()
        assert release_probe.wait(timeout=10)
        raise OSError("down")

    monkeypatch.setattr(socket, "create_connection", stuck_connect)
    monkeypatch.delenv("TCPM_ALLOW_PLAINTEXT_IRC", raising=False)
    chat = ThreadChat("u", "t", "c")
    chat.daemon = True
    chat.start()
    assert in_probe.wait(timeout=5)

    chat.stop()
    chat.join(timeout=2)
    stopped_promptly = not chat.is_alive()
    probe_threads = [t for t in threading.enumerate() if t.name == "irc-tls-probe"]
    release_probe.set()
    for probe in probe_threads:
        probe.join(timeout=5)

    assert stopped_promptly
    assert chat.chat_irc is None
    assert probe_threads and all(t.daemon for t in probe_threads)


def test_concurrent_probes_share_one_handshake(monkeypatch):
    release_probe = threading.Event()
    calls = []

    def slow_connect(address, timeout=None):
        calls.append(address)
        assert release_probe.wait(timeout=10)
        return FakeSocket()

    monkeypatch.setattr(socket, "create_connection", slow_connect)
    monkeypatch.setattr(
        ssl.SSLContext, "wrap_socket", lambda self, sock, server_hostname=None: sock
    )
    results = []
    callers = [
        threading.Thread(target=lambda: results.append(Chat._irc_tls_available()))
        for _ in range(3)
    ]
    for caller in callers:
        caller.start()
    time.sleep(0.3)
    release_probe.set()
    for caller in callers:
        caller.join(timeout=5)

    assert results == [True, True, True]
    assert len(calls) == 1


def test_concurrent_rejoins_start_the_chat_once(monkeypatch):
    # Online/offline updates arrive from the websocket and the watch worker;
    # two joins racing on an ended chat must not both start the same
    # replacement thread ("threads can only be started once").
    from TwitchChannelPointsMiner.classes.Chat import ChatPresence
    from TwitchChannelPointsMiner.classes.entities import Streamer as streamer_module
    from TwitchChannelPointsMiner.classes.entities.Streamer import Streamer

    release = threading.Event()
    starts = []

    class SlowStartChat(ThreadChat):
        def start(self):
            starts.append(self)
            # Widen the window between publishing and starting a thread.
            time.sleep(0.05)
            super().start()

        def run(self):
            release.wait(timeout=5)

    monkeypatch.setattr(streamer_module, "ThreadChat", SlowStartChat)
    streamer = Streamer(
        "somechannel", settings=StreamerSettings(chat=ChatPresence.ALWAYS)
    )
    ended = ThreadChat("u", "t", "somechannel")
    ended.run = lambda: None
    ended.start()
    ended.join(timeout=5)
    streamer.irc_chat = ended

    errors = []

    def rejoin():
        try:
            streamer.toggle_chat()
        except RuntimeError as error:
            errors.append(error)

    callers = [threading.Thread(target=rejoin) for _ in range(4)]
    for caller in callers:
        caller.start()
    for caller in callers:
        caller.join(timeout=5)
    release.set()
    streamer.irc_chat.join(timeout=5)

    assert errors == []
    assert len(starts) == 1


@pytest.mark.parametrize("presence", ["ALWAYS", "ONLINE"])
def test_late_status_update_does_not_rejoin_a_retired_chat(monkeypatch, presence):
    # The watch worker can still hold a streamer that was just removed; its
    # late online/offline update must not start a new chat for it.
    from TwitchChannelPointsMiner.classes.Chat import ChatPresence
    from TwitchChannelPointsMiner.classes.entities import Streamer as streamer_module
    from TwitchChannelPointsMiner.classes.entities.Streamer import Streamer

    started = []

    class RecordingChat(ThreadChat):
        def start(self):
            started.append(self)
            super().start()

        def run(self):
            pass

    monkeypatch.setattr(streamer_module, "ThreadChat", RecordingChat)
    streamer = Streamer(
        "somechannel", settings=StreamerSettings(chat=ChatPresence[presence])
    )
    streamer.irc_chat = RecordingChat("u", "t", "somechannel")
    streamer.is_online = True
    streamer.toggle_chat()
    streamer.irc_chat.join(timeout=5)
    started.clear()

    streamer.retire_chat()
    streamer.is_online = False
    streamer.toggle_chat()
    streamer.is_online = True
    streamer.toggle_chat()

    assert started == []
    assert streamer.irc_chat.retired is True


def test_streamer_added_back_with_a_new_chat_can_join(monkeypatch):
    from TwitchChannelPointsMiner.classes.Chat import ChatPresence
    from TwitchChannelPointsMiner.classes.entities.Streamer import Streamer

    class EndsAtOnce(ThreadChat):
        def run(self):
            pass

    streamer = Streamer(
        "somechannel", settings=StreamerSettings(chat=ChatPresence.ALWAYS)
    )
    streamer.irc_chat = EndsAtOnce("u", "t", "somechannel")
    streamer.retire_chat()

    # Re-adding a streamer assigns a fresh ThreadChat.
    fresh = EndsAtOnce("u", "t", "somechannel")
    streamer.irc_chat = fresh
    streamer.toggle_chat()
    fresh.join(timeout=5)

    assert fresh.ident is not None


def test_thread_chat_is_a_daemon_thread():
    assert ThreadChat("u", "t", "c").daemon is True


def _silent_listener():
    """A loopback listener that accepts TCP but never answers, so a TLS
    handshake against it stalls."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(5)
    server.settimeout(5)
    return server


def _tls_wrapper():
    return functools.partial(
        Chat._irc_tls_context().wrap_socket, server_hostname="localhost"
    )


def test_stopping_a_chat_interrupts_a_stalled_tls_handshake(monkeypatch):
    server = _silent_listener()
    accepted = []
    try:
        monkeypatch.setattr(Chat, "_irc_tls_available", lambda **_: True)
        monkeypatch.setattr(Chat, "IRC", "localhost")
        monkeypatch.setattr(Chat, "IRC_TLS_PORT", server.getsockname()[1])
        chat = ThreadChat("someuser", "sometoken", "somechannel")
        chat.start()
        # The TCP connect has gone through; the handshake now waits for a
        # server that never answers.
        accepted.append(server.accept()[0])

        started = time.monotonic()
        chat.stop()
        chat.join(timeout=3)
        stopped_promptly = not chat.is_alive()
        elapsed = time.monotonic() - started
    finally:
        for connection in accepted:
            connection.close()
        server.close()

    assert stopped_promptly
    assert elapsed < Chat.IRC_CONNECT_TIMEOUT_SECONDS


def _run_in_thread(target):
    result = {}

    def runner():
        try:
            result["value"] = target()
        except Exception as error:
            result["error"] = error

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    return thread, result


def _wait_for_pending(factory):
    deadline = time.monotonic() + 5
    while factory._pending is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert factory._pending is not None


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="relies on Linux dropping SYNs when the accept queue is full",
)
def test_cancel_interrupts_a_stalled_tcp_connect():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(0)
    address = server.getsockname()
    # Fill the accept queue, so the next connect's SYNs are dropped.
    fillers = []
    for _ in range(4):
        filler = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        filler.setblocking(False)
        try:
            filler.connect(address)
        except BlockingIOError:
            pass
        fillers.append(filler)
    factory = Chat._StoppableConnectFactory(timeout=30)
    try:
        thread, result = _run_in_thread(lambda: factory(address))
        _wait_for_pending(factory)
        started = time.monotonic()
        factory.cancel()
        thread.join(timeout=3)
        elapsed = time.monotonic() - started
    finally:
        for filler in fillers:
            filler.close()
        server.close()

    assert not thread.is_alive()
    assert isinstance(result.get("error"), OSError)
    assert elapsed < 3


def test_a_stalled_tls_handshake_times_out():
    server = _silent_listener()
    factory = Chat._StoppableConnectFactory(wrapper=_tls_wrapper(), timeout=0.5)
    try:
        thread, result = _run_in_thread(lambda: factory(server.getsockname()))
        thread.join(timeout=5)
    finally:
        server.close()

    assert not thread.is_alive()
    assert isinstance(result.get("error"), OSError)
    assert factory._pending is None


def test_a_connected_socket_gets_the_default_timeout_back():
    server = _silent_listener()
    factory = Chat._StoppableConnectFactory(timeout=3)
    try:
        sock = factory(server.getsockname())
        try:
            assert sock.gettimeout() == socket.getdefaulttimeout()
            assert factory._pending is None
        finally:
            sock.close()
    finally:
        server.close()


def test_a_cancelled_factory_does_not_connect():
    server = _silent_listener()
    server.settimeout(0.2)
    factory = Chat._StoppableConnectFactory(timeout=3)
    factory.cancel()
    try:
        with pytest.raises(OSError, match="stopped before connecting"):
            factory(server.getsockname())
        with pytest.raises(socket.timeout):
            server.accept()
    finally:
        server.close()


def test_die_cancels_the_connect_factory(monkeypatch):
    monkeypatch.setattr(Chat, "_irc_tls_available", lambda **_: True)
    client = ClientIRC("someuser", "sometoken", "somechannel")
    factory = client._SingleServerIRCBot__connect_params["connect_factory"]

    client.die()

    assert factory._cancelled is True


def test_a_chat_that_leaves_does_not_stop_other_chats_reconnecting(monkeypatch):
    # The leaving chat's disconnect schedules a reconnect check on its own
    # reactor, which stops and never runs it. Another chat that loses its
    # connection afterwards must still get a reconnect scheduled.
    monkeypatch.setattr(Chat, "_irc_tls_available", lambda **_: True)
    leaving = ClientIRC("someuser", "sometoken", "leaving")
    staying = ClientIRC("someuser", "sometoken", "staying")
    disconnect = irc.client.Event("disconnect", "", "", [""])

    leaving._on_disconnect(leaving.connection, disconnect)
    staying._on_disconnect(staying.connection, disconnect)

    assert len(staying.reactor.scheduler.queue) == 1
    assert leaving.recon is not staying.recon


def test_refused_login_is_retried_after_another_chat_left(monkeypatch, caplog):
    # The warning promises a retry in the background. That needs this chat's
    # own reconnect strategy: with one shared by every chat, a chat that left
    # earlier would have left its check scheduled on its own stopped reactor.
    caplog.set_level(logging.DEBUG)
    leaving = _wired_client(monkeypatch)
    refused = _wired_client(monkeypatch)
    disconnect = irc.client.Event("disconnect", "", "", [""])

    leaving._on_disconnect(leaving.connection, disconnect)
    _feed(refused, LOGIN_REFUSED)
    refused._on_disconnect(refused.connection, disconnect)

    assert [
        level for level, _ in _chat_records(caplog, "Twitch refused the chat login")
    ] == [logging.WARNING]
    assert len(refused.reactor.scheduler.queue) == 1
