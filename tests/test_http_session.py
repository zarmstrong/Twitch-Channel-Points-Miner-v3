import gc
import re
import threading
import time
import weakref
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
import requests
from requests.adapters import HTTPAdapter

from TwitchChannelPointsMiner.classes import HttpSession
from TwitchChannelPointsMiner.classes.Twitch import Twitch
from TwitchChannelPointsMiner.classes.gql.Integration import GQL
from tests.test_streams_watched import _run_one_watch_iteration, _watch_streamer

PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
SLOW_SECONDS = 1.0


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.connections_closed = threading.Semaphore(0)

    def shutdown_request(self, request):
        super().shutdown_request(request)
        self.connections_closed.release()

    def handle_error(self, request, client_address):
        # A client that gave up on /slow makes the late write fail; that's
        # expected and not worth a traceback.
        pass


@pytest.fixture
def make_server(monkeypatch):
    """Start loopback HTTP/1.1 servers that record each request's client
    port, Cookie and Authorization headers, and answer with a Set-Cookie.
    /redirect answers 302 to /next, also with a Set-Cookie. /slow waits
    SLOW_SECONDS before answering.

    With close_after_response, the server closes each connection after
    answering, without a ``Connection: close`` header, as a server dropping
    an idle keep-alive connection would."""
    for name in PROXY_VARIABLES:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    monkeypatch.setenv("NO_PROXY", "*")
    started = []

    def start(close_after_response=False):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _answer(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                seen.append(
                    {
                        "path": self.path,
                        "port": self.client_address[1],
                        "cookie": self.headers.get("Cookie"),
                        "authorization": self.headers.get("Authorization"),
                        "method": self.command,
                        "body": body,
                    }
                )
                if self.path == "/slow":
                    time.sleep(SLOW_SECONDS)
                payload = b"ok"
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/next")
                else:
                    self.send_response(200)
                self.send_header("Set-Cookie", "unique_id=abc; Path=/")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                if close_after_response:
                    self.close_connection = True

            do_GET = _answer
            do_POST = _answer

            def log_message(self, *args):
                pass

        httpd = _Server(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        started.append((httpd, thread))
        return SimpleNamespace(
            url=f"http://127.0.0.1:{httpd.server_address[1]}/",
            seen=seen,
            connections_closed=httpd.connections_closed,
        )

    yield start
    for httpd, thread in started:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)


@pytest.fixture
def server(make_server):
    started = make_server()
    return started.url, started.seen


def _in_thread(target):
    result = {}

    def run():
        result["value"] = target()

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive()
    return result["value"]


def test_requests_on_one_thread_reuse_its_connection(server):
    url, seen = server

    def two_requests():
        first = HttpSession.get(url, timeout=5)
        second = HttpSession.post(url, data=b"payload", timeout=5)
        return first.status_code, second.status_code

    assert _in_thread(two_requests) == (200, 200)
    assert [request["method"] for request in seen] == ["GET", "POST"]
    assert seen[1]["body"] == b"payload"
    assert seen[0]["port"] == seen[1]["port"]


def test_each_thread_gets_its_own_session_and_connection(server):
    url, seen = server
    sessions = []

    def one_request():
        sessions.append(HttpSession.session(url))
        assert HttpSession.session(url) is sessions[-1]
        return HttpSession.get(url, timeout=5).status_code

    assert _in_thread(one_request) == 200
    assert _in_thread(one_request) == 200
    assert sessions[0] is not sessions[1]
    assert seen[0]["port"] != seen[1]["port"]


def test_cookies_are_never_kept_or_sent_back(server):
    url, seen = server

    def requests_after_a_set_cookie():
        HttpSession.get(url, timeout=5)
        HttpSession.post(url, json={}, timeout=5)
        return len(HttpSession.session(url).cookies)

    assert _in_thread(requests_after_a_set_cookie) == 0
    assert [request["cookie"] for request in seen] == [None, None]


def test_redirect_cookies_match_bare_requests(server):
    """Within one call, requests carries a redirect's Set-Cookie to the next
    hop whether or not a session is used; neither keeps it for later calls.
    The shared sessions must not change either behaviour."""
    url, seen = server

    def redirect_then_request(get):
        get(url + "redirect", timeout=5)
        get(url, timeout=5)

    _in_thread(lambda: redirect_then_request(requests.get))
    bare = [(request["path"], request["cookie"]) for request in seen]
    seen.clear()
    _in_thread(lambda: redirect_then_request(HttpSession.get))
    shared = [(request["path"], request["cookie"]) for request in seen]

    assert bare == [("/redirect", None), ("/next", "unique_id=abc"), ("/", None)]
    assert shared == bare


def test_get_and_post_pass_their_arguments_through(monkeypatch):
    calls = []

    class FakeSession:
        def get(self, url, **kwargs):
            calls.append(("get", url, kwargs))
            return "got"

        def post(self, url, **kwargs):
            calls.append(("post", url, kwargs))
            return "posted"

    fake = FakeSession()
    monkeypatch.setattr(HttpSession, "_entry", lambda url: [fake, 0.0])
    assert HttpSession.get("https://a", params={"x": 1}, timeout=20) == "got"
    assert HttpSession.post("https://b", data=b"d", headers={"h": "v"}) == "posted"
    assert calls == [
        ("get", "https://a", {"params": {"x": 1}, "timeout": 20}),
        ("post", "https://b", {"data": b"d", "headers": {"h": "v"}}),
    ]


def test_gql_posts_through_the_thread_session_with_its_timeout(monkeypatch):
    calls = []
    monkeypatch.setattr(
        HttpSession,
        "post",
        lambda url, **kwargs: calls.append((url, kwargs)) or "response",
    )
    post_request = GQL(client_session=None).post_request
    assert post_request("https://gql", {"q": 1}, {"h": "v"}) == "response"
    assert calls == [
        (
            "https://gql",
            {"json": {"q": 1}, "headers": {"h": "v"}, "timeout": (5, 30)},
        )
    ]


def test_a_session_idle_for_too_long_is_replaced_with_a_new_connection(
    server, monkeypatch
):
    url, seen = server
    now = [1000.0]
    monkeypatch.setattr(HttpSession, "_clock", lambda: now[0])

    def requests_around_an_idle_gap():
        first = HttpSession.session(url)
        HttpSession.get(url, timeout=5)
        now[0] += HttpSession.IDLE_SECONDS - 1
        HttpSession.get(url, timeout=5)
        kept = HttpSession.session(url)
        now[0] += HttpSession.IDLE_SECONDS
        HttpSession.post(url, data=b"x", timeout=5)
        return first, kept, HttpSession.session(url)

    first, kept, replaced = _in_thread(requests_around_an_idle_gap)
    assert kept is first
    assert replaced is not first
    assert seen[0]["port"] == seen[1]["port"]
    assert seen[2]["port"] != seen[1]["port"]
    # The old session's pooled connection was closed, not left open.
    assert len(first.get_adapter(url).poolmanager.pools) == 0


def test_idle_time_counts_from_the_end_of_the_last_request(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(HttpSession, "_clock", lambda: now[0])

    class SlowSession(requests.Session):
        def get(self, url, **kwargs):
            now[0] += HttpSession.IDLE_SECONDS + 5
            return "response"

    monkeypatch.setattr(HttpSession, "_new_session", SlowSession)

    def slow_request_then_another():
        first = HttpSession.session("https://slow")
        HttpSession.get("https://slow")
        return first, HttpSession.session("https://slow")

    first, second = _in_thread(slow_request_then_another)
    assert second is first


def test_idle_time_is_tracked_per_host(monkeypatch):
    """Steady requests to one host must not keep another host's idle
    connection in use past IDLE_SECONDS."""
    now = [0.0]
    monkeypatch.setattr(HttpSession, "_clock", lambda: now[0])
    monkeypatch.setattr(HttpSession, "_new_session", requests.Session)

    def busy_host_and_idle_host():
        idle = HttpSession.session("https://idle.example")
        busy = HttpSession.session("https://busy.example")
        for _ in range(4):
            now[0] += HttpSession.IDLE_SECONDS / 2
            assert HttpSession.session("https://busy.example") is busy
        return idle, HttpSession.session("https://IDLE.example/path")

    idle, again = _in_thread(busy_host_and_idle_host)
    assert again is not idle


def test_hosts_share_nothing_and_idle_ones_are_dropped(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(HttpSession, "_clock", lambda: now[0])
    closed = []

    class TrackedSession(requests.Session):
        def close(self):
            closed.append(self)
            super().close()

    monkeypatch.setattr(HttpSession, "_new_session", TrackedSession)

    def two_hosts_then_one():
        a = HttpSession.session("https://a.example/x")
        b = HttpSession.session("https://b.example")
        assert HttpSession.session("https://a.example/y") is a
        assert a is not b
        now[0] += HttpSession.IDLE_SECONDS
        HttpSession.session("https://a.example")
        return a, b, dict(HttpSession._local.sessions)

    a, b, left = _in_thread(two_hosts_then_one)
    # Both had gone IDLE_SECONDS unused: both closed, only a new a remains.
    assert closed == [a, b]
    assert list(left) == ["https://a.example"]


def test_sessions_keep_a_small_connection_pool_per_host():
    def adapters():
        return [
            HttpSession.session(url).get_adapter(url)
            for url in ("https://a", "http://b")
        ]

    for adapter in _in_thread(adapters):
        assert isinstance(adapter, HTTPAdapter)
        assert adapter._pool_maxsize == HttpSession.POOL_MAXSIZE


def test_a_finished_threads_session_closes_its_connections(monkeypatch):
    closed = []
    close = HTTPAdapter.close
    monkeypatch.setattr(
        HTTPAdapter, "close", lambda self: closed.append(self) or close(self)
    )
    refs = []

    def make_session():
        current = HttpSession.session("https://a")
        refs.append((weakref.ref(current), current.get_adapter("https://a")))

    _in_thread(make_session)
    gc.collect()
    session_ref, adapter = refs[0]
    assert session_ref() is None
    assert adapter in closed


@pytest.mark.parametrize("send", [requests.post, HttpSession.post])
def test_post_after_the_server_closed_the_kept_alive_connection(make_server, send):
    """The server closes the connection after each answer without saying so.
    The next POST on the same thread must still go through, as it does with
    a bare requests.post (which always opens a new connection)."""
    started = make_server(close_after_response=True)

    def two_posts():
        first = send(started.url, data=b"one", timeout=5)
        assert started.connections_closed.acquire(timeout=5)
        second = send(started.url, data=b"two", timeout=5)
        return first.status_code, second.status_code

    assert _in_thread(two_posts) == (200, 200)
    assert [request["body"] for request in started.seen] == [b"one", b"two"]
    assert started.seen[0]["port"] != started.seen[1]["port"]


@pytest.mark.parametrize(
    "get, post", [(requests.get, requests.post), (HttpSession.get, HttpSession.post)]
)
def test_read_timeout_on_a_reused_connection_matches_bare_requests(server, get, post):
    """A request that times out on a kept-alive connection raises the same
    ReadTimeout as a bare call, and the thread's next request still works."""
    url, seen = server

    def requests_around_a_timeout():
        get(url, timeout=5)
        with pytest.raises(requests.exceptions.ReadTimeout):
            post(url + "slow", data=b"x", timeout=(5, SLOW_SECONDS / 4))
        return post(url, data=b"y", timeout=5).status_code

    assert _in_thread(requests_around_a_timeout) == 200
    assert [request["path"] for request in seen] == ["/", "/slow", "/"]


def test_a_per_call_authorization_header_is_not_kept_for_the_next_call(server):
    url, seen = server

    def with_then_without_authorization():
        HttpSession.get(url, headers={"Authorization": "Bearer secret"}, timeout=5)
        HttpSession.get(url, timeout=5)
        HttpSession.post(url, json={}, timeout=5)

    _in_thread(with_then_without_authorization)
    assert [request["authorization"] for request in seen] == [
        "Bearer secret",
        None,
        None,
    ]
    assert seen[0]["port"] == seen[1]["port"] == seen[2]["port"]


class _FakeResponse:
    status_code = 200

    def __init__(self, text=""):
        self.text = text

    def raise_for_status(self):
        pass

    def json(self):
        return {"data": []}


def _helix(monkeypatch):
    twitch = Twitch.__new__(Twitch)
    twitch.twitch_login = SimpleNamespace(get_auth_token=lambda: "token")
    twitch.user_agent = "test-agent"
    twitch._Twitch__helix_get("streams", {"first": 1})


def _spade_get(monkeypatch):
    twitch = Twitch.__new__(Twitch)
    streamer = SimpleNamespace(
        streamer_url="https://www.twitch.tv/someone",
        stream=SimpleNamespace(spade_url=None),
    )
    twitch.get_spade_url(streamer)
    assert streamer.stream.spade_url == "https://spade.test/x"


def _spade_post(monkeypatch):
    post_kwargs = []
    _run_one_watch_iteration(
        monkeypatch, [_watch_streamer("one")], 1, post_kwargs=post_kwargs
    )
    return [(kwargs["timeout"],) for kwargs in post_kwargs]


def _update_client_version(monkeypatch):
    twitch = Twitch.__new__(Twitch)
    twitch.client_version = "old"
    twitch.twilight_build_id_pattern = re.compile(r"no build id here")
    assert twitch.update_client_version() == "old"


@pytest.mark.parametrize(
    "call_site, method, expected",
    [
        (_helix, "get", [20]),
        (_spade_get, "get", [(5, 20), (5, 20)]),
        (_spade_post, "post", [20]),
        (_update_client_version, "get", [(5, 20)]),
    ],
    ids=["helix", "spade-get", "spade-post", "update-client-version"],
)
def test_call_sites_pass_their_timeouts_through(
    monkeypatch, call_site, method, expected
):
    timeouts = []
    responses = [
        _FakeResponse('<script src="https://assets.twitch.tv/config/settings.a.js">'),
        _FakeResponse('{"spade_url":"https://spade.test/x"}'),
    ]

    def fake(url, **kwargs):
        timeouts.append(kwargs["timeout"])
        return responses.pop(0) if responses else _FakeResponse()

    monkeypatch.setattr(HttpSession, method, fake)
    recorded = call_site(monkeypatch)
    if recorded is not None:
        timeouts = [timeout for (timeout,) in recorded]
    assert timeouts == expected
