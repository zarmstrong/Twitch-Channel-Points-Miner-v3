from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from TwitchChannelPointsMiner.classes.Discord import Discord
from TwitchChannelPointsMiner.classes.Settings import Events, Settings


@contextmanager
def patched_post(**kwargs):
    """Patch the session Discord posts through and check it is closed."""
    with patch(
        "TwitchChannelPointsMiner.classes.Discord.requests.Session"
    ) as session_class:
        post = session_class.return_value.post
        post.configure_mock(**kwargs)
        yield post
    if session_class.called:
        session_class.return_value.close.assert_called_once_with()


def test_discord_formats_single_line_as_inline_code():
    discord = Discord("https://example.com/discord", [Events.CHAT_MENTION])

    with patched_post() as request:
        discord.send("hello #channel", Events.CHAT_MENTION)

    request.assert_called_once_with(
        url="https://example.com/discord",
        params={"wait": "true"},
        data={
            "content": "`hello #channel`",
            "username": "Twitch Channel Points Miner",
            "avatar_url": "https://i.imgur.com/X9fEkhT.png",
        },
        timeout=(5, 15),
    )


def test_discord_formats_multiline_message_as_code_block():
    discord = Discord("https://example.com/discord", [Events.DROP_STATUS])

    with patched_post() as request:
        discord.send(
            """
            Campaign: Example
            Progress: 50%
            """,
            Events.DROP_STATUS,
        )

    request.assert_called_once_with(
        url="https://example.com/discord",
        params={"wait": "true"},
        data={
            "content": "```\nCampaign: Example\nProgress: 50%\n```",
            "username": "Twitch Channel Points Miner",
            "avatar_url": "https://i.imgur.com/X9fEkhT.png",
        },
        timeout=(5, 15),
    )


def test_discord_uses_longer_inline_fence_for_message_with_backticks():
    discord = Discord("https://example.com/discord", [Events.CHAT_MENTION])

    with patched_post() as request:
        discord.send("Use `code` @everyone", Events.CHAT_MENTION)

    request.assert_called_once()
    assert request.call_args.kwargs["data"]["content"] == "`` Use `code` @everyone ``"


def test_discord_uses_longer_code_fence_for_multiline_message_with_backticks():
    discord = Discord("https://example.com/discord", [Events.DROP_STATUS])

    with patched_post() as request:
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

    with patched_post(
        side_effect=requests.Timeout("secret-token"),
    ):
        result = discord.send("test", Events.CONFIGURATION)

    assert result == (False, "The connection to Discord timed out.")
    assert "secret-token" not in result[1]


def test_discord_sends_message_under_limit_as_single_post():
    discord = Discord("https://example.com/discord", [Events.DROP_STATUS])
    message = "\n".join(f"Line {index:03d}" for index in range(100))
    assert len(message) < 2000

    with patched_post() as request:
        assert discord.send(message, Events.DROP_STATUS) == (True, None)

    request.assert_called_once()
    assert request.call_args.kwargs["data"]["content"] == f"```\n{message}\n```"


def test_discord_splits_long_multiline_message_on_line_boundaries():
    discord = Discord("https://example.com/discord", [Events.DROP_STATUS])
    lines = [f"Campaign {index:03d}: progress {index % 100}%" for index in range(120)]
    message = "\n".join(lines)
    assert len(message) > 2000

    with patched_post() as request:
        assert discord.send(message, Events.DROP_STATUS) == (True, None)

    assert request.call_count > 1
    contents = [call.kwargs["data"]["content"] for call in request.call_args_list]
    chunks = []
    for content in contents:
        assert len(content) <= 2000
        assert content.startswith("```\n") and content.endswith("\n```")
        chunks.append(content[len("```\n") : -len("\n```")])
    assert [line for chunk in chunks for line in chunk.split("\n")] == lines
    for call in request.call_args_list:
        assert call.kwargs["url"] == "https://example.com/discord"
        assert call.kwargs["params"] == {"wait": "true"}
        assert call.kwargs["data"]["username"] == "Twitch Channel Points Miner"


