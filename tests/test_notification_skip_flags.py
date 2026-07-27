from types import SimpleNamespace
from unittest.mock import patch

import pytest

from TwitchChannelPointsMiner.logger import GlobalFormatter


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
    formatter = GlobalFormatter.__new__(GlobalFormatter)
    formatter.settings = SimpleNamespace(**{provider: notifier})
    included = SimpleNamespace(**{f"skip_{provider}": False})
    skipped = SimpleNamespace(**{f"skip_{provider}": True})

    with patch.object(formatter, "_send") as send:
        getattr(formatter, provider)(included)
        getattr(formatter, provider)(skipped)

    send.assert_called_once_with(notifier, included)
