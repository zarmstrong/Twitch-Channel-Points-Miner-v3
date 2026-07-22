from textwrap import dedent

import requests

from TwitchChannelPointsMiner.classes.Settings import Events


class Discord(object):
    __slots__ = ["webhook_api", "events"]

    def __init__(self, webhook_api: str, events: list):
        self.webhook_api = webhook_api
        self.events = [str(e) for e in events]

    def send(self, message: str, event: Events) -> None:
        if str(event) in self.events:
            try:
                message = dedent(message).strip()
                max_backticks = 0
                backticks = 0
                for character in message:
                    backticks = backticks + 1 if character == "`" else 0
                    max_backticks = max(max_backticks, backticks)

                multiline = "\n" in message
                fence = "`" * max(3 if multiline else 1, max_backticks + 1)
                content = (
                    f"{fence}\n{message}\n{fence}"
                    if multiline
                    else f"{fence} {message} {fence}"
                    if max_backticks
                    else f"`{message}`"
                )
                requests.post(
                    url=self.webhook_api,
                    data={
                        "content": content,
                        "username": "Twitch Channel Points Miner",
                        "avatar_url": "https://i.imgur.com/X9fEkhT.png",
                    },
                    timeout=(5, 15),
                )
            except requests.RequestException:
                return
