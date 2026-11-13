from types import SimpleNamespace

import pytest
from websocket import (
    WebSocketApp,
    WebSocketConnectionClosedException,
    WebSocketTimeoutException,
)

from TwitchChannelPointsMiner.classes.TwitchWebSocket import TwitchWebSocket


def _websocket():
    pool = SimpleNamespace(twitch=None, streamers=[], events_predictions={})
    return TwitchWebSocket(0, pool, "wss://example.invalid")


@pytest.mark.parametrize(
    "error",
    [
        WebSocketConnectionClosedException("closed"),
        WebSocketTimeoutException("timed out"),
        OSError("connection reset"),
    ],
)
def test_send_marks_the_socket_closed_instead_of_raising(monkeypatch, error):
    # A timeout or socket error used to escape send() and end the ping thread.
    websocket = _websocket()

    def failing_send(self, data, *args, **kwargs):
        raise error

    monkeypatch.setattr(WebSocketApp, "send", failing_send)

    assert websocket.send({"type": "PING"}) is False
    assert websocket.is_closed is True


def test_failed_ping_does_not_count_as_sent(monkeypatch):
    # The main loop reconnects a socket with no PING for 10 minutes, so a
    # PING that never went out must not reset that clock.
    websocket = _websocket()
    websocket.last_ping = 0

    def failing_send(self, data, *args, **kwargs):
        raise OSError("connection reset")

    monkeypatch.setattr(WebSocketApp, "send", failing_send)
    websocket.ping()

    assert websocket.last_ping == 0


def test_successful_ping_updates_the_last_ping(monkeypatch):
    websocket = _websocket()
    websocket.last_ping = 0
    sent = []
    monkeypatch.setattr(
        WebSocketApp, "send", lambda self, data, *args, **kwargs: sent.append(data)
    )

    websocket.ping()

    assert sent == ['{"type":"PING"}']
    assert websocket.last_ping > 0
