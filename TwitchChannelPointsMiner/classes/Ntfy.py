from textwrap import dedent
from urllib.parse import quote

import requests

from TwitchChannelPointsMiner.classes.NotificationError import format_request_failure
from TwitchChannelPointsMiner.classes.Settings import Events


class Ntfy(object):
    __slots__ = [
        "topic",
        "events",
        "server_url",
        "token",
        "priority",
        "tags",
        "timeout",
    ]

    def __init__(
        self,
        topic: str,
        events: list,
        server_url: str = "https://ntfy.sh",
        token: str | None = None,
        priority: int | None = None,
        tags: list | tuple | str | None = None,
        timeout: float = 10,
    ):
        self.topic = topic
        self.events = [str(event) for event in events]
        self.server_url = server_url.rstrip("/")
        self.token = token
        self.priority = priority
        self.tags = [tags] if isinstance(tags, str) else list(tags or [])
        self.timeout = timeout

    def send(self, message: str, event: Events) -> tuple[bool, str | None]:
        if str(event) not in self.events:
            return False, "This event is not enabled for ntfy."

        title = event.name.replace("_", " ").title()
        headers = {"Title": f"Twitch Channel Points Miner: {title}"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.priority is not None:
            headers["Priority"] = str(self.priority)
        if self.tags:
            headers["Tags"] = ",".join(self.tags)

        try:
            response = requests.post(
                url=f"{self.server_url.rstrip('/')}/{quote(self.topic, safe='')}",
                data=dedent(message).encode("utf-8"),
                headers=headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            return True, None
        except requests.RequestException as error:
            return False, format_request_failure("ntfy", error)
