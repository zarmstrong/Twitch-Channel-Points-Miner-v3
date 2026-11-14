import logging
import socket
import threading
import ssl
import time
import types
from unittest.mock import MagicMock, call

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
    yield
    Chat._tls_supported = None
    Chat._tls_failure_warned = False
    Chat._tls_failed_at = None


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
    assert "connect_factory" in client._SingleServerIRCBot__connect_params


def test_client_irc_uses_plaintext_port_when_explicitly_allowed(monkeypatch):
    monkeypatch.setattr(Chat, "_irc_tls_available", lambda **_: False)
    monkeypatch.setenv("TCPM_ALLOW_PLAINTEXT_IRC", "1")

    client = ClientIRC("someuser", "sometoken", "somechannel")

    server = client.servers.peek()
    assert server.port == IRC_PORT
    assert "connect_factory" not in client._SingleServerIRCBot__connect_params


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
