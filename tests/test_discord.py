from unittest.mock import patch

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
