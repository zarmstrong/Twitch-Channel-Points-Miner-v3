import time
from textwrap import dedent
from urllib.parse import quote

import requests

from TwitchChannelPointsMiner.classes.NotificationError import format_request_failure
from TwitchChannelPointsMiner.classes.Settings import Events

# After a failed login, wait this long before the next attempt, doubling on
# each further failure up to the maximum, so wrong credentials or an
# unreachable homeserver don't get a login request with every notification.
LOGIN_RETRY_INITIAL_SECONDS = 60
LOGIN_RETRY_MAX_SECONDS = 3600


def _describe_delay(seconds):
    seconds = int(round(seconds))
    if seconds < 120:
        return f"{seconds} seconds"
    return f"{round(seconds / 60)} minutes"


def _retry_after_seconds(response):
    """The Retry-After header of a 429, in seconds, or None."""
    headers = getattr(response, "headers", None) or {}
    try:
        value = float(headers.get("Retry-After"))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


class Matrix(object):
    __slots__ = [
        "username",
        "password",
        "access_token",
        "homeserver",
        "room_id",
        "events",
        "login_retry_at",
        "login_retry_delay",
        "login_failure",
    ]

    def __init__(
        self, username: str, password: str, homeserver: str, room_id: str, events: list
    ):
        # No network call here: config.py runs before logging is configured, so
        # a failed login would be lost. The login happens on the first send.
        self.username = username
        self.password = password
        self.homeserver = homeserver
        self.room_id = quote(room_id)
        self.events = [str(e) for e in events]
        self.access_token = None
        self.login_retry_at = None
        self.login_retry_delay = None
        self.login_failure = None

    def __repr__(self):
        # Never show the password or token.
        return (
            f"Matrix(homeserver={self.homeserver!r}, room_id={self.room_id!r}, "
            f"events={self.events!r})"
        )

    def _login(self) -> str | None:
        """Log in and store the access token. Return None on success, else a reason.

        After a failure, later calls return the same reason without a request
        until the retry time has passed.
        """
        if self.login_retry_at is not None and time.monotonic() < self.login_retry_at:
            return self.login_failure
        try:
            response = requests.post(
                url=f"https://{self.homeserver}/_matrix/client/r0/login",
                json={
                    "user": self.username,
                    "password": self.password,
                    "type": "m.login.password",
                },
                timeout=(5, 15),
            )
        except requests.RequestException as error:
            return self._login_failed(format_request_failure("Matrix", error))

        try:
            body = response.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}

        access_token = body.get("access_token")
        if access_token:
            self.access_token = access_token
            self.login_retry_at = None
            self.login_retry_delay = None
            self.login_failure = None
            return None
        status = response.status_code
        if status == 429:
            return self._login_failed(
                "Matrix login was rate limited.", _retry_after_seconds(response)
            )
        if status in (401, 403):
            return self._login_failed(
                "Matrix rejected the login. Check the username and password."
            )
        if 200 <= status < 300:
            return self._login_failed(
                f"Matrix login returned no access token (HTTP {status}). "
                "Check the homeserver address."
            )
        return self._login_failed(f"Matrix login failed (HTTP {status}).")

    def _login_failed(self, reason, retry_after=None):
        delay = (
            LOGIN_RETRY_INITIAL_SECONDS
            if self.login_retry_delay is None
            else min(self.login_retry_delay * 2, LOGIN_RETRY_MAX_SECONDS)
        )
        self.login_retry_delay = delay
        if retry_after is not None:
            # The backoff still grows, but never retry before the homeserver
            # allows it.
            delay = min(max(delay, retry_after), LOGIN_RETRY_MAX_SECONDS)
        self.login_retry_at = time.monotonic() + delay
        self.login_failure = (
            f"{reason} The login is retried in {_describe_delay(delay)}."
        )
        return self.login_failure

    def send(self, message: str, event: Events) -> tuple[bool, str | None]:
        if str(event) not in self.events:
            return False, "This event is not enabled for Matrix."
        for attempt in range(2):
            if not self.access_token:
                reason = self._login()
                if reason:
                    return False, reason
            try:
                response = requests.post(
                    url=f"https://{self.homeserver}/_matrix/client/r0/rooms/{self.room_id}/send/m.room.message",
                    headers={"Authorization": f"Bearer {self.access_token}"},
                    json={
                        "body": dedent(message),
                        "msgtype": "m.text",
                    },
                    timeout=(5, 15),
                )
                if response.status_code == 401:
                    # The token expired or was revoked. Log in again and
                    # resend this message once.
                    self.access_token = None
                    if attempt == 0:
                        continue
                response.raise_for_status()
                return True, None
            except requests.RequestException as error:
                return False, format_request_failure("Matrix", error)
