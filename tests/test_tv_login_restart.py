import threading
from types import SimpleNamespace

import pytest

import TwitchChannelPointsMiner.classes.TwitchLogin as twitch_login_module
from TwitchChannelPointsMiner.classes.Twitch import Twitch
from TwitchChannelPointsMiner.classes.TwitchLogin import TwitchLogin
from TwitchChannelPointsMiner.constants import CLIENT_ID
from TwitchChannelPointsMiner.TwitchChannelPointsMiner import TwitchChannelPointsMiner

DEVICE_CODE_RESPONSE = {
    "device_code": "d" * 40,
    "expires_in": 1800,
    "interval": 5,
    "user_code": "ABCDEFGH",
    "verification_uri": "https://www.twitch.tv/activate",
}


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self.payload = payload

    def json(self):
        return self.payload


class RestartDuringWait:
    """Stands in for restart_requested: the restart lands during the
    n-th wait that has a timeout (a poll interval)."""

    def __init__(self, on_wait=1):
        self.on_wait = on_wait
        self.timed_waits = []

    def wait(self, timeout=None):
        if not timeout:
            return len(self.timed_waits) >= self.on_wait
        self.timed_waits.append(timeout)
        return len(self.timed_waits) >= self.on_wait


@pytest.fixture
def tv_login(monkeypatch):
    requests_sent = []

    def send_oauth_request(self, url, json_data):
        requests_sent.append(url)
        if url.endswith("/device"):
            return FakeResponse(200, DEVICE_CODE_RESPONSE)
        if len(requests_sent) > 5:
            raise AssertionError("still polling Twitch after the restart")
        # 400: the user hasn't entered the code yet.
        return FakeResponse(400, {"message": "authorization_pending"})

    def no_real_sleep(seconds):
        if len(requests_sent) > 5:
            raise AssertionError("still polling Twitch after the restart")

    monkeypatch.setattr(TwitchLogin, "send_oauth_request", send_oauth_request)
    monkeypatch.setattr(twitch_login_module, "sleep", no_real_sleep)
    return TwitchLogin(CLIENT_ID, "device", "miner", "agent"), requests_sent


def test_tv_login_stops_when_a_restart_is_requested_while_it_waits(tv_login):
    login, requests_sent = tv_login
    login.stop_event = RestartDuringWait(on_wait=2)

    assert login.login_flow() is False

    # One device code, one poll after the first wait, none after the restart.
    assert requests_sent == [
        "https://id.twitch.tv/oauth2/device",
        "https://id.twitch.tv/oauth2/token",
    ]
    # The poll interval is waited on the event, so the restart ends the wait.
    assert login.stop_event.timed_waits == [5, 5]


def test_tv_login_requests_no_code_once_a_restart_is_pending(tv_login):
    login, requests_sent = tv_login
    login.stop_event = threading.Event()
    login.stop_event.set()

    assert login.login_flow() is False
    assert requests_sent == []


def test_tv_login_without_a_stop_event_keeps_polling(tv_login):
    # Code that builds TwitchLogin on its own (no restart event) still waits
    # for the user as before.
    login, requests_sent = tv_login

    with pytest.raises(AssertionError, match="still polling"):
        login.login_flow()


def test_twitch_creates_the_login_with_its_restart_event(monkeypatch, tmp_path):
    # Twitch() creates cookies/ in the working directory.
    monkeypatch.chdir(tmp_path)
    twitch = Twitch("miner", "agent")

    assert twitch.twitch_login.stop_event is twitch.restart_requested


def test_login_stopped_for_a_restart_returns_without_finishing_startup(
    monkeypatch, tmp_path
):
    twitch = Twitch.__new__(Twitch)
    twitch.cookies_file = str(tmp_path / "missing-cookies.json")
    twitch.restart_requested = threading.Event()

    def login_flow():
        twitch.restart_requested.set()
        return False

    twitch.twitch_login = SimpleNamespace(login_flow=login_flow)
    refreshed = []
    monkeypatch.setattr(
        Twitch, "update_client_version", lambda self: refreshed.append(True)
    )

    twitch.login()

    assert refreshed == []


def test_run_returns_right_after_a_login_stopped_for_a_restart():
    miner = TwitchChannelPointsMiner.__new__(TwitchChannelPointsMiner)
    miner.running = False
    miner.session_id = "session"
    restart_requested = threading.Event()
    miner.twitch = SimpleNamespace(
        restart_requested=restart_requested, login=restart_requested.set
    )

    miner.run(streamers=[])

    # Nothing after the login ran: run() didn't configure the Twitch client.
    assert not hasattr(miner.twitch, "track_drop_item_art")
