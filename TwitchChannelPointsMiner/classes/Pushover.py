from functools import partial
from textwrap import dedent

import requests

from TwitchChannelPointsMiner.classes.NotificationError import (
    notification_prefix,
    send_parts,
)
from TwitchChannelPointsMiner.classes.Settings import Events
from TwitchChannelPointsMiner.utils import split_message_lines

# Pushover counts Unicode characters, including astral emoji as one character.
# It rejects messages whose ``message`` exceeds this many characters
# (the ``title`` limit is 250, which the fixed title stays well under).
PUSHOVER_MESSAGE_LIMIT = 1024


class Pushover(object):
    __slots__ = ["userkey", "token", "priority", "sound", "events"]

    def __init__(self, userkey: str, token: str, priority, sound, events: list):
        self.userkey = userkey
        self.token = token
        self.priority = priority
        self.sound = sound
        self.events = [str(e) for e in events]

    def send(self, message: str, event: Events) -> tuple[bool, str | None]:
        if str(event) in self.events:
            texts = split_message_lines(
                dedent(message),
                PUSHOVER_MESSAGE_LIMIT,
                notification_prefix(),
                length_func=len,
            )
            # Only the first part alerts as configured. Later parts are sent at
            # normal priority with the default sound, so a long report does not
            # become several high-priority alerts that bypass quiet hours.
            follow_up = (
                (0, None) if _alerts(self.priority) else (self.priority, self.sound)
            )
            parts = [(texts[0], self.priority, self.sound)] + [
                (text, *follow_up) for text in texts[1:]
            ]
            session = requests.Session()
            try:
                return send_parts("Pushover", parts, partial(self._post, session))
            finally:
                session.close()
        return False, "This event is not enabled for Pushover."

    def _post(self, session: requests.Session, part: tuple) -> None:
        text, priority, sound = part
        response = session.post(
            url="https://api.pushover.net/1/messages.json",
            data={
                "user": self.userkey,
                "token": self.token,
                "message": text,
                "title": "Twitch Channel Points Miner",
                "priority": priority,
                # requests leaves out None values, so Pushover uses the
                # default sound.
                "sound": sound,
            },
            timeout=(5, 15),
        )
        response.raise_for_status()


def _alerts(priority) -> bool:
    """Return True when a Pushover priority is above normal (0)."""
    try:
        return int(priority) > 0
    except (TypeError, ValueError):
        return False
