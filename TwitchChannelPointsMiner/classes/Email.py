import smtplib
import socket
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

    def send(self, message: str, event: Events) -> tuple[bool, str | None]:
        if str(event) not in self.events:
            return False, "This event is not enabled for email."

        email = EmailMessage()
        event_name = event.name.replace("_", " ").title()
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
            return True, None
        except smtplib.SMTPAuthenticationError:
            return False, "SMTP authentication failed. Check the username and password."
        except socket.gaierror:
            return False, f"Could not resolve SMTP host '{self.host}'."
        except (TimeoutError, socket.timeout):
            return False, f"SMTP connection to {self.host}:{self.port} timed out."
        except ConnectionRefusedError:
            return False, f"SMTP server {self.host}:{self.port} refused the connection."
        except ssl.SSLError:
            return (
                False,
                "SMTP TLS negotiation failed. Check the SSL and STARTTLS settings.",
            )
        except smtplib.SMTPRecipientsRefused:
            return False, "The SMTP server rejected every recipient address."
        except smtplib.SMTPSenderRefused:
            return False, "The SMTP server rejected the configured sender address."
        except smtplib.SMTPConnectError as error:
            return False, _smtp_status_error("SMTP connection failed", error)
        except smtplib.SMTPDataError as error:
            return False, _smtp_status_error("SMTP message was rejected", error)
        except smtplib.SMTPServerDisconnected:
            return False, "The SMTP server disconnected before delivery completed."
        except (ConnectionError, OSError):
            return False, f"Unable to reach SMTP server {self.host}:{self.port}."
        except smtplib.SMTPException:
            return False, "The SMTP server rejected the test message."


def _smtp_status_error(prefix, error):
    code = getattr(error, "smtp_code", None)
    response = getattr(error, "smtp_error", b"")
    if isinstance(response, bytes):
        response = response.decode("utf-8", errors="replace")
    response = " ".join(str(response).split())[:200]
    status = f" (SMTP {code})" if code is not None else ""
    detail = f": {response}" if response else ""
    return f"{prefix}{status}{detail}."
