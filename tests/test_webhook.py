from unittest.mock import patch

from TwitchChannelPointsMiner.classes.Settings import Events
from TwitchChannelPointsMiner.classes.Webhook import Webhook


def test_get_webhook_sends_encoded_query_parameters():
    webhook = Webhook(
        "https://example.com/webhook",
        "GET",
        [Events.CHAT_MENTION],
        timeout=5,
    )

    with patch("TwitchChannelPointsMiner.classes.Webhook.requests.get") as request:
        webhook.send("hello #channel", Events.CHAT_MENTION)

    request.assert_called_once_with(
        url="https://example.com/webhook",
        params={"event_name": "CHAT_MENTION", "message": "hello #channel"},
        timeout=5,
    )


def test_post_webhook_sends_form_body():
    webhook = Webhook(
        "https://example.com/webhook",
        "POST",
        [Events.CHAT_MENTION],
    )

    with patch("TwitchChannelPointsMiner.classes.Webhook.requests.post") as request:
        webhook.send("hello #channel", Events.CHAT_MENTION)

    request.assert_called_once_with(
        url="https://example.com/webhook",
        data={"event_name": "CHAT_MENTION", "message": "hello #channel"},
        timeout=10,
    )
