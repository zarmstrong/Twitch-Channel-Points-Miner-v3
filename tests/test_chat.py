import socket
import ssl

import pytest

from TwitchChannelPointsMiner.classes import Chat
from TwitchChannelPointsMiner.classes.Chat import ClientIRC
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
