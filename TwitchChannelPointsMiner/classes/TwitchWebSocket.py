import json
import logging
import time
from threading import Lock

from websocket import (
    WebSocketApp,
    WebSocketConnectionClosedException,
    WebSocketTimeoutException,
)

from TwitchChannelPointsMiner.utils import create_nonce

logger = logging.getLogger(__name__)


class TwitchWebSocket(WebSocketApp):
    def __init__(self, index, parent_pool, *args, **kw):
        super().__init__(*args, **kw)
        self.index = index

        self.parent_pool = parent_pool
        self.is_closed = False
        self.is_opened = False

        self.is_reconnecting = False
        self.reconnect_lock = Lock()
        self.forced_close = False

        # Custom attribute
        self.topics = []
        self.pending_topics = []

        self.twitch = parent_pool.twitch
        self.streamers = parent_pool.streamers
        self.events_predictions = parent_pool.events_predictions

        self.last_message_timestamp = None
        self.last_message_type_channel = None

        self.last_pong = time.time()
        self.last_ping = time.time()

    # def close(self):
    #     self.forced_close = True
    #     super().close()

    def listen(self, topic, auth_token=None):
        data = {"topics": [str(topic)]}
        if topic.is_user_topic() and auth_token is not None:
            data["auth_token"] = auth_token
        nonce = create_nonce()
        self.send({"type": "LISTEN", "nonce": nonce, "data": data})

    def unlisten(self, topic, auth_token=None):
        data = {"topics": [str(topic)]}
        if topic.is_user_topic() and auth_token is not None:
            data["auth_token"] = auth_token
        self.send(
            {
                "type": "UNLISTEN",
                "nonce": create_nonce(),
                "data": data,
            }
        )

    def ping(self):
        # Only a PING that went out counts, so the main loop's "no PING for
        # 10 minutes" check reconnects a socket whose sends keep failing.
        if self.send({"type": "PING"}):
            self.last_ping = time.time()

    def send(self, request):
        """Send a request; return False when the socket can't take it."""
        try:
            request_str = json.dumps(request, separators=(",", ":"))
            logger.debug(f"#{self.index} - Send: {request_str}")
            super().send(request_str)
            return True
        except WebSocketConnectionClosedException:
            self.is_closed = True
        except (WebSocketTimeoutException, OSError) as error:
            # A timeout or socket error used to escape and silently end the
            # ping thread. Treat the socket as closed; the reconnect checks
            # take it from here.
            logger.warning(f"#{self.index} - WebSocket send failed: {error}")
            self.is_closed = True
        return False

    def elapsed_last_pong(self):
        return (time.time() - self.last_pong) // 60

    def elapsed_last_ping(self):
        return (time.time() - self.last_ping) // 60
