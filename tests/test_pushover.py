from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import requests

from TwitchChannelPointsMiner.classes.Pushover import Pushover
from TwitchChannelPointsMiner.classes.Settings import Events, Settings


@contextmanager
def patched_post(**kwargs):
    """Patch the session Pushover posts through and check it is closed."""
    with patch(
        "TwitchChannelPointsMiner.classes.Pushover.requests.Session"
    ) as session_class:
        post = session_class.return_value.post
        post.configure_mock(**kwargs)
        yield post
    if session_class.called:
        session_class.return_value.close.assert_called_once_with()


def make_pushover(events, priority=0, sound="pushover"):
    return Pushover(
        userkey="user-key",
        token="app-token",
        priority=priority,
        sound=sound,
        events=events,
    )


def test_pushover_sends_short_message_as_single_request():
    pushover = make_pushover([Events.DROP_CLAIM])

    with patched_post() as post:
        assert pushover.send("  Drop claimed", Events.DROP_CLAIM) == (True, None)

    post.assert_called_once_with(
        url="https://api.pushover.net/1/messages.json",
        data={
            "user": "user-key",
            "token": "app-token",
            "message": "Drop claimed",
            "title": "Twitch Channel Points Miner",
            "priority": 0,
            "sound": "pushover",
        },
        timeout=(5, 15),
    )
    post.return_value.raise_for_status.assert_called_once_with()


def test_pushover_ignores_events_not_selected():
    pushover = make_pushover([Events.DROP_CLAIM])

    with patched_post() as post:
        assert pushover.send("online", Events.STREAMER_ONLINE) == (
            False,
            "This event is not enabled for Pushover.",
        )

    post.assert_not_called()


def test_pushover_splits_long_message_on_line_boundaries():
    pushover = make_pushover([Events.DROP_STATUS])
    lines = [f"Campaign {index:03d}: progress {index % 100}%" for index in range(60)]
    message = "\n".join(lines)
    assert len(message) > 1024

    with patched_post() as post:
        assert pushover.send(message, Events.DROP_STATUS) == (True, None)

    assert post.call_count > 1
    texts = [call.kwargs["data"]["message"] for call in post.call_args_list]
    for text in texts:
        assert len(text) <= 1024
    assert [line for text in texts for line in text.split("\n")] == lines
    for call in post.call_args_list:
        assert call.kwargs["data"]["title"] == "Twitch Channel Points Miner"
        assert call.kwargs["data"]["user"] == "user-key"


def test_pushover_wraps_single_line_longer_than_limit():
    pushover = make_pushover([Events.DROP_STATUS])
    message = "x" * 1500

    with patched_post() as post:
        assert pushover.send(message, Events.DROP_STATUS) == (True, None)

    texts = [call.kwargs["data"]["message"] for call in post.call_args_list]
    assert [len(text) for text in texts] == [1024, 476]
    assert "".join(texts) == message


@pytest.mark.parametrize("emoji_count", [100, 124])
def test_pushover_keeps_unicode_message_within_character_limit_whole(emoji_count):
    pushover = make_pushover([Events.DAILY_REPORT])
    message = "a" * 900 + "\U0001f3c6" * emoji_count

    with patched_post() as post:
        assert pushover.send(message, Events.DAILY_REPORT) == (True, None)

    post.assert_called_once()
    assert post.call_args.kwargs["data"]["message"] == message


def test_pushover_wraps_unicode_message_by_character_count():
    pushover = make_pushover([Events.DAILY_REPORT])
    prefix = "[\U0001f3c6] "
    body = "a" * 900 + "\U0001f3c6" * 600

    with (
        patch.object(Settings, "logger", SimpleNamespace(username=prefix)),
        patched_post() as post,
    ):
        assert pushover.send(prefix + body, Events.DAILY_REPORT) == (True, None)

    texts = [call.kwargs["data"]["message"] for call in post.call_args_list]
    assert [len(text) for text in texts] == [1024, 484]
    assert all(text.startswith(prefix) for text in texts)
    assert "".join(text[len(prefix) :] for text in texts) == body


def test_pushover_reports_failure_when_a_later_chunk_fails():
    pushover = make_pushover([Events.DROP_STATUS])
    message = "\n".join(f"Line {index:03d}" for index in range(300))
    failing = Mock()
    failing.raise_for_status.side_effect = requests.HTTPError(
        response=SimpleNamespace(status_code=400)
    )

    with patched_post(
        side_effect=[Mock(), failing],
    ) as post:
        result = pushover.send(message, Events.DROP_STATUS)

    assert post.call_count == 2
    assert result == (
        False,
        "Pushover rejected the notification (HTTP 400) (part 2 of 3).",
    )


