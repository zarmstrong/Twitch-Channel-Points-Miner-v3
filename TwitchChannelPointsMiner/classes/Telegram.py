from functools import partial
from textwrap import dedent

import requests

from TwitchChannelPointsMiner.classes.NotificationError import (
    MAX_NOTIFICATION_PARTS,
    notification_prefix,
    send_parts,
)
from TwitchChannelPointsMiner.classes.Settings import Events
from TwitchChannelPointsMiner.utils import split_message_lines

# Telegram counts Unicode characters for ``text`` length; only entity offsets
# and lengths use UTF-16 code units.
TELEGRAM_TEXT_LIMIT = 4096


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
            texts = split_message_lines(
                dedent(message),
                TELEGRAM_TEXT_LIMIT,
                notification_prefix(),
                length_func=len,
                max_pieces=MAX_NOTIFICATION_PARTS,
            )
            # Only the first part notifies as configured. Later parts are sent
            # silently, so a long report does not buzz the device once per
            # part (like Pushover's later parts at quiet priority).
            parts = [(texts[0], self.disable_notification)] + [
                (text, True) for text in texts[1:]
            ]
            session = requests.Session()
            try:
                return send_parts("Telegram", parts, partial(self._post, session))
            finally:
                session.close()
        return False, "This event is not enabled for Telegram."

    def _post(self, session: requests.Session, part: tuple) -> None:
        text, disable_notification = part
        data = {
            "chat_id": self.chat_id,
            "text": text,
            "disable_web_page_preview": True,
            "disable_notification": disable_notification,
        }
        if self.message_thread_id is not None:
            data["message_thread_id"] = self.message_thread_id

        response = session.post(
            url=self.telegram_api,
            data=data,
            timeout=(5, 15),
        )
        response.raise_for_status()
