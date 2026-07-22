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
                content = f"```\n{message}\n```" if "\n" in message else f"`{message}`"
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
