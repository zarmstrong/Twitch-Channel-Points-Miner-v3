import json
import socket
from unittest.mock import Mock, patch

import pytest
import requests

from TwitchChannelPointsMiner.classes.NotificationError import (
    format_request_failure,
    send_parts,
)


def http_error(status):
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(response=response)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            http_error(401),
            "Discord rejected the configured credentials (HTTP 401).",
        ),
        (http_error(403), "Discord denied the request (HTTP 403)."),
        (http_error(404), "Discord endpoint was not found (HTTP 404)."),
        (
            http_error(429),
            "Discord rate limited the notification (HTTP 429).",
        ),
        (http_error(503), "Discord reported a server error (HTTP 503)."),
        (
            requests.Timeout("https://secret.invalid/token"),
            "The connection to Discord timed out.",
        ),
        (
            requests.exceptions.SSLError("certificate details"),
            "TLS negotiation with Discord failed. Check its HTTPS certificate.",
        ),
        (
            requests.exceptions.ProxyError("https://proxy-secret.invalid"),
            "The configured proxy could not connect to Discord.",
        ),
        (
            requests.exceptions.InvalidURL("https://token@invalid"),
            "The configured Discord URL is invalid.",
        ),
        (
            requests.exceptions.TooManyRedirects("https://secret.invalid"),
            "Discord returned too many redirects.",
        ),
    ],
)
def test_request_failure_messages_are_specific_and_sanitized(error, expected):
    assert format_request_failure("Discord", error) == expected


@pytest.mark.parametrize(
    ("cause", "expected"),
    [
        (
            socket.gaierror(),
            "Could not resolve the Discord host. Check its hostname and DNS.",
        ),
        (
            ConnectionRefusedError(),
            "Discord refused the connection. Check its host and port.",
        ),
        (
            ConnectionResetError(),
            "The connection to Discord was reset before delivery completed.",
        ),
    ],
)
def test_request_failure_finds_nested_connection_cause(cause, expected):
    error = requests.ConnectionError("https://user:secret@example.invalid/token")
    error.__cause__ = cause

    result = format_request_failure("Discord", error)

    assert result == expected
    assert "secret" not in result


def test_unknown_connection_failure_gives_actionable_safe_checks():
    error = requests.ConnectionError("https://token@example.invalid/private")

    result = format_request_failure("Gotify", error)

    assert result == (
        "Could not connect to Gotify. Check its URL, network, DNS, and firewall."
    )
    assert "token" not in result


def rate_limited(retry_after=None, body=None):
    response = requests.Response()
    response.status_code = 429
    if retry_after is not None:
        response.headers["Retry-After"] = retry_after
    if body is not None:
        response._content = json.dumps(body).encode()
    return requests.HTTPError(response=response)


def test_send_parts_posts_every_part_in_order():
    post_one = Mock()

    assert send_parts("Discord", ["one", "two", "three"], post_one) == (True, None)

    assert [call.args for call in post_one.call_args_list] == [
        ("one",),
        ("two",),
        ("three",),
    ]


def test_send_parts_reports_failing_part_and_stops():
    post_one = Mock(side_effect=[None, http_error(400), None])

    assert send_parts("Discord", ["one", "two", "three"], post_one) == (
        False,
        "Discord rejected the notification (HTTP 400) (part 2 of 3).",
    )
    assert post_one.call_count == 2


def test_send_parts_single_part_failure_has_no_part_suffix():
    post_one = Mock(side_effect=requests.Timeout())

    assert send_parts("Telegram", ["only"], post_one) == (
        False,
        "The connection to Telegram timed out.",
    )


def test_send_parts_retries_rate_limited_part_once_after_retry_after():
    post_one = Mock(side_effect=[None, rate_limited("1.5"), None, None])

    with patch(
        "TwitchChannelPointsMiner.classes.NotificationError.time.sleep"
    ) as sleep:
        assert send_parts("Discord", ["one", "two", "three"], post_one) == (
            True,
            None,
        )

    sleep.assert_called_once_with(1.5)
    assert [call.args for call in post_one.call_args_list] == [
        ("one",),
        ("two",),
        ("two",),
        ("three",),
    ]


def test_send_parts_does_not_retry_429_without_wait_hint():
    post_one = Mock(side_effect=[rate_limited(), None])

    with patch(
        "TwitchChannelPointsMiner.classes.NotificationError.time.sleep"
    ) as sleep:
        assert send_parts("Pushover", ["one"], post_one) == (
            False,
            "Pushover rate limited the notification (HTTP 429).",
        )

    sleep.assert_not_called()
    post_one.assert_called_once_with("one")


