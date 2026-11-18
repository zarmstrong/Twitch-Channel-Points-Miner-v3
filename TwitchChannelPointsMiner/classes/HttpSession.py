"""Per-thread HTTP sessions that keep connections to Twitch open.

A bare ``requests.get`` or ``requests.post`` builds a throwaway session, so
every call opens a new TCP connection and repeats the TLS handshake. The
miner's frequent Twitch requests (GQL, minute-watched events, Helix) use
these helpers instead, and each thread reuses its own pooled connections.

``requests.Session`` isn't documented as thread-safe, so sessions are never
shared between threads. A thread's session is dropped when the thread ends.

Bare calls never carried cookies from one request to the next, and these
sessions don't either: their cookie jar rejects every cookie.
"""

import threading
from http.cookiejar import CookiePolicy

import requests


class _RejectAllCookies(CookiePolicy):
    """Cookie policy that never stores or sends a cookie."""

    netscape = True
    rfc2965 = False
    hide_cookie2 = False

    def set_ok(self, cookie, request):
        return False

    def return_ok(self, cookie, request):
        return False

    def domain_return_ok(self, domain, request):
        return False

    def path_return_ok(self, path, request):
        return False


_local = threading.local()


def _new_session() -> requests.Session:
    session = requests.Session()
    session.cookies.set_policy(_RejectAllCookies())
    return session


def session() -> requests.Session:
    """Return this thread's session, creating it on first use."""
    current = getattr(_local, "session", None)
    if current is None:
        current = _new_session()
        _local.session = current
    return current


def get(url, **kwargs) -> requests.Response:
    """``requests.get`` over this thread's session."""
    return session().get(url, **kwargs)


def post(url, **kwargs) -> requests.Response:
    """``requests.post`` over this thread's session."""
    return session().post(url, **kwargs)