def test_pushover_reports_single_part_failure_without_part_suffix():
    pushover = make_pushover([Events.DROP_CLAIM])

    with patched_post(
        side_effect=requests.Timeout(),
    ):
        result = pushover.send("claimed", Events.DROP_CLAIM)

    assert result == (False, "The connection to Pushover timed out.")


@pytest.mark.parametrize("priority", [0, 1, "1"])
@pytest.mark.parametrize("sound", ["siren", "none"])
def test_pushover_sends_later_parts_at_quiet_priority(priority, sound):
    # A long report alerts once: the first part at the configured priority
    # and sound, later parts quietly (no sound or vibration), as on Telegram.
    pushover = make_pushover([Events.DROP_STATUS], priority=priority, sound=sound)
    message = "x" * 3000

    with patched_post() as post:
        assert pushover.send(message, Events.DROP_STATUS) == (True, None)

    data = [call.kwargs["data"] for call in post.call_args_list]
    assert len(data) == 3
    assert (data[0]["priority"], data[0]["sound"]) == (priority, sound)
    for later in data[1:]:
        assert (later["priority"], later["sound"]) == (-1, sound)
    assert "".join(item["message"] for item in data) == message


@pytest.mark.parametrize(
    "priority, later_priority",
    [
        # Already quieter than quiet: no notification at all stays that way.
        (-2, -2),
        # Below Pushover's range, later parts still get a valid priority.
        (-3, -2),
        # An unreadable setting still keeps later parts quiet.
        ("loud", -1),
        (None, -1),
    ],
)
def test_pushover_later_part_priority_edge_cases(priority, later_priority):
    pushover = make_pushover([Events.DROP_STATUS], priority=priority)

    with patched_post() as post:
        assert pushover.send("x" * 3000, Events.DROP_STATUS) == (True, None)

    data = [call.kwargs["data"] for call in post.call_args_list]
    if isinstance(priority, int):
        # The first part keeps the configured priority.
        assert data[0]["priority"] == priority
    assert [item["priority"] for item in data[1:]] == [later_priority] * 2


def test_pushover_keeps_quiet_priority_and_sound_on_later_parts():
    pushover = make_pushover([Events.DROP_STATUS], priority=-1, sound="none")
    message = "x" * 3000

    with patched_post() as post:
        assert pushover.send(message, Events.DROP_STATUS) == (True, None)

    data = [call.kwargs["data"] for call in post.call_args_list]
    assert [(item["priority"], item["sound"]) for item in data] == [(-1, "none")] * 3


def test_pushover_repeats_username_prefix_on_later_parts():
    pushover = make_pushover([Events.DROP_STATUS])
    prefix = "[miner] "
    lines = [f"Campaign {index:03d}: progress {index % 100}%" for index in range(60)]
    message = prefix + "\n".join(lines)

    with (
        patch.object(Settings, "logger", SimpleNamespace(username=prefix)),
        patched_post() as post,
    ):
        assert pushover.send(message, Events.DROP_STATUS) == (True, None)

    texts = [call.kwargs["data"]["message"] for call in post.call_args_list]
    assert len(texts) > 1
    for text in texts:
        assert len(text) <= 1024
        assert text.startswith(prefix)
    bodies = [text[len(prefix) :] for text in texts]
    assert [line for body in bodies for line in body.split("\n")] == lines


def test_pushover_does_not_retry_quota_429_without_wait_hint():
    pushover = make_pushover([Events.DROP_STATUS])
    quota = requests.Response()
    quota.status_code = 429
    quota._content = b'{"status": 0, "errors": ["application is over quota"]}'
    failing = Mock()
    failing.raise_for_status.side_effect = requests.HTTPError(response=quota)

    with (
        patched_post(side_effect=[failing]) as post,
        patch("TwitchChannelPointsMiner.classes.NotificationError.time.sleep") as sleep,
    ):
        assert pushover.send("claimed", Events.DROP_STATUS) == (
            False,
            "Pushover rate limited the notification (HTTP 429).",
        )

    sleep.assert_not_called()
    post.assert_called_once()


def test_pushover_caps_a_huge_notification_at_ten_posts():
    pushover = make_pushover([Events.DROP_STATUS])
    message = "\n".join(f"reward {index:05}: progress updated" for index in range(3000))

    with patched_post() as post:
        assert pushover.send(message, Events.DROP_STATUS) == (True, None)

    texts = [call.kwargs["data"]["message"] for call in post.call_args_list]
    assert len(texts) == 10
    assert all(len(text) <= 1024 for text in texts)
    assert texts[-1].endswith("more parts not sent]")
