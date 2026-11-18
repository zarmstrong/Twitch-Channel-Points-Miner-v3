import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from TwitchChannelPointsMiner.classes import HttpSession
from TwitchChannelPointsMiner.classes.gql.Integration import GQL

PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")


@pytest.fixture
def server(monkeypatch):
    """Loopback HTTP/1.1 server that records each request's client port and
    Cookie header, and answers with a Set-Cookie. /redirect answers 302 to
    /next, also with a Set-Cookie."""
    for name in PROXY_VARIABLES:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    monkeypatch.setenv("NO_PROXY", "*")
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
                    "method": self.command,
                    "body": body,
                }
            )
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

        do_GET = _answer
        do_POST = _answer

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/", seen
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)


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
        sessions.append(HttpSession.session())
        assert HttpSession.session() is sessions[-1]
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
        return len(HttpSession.session().cookies)

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

    monkeypatch.setattr(HttpSession, "session", FakeSession)
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