def test_send_parts_uses_telegram_body_retry_after_hint():
    error = rate_limited(body={"ok": False, "parameters": {"retry_after": 4}})
    post_one = Mock(side_effect=[error, None])

    with patch(
        "TwitchChannelPointsMiner.classes.NotificationError.time.sleep"
    ) as sleep:
        assert send_parts("Telegram", ["one"], post_one) == (True, None)

    sleep.assert_called_once_with(4.0)
    assert post_one.call_count == 2


def test_send_parts_uses_top_level_body_retry_after_hint():
    post_one = Mock(side_effect=[rate_limited(body={"retry_after": 0.5}), None])

    with patch(
        "TwitchChannelPointsMiner.classes.NotificationError.time.sleep"
    ) as sleep:
        assert send_parts("Discord", ["one"], post_one) == (True, None)

    sleep.assert_called_once_with(0.5)


def test_send_parts_prefers_retry_after_header_over_body_hint():
    error = rate_limited("2", body={"parameters": {"retry_after": 7}})
    post_one = Mock(side_effect=[error, None])

    with patch(
        "TwitchChannelPointsMiner.classes.NotificationError.time.sleep"
    ) as sleep:
        assert send_parts("Telegram", ["one"], post_one) == (True, None)

    sleep.assert_called_once_with(2.0)


def test_send_parts_gives_up_when_body_retry_after_exceeds_cap():
    error = rate_limited(body={"ok": False, "parameters": {"retry_after": 30}})
    post_one = Mock(side_effect=[error])

    with patch(
        "TwitchChannelPointsMiner.classes.NotificationError.time.sleep"
    ) as sleep:
        assert send_parts("Telegram", ["one", "two"], post_one) == (
            False,
            "Telegram rate limited the notification (HTTP 429) (part 1 of 2).",
        )

    sleep.assert_not_called()
    post_one.assert_called_once_with("one")


def test_send_parts_gives_up_when_retry_after_exceeds_cap():
    post_one = Mock(side_effect=[None, rate_limited("60")])

    with patch(
        "TwitchChannelPointsMiner.classes.NotificationError.time.sleep"
    ) as sleep:
        assert send_parts("Discord", ["one", "two"], post_one) == (
            False,
            "Discord rate limited the notification (HTTP 429) (part 2 of 2).",
        )

    sleep.assert_not_called()
    assert post_one.call_count == 2


def test_send_parts_gives_up_when_rate_limited_again_after_retry():
    post_one = Mock(side_effect=[rate_limited("1"), rate_limited("1")])

    with patch(
        "TwitchChannelPointsMiner.classes.NotificationError.time.sleep"
    ) as sleep:
        assert send_parts("Pushover", ["one", "two"], post_one) == (
            False,
            "Pushover rate limited the notification (HTTP 429) (part 1 of 2).",
        )

    sleep.assert_called_once_with(1.0)
    assert post_one.call_count == 2


def test_send_parts_retries_when_retry_after_equals_cap():
    post_one = Mock(side_effect=[rate_limited("10"), None])

    with patch(
        "TwitchChannelPointsMiner.classes.NotificationError.time.sleep"
    ) as sleep:
        assert send_parts("Discord", ["one"], post_one) == (True, None)

    sleep.assert_called_once_with(10.0)
    assert post_one.call_count == 2


def test_send_parts_does_not_retry_other_errors_with_retry_after():
    error = http_error(503)
    error.response.headers["Retry-After"] = "1"
    post_one = Mock(side_effect=error)

    with patch(
        "TwitchChannelPointsMiner.classes.NotificationError.time.sleep"
    ) as sleep:
        success, reason = send_parts("Discord", ["one"], post_one)

    assert success is False
    assert reason == format_request_failure("Discord", error)
    sleep.assert_not_called()
    post_one.assert_called_once_with("one")


def test_send_parts_treats_negative_retry_after_as_no_wait():
    post_one = Mock(side_effect=[rate_limited("-1"), None])

    with patch(
        "TwitchChannelPointsMiner.classes.NotificationError.time.sleep"
    ) as sleep:
        assert send_parts("Discord", ["one"], post_one) == (True, None)

    sleep.assert_called_once_with(0.0)
    assert post_one.call_count == 2
