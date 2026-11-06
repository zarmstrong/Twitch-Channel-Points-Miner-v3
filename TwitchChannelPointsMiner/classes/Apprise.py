import logging
from textwrap import dedent

from TwitchChannelPointsMiner.classes.Settings import Events

logger = logging.getLogger(__name__)

# URLs from config.example.py. Notifications are skipped until real URLs replace them.
EXAMPLE_URLS = ("discord://WEBHOOK_ID/WEBHOOK_TOKEN", "ntfy://YOUR_NTFY_TOPIC")


class Apprise(object):
    """Send events through Apprise (https://github.com/caronc/apprise).

    Apprise supports 100+ services, each configured with one URL. The
    package is optional and only imported when this notifier is created.
    Install it with `pip install apprise`.

        Apprise(
            urls=["discord://WEBHOOK_ID/WEBHOOK_TOKEN", "tgram://BOT_TOKEN/CHAT_ID"],
            events=[Events.DROP_CLAIM, Events.DROP_STATUS],
        )

    URL formats: https://github.com/caronc/apprise/wiki
    """

    __slots__ = ["urls", "events", "title", "_client", "_reason"]

    def __init__(self, urls, events: list, title: str = "Twitch Channel Points Miner"):
        self.urls = [urls] if isinstance(urls, str) else [u for u in (urls or []) if u]
        self.events = [str(event) for event in events]
        self.title = title
        self._client = None
        self._reason = "No Apprise URL is configured."
        if not self.urls:
            return
        try:
            import apprise
        except ImportError:
            self._reason = (
                "The apprise package is not installed. Run: pip install apprise"
            )
            return
        client = apprise.Apprise()
        for url in self.urls:
            if not client.add(url):
                # Log only the scheme: the rest of the URL holds credentials.
                logger.error(
                    f"Apprise: invalid notification URL ignored: {url.split('://')[0]}://..."
                )
        if len(client) == 0:
            self._reason = "No valid Apprise URL is configured."
            return
        self._client = client
        self._reason = None

    def send(self, message: str, event: Events) -> tuple[bool, str | None]:
        if str(event) not in self.events:
            return False, "This event is not enabled for Apprise."
        if self._client is None:
            return False, self._reason
        try:
            delivered = self._client.notify(title=self.title, body=dedent(message))
        except Exception as error:
            # Only the type is kept: error text can quote the service URL.
            return False, f"Apprise raised {type(error).__name__}."
        if not delivered:
            return False, "Apprise could not deliver the notification to any service."
        return True, None
