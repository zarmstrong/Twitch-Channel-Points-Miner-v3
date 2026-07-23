from textwrap import dedent

import requests

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
            except requests.HTTPError as error:
                status = getattr(error.response, "status_code", None)
                detail = f" (HTTP {status})" if status is not None else ""
                return False, f"Gotify rejected the test notification{detail}."
            except requests.RequestException:
                return False, "Unable to connect to Gotify."
        return False, "This event is not enabled for Gotify."
