from textwrap import dedent

import requests

from TwitchChannelPointsMiner.classes.NotificationError import format_request_failure
from TwitchChannelPointsMiner.classes.Settings import Events


class Pushover(object):
    __slots__ = ["userkey", "token", "priority", "sound", "events"]

    def __init__(self, userkey: str, token: str, priority, sound, events: list):
        self.userkey = userkey
        self.token = token
        self.priority = priority
        self.sound = sound
        self.events = [str(e) for e in events]

    def send(self, message: str, event: Events) -> tuple[bool, str | None]:
        if str(event) in self.events:
            try:
                response = requests.post(
                    url="https://api.pushover.net/1/messages.json",
                    data={
                        "user": self.userkey,
                        "token": self.token,
                        "message": dedent(message),
                        "title": "Twitch Channel Points Miner",
                        "priority": self.priority,
                        "sound": self.sound,
                    },
                    timeout=(5, 15),
                )
                response.raise_for_status()
                return True, None
            except requests.RequestException as error:
                return False, format_request_failure("Pushover", error)
        return False, "This event is not enabled for Pushover."
