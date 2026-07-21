import smtplib
import ssl
from email.message import EmailMessage

from TwitchChannelPointsMiner.classes.Settings import Events


class Email(object):
    __slots__ = [
        "host",
        "port",
        "username",
        "password",
        "sender",
        "recipients",
        "events",
        "use_ssl",
        "starttls",
        "timeout",
    ]

    def __init__(
        self,
        host: str,
        port: int,
        sender: str,
        recipients: list | tuple | str,
        events: list,
        username: str | None = None,
        password: str | None = None,
        use_ssl: bool = False,
        starttls: bool = True,
        timeout: float = 15,
    ):
        if use_ssl and starttls:
            raise ValueError("use_ssl and starttls cannot both be enabled")
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.sender = sender
        self.recipients = (
            [recipients] if isinstance(recipients, str) else list(recipients)
        )
        self.events = [str(event) for event in events]
        self.use_ssl = use_ssl
        self.starttls = starttls
        self.timeout = timeout

    def send(self, message: str, event: Events) -> None:
        if str(event) not in self.events:
            return

        email = EmailMessage()
        event_name = str(event).replace("_", " ").title()
        email["Subject"] = f"Twitch Channel Points Miner: {event_name}"
        email["From"] = self.sender
        email["To"] = ", ".join(self.recipients)
        email.set_content(message)

        try:
            smtp_class = smtplib.SMTP_SSL if self.use_ssl else smtplib.SMTP
            with smtp_class(self.host, self.port, timeout=self.timeout) as smtp:
                if self.starttls:
                    smtp.starttls(context=ssl.create_default_context())
                if self.username:
                    smtp.login(self.username, self.password or "")
                smtp.send_message(email)
        except (OSError, smtplib.SMTPException):
            return
