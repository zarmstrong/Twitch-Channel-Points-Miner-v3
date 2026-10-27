import socket
import time

import requests

from TwitchChannelPointsMiner.classes.Settings import Settings

# Longest Retry-After (seconds) honoured before a rate-limited part is given up on.
RETRY_AFTER_CAP = 10
# Most parts one notification is split into. Each part is its own post, so an
# unbounded split could flood a channel and hold the notification thread
# through rate-limit waits; the last part says what was left out.
MAX_NOTIFICATION_PARTS = 10


def notification_prefix():
    """Return the username prefix the logger puts at the start of notifications."""
    prefix = getattr(getattr(Settings, "logger", None), "username", None)
    return prefix if isinstance(prefix, str) else ""


def send_parts(service, parts, post_one):
    """Deliver the parts of a split notification in order.

    ``post_one(part)`` performs the request for one part and raises
    ``requests.RequestException`` (for example from ``raise_for_status``) when
    it fails. A part rejected with HTTP 429 is retried once after the wait the
    service asks for, as long as it gave one and it is at most
    ``RETRY_AFTER_CAP`` seconds. Returns the ``(success, reason)`` tuple
    notifiers use.
    """
    total = len(parts)
    for index, part in enumerate(parts, start=1):
        try:
            try:
                post_one(part)
            except requests.HTTPError as error:
                delay = _retry_after_seconds(error)
                if delay is None or delay > RETRY_AFTER_CAP:
                    raise
                time.sleep(delay)
                post_one(part)
        except requests.RequestException as error:
            return False, format_request_failure(service, error, index, total)
    return True, None


def _retry_after_seconds(error):
    """Return the wait a 429 response asks for, or None when it gives none.

    The Retry-After header is used first, then the JSON body's
    ``parameters.retry_after`` (Telegram) or ``retry_after`` (Discord).
    """
    response = getattr(error, "response", None)
    if getattr(response, "status_code", None) != 429:
        return None
    headers = getattr(response, "headers", None) or {}
    for hint in (headers.get("Retry-After"), _body_retry_after(response)):
        try:
            return max(0.0, float(hint))
        except (TypeError, ValueError):
            continue
    return None


def _body_retry_after(response):
    try:
        body = response.json()
    except (AttributeError, TypeError, ValueError):
        return None
    if not isinstance(body, dict):
        return None
    parameters = body.get("parameters")
    if isinstance(parameters, dict) and "retry_after" in parameters:
        return parameters["retry_after"]
    return body.get("retry_after")


def format_request_failure(service, error, part=1, parts=1):
    """Return an actionable notification error without exposing URLs or secrets.

    When a notification was split into several parts, ``part`` and ``parts``
    identify which delivery failed so partial deliveries are distinguishable.
    """
    message = _describe_request_failure(service, error)
    if parts > 1:
        return f"{message.rstrip('.')} (part {part} of {parts})."
    return message


def _describe_request_failure(service, error):
    if isinstance(error, requests.HTTPError):
        status = getattr(error.response, "status_code", None)
        if status == 401:
            return f"{service} rejected the configured credentials (HTTP 401)."
        if status == 403:
            return f"{service} denied the request (HTTP 403)."
        if status == 404:
            return f"{service} endpoint was not found (HTTP 404)."
        if status == 429:
            return f"{service} rate limited the notification (HTTP 429)."
        if status is not None and status >= 500:
            return f"{service} reported a server error (HTTP {status})."
        detail = f" (HTTP {status})" if status is not None else ""
        return f"{service} rejected the notification{detail}."
    if isinstance(error, requests.exceptions.SSLError):
        return f"TLS negotiation with {service} failed. Check its HTTPS certificate."
    if isinstance(error, requests.exceptions.Timeout):
        return f"The connection to {service} timed out."
    if isinstance(error, requests.exceptions.ProxyError):
        return f"The configured proxy could not connect to {service}."
    if isinstance(
        error,
        (
            requests.exceptions.InvalidURL,
            requests.exceptions.InvalidSchema,
            requests.exceptions.MissingSchema,
        ),
    ):
        return f"The configured {service} URL is invalid."
    if isinstance(error, requests.exceptions.TooManyRedirects):
        return f"{service} returned too many redirects."
    if _contains_exception(error, socket.gaierror):
        return f"Could not resolve the {service} host. Check its hostname and DNS."
    if _contains_exception(error, ConnectionRefusedError):
        return f"{service} refused the connection. Check its host and port."
    if _contains_exception(error, ConnectionResetError):
        return f"The connection to {service} was reset before delivery completed."
    return f"Could not connect to {service}. Check its URL, network, DNS, and firewall."


def _contains_exception(error, exception_type):
    pending = [error]
    seen = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, exception_type):
            return True
        for related in (
            getattr(current, "__cause__", None),
            getattr(current, "__context__", None),
            getattr(current, "reason", None),
        ):
            if isinstance(related, BaseException):
                pending.append(related)
        pending.extend(
            argument
            for argument in getattr(current, "args", ())
            if isinstance(argument, BaseException)
        )
    return False
