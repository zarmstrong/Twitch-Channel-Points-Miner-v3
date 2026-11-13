import logging
from types import SimpleNamespace

import pytest
import requests

from TwitchChannelPointsMiner.classes import Matrix as matrix_module
from TwitchChannelPointsMiner.classes.Matrix import Matrix
from TwitchChannelPointsMiner.classes.Settings import Events
from TwitchChannelPointsMiner.logger import (
    LoggerSettings,
    NotificationHandler,
)

HOMESERVER = "matrix.example"
LOGIN_URL = f"https://{HOMESERVER}/_matrix/client/r0/login"
SEND_URL_PREFIX = f"https://{HOMESERVER}/_matrix/client/r0/rooms/"


def _matrix(events=(Events.STREAMER_ONLINE,)):
    return Matrix(
        username="miner",
        password="secret",
        homeserver=HOMESERVER,
        room_id="!room:matrix.example",
        events=list(events),
    )


def _response(status_code=200, body=None):
    payload = {} if body is None else body

    def json():
        if isinstance(payload, Exception):
            raise payload
        return payload

    def raise_for_status():
        if status_code >= 400:
            error = requests.HTTPError(f"HTTP {status_code}")
            error.response = response
            raise error

    response = SimpleNamespace(
        status_code=status_code, json=json, raise_for_status=raise_for_status
    )
    return response


class _FakePost:
    """Records requests.post calls and answers them from a queue of results."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def login_calls(self):
        return [call for call in self.calls if call[0] == LOGIN_URL]


@pytest.fixture
def fake_post(monkeypatch):
    def install(*results):
        post = _FakePost(*results)
        monkeypatch.setattr(matrix_module.requests, "post", post)
        return post

    return install


def test_constructing_matrix_makes_no_request(fake_post):
    post = fake_post()

    matrix = _matrix()

    assert post.calls == []
    assert matrix.access_token is None
    assert matrix.room_id == "%21room%3Amatrix.example"


def test_first_send_logs_in_and_later_sends_reuse_the_token(fake_post):
    post = fake_post(
        _response(200, {"access_token": "tok-1"}),
        _response(200, {}),
        _response(200, {}),
    )
    matrix = _matrix()

    assert matrix.send("hello", Events.STREAMER_ONLINE) == (True, None)
    assert matrix.send("again", Events.STREAMER_ONLINE) == (True, None)

    assert len(post.login_calls()) == 1
    first_url, first_kwargs = post.calls[1]
    assert first_url.startswith(SEND_URL_PREFIX)
    assert first_kwargs["headers"] == {"Authorization": "Bearer tok-1"}
    assert first_kwargs["json"] == {"body": "hello", "msgtype": "m.text"}
    assert post.calls[2][1]["headers"] == {"Authorization": "Bearer tok-1"}


def test_login_network_error_is_reported_and_retried_on_next_send(fake_post):
    post = fake_post(
        requests.ConnectionError("dns failure"),
        _response(200, {"access_token": "tok-2"}),
        _response(200, {}),
    )
    matrix = _matrix()

    success, reason = matrix.send("hello", Events.STREAMER_ONLINE)
    assert success is False
    assert "Could not connect to Matrix" in reason

    assert matrix.send("hello", Events.STREAMER_ONLINE) == (True, None)
    assert len(post.login_calls()) == 2


def test_login_timeout_is_reported_as_timeout(fake_post):
    fake_post(requests.Timeout("slow"))

    success, reason = _matrix().send("hello", Events.STREAMER_ONLINE)

    assert success is False
    assert "timed out" in reason


def test_login_401_mentions_username_and_password(fake_post):
    fake_post(_response(401, {"errcode": "M_FORBIDDEN"}))
    matrix = _matrix()

    success, reason = matrix.send("hello", Events.STREAMER_ONLINE)

    assert success is False
    assert "username and password" in reason
    assert matrix.access_token is None


def test_login_403_mentions_username_and_password(fake_post):
    fake_post(_response(403, {}))

    success, reason = _matrix().send("hello", Events.STREAMER_ONLINE)

    assert success is False
    assert "username and password" in reason


def test_login_429_mentions_rate_limit(fake_post):
    fake_post(_response(429, {"errcode": "M_LIMIT_EXCEEDED"}))

    success, reason = _matrix().send("hello", Events.STREAMER_ONLINE)

    assert success is False
    assert "rate limited" in reason


def test_login_other_status_reports_http_code(fake_post):
    fake_post(_response(503, ValueError("not json")))

    success, reason = _matrix().send("hello", Events.STREAMER_ONLINE)

    assert success is False
    assert "HTTP 503" in reason
    assert "retried with the next notification" in reason


def test_login_non_json_body_without_token_is_not_a_crash(fake_post):
    fake_post(_response(200, ValueError("not json")))

    success, reason = _matrix().send("hello", Events.STREAMER_ONLINE)

    assert success is False
    assert "HTTP 200" in reason


def test_event_not_enabled_makes_no_request(fake_post):
    post = fake_post()

    result = _matrix(events=()).send("hello", Events.STREAMER_ONLINE)

    assert result == (False, "This event is not enabled for Matrix.")
    assert post.calls == []


def test_message_401_clears_token_so_next_send_logs_in_again(fake_post):
    post = fake_post(
        _response(200, {"access_token": "old"}),
        _response(401, {}),
        _response(200, {"access_token": "new"}),
        _response(200, {}),
    )
    matrix = _matrix()

    success, reason = matrix.send("first", Events.STREAMER_ONLINE)
    assert success is False
    assert "rejected the configured credentials" in reason
    assert matrix.access_token is None

    assert matrix.send("second", Events.STREAMER_ONLINE) == (True, None)
    assert len(post.login_calls()) == 2
    assert post.calls[-1][1]["headers"] == {"Authorization": "Bearer new"}


def test_configured_matrix_without_token_logs_failure_warning(caplog, fake_post):
    fake_post(requests.ConnectionError("dns not ready"))
    matrix = _matrix()
    handler = NotificationHandler(LoggerSettings(matrix=matrix, username=""))

    assert handler.has_providers() is True
    with caplog.at_level(logging.WARNING):
        handler.handle(_event_record())

    assert "Notifications to matrix are failing: Could not connect to Matrix" in (
        caplog.text
    )


def test_matrix_placeholder_check(fake_post):
    fake_post()
    placeholder = NotificationHandler.PLACEHOLDERS["matrix"]

    assert placeholder(_matrix()) is False
    assert placeholder(_matrix_with(room_id="...", password="secret")) is True
    assert placeholder(_matrix_with(room_id="!room:x", password="...")) is True


def _matrix_with(room_id, password):
    return Matrix(
        username="miner",
        password=password,
        homeserver=HOMESERVER,
        room_id=room_id,
        events=[str(Events.STREAMER_ONLINE)],
    )


def _event_record():
    record = logging.LogRecord(
        "miner", logging.INFO, __file__, 1, "Streamer is online", None, None
    )
    record.event = Events.STREAMER_ONLINE
    return record
