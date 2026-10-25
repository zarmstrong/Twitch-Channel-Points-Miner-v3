from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from TwitchChannelPointsMiner.classes.Settings import Events, Settings
from TwitchChannelPointsMiner.classes.Telegram import Telegram


@contextmanager
def patched_post(**kwargs):
    """Patch the session Telegram posts through and check it is closed."""
    with patch(
        "TwitchChannelPointsMiner.classes.Telegram.requests.Session"
    ) as session_class:
        post = session_class.return_value.post
        post.configure_mock(**kwargs)
        yield post
    if session_class.called:
        session_class.return_value.close.assert_called_once_with()


def test_telegram_sends_message_without_topic_by_default():
    telegram = Telegram(
        chat_id=123,
        token="token",
        events=[Events.DROP_CLAIM],
        disable_notification=True,
    )

    with patched_post() as post:
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

    with patched_post() as post:
        assert telegram.send("mentioned", Events.CHAT_MENTION) == (True, None)

    assert post.call_args.kwargs["data"]["message_thread_id"] == 987


def test_telegram_ignores_events_not_selected():
    telegram = Telegram(chat_id=123, token="token", events=[Events.DROP_CLAIM])

    with patched_post() as post:
        assert telegram.send("online", Events.STREAMER_ONLINE) == (
            False,
            "This event is not enabled for Telegram.",
        )

    post.assert_not_called()


def test_telegram_reports_sanitized_request_failure():
    telegram = Telegram(chat_id=123, token="token", events=[Events.DROP_CLAIM])
    error = requests.HTTPError(response=SimpleNamespace(status_code=401))

    with patched_post(side_effect=error):
        success, message = telegram.send("claimed", Events.DROP_CLAIM)

    assert success is False
    assert message == "Telegram rejected the configured credentials (HTTP 401)."


def test_telegram_splits_long_message_on_line_boundaries():
    telegram = Telegram(
        chat_id=-100123,
        token="token",
        events=[Events.DROP_STATUS],
        message_thread_id=987,
    )
    lines = [f"Campaign {index:03d}: progress {index % 100}%" for index in range(200)]
    message = "\n".join(lines)
    assert len(message) > 4096

    with patched_post() as post:
        assert telegram.send(message, Events.DROP_STATUS) == (True, None)

    assert post.call_count > 1
    texts = [call.kwargs["data"]["text"] for call in post.call_args_list]
    for text in texts:
        assert len(text) <= 4096
    assert [line for text in texts for line in text.split("\n")] == lines
    for call in post.call_args_list:
        assert call.kwargs["url"] == "https://api.telegram.org/bottoken/sendMessage"
        assert call.kwargs["data"]["chat_id"] == -100123
        assert call.kwargs["data"]["message_thread_id"] == 987


def test_telegram_wraps_single_line_longer_than_limit():
    telegram = Telegram(chat_id=123, token="token", events=[Events.DROP_STATUS])
    message = "x" * 5000

    with patched_post() as post:
        assert telegram.send(message, Events.DROP_STATUS) == (True, None)

    texts = [call.kwargs["data"]["text"] for call in post.call_args_list]
    assert [len(text) for text in texts] == [4096, 904]
    assert "".join(texts) == message


def test_telegram_reports_failure_when_a_later_chunk_fails():
    telegram = Telegram(chat_id=123, token="token", events=[Events.DROP_STATUS])
    message = "\n".join(f"Line {index:03d}" for index in range(600))
    failing = Mock()
    failing.raise_for_status.side_effect = requests.HTTPError(
        response=SimpleNamespace(status_code=400)
    )

    with patched_post(
        side_effect=[Mock(), failing],
    ) as post:
        result = telegram.send(message, Events.DROP_STATUS)

    assert post.call_count == 2
    assert result == (
        False,
        "Telegram rejected the notification (HTTP 400) (part 2 of 2).",
    )


def test_telegram_does_not_send_empty_trailing_part():
    telegram = Telegram(chat_id=123, token="token", events=[Events.DROP_STATUS])
    message = "x" * 4096 + "\n"

    with patched_post() as post:
        assert telegram.send(message, Events.DROP_STATUS) == (True, None)

    post.assert_called_once()
    assert post.call_args.kwargs["data"]["text"] == "x" * 4096


def test_telegram_counts_astral_emoji_as_two_characters():
    telegram = Telegram(chat_id=123, token="token", events=[Events.DROP_STATUS])
    message = "\U0001f3c6" * 3000
    assert len(message) < 4096

    with patched_post() as post:
        assert telegram.send(message, Events.DROP_STATUS) == (True, None)

    texts = [call.kwargs["data"]["text"] for call in post.call_args_list]
    assert [len(text.encode("utf-16-le")) // 2 for text in texts] == [4096, 1904]
    assert "".join(texts) == message


def test_telegram_drops_leading_blank_line_of_long_message():
    telegram = Telegram(chat_id=123, token="token", events=[Events.DROP_STATUS])
    message = "\n" + "x" * 5000

    with patched_post() as post:
        assert telegram.send(message, Events.DROP_STATUS) == (True, None)

    texts = [call.kwargs["data"]["text"] for call in post.call_args_list]
    assert [len(text) for text in texts] == [4096, 904]
    assert "".join(texts) == "x" * 5000


def test_telegram_sends_only_parts_with_visible_content():
    telegram = Telegram(chat_id=123, token="token", events=[Events.DROP_STATUS])
    lines = ["Report", "", "", "y" * 4095, "", "", "", "z" * 4000, "", "Done", ""]
    message = "\n".join(lines)

    with patched_post() as post:
        assert telegram.send(message, Events.DROP_STATUS) == (True, None)

    texts = [call.kwargs["data"]["text"] for call in post.call_args_list]
    assert len(texts) == 3
    for text in texts:
        assert text.strip()
        assert len(text) <= 4096
    visible = [line for text in texts for line in text.split("\n") if line]
    assert visible == [line for line in lines if line]


def test_telegram_repeats_username_prefix_on_later_parts():
    telegram = Telegram(chat_id=123, token="token", events=[Events.DROP_STATUS])
    prefix = "[miner] "
    message = prefix + "x" * 5000

    with patch.object(
        Settings, "logger", SimpleNamespace(username=prefix)
    ), patched_post() as post:
        assert telegram.send(message, Events.DROP_STATUS) == (True, None)

    texts = [call.kwargs["data"]["text"] for call in post.call_args_list]
    assert [len(text) for text in texts] == [4096, 920]
    assert all(text.startswith(prefix) for text in texts)
    assert "".join(text[len(prefix) :] for text in texts) == "x" * 5000


def test_telegram_retries_after_body_retry_after_hint():
    telegram = Telegram(chat_id=123, token="token", events=[Events.DROP_STATUS])
    limited_response = requests.Response()
    limited_response.status_code = 429
    limited_response._content = (
        b'{"ok": false, "error_code": 429, "parameters": {"retry_after": 3}}'
    )
    limited = Mock()
    limited.raise_for_status.side_effect = requests.HTTPError(response=limited_response)

    with patched_post(side_effect=[limited, Mock()]) as post, patch(
        "TwitchChannelPointsMiner.classes.NotificationError.time.sleep"
    ) as sleep:
        assert telegram.send("claimed", Events.DROP_STATUS) == (True, None)

    sleep.assert_called_once_with(3.0)
    assert post.call_count == 2
