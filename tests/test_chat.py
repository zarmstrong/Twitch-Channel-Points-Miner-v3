import logging
import socket
import ssl
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
    yield
    Chat._tls_supported = None


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


def test_irc_tls_available_returns_false_and_caches_on_failure(monkeypatch):
    calls = 0

    def failing_connect(address, timeout=None):
        nonlocal calls
        calls += 1
        raise OSError("connection refused")

    monkeypatch.setattr(socket, "create_connection", failing_connect)

    assert Chat._irc_tls_available() is False
    assert Chat._irc_tls_available() is False
    # The probe result is cached after the first attempt, so a network
    # blip only costs one failed handshake for the life of the process.
    assert calls == 1


def test_client_irc_uses_tls_port_and_wrapped_factory_when_available(monkeypatch):
    monkeypatch.setattr(Chat, "_irc_tls_available", lambda: True)

    client = ClientIRC("someuser", "sometoken", "somechannel")

    server = client.servers.peek()
    assert server.port == IRC_TLS_PORT
    assert "connect_factory" in client._SingleServerIRCBot__connect_params


def test_client_irc_falls_back_to_plaintext_port_when_tls_unavailable(monkeypatch):
    monkeypatch.setattr(Chat, "_irc_tls_available", lambda: False)

    client = ClientIRC("someuser", "sometoken", "somechannel")

    server = client.servers.peek()
    assert server.port == IRC_PORT
    assert "connect_factory" not in client._SingleServerIRCBot__connect_params


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
    monkeypatch.setattr(Chat, "_irc_tls_available", lambda: False)
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
    client = _client_with(monkeypatch)
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


def test_format_gift_sub_message_is_capped_at_500_characters():
    message = Chat.format_gift_sub_message("x" * 600, "Bob", "chan", "Tier 1")
    assert len(message) == Chat.MAX_CHAT_MESSAGE_LENGTH == 500


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
    sentinel = object()
    thread = ThreadChat("u", "t", "c")
    thread.settings = sentinel

    thread.run()

    assert created["kwargs"] == {"settings": sentinel}
