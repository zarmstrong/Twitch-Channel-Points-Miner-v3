import requests

from TwitchChannelPointsMiner.classes.Settings import Events


class Webhook(object):
    __slots__ = ["endpoint", "method", "events", "timeout"]

    def __init__(self, endpoint: str, method: str, events: list, timeout: float = 10):
        self.endpoint = endpoint
        self.method = method
        self.events = [str(e) for e in events]
        self.timeout = timeout

    def send(self, message: str, event: Events) -> None:
        if str(event) in self.events:
            parameters = {"event_name": str(event), "message": message}

            try:
                if self.method.lower() == "get":
                    requests.get(
                        url=self.endpoint,
                        params=parameters,
                        timeout=self.timeout,
                    )
                elif self.method.lower() == "post":
                    requests.post(
                        url=self.endpoint,
                        data=parameters,
                        timeout=self.timeout,
                    )
                else:
                    raise ValueError("Invalid method, use POST or GET")
            except requests.RequestException:
                return