def test_discord_splits_long_message_using_fence_from_whole_message():
    discord = Discord("https://example.com/discord", [Events.DROP_STATUS])
    lines = [f"Line {index:03d} with ```backticks```" for index in range(100)]
    message = "\n".join(lines)
    assert len(message) > 2000

    with patched_post() as request:
        assert discord.send(message, Events.DROP_STATUS) == (True, None)

    contents = [call.kwargs["data"]["content"] for call in request.call_args_list]
    assert len(contents) > 1
    for content in contents:
        assert len(content) <= 2000
        assert content.startswith("````\n") and content.endswith("\n````")


def test_discord_wraps_single_line_longer_than_limit():
    discord = Discord("https://example.com/discord", [Events.DROP_STATUS])
    message = "x" * 4500

    with patched_post() as request:
        assert discord.send(message, Events.DROP_STATUS) == (True, None)

    contents = [call.kwargs["data"]["content"] for call in request.call_args_list]
    assert len(contents) == 3
    for content in contents:
        assert len(content) <= 2000
        assert content.startswith("`") and content.endswith("`")
    assert "".join(content.strip("`") for content in contents) == message


def test_discord_reports_failure_when_a_later_chunk_fails():
    discord = Discord("https://example.com/discord", [Events.DROP_STATUS])
    message = "\n".join(f"Line {index:03d}" for index in range(600))
    failing = Mock()
    failing.raise_for_status.side_effect = requests.HTTPError(
        response=SimpleNamespace(status_code=400)
    )

    with patched_post(
        side_effect=[Mock(), failing],
    ) as request:
        result = discord.send(message, Events.DROP_STATUS)

    assert request.call_count == 2
    assert result == (
        False,
        "Discord rejected the notification (HTTP 400) (part 2 of 3).",
    )


def test_discord_counts_astral_emoji_as_two_characters():
    discord = Discord("https://example.com/discord", [Events.DROP_STATUS])
    lines = ["\U0001f3c6" * 100 for _ in range(15)]
    message = "\n".join(lines)
    assert len(message) < 2000
    assert len(message.encode("utf-16-le")) // 2 > 2000

    with patched_post() as request:
        assert discord.send(message, Events.DROP_STATUS) == (True, None)

    contents = [call.kwargs["data"]["content"] for call in request.call_args_list]
    assert len(contents) > 1
    for content in contents:
        assert len(content.encode("utf-16-le")) // 2 <= 2000


def test_discord_appends_wait_to_webhook_url_with_existing_query():
    discord = Discord(
        "https://example.com/discord?thread_id=42&wait=false", [Events.CHAT_MENTION]
    )

    with patched_post() as request:
        discord.send("hello", Events.CHAT_MENTION)

    url = request.call_args.kwargs["url"]
    params = request.call_args.kwargs["params"]
    assert url == "https://example.com/discord?thread_id=42"
    assert params == {"wait": "true"}
    prepared = requests.Request("POST", url, params=params).prepare()
    assert prepared.url == "https://example.com/discord?thread_id=42&wait=true"


def test_discord_adds_wait_to_webhook_url_without_query():
    discord = Discord("https://example.com/discord?wait=false", [Events.CHAT_MENTION])

    with patched_post() as request:
        discord.send("hello", Events.CHAT_MENTION)

    assert request.call_args.kwargs["url"] == "https://example.com/discord"
    assert request.call_args.kwargs["params"] == {"wait": "true"}


def test_discord_reuses_one_session_for_all_parts():
    discord = Discord("https://example.com/discord", [Events.DROP_STATUS])
    message = "\n".join(f"Line {index:03d}" for index in range(600))

    with patch(
        "TwitchChannelPointsMiner.classes.Discord.requests.Session"
    ) as session_class:
        assert discord.send(message, Events.DROP_STATUS) == (True, None)

    session_class.assert_called_once_with()
    assert session_class.return_value.post.call_count == 3
    session_class.return_value.close.assert_called_once_with()


