from textwrap import dedent

import requests

from TwitchChannelPointsMiner.classes.NotificationError import format_request_failure
from TwitchChannelPointsMiner.classes.Settings import Events


class Telegram(object):
    __slots__ = [
        "chat_id",
        "telegram_api",
        "events",
        "disable_notification",
        "message_thread_id",
    ]

    def __init__(
        self,
        chat_id: int,
        token: str,
        events: list,
        disable_notification: bool = False,
        message_thread_id: int | None = None,
    ):
        self.chat_id = chat_id
        self.telegram_api = f"https://api.telegram.org/bot{token}/sendMessage"
        self.events = [str(e) for e in events]
        self.disable_notification = disable_notification
        self.message_thread_id = message_thread_id

    def send(self, message: str, event: Events) -> tuple[bool, str | None]:
        if str(event) in self.events:
            try:
                data = {
                    "chat_id": self.chat_id,
                    "text": dedent(message),
                    "disable_web_page_preview": True,
                    "disable_notification": self.disable_notification,
                }
                if self.message_thread_id is not None:
                    data["message_thread_id"] = self.message_thread_id

                response = requests.post(
                    url=self.telegram_api,
                    data=data,
                    timeout=(5, 15),
                )
                response.raise_for_status()
                return True, None
            except requests.RequestException as error:
                return False, format_request_failure("Telegram", error)
        return False, "This event is not enabled for Telegram."
