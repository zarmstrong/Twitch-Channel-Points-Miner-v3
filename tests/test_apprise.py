import inspect
import logging
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from TwitchChannelPointsMiner.classes.Apprise import (
    EXAMPLE_URLS,
    MISSING_FROM_EXECUTABLE,
    MISSING_PACKAGE,
    Apprise,
)
from TwitchChannelPointsMiner.classes.Settings import Events
from TwitchChannelPointsMiner.logger import LoggerSettings, NotificationHandler


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


def test_apprise_title_can_name_the_event():
    module, client = fake_apprise_module()
    with patch.dict(sys.modules, {"apprise": module}):
        notifier = Apprise(
            "discord://123/abc", [Events.DROP_CLAIM], title="Miner: {event}"
        )

    assert notifier.send("Drop claimed", Events.DROP_CLAIM) == (True, None)
    client.notify.assert_called_once_with(
        title="Miner: DROP_CLAIM", body="Drop claimed"
    )


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
        "Apprise could not deliver the notification to every service.",
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

    assert notifier.send("claimed", Events.DROP_CLAIM) == (False, MISSING_PACKAGE)
    assert "requirements-optional.txt" in MISSING_PACKAGE


def test_apprise_missing_from_windows_executable_names_alternatives():
    # pip cannot add packages to the frozen executable, so it must not be advised.
    with (
        patch.dict(sys.modules, {"apprise": None}),
        patch.object(sys, "frozen", True, create=True),
    ):
        notifier = Apprise(["discord://123/abc"], [Events.DROP_CLAIM])

    assert notifier.send("claimed", Events.DROP_CLAIM) == (
        False,
        MISSING_FROM_EXECUTABLE,
    )
    assert "pip" not in MISSING_FROM_EXECUTABLE


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


def test_apprise_drops_example_urls_kept_beside_real_ones():
    module, client = fake_apprise_module()
    with patch.dict(sys.modules, {"apprise": module}):
        notifier = Apprise([*EXAMPLE_URLS, "discord://123/abc"], [Events.DROP_CLAIM])

    assert notifier.urls == ["discord://123/abc"]
    assert [call.args for call in client.add.call_args_list] == [("discord://123/abc",)]


def test_apprise_with_only_example_urls_does_not_import_the_package():
    with patch.dict(sys.modules, {"apprise": None}):
        notifier = Apprise(list(EXAMPLE_URLS), [Events.DROP_CLAIM])

    assert notifier.urls == []
    assert notifier.send("claimed", Events.DROP_CLAIM) == (
        False,
        "No Apprise URL is configured.",
    )


def test_apprise_library_records_never_reach_the_logs(caplog):
    # Apprise quotes service URLs, which hold credentials, even at ERROR.
    secret = "FAKE_SECRET_TOKEN"
    with caplog.at_level(logging.DEBUG):
        for level in (logging.DEBUG, logging.WARNING, logging.ERROR, logging.CRITICAL):
            logging.getLogger("apprise").log(level, f"Unsupported URL: {secret}")
            logging.getLogger("apprise.plugins").log(level, f"URL: {secret}")

    assert secret not in caplog.text


def test_apprise_invalid_url_log_keeps_only_a_valid_scheme(caplog):
    secret = "FAKE_SECRET_TOKEN"
    urls = [
        f"discord://123/{secret}",
        f"discord:/123/{secret}",
        secret,
        f"{secret} ://x",
    ]
    module, _ = fake_apprise_module(accepts=False)
    with caplog.at_level(logging.ERROR), patch.dict(sys.modules, {"apprise": module}):
        Apprise(urls, [Events.DROP_CLAIM])

    assert secret not in caplog.text
    messages = [record.getMessage() for record in caplog.records]
    assert messages == [
        "Apprise: invalid notification URL ignored: discord://...",
        "Apprise: invalid notification URL ignored: a URL",
        "Apprise: invalid notification URL ignored: a URL",
        "Apprise: invalid notification URL ignored: a URL",
    ]


def test_logger_settings_keeps_positional_arguments_before_apprise():
    # apprise was added last, so calls written for the earlier signature
    # still bind email and the settings after it to the same parameters.
    params = list(inspect.signature(LoggerSettings.__init__).parameters.values())[1:]
    names = [param.name for param in params]
    assert names[-1] == "apprise"
    assert names[names.index("ntfy") + 1] == "email"

    email = object()
    args = [param.default for param in params[: names.index("email") + 1]]
    args[-1] = email
    args += [True, "08:30", "someone"]  # daily_report, daily_report_time, username
    settings = LoggerSettings(*args)

    assert settings.email is email
    assert settings.apprise is None
    assert (settings.daily_report, settings.daily_report_time) == (True, "08:30")
    assert settings.username == "someone"


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
