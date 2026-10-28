import re
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

# Discord rejects webhook posts whose ``content`` exceeds this many characters
# (counted in UTF-16 code units).
DISCORD_CONTENT_LIMIT = 2000

# Stands in for backticks when escaping them would need a fence that crowds the
# message out of each post.
BACKTICK_LOOKALIKE = "\u02cb"


class Discord(object):
    __slots__ = ["webhook_api", "events"]

    def __init__(self, webhook_api: str, events: list):
        self.webhook_api = webhook_api
        self.events = [str(e) for e in events]

    def send(self, message: str, event: Events) -> tuple[bool, str | None]:
        if str(event) in self.events:
            contents = self._format_contents(dedent(message).strip())
            session = requests.Session()
            try:
                return send_parts("Discord", contents, partial(self._post, session))
            finally:
                session.close()
        return False, "This event is not enabled for Discord."

    def _post(self, session: requests.Session, content: str) -> None:
        # ``wait=true`` makes Discord confirm each part before the next is sent,
        # so split notifications are neither dropped nor reordered.
        response = session.post(
            url=_without_wait(self.webhook_api),
            params={"wait": "true"},
            data={
                "content": content,
                "username": "Twitch Channel Points Miner",
                "avatar_url": "https://i.imgur.com/X9fEkhT.png",
            },
            timeout=(5, 15),
        )
        response.raise_for_status()

    @staticmethod
    def _format_contents(message: str) -> list[str]:
        """Wrap the message in a code fence, split so each post fits the limit.

        The fence is computed once from the whole message so every chunk uses
        the same fence and no chunk can be broken by backticks in the content.
        """
        longest_run = max(map(len, re.findall("`+", message)), default=0)
        multiline = "\n" in message
        fence_length = max(3 if multiline else 1, longest_run + 1)
        if 2 * fence_length + 2 > DISCORD_CONTENT_LIMIT // 2:
            # A fence long enough to escape this message would leave little or
            # no room for the message in each post, spraying it over many
            # near-empty posts: replace the backticks with a look-alike instead.
            message = message.replace("`", BACKTICK_LOOKALIKE)
            longest_run = 0
            fence_length = 3 if multiline else 1

        fence = "`" * fence_length
        if multiline:
            opening, closing = f"{fence}\n", f"\n{fence}"
        elif longest_run:
            opening, closing = f"{fence} ", f" {fence}"
        else:
            opening, closing = "`", "`"
        limit = DISCORD_CONTENT_LIMIT - len(opening) - len(closing)
        chunks = split_message_lines(
            message,
            limit,
            notification_prefix(),
            max_pieces=MAX_NOTIFICATION_PARTS,
            # Inline code must stay on one line; a code block takes a new one.
            notice_separator="\n" if multiline else " ",
        )
        return [f"{opening}{chunk}{closing}" for chunk in chunks]


def _without_wait(webhook_url: str) -> str:
    """Drop any ``wait`` query parameter so ``params`` can set it.

    requests appends ``params`` to an existing query string instead of
    replacing keys, which would leave a configured ``wait=false`` in place.
    A value that is not a string is returned unchanged, so requests reports
    it as an invalid URL.
    """
    if not isinstance(webhook_url, str):
        return webhook_url
    base, _, query = webhook_url.partition("?")
    kept = [item for item in query.split("&") if item and item.split("=")[0] != "wait"]
    return f"{base}?{'&'.join(kept)}" if kept else base
