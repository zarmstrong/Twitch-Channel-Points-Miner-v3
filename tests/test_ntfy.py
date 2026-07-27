from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import requests

from TwitchChannelPointsMiner.classes.Ntfy import Ntfy
from TwitchChannelPointsMiner.classes.Settings import Events
from TwitchChannelPointsMiner.logger import GlobalFormatter


def test_ntfy_publishes_message_with_optional_headers():
    ntfy = Ntfy(
        topic="private topic",
        events=[Events.DROP_CLAIM],
        token="secret-token",
        priority=4,
        tags=["twitch", "gift"],
    )

    with patch("TwitchChannelPointsMiner.classes.Ntfy.requests.post") as post:
        assert ntfy.send("  Drop claimed", Events.DROP_CLAIM) == (True, None)

    post.assert_called_once_with(
        url="https://ntfy.sh/private%20topic",
        data=b"Drop claimed",
        headers={
            "Title": "Twitch Channel Points Miner: Drop Claim",
            "Authorization": "Bearer secret-token",
            "Priority": "4",
            "Tags": "twitch,gift",
        },
        timeout=10,
    )
    post.return_value.raise_for_status.assert_called_once_with()


def test_ntfy_ignores_events_not_selected():
    ntfy = Ntfy("topic", [Events.DROP_CLAIM])

    with patch("TwitchChannelPointsMiner.classes.Ntfy.requests.post") as post:
        assert ntfy.send("online", Events.STREAMER_ONLINE) == (
            False,
            "This event is not enabled for ntfy.",
        )

    post.assert_not_called()


def test_ntfy_normalizes_server_url_after_dashboard_update():
    ntfy = Ntfy("topic", [Events.DROP_CLAIM])
    ntfy.server_url = "https://ntfy.example///"

    with patch("TwitchChannelPointsMiner.classes.Ntfy.requests.post") as post:
        ntfy.send("claimed", Events.DROP_CLAIM)

    assert post.call_args.kwargs["url"] == "https://ntfy.example/topic"


def test_ntfy_reports_sanitized_request_failure():
    ntfy = Ntfy("topic", [Events.DROP_CLAIM])
    error = requests.HTTPError(response=SimpleNamespace(status_code=401))

    with patch(
        "TwitchChannelPointsMiner.classes.Ntfy.requests.post", side_effect=error
    ):
        success, message = ntfy.send("claimed", Events.DROP_CLAIM)

    assert success is False
    assert message == "ntfy rejected the configured credentials (HTTP 401)."


def test_logger_skips_placeholder_ntfy_topic():
    formatter = GlobalFormatter.__new__(GlobalFormatter)
    formatter.settings = MagicMock(
        ntfy=Ntfy("YOUR_NTFY_TOPIC", [Events.DROP_CLAIM])
    )

    with patch.object(formatter, "_send") as send:
        formatter.ntfy(SimpleNamespace())

    send.assert_not_called()


def test_logger_ntfy_skip_flag_must_be_truthy():
    notifier = Ntfy("private-topic", [Events.DROP_CLAIM])
    formatter = GlobalFormatter.__new__(GlobalFormatter)
    formatter.settings = MagicMock(ntfy=notifier)
    included = SimpleNamespace(skip_ntfy=False)
    skipped = SimpleNamespace(skip_ntfy=True)

    with patch.object(formatter, "_send") as send:
        formatter.ntfy(included)
        formatter.ntfy(skipped)

    send.assert_called_once_with(notifier, included)
