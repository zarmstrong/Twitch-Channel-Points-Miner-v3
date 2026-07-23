import socket

import pytest
import requests

from TwitchChannelPointsMiner.classes.NotificationError import format_request_failure


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
            "Discord rate limited the test notification (HTTP 429).",
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
