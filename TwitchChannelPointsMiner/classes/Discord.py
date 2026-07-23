from textwrap import dedent

import requests

from TwitchChannelPointsMiner.classes.NotificationError import format_request_failure
from TwitchChannelPointsMiner.classes.Settings import Events


class Discord(object):
    __slots__ = ["webhook_api", "events"]

    def __init__(self, webhook_api: str, events: list):
        self.webhook_api = webhook_api
        self.events = [str(e) for e in events]

    def send(self, message: str, event: Events) -> tuple[bool, str | None]:
        if str(event) in self.events:
            try:
                message = dedent(message).strip()
                max_backticks = 0
                backticks = 0
                for character in message:
                    if character == "`":
                        backticks += 1
                        max_backticks = max(max_backticks, backticks)
                    else:
                        backticks = 0

                multiline = "\n" in message
                fence = "`" * max(3 if multiline else 1, max_backticks + 1)
                if multiline:
                    content = f"{fence}\n{message}\n{fence}"
                elif max_backticks:
                    content = f"{fence} {message} {fence}"
                else:
                    content = f"`{message}`"
                response = requests.post(
                    url=self.webhook_api,
                    data={
                        "content": content,
                        "username": "Twitch Channel Points Miner",
                        "avatar_url": "https://i.imgur.com/X9fEkhT.png",
                    },
                    timeout=(5, 15),
                )
                response.raise_for_status()
                return True, None
            except requests.RequestException as error:
                return False, format_request_failure("Discord", error)
        return False, "This event is not enabled for Discord."
