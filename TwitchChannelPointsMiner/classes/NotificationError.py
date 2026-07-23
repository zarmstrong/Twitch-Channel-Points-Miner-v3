import socket

import requests


def format_request_failure(service, error):
    """Return an actionable notification error without exposing URLs or secrets."""
    if isinstance(error, requests.HTTPError):
        status = getattr(error.response, "status_code", None)
        if status == 401:
            return f"{service} rejected the configured credentials (HTTP 401)."
        if status == 403:
            return f"{service} denied the request (HTTP 403)."
        if status == 404:
            return f"{service} endpoint was not found (HTTP 404)."
        if status == 429:
            return f"{service} rate limited the test notification (HTTP 429)."
        if status is not None and status >= 500:
            return f"{service} reported a server error (HTTP {status})."
        detail = f" (HTTP {status})" if status is not None else ""
        return f"{service} rejected the test notification{detail}."
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
