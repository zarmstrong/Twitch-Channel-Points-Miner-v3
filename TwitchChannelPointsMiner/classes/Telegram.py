from textwrap import dedent

import requests

from TwitchChannelPointsMiner.classes.Settings import Events


class Telegram(object):
    __slots__ = ["chat_id", "telegram_api", "events", "disable_notification"]

    def __init__(
        self, chat_id: int, token: str, events: list, disable_notification: bool = False
    ):
        self.chat_id = chat_id
        self.telegram_api = f"https://api.telegram.org/bot{token}/sendMessage"
        self.events = [str(e) for e in events]
        self.disable_notification = disable_notification

    def send(self, message: str, event: Events) -> tuple[bool, str | None]:
        if str(event) in self.events:
            try:
                response = requests.post(
                    url=self.telegram_api,
                    data={
                        "chat_id": self.chat_id,
                        "text": dedent(message),
                        "disable_web_page_preview": True,
                        "disable_notification": self.disable_notification,
                    },
                    timeout=(5, 15),
                )
                response.raise_for_status()
                return True, None
            except requests.HTTPError as error:
                status = getattr(error.response, "status_code", None)
                detail = f" (HTTP {status})" if status is not None else ""
                return False, f"Telegram rejected the test notification{detail}."
            except requests.RequestException:
                return False, "Unable to connect to Telegram."
        return False, "This event is not enabled for Telegram."
