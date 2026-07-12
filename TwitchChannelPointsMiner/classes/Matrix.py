import logging
from textwrap import dedent
from urllib.parse import quote

import requests

from TwitchChannelPointsMiner.classes.Settings import Events


class Matrix(object):
    __slots__ = ["access_token", "homeserver", "room_id", "events"]

    def __init__(
        self, username: str, password: str, homeserver: str, room_id: str, events: list
    ):
        self.homeserver = homeserver
        self.room_id = quote(room_id)
        self.events = [str(e) for e in events]

        try:
            body = requests.post(
                url=f"https://{self.homeserver}/_matrix/client/r0/login",
                json={
                    "user": username,
                    "password": password,
                    "type": "m.login.password",
                },
                timeout=(5, 15),
            ).json()
        except (requests.RequestException, ValueError):
            body = {}

        self.access_token = body.get("access_token")

        if not self.access_token:
            logging.getLogger(__name__).info(
                "Invalid Matrix password provided. Notifications will not be sent."
            )

    def send(self, message: str, event: Events) -> None:
        if str(event) in self.events:
            try:
                requests.post(
                    url=f"https://{self.homeserver}/_matrix/client/r0/rooms/{self.room_id}/send/m.room.message",
                    headers={"Authorization": f"Bearer {self.access_token}"},
                    json={
                        "body": dedent(message),
                        "msgtype": "m.text",
                    },
                    timeout=(5, 15),
                )
            except requests.RequestException:
                return
