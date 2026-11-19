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
requests doesn't retry a POST. To keep that window small, a thread keeps one
session per host, and a session is closed and replaced once its host has
gone ``IDLE_SECONDS`` without a request, before a server or load balancer is
likely to drop the idle connection. A session's connections are also closed
when its thread ends and the session is garbage-collected.

Bare calls never carried cookies from one request to the next, and these
sessions don't either: their cookie jar rejects every cookie. Headers are
per call, as with bare calls; nothing passed to one call is kept for the
next.
"""

import threading
import time
import weakref
from http.cookiejar import CookiePolicy
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.exceptions import ReadTimeoutError
from urllib3.util.retry import Retry

# Seconds a thread's session may sit unused before it's replaced, closing its
# pooled connections. Servers and load balancers commonly drop idle
# keep-alive connections after about 60 seconds; staying well under that
# avoids reusing a socket the other end is about to close.
IDLE_SECONDS = 30

# Connections kept per pool. A thread sends one request at a time, so it only
# ever needs one; a little headroom costs nothing. requests' default is 10.
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


class _DroppedConnectionRetry(Retry):
    """Retry a GET once when its connection drops before the response.

    A server can close a pooled connection after urllib3 checks it and before
    the request lands. A bare call would have used a fresh connection, so a
    GET (safe to send again) gets one more try on a new connection. A read
    timeout is re-raised as it is, as with bare calls: retrying it would
    double how long a slow GET hangs. POSTs aren't retried here; the GQL and
    minute-watched callers retry connection errors themselves."""

    def increment(
        self,
        method=None,
        url=None,
        response=None,
        error=None,
        _pool=None,
        _stacktrace=None,
    ):
        if isinstance(error, ReadTimeoutError):
            raise error.with_traceback(_stacktrace)
        return super().increment(method, url, response, error, _pool, _stacktrace)


def _retries() -> Retry:
    return _DroppedConnectionRetry(
        total=1,
        connect=0,
        read=1,
        redirect=0,
        status=0,
        other=0,
        allowed_methods=frozenset({"GET"}),
        raise_on_redirect=False,
        raise_on_status=False,
    )


_local = threading.local()


def _new_session() -> requests.Session:
    session = requests.Session()
    session.cookies.set_policy(_RejectAllCookies())
    adapter = HTTPAdapter(pool_maxsize=POOL_MAXSIZE, max_retries=_retries())
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    # Close the pooled sockets as soon as the session is collected, for
    # example when its thread ends, instead of leaving them to the sockets'
    # own finalizers.
    weakref.finalize(session, adapter.close)
    return session


def _origin(url) -> str:
    parts = urlsplit(str(url))
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


def _entry(url) -> list:
    """Return this thread's ``[session, last_used]`` for ``url``'s host.

    Each host gets its own session, so idle time is per host: a thread that
    keeps posting to one host doesn't keep another host's idle connection
    alive. Sessions idle for ``IDLE_SECONDS`` are closed and dropped here,
    which also stops spade hosts of streamers no longer watched from
    piling up.
    """
    sessions = getattr(_local, "sessions", None)
    if sessions is None:
        sessions = _local.sessions = {}
    now = _clock()
    for origin, (old, last_used) in list(sessions.items()):
        if now - last_used >= IDLE_SECONDS:
            del sessions[origin]
            old.close()
    key = _origin(url)
    entry = sessions.get(key)
    if entry is None:
        entry = sessions[key] = [_new_session(), now]
    entry[1] = now
    return entry


def session(url="") -> requests.Session:
    """Return this thread's session for ``url``'s host, creating it on first
    use and replacing it after an idle gap of ``IDLE_SECONDS``."""
    return _entry(url)[0]


def _send(method: str, url, kwargs) -> requests.Response:
    entry = _entry(url)
    try:
        return getattr(entry[0], method)(url, **kwargs)
    finally:
        # Idle time counts from the end of the last request, so a slow
        # response doesn't make its own connection look idle.
        entry[1] = _clock()


def get(url, **kwargs) -> requests.Response:
    """``requests.get`` over this thread's session."""
    return _send("get", url, kwargs)


def post(url, **kwargs) -> requests.Response:
    """``requests.post`` over this thread's session."""
    return _send("post", url, kwargs)
