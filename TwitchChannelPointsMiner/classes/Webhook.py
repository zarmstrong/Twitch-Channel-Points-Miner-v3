import requests

from TwitchChannelPointsMiner.classes.Settings import Events


class Webhook(object):
    __slots__ = ["endpoint", "method", "events", "timeout"]

    def __init__(self, endpoint: str, method: str, events: list, timeout: float = 10):
        self.endpoint = endpoint
        self.method = method
        self.events = [str(e) for e in events]
        self.timeout = timeout

    def send(self, message: str, event: Events) -> tuple[bool, str | None]:
        if str(event) in self.events:
            parameters = {"event_name": str(event), "message": message}

            try:
                if self.method.lower() == "get":
                    response = requests.get(
                        url=self.endpoint,
                        params=parameters,
                        timeout=self.timeout,
                    )
                elif self.method.lower() == "post":
                    response = requests.post(
                        url=self.endpoint,
                        data=parameters,
                        timeout=self.timeout,
                    )
                else:
                    raise ValueError("Invalid method, use POST or GET")
                response.raise_for_status()
                return True, None
            except requests.HTTPError as error:
                status = getattr(error.response, "status_code", None)
                detail = f" (HTTP {status})" if status is not None else ""
                return False, f"Webhook rejected the test notification{detail}."
            except requests.RequestException:
                return False, "Unable to connect to the webhook endpoint."
        return False, "This event is not enabled for the webhook."
