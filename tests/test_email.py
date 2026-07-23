import smtplib
import socket
import ssl
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from TwitchChannelPointsMiner.classes.Email import Email
from TwitchChannelPointsMiner.classes.Settings import Events
from TwitchChannelPointsMiner.logger import GlobalFormatter


def test_email_sends_with_starttls_and_authentication():
    notifier = Email(
        host="smtp.example.com",
        port=587,
        username="user",
        password="secret",
        sender="miner@example.com",
        recipients=["one@example.com", "two@example.com"],
        events=[Events.DAILY_REPORT],
    )
    smtp = MagicMock()
    smtp.__enter__.return_value = smtp

    with patch(
        "TwitchChannelPointsMiner.classes.Email.smtplib.SMTP", return_value=smtp
    ):
        notifier.send("Today's activity", Events.DAILY_REPORT)

    smtp.starttls.assert_called_once()
    smtp.login.assert_called_once_with("user", "secret")
    message = smtp.send_message.call_args.args[0]
    assert message["To"] == "one@example.com, two@example.com"
    assert message["Subject"] == "Twitch Channel Points Miner: Daily Report"


def test_email_ignores_events_not_selected():
    notifier = Email(
        "smtp.example.com",
        25,
        "miner@example.com",
        "you@example.com",
        [Events.DAILY_REPORT],
        starttls=False,
    )
    with patch("TwitchChannelPointsMiner.classes.Email.smtplib.SMTP") as smtp:
        notifier.send("online", Events.STREAMER_ONLINE)
    smtp.assert_not_called()


def test_email_reports_authentication_failure():
    notifier = Email(
        host="smtp.example.com",
        port=587,
        username="user",
        password="wrong-password",
        sender="miner@example.com",
        recipients=["you@example.com"],
        events=[Events.CONFIGURATION],
    )
    smtp = MagicMock()
    smtp.__enter__.return_value = smtp
    smtp.login.side_effect = smtplib.SMTPAuthenticationError(
        535, b"Authentication failed"
    )

    with patch(
        "TwitchChannelPointsMiner.classes.Email.smtplib.SMTP", return_value=smtp
    ):
        result = notifier.send("test", Events.CONFIGURATION)

    assert result == (
        False,
        "SMTP authentication failed. Check the username and password.",
    )


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            socket.gaierror(),
            "Could not resolve SMTP host 'mail.example.com'.",
        ),
        (
            TimeoutError(),
            "SMTP connection to mail.example.com:587 timed out.",
        ),
        (
            ConnectionRefusedError(),
            "SMTP server mail.example.com:587 refused the connection.",
        ),
    ],
)
def test_email_reports_specific_connection_failures(error, expected):
    notifier = Email(
        "mail.example.com",
        587,
        "miner@example.com",
        "you@example.com",
        [Events.CONFIGURATION],
    )

    with patch(
        "TwitchChannelPointsMiner.classes.Email.smtplib.SMTP", side_effect=error
    ):
        result = notifier.send("test", Events.CONFIGURATION)

    assert result == (False, expected)


def test_email_reports_tls_negotiation_failure():
    notifier = Email(
        "mail.example.com",
        587,
        "miner@example.com",
        "you@example.com",
        [Events.CONFIGURATION],
    )
    smtp = MagicMock()
    smtp.__enter__.return_value = smtp
    smtp.starttls.side_effect = ssl.SSLError("certificate verify failed")

    with patch(
        "TwitchChannelPointsMiner.classes.Email.smtplib.SMTP", return_value=smtp
    ):
        result = notifier.send("test", Events.CONFIGURATION)

    assert result == (
        False,
        "SMTP TLS negotiation failed. Check the SSL and STARTTLS settings.",
    )


def test_email_reports_sanitized_smtp_status_response():
    notifier = Email(
        "mail.example.com",
        587,
        "miner@example.com",
        "you@example.com",
        [Events.CONFIGURATION],
        starttls=False,
    )
    smtp = MagicMock()
    smtp.__enter__.return_value = smtp
    smtp.send_message.side_effect = smtplib.SMTPDataError(
        554, b"Transaction failed"
    )

    with patch(
        "TwitchChannelPointsMiner.classes.Email.smtplib.SMTP", return_value=smtp
    ):
        result = notifier.send("test", Events.CONFIGURATION)

    assert result == (
        False,
        "SMTP message was rejected (SMTP 554): Transaction failed.",
    )


def test_email_rejects_two_tls_modes():
    with pytest.raises(ValueError, match="cannot both"):
        Email(
            "smtp.example.com",
            465,
            "miner@example.com",
            "you@example.com",
            [],
            use_ssl=True,
            starttls=True,
        )


def test_logger_skips_placeholder_email_host():
    notifier = Email(
        "smtp.example.com",
        587,
        "miner@example.com",
        "you@example.com",
        [Events.DAILY_REPORT],
    )
    formatter = GlobalFormatter.__new__(GlobalFormatter)
    formatter.settings = MagicMock(email=notifier)
    record = SimpleNamespace()

    with patch.object(formatter, "_send") as send:
        formatter.email(record)

    send.assert_not_called()
