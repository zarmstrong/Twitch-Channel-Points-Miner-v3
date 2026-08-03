from types import SimpleNamespace
from unittest.mock import patch

import requests

from TwitchChannelPointsMiner.classes.Settings import Events
from TwitchChannelPointsMiner.classes.Telegram import Telegram


def test_telegram_sends_message_without_topic_by_default():
    telegram = Telegram(
        chat_id=123,
        token="token",
        events=[Events.DROP_CLAIM],
        disable_notification=True,
    )

    with patch("TwitchChannelPointsMiner.classes.Telegram.requests.post") as post:
        assert telegram.send("  Drop claimed", Events.DROP_CLAIM) == (True, None)

    post.assert_called_once_with(
        url="https://api.telegram.org/bottoken/sendMessage",
        data={
            "chat_id": 123,
            "text": "Drop claimed",
            "disable_web_page_preview": True,
            "disable_notification": True,
        },
        timeout=(5, 15),
    )
    post.return_value.raise_for_status.assert_called_once_with()


def test_telegram_sends_message_to_configured_topic():
    telegram = Telegram(
        chat_id=-100123,
        token="token",
        events=[Events.CHAT_MENTION],
        message_thread_id=987,
    )

    with patch("TwitchChannelPointsMiner.classes.Telegram.requests.post") as post:
        assert telegram.send("mentioned", Events.CHAT_MENTION) == (True, None)

    assert post.call_args.kwargs["data"]["message_thread_id"] == 987


def test_telegram_ignores_events_not_selected():
    telegram = Telegram(chat_id=123, token="token", events=[Events.DROP_CLAIM])

    with patch("TwitchChannelPointsMiner.classes.Telegram.requests.post") as post:
        assert telegram.send("online", Events.STREAMER_ONLINE) == (
            False,
            "This event is not enabled for Telegram.",
        )

    post.assert_not_called()


def test_telegram_reports_sanitized_request_failure():
    telegram = Telegram(chat_id=123, token="token", events=[Events.DROP_CLAIM])
    error = requests.HTTPError(response=SimpleNamespace(status_code=401))

    with patch(
        "TwitchChannelPointsMiner.classes.Telegram.requests.post", side_effect=error
    ):
        success, message = telegram.send("claimed", Events.DROP_CLAIM)

    assert success is False
    assert message == "Telegram rejected the configured credentials (HTTP 401)."
