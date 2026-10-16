from types import SimpleNamespace
from unittest.mock import patch

import pytest

from TwitchChannelPointsMiner.logger import NotificationHandler


@pytest.mark.parametrize(
    ("provider", "notifier"),
    (
        ("telegram", SimpleNamespace(chat_id=1)),
        ("discord", SimpleNamespace(webhook_api="https://discord.example/hook")),
        ("webhook", SimpleNamespace(endpoint="https://webhook.example/hook")),
        ("matrix", SimpleNamespace(room_id="room", access_token="token")),
        ("pushover", SimpleNamespace(userkey="user", token="token")),
        ("gotify", SimpleNamespace(endpoint="https://gotify.example/message")),
    ),
)
def test_logger_notification_skip_flags_must_be_truthy(provider, notifier):
    handler = NotificationHandler.__new__(NotificationHandler)
    handler.settings = SimpleNamespace(**{provider: notifier})
    included = SimpleNamespace(**{f"skip_{provider}": False})
    skipped = SimpleNamespace(**{f"skip_{provider}": True})

    with patch.object(handler, "_send") as send:
        getattr(handler, provider)(included, "msg")
        getattr(handler, provider)(skipped, "msg")

    send.assert_called_once_with(notifier, included, "msg")
