import logging
import sys
from textwrap import dedent

from TwitchChannelPointsMiner.classes.Settings import Events

logger = logging.getLogger(__name__)

# Apprise logs each request URL at DEBUG and some failures at WARNING. Those
# URLs hold webhook and bot tokens or private ntfy topics, so only errors are
# kept. Delivery failures still reach the user through send()'s reason.
logging.getLogger("apprise").setLevel(logging.ERROR)

# URLs from config.example.py. Notifications are skipped until real URLs replace them.
EXAMPLE_URLS = ("discord://WEBHOOK_ID/WEBHOOK_TOKEN", "ntfy://YOUR_NTFY_TOPIC")

# The Windows executable cannot install packages, so it gets its own advice.
MISSING_PACKAGE = (
    "The apprise package is not installed. "
    "Run: pip install -r requirements-optional.txt"
)
MISSING_FROM_EXECUTABLE = (
    "The Windows executable does not include Apprise. "
    "Use the Docker image or a source checkout to send Apprise notifications."
)


class Apprise(object):
    """Send events through Apprise (https://github.com/caronc/apprise).

    Apprise supports 100+ services, each configured with one URL. The
    package is optional and only imported when this notifier is created.
    The Docker image includes it. A source checkout installs it with
    `pip install -r requirements-optional.txt`. The Windows executable does
    not include it.

        Apprise(
            urls=["discord://WEBHOOK_ID/WEBHOOK_TOKEN", "tgram://BOT_TOKEN/CHAT_ID"],
            events=[Events.DROP_CLAIM, Events.DROP_STATUS],
        )

    URL formats: https://github.com/caronc/apprise/wiki

    `{event}` in `title` is replaced with the event name, such as DROP_CLAIM.
    """

    __slots__ = ["urls", "events", "title", "_client", "_reason"]

    def __init__(self, urls, events: list, title: str = "Twitch Channel Points Miner"):
        self.urls = [urls] if isinstance(urls, str) else [u for u in (urls or []) if u]
        # An example URL left beside a real one would fail on every send.
        self.urls = [url for url in self.urls if url not in EXAMPLE_URLS]
        self.events = [str(event) for event in events]
        self.title = title
        self._client = None
        self._reason = "No Apprise URL is configured."
        if not self.urls:
            return
        try:
            import apprise
        except ImportError:
            frozen = getattr(sys, "frozen", False)
            self._reason = MISSING_FROM_EXECUTABLE if frozen else MISSING_PACKAGE
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
            title = self.title.replace("{event}", str(event))
            delivered = self._client.notify(title=title, body=dedent(message))
        except Exception as error:
            # Only the type is kept: error text can quote the service URL.
            return False, f"Apprise raised {type(error).__name__}."
        if not delivered:
            return False, "Apprise could not deliver the notification to every service."
        return True, None
