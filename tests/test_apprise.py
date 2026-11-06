import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from TwitchChannelPointsMiner.classes.Apprise import EXAMPLE_URLS, Apprise
from TwitchChannelPointsMiner.classes.Settings import Events
from TwitchChannelPointsMiner.logger import NotificationHandler


def fake_apprise_module(delivered=True, accepts=True):
    """Stand in for the optional apprise package so no network is used."""
    client = MagicMock()
    client.add.return_value = accepts
    client.__len__.return_value = 1 if accepts else 0
    client.notify.return_value = delivered
    module = MagicMock()
    module.Apprise.return_value = client
    return module, client


def make_apprise(events=(Events.DROP_CLAIM,), urls=("discord://123/abc",)):
    module, client = fake_apprise_module()
    with patch.dict(sys.modules, {"apprise": module}):
        notifier = Apprise(list(urls), list(events))
    return notifier, client


def test_apprise_notifies_each_url_and_returns_success():
    module, client = fake_apprise_module()
    with patch.dict(sys.modules, {"apprise": module}):
        notifier = Apprise(["discord://123/abc", "ntfy://topic"], [Events.DROP_CLAIM])

    assert client.add.call_args_list[0].args == ("discord://123/abc",)
    assert client.add.call_args_list[1].args == ("ntfy://topic",)
    assert notifier.send("  Drop claimed", Events.DROP_CLAIM) == (True, None)
    client.notify.assert_called_once_with(
        title="Twitch Channel Points Miner", body="Drop claimed"
    )


def test_apprise_accepts_a_single_url_string():
    module, client = fake_apprise_module()
    with patch.dict(sys.modules, {"apprise": module}):
        notifier = Apprise("discord://123/abc", [Events.DROP_CLAIM])

    assert notifier.urls == ["discord://123/abc"]
    assert client.add.call_count == 1


def test_apprise_ignores_events_not_selected():
    notifier, client = make_apprise(events=[Events.DROP_CLAIM])

    assert notifier.send("online", Events.STREAMER_ONLINE) == (
        False,
        "This event is not enabled for Apprise.",
    )
    client.notify.assert_not_called()


def test_apprise_reports_undelivered_notification():
    module, client = fake_apprise_module(delivered=False)
    with patch.dict(sys.modules, {"apprise": module}):
        notifier = Apprise(["discord://123/abc"], [Events.DROP_CLAIM])

    assert notifier.send("claimed", Events.DROP_CLAIM) == (
        False,
        "Apprise could not deliver the notification to any service.",
    )


def test_apprise_reports_exception_type_without_url():
    module, client = fake_apprise_module()
    client.notify.side_effect = RuntimeError("failed for discord://123/abc")
    with patch.dict(sys.modules, {"apprise": module}):
        notifier = Apprise(["discord://123/abc"], [Events.DROP_CLAIM])

    success, reason = notifier.send("claimed", Events.DROP_CLAIM)
    assert (success, reason) == (False, "Apprise raised RuntimeError.")
    assert "123/abc" not in reason


def test_apprise_reports_missing_package():
    with patch.dict(sys.modules, {"apprise": None}):
        notifier = Apprise(["discord://123/abc"], [Events.DROP_CLAIM])

    assert notifier.send("claimed", Events.DROP_CLAIM) == (
        False,
        "The apprise package is not installed. Run: pip install apprise",
    )


def test_apprise_without_urls_does_not_import_the_package():
    with patch.dict(sys.modules, {"apprise": None}):
        notifier = Apprise([], [Events.DROP_CLAIM])

    assert notifier.send("claimed", Events.DROP_CLAIM) == (
        False,
        "No Apprise URL is configured.",
    )


def test_apprise_reports_when_every_url_is_invalid():
    module, _ = fake_apprise_module(accepts=False)
    with patch.dict(sys.modules, {"apprise": module}):
        notifier = Apprise(["not-a-url"], [Events.DROP_CLAIM])

    assert notifier.send("claimed", Events.DROP_CLAIM) == (
        False,
        "No valid Apprise URL is configured.",
    )


def test_logger_skips_example_apprise_urls():
    module, client = fake_apprise_module()
    with patch.dict(sys.modules, {"apprise": module}):
        notifier = Apprise(list(EXAMPLE_URLS), [Events.DROP_CLAIM])
    handler = NotificationHandler.__new__(NotificationHandler)
    handler.settings = SimpleNamespace(apprise=notifier)

    with patch.object(handler, "_send") as send:
        handler._dispatch("apprise", SimpleNamespace(), "msg")

    send.assert_not_called()


def test_logger_apprise_skip_flag_must_be_truthy():
    notifier, _ = make_apprise()
    handler = NotificationHandler.__new__(NotificationHandler)
    handler.settings = SimpleNamespace(apprise=notifier)
    included = SimpleNamespace(skip_apprise=False)
    skipped = SimpleNamespace(skip_apprise=True)

    with patch.object(handler, "_send") as send:
        handler._dispatch("apprise", included, "msg")
        handler._dispatch("apprise", skipped, "msg")

    send.assert_called_once_with(notifier, included, "msg")
