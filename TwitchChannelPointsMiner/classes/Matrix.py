from textwrap import dedent
from urllib.parse import quote

import requests

from TwitchChannelPointsMiner.classes.NotificationError import format_request_failure
from TwitchChannelPointsMiner.classes.Settings import Events


class Matrix(object):
    __slots__ = [
        "username",
        "password",
        "access_token",
        "homeserver",
        "room_id",
        "events",
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

    def _login(self) -> str | None:
        """Log in and store the access token. Return None on success, else a reason."""
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
            return format_request_failure("Matrix", error)

        try:
            body = response.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}

        access_token = body.get("access_token")
        if access_token:
            self.access_token = access_token
            return None
        if response.status_code == 429:
            return "Matrix login was rate limited. It is retried with the next notification."
        if response.status_code in (401, 403):
            return "Matrix rejected the login. Check the username and password."
        return f"Matrix login failed (HTTP {response.status_code}). It is retried with the next notification."

    def send(self, message: str, event: Events) -> tuple[bool, str | None]:
        if str(event) in self.events:
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
                    # The token expired or was revoked; the next send logs in again.
                    self.access_token = None
                response.raise_for_status()
                return True, None
            except requests.RequestException as error:
                return False, format_request_failure("Matrix", error)
        return False, "This event is not enabled for Matrix."