def test_discord_repeats_username_prefix_on_later_parts():
    discord = Discord("https://example.com/discord", [Events.DROP_STATUS])
    prefix = "[miner] "
    lines = [f"Campaign {index:03d}: progress {index % 100}%" for index in range(120)]
    message = prefix + "\n".join(lines)

    with patch.object(
        Settings, "logger", SimpleNamespace(username=prefix)
    ), patched_post() as request:
        assert discord.send(message, Events.DROP_STATUS) == (True, None)

    contents = [call.kwargs["data"]["content"] for call in request.call_args_list]
    assert len(contents) > 1
    bodies = []
    for content in contents:
        assert len(content) <= 2000
        assert content.startswith("```\n" + prefix)
        bodies.append(content[len("```\n" + prefix) : -len("\n```")])
    assert [line for body in bodies for line in body.split("\n")] == lines


def test_discord_never_posts_an_empty_code_block():
    discord = Discord("https://example.com/discord", [Events.DROP_STATUS])
    message = "Header\n\n\n" + "x" * 2500 + "\n\n\nFooter"

    with patched_post() as request:
        assert discord.send(message, Events.DROP_STATUS) == (True, None)

    for call in request.call_args_list:
        assert call.kwargs["data"]["content"].strip("`\n ")


def test_discord_falls_back_before_the_fence_crowds_out_the_message():
    discord = Discord("https://example.com/discord", [Events.CHAT_MENTION])
    # A 901-backtick fence would leave under 200 characters per post and
    # spray the message over a dozen posts.
    message = "`" * 900 + "x" * 1000

    with patched_post() as request:
        assert discord.send(message, Events.CHAT_MENTION) == (True, None)

    request.assert_called_once()
    content = request.call_args.kwargs["data"]["content"]
    assert content == "`" + "\u02cb" * 900 + "x" * 1000 + "`"


def test_discord_handles_message_whose_fence_cannot_fit():
    discord = Discord("https://example.com/discord", [Events.CHAT_MENTION])
    message = "`" * 1000

    with patched_post() as request:
        assert discord.send(message, Events.CHAT_MENTION) == (True, None)

    request.assert_called_once()
    content = request.call_args.kwargs["data"]["content"]
    assert len(content) <= 2000
    assert content == "`" + "\u02cb" * 1000 + "`"


def test_discord_retries_rate_limited_part_after_retry_after():
    discord = Discord("https://example.com/discord", [Events.DROP_STATUS])
    message = "\n".join(f"Line {index:03d}" for index in range(400))
    limited = Mock()
    limited.raise_for_status.side_effect = requests.HTTPError(
        response=SimpleNamespace(status_code=429, headers={"Retry-After": "2"})
    )

    with patched_post(
        side_effect=[Mock(), limited, Mock()],
    ) as request, patch(
        "TwitchChannelPointsMiner.classes.NotificationError.time.sleep"
    ) as sleep:
        assert discord.send(message, Events.DROP_STATUS) == (True, None)

    assert request.call_count == 3
    sleep.assert_called_once_with(2.0)
    contents = [call.kwargs["data"]["content"] for call in request.call_args_list]
    assert contents[1] == contents[2]


def test_discord_caps_a_huge_notification_at_ten_posts():
    discord = Discord("https://example.com/discord", [Events.CHAT_MENTION])
    message = "\n".join(f"reward {index:05}: progress updated" for index in range(3000))

    with patched_post() as request:
        assert discord.send(message, Events.CHAT_MENTION) == (True, None)

    contents = [call.kwargs["data"]["content"] for call in request.call_args_list]
    assert len(contents) == 10
    assert all(len(content) <= 2000 for content in contents)
    assert contents[-1].endswith("more parts not sent]\n```")


def test_discord_capped_one_line_message_keeps_its_inline_code_on_one_line():
    discord = Discord("https://example.com/discord", [Events.CHAT_MENTION])

    with patched_post() as request:
        assert discord.send("z" * 25000, Events.CHAT_MENTION) == (True, None)

    contents = [call.kwargs["data"]["content"] for call in request.call_args_list]
    assert len(contents) == 10
    assert all(len(content) <= 2000 for content in contents)
    assert "\n" not in contents[-1]
    assert contents[-1].endswith("z [3 more parts not sent]`")


def test_discord_reports_a_missing_webhook_url_as_invalid():
    # requests rejects the URL before any connection is made.
    for webhook_api in (None, ""):
        discord = Discord(webhook_api, [Events.CHAT_MENTION])

        assert discord.send("hello", Events.CHAT_MENTION) == (
            False,
            "The configured Discord URL is invalid.",
        )
