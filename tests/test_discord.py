from unittest.mock import patch

import requests

from TwitchChannelPointsMiner.classes.Discord import Discord
from TwitchChannelPointsMiner.classes.Settings import Events


def test_discord_formats_single_line_as_inline_code():
    discord = Discord("https://example.com/discord", [Events.CHAT_MENTION])

    with patch("TwitchChannelPointsMiner.classes.Discord.requests.post") as request:
        discord.send("hello #channel", Events.CHAT_MENTION)

    request.assert_called_once_with(
        url="https://example.com/discord",
        data={
            "content": "`hello #channel`",
            "username": "Twitch Channel Points Miner",
            "avatar_url": "https://i.imgur.com/X9fEkhT.png",
        },
        timeout=(5, 15),
    )


def test_discord_formats_multiline_message_as_code_block():
    discord = Discord("https://example.com/discord", [Events.DROP_STATUS])

    with patch("TwitchChannelPointsMiner.classes.Discord.requests.post") as request:
        discord.send(
            """
            Campaign: Example
            Progress: 50%
            """,
            Events.DROP_STATUS,
        )

    request.assert_called_once_with(
        url="https://example.com/discord",
        data={
            "content": "```\nCampaign: Example\nProgress: 50%\n```",
            "username": "Twitch Channel Points Miner",
            "avatar_url": "https://i.imgur.com/X9fEkhT.png",
        },
        timeout=(5, 15),
    )


def test_discord_uses_longer_inline_fence_for_message_with_backticks():
    discord = Discord("https://example.com/discord", [Events.CHAT_MENTION])

    with patch("TwitchChannelPointsMiner.classes.Discord.requests.post") as request:
        discord.send("Use `code` @everyone", Events.CHAT_MENTION)

    request.assert_called_once()
    assert request.call_args.kwargs["data"]["content"] == "`` Use `code` @everyone ``"


def test_discord_uses_longer_code_fence_for_multiline_message_with_backticks():
    discord = Discord("https://example.com/discord", [Events.DROP_STATUS])

    with patch("TwitchChannelPointsMiner.classes.Discord.requests.post") as request:
        discord.send("First line\n```dangerous\n@everyone", Events.DROP_STATUS)

    request.assert_called_once()
    assert (
        request.call_args.kwargs["data"]["content"]
        == "````\nFirst line\n```dangerous\n@everyone\n````"
    )


def test_discord_reports_timeout_without_exposing_webhook():
    discord = Discord(
        "https://discord.com/api/webhooks/secret-token", [Events.CONFIGURATION]
    )

    with patch(
        "TwitchChannelPointsMiner.classes.Discord.requests.post",
        side_effect=requests.Timeout("secret-token"),
    ):
        result = discord.send("test", Events.CONFIGURATION)

    assert result == (False, "The connection to Discord timed out.")
    assert "secret-token" not in result[1]
