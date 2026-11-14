import json
import logging
from pathlib import Path

import pytest
from websocket import WebSocketApp

from TwitchChannelPointsMiner.classes.TwitchWebSocket import TwitchWebSocket
from TwitchChannelPointsMiner.logger import LoggerSettings, configure_loggers

TOKEN = "leaked-oauth-token-0123456789"


def _websocket(monkeypatch):
    sent = []
    monkeypatch.setattr(WebSocketApp, "send", lambda self, data: sent.append(data))
    websocket = object.__new__(TwitchWebSocket)
    websocket.index = 0
    websocket.is_closed = False
    return websocket, sent


class _Topic:
    def __init__(self, user_topic):
        self.user_topic = user_topic

    def is_user_topic(self):
        return self.user_topic

    def __str__(self):
        return "community-points-user-v1.42"


@pytest.mark.parametrize("method", ["listen", "unlisten"])
def test_logged_request_redacts_the_token_but_sends_it(monkeypatch, caplog, method):
    websocket, sent = _websocket(monkeypatch)

    with caplog.at_level(logging.DEBUG):
        getattr(websocket, method)(_Topic(user_topic=True), TOKEN)

    assert len(sent) == 1
    assert json.loads(sent[0])["data"]["auth_token"] == TOKEN
    assert TOKEN not in caplog.text
    assert '"auth_token":"<redacted>"' in caplog.text
    assert "community-points-user-v1.42" in caplog.text


def test_requests_without_a_token_are_logged_unchanged(monkeypatch, caplog):
    websocket, sent = _websocket(monkeypatch)

    with caplog.at_level(logging.DEBUG):
        websocket.listen(_Topic(user_topic=False), TOKEN)
        websocket.ping()

    assert all("auth_token" not in json.loads(data).get("data", {}) for data in sent)
    assert "<redacted>" not in caplog.text
    assert '{"type":"PING"}' in caplog.text


def test_redaction_leaves_the_request_unchanged():
    request = {"type": "LISTEN", "data": {"topics": ["t"], "auth_token": TOKEN}}

    TwitchWebSocket.loggable_request(request)

    assert request["data"]["auth_token"] == TOKEN


def test_configured_file_log_never_contains_the_token(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    websocket, sent = _websocket(monkeypatch)
    root_logger = logging.getLogger()
    existing_handlers = set(root_logger.handlers)
    existing_level = root_logger.level

    # The default file_level is DEBUG, as on an ordinary install.
    log_file, listener = configure_loggers("test-user", LoggerSettings())
    try:
        websocket.listen(_Topic(user_topic=True), TOKEN)
    finally:
        listener.stop()
        for handler in listener.handlers:
            handler.close()
        for handler in set(root_logger.handlers) - existing_handlers:
            root_logger.removeHandler(handler)
            handler.close()
        root_logger.setLevel(existing_level)

    contents = Path(log_file).read_text(encoding="utf-8")
    assert "Send:" in contents
    assert TOKEN not in contents
    assert json.loads(sent[0])["data"]["auth_token"] == TOKEN
