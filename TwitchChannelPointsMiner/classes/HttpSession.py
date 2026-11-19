"""Per-thread HTTP sessions that keep connections to Twitch open.

A bare ``requests.get`` or ``requests.post`` builds a throwaway session, so
every call opens a new TCP connection and repeats the TLS handshake. The
miner's frequent Twitch requests (GQL, minute-watched events, Helix) use
these helpers instead, and each thread reuses its own pooled connections.

``requests.Session`` isn't documented as thread-safe, so each thread gets its
own from ``session()``. Don't pass the returned session to another thread.

A kept-alive connection can be closed by the server while it sits idle in
the pool. urllib3 notices most of those closes before it reuses the socket,
but a close that lands just before a request makes that request fail, and
requests doesn't retry a POST. To keep that window small, a thread's session
is closed and replaced once it has gone ``IDLE_SECONDS`` without a request,
before a server or load balancer is likely to drop the idle connection. A
session's connections are also closed when its thread ends and the session
is garbage-collected.

Bare calls never carried cookies from one request to the next, and these
sessions don't either: their cookie jar rejects every cookie. Headers are
per call, as with bare calls; nothing passed to one call is kept for the
next.
"""

import threading
import time
import weakref
from http.cookiejar import CookiePolicy

import requests
from requests.adapters import HTTPAdapter

# Seconds a thread's session may sit unused before it's replaced, closing its
# pooled connections. Servers and load balancers commonly drop idle
# keep-alive connections after about 60 seconds; staying well under that
# avoids reusing a socket the other end is about to close.
IDLE_SECONDS = 30

# Connections kept per host. A thread sends one request at a time, so it only
# ever needs one; a little headroom costs nothing. requests' default is 10,
# which matters because spade hosts vary per streamer.
POOL_MAXSIZE = 2

_clock = time.monotonic


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
    adapter = HTTPAdapter(pool_maxsize=POOL_MAXSIZE)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    # Close the pooled sockets as soon as the session is collected, for
    # example when its thread ends, instead of leaving them to the sockets'
    # own finalizers.
    weakref.finalize(session, adapter.close)
    return session


def session() -> requests.Session:
    """Return this thread's session, creating it on first use and replacing
    it after an idle gap of ``IDLE_SECONDS``."""
    current = getattr(_local, "session", None)
    if current is not None and _clock() - _local.last_used >= IDLE_SECONDS:
        current.close()
        current = None
    if current is None:
        current = _new_session()
        _local.session = current
    _local.last_used = _clock()
    return current


def _send(method: str, url, kwargs) -> requests.Response:
    try:
        return getattr(session(), method)(url, **kwargs)
    finally:
        # Idle time counts from the end of the last request, so a slow
        # response doesn't make its own connection look idle.
        _local.last_used = _clock()


def get(url, **kwargs) -> requests.Response:
    """``requests.get`` over this thread's session."""
    return _send("get", url, kwargs)


def post(url, **kwargs) -> requests.Response:
    """``requests.post`` over this thread's session."""
    return _send("post", url, kwargs)
