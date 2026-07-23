from textwrap import dedent

import requests

from TwitchChannelPointsMiner.classes.NotificationError import format_request_failure
from TwitchChannelPointsMiner.classes.Settings import Events


class Gotify(object):
    __slots__ = ["endpoint", "priority", "events"]

    def __init__(self, endpoint: str, priority: int, events: list):
        self.endpoint = endpoint
        self.priority = priority
        self.events = [str(e) for e in events]

    def send(self, message: str, event: Events) -> tuple[bool, str | None]:
        if str(event) in self.events:
            try:
                response = requests.post(
                    url=self.endpoint,
                    data={
                        "message": dedent(message),
                        "priority": self.priority,
                    },
                    timeout=(5, 15),
                )
                response.raise_for_status()
                return True, None
            except requests.RequestException as error:
                return False, format_request_failure("Gotify", error)
        return False, "This event is not enabled for Gotify."
