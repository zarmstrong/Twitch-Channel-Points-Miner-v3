import pytest

from TwitchChannelPointsMiner.classes.TwitchLogin import TwitchLogin
from TwitchChannelPointsMiner.constants import CLIENT_ID


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self.payload = payload

    def json(self):
        return self.payload


def make_login(monkeypatch, response):
    login = TwitchLogin(CLIENT_ID, "device", "miner", "agent")
    login.set_token("t" * 30)
    monkeypatch.setattr(login.session, "post", lambda *args, **kwargs: response)
    return login


def test_check_login_sets_the_user_id(monkeypatch):
    login = make_login(
        monkeypatch, FakeResponse(200, {"data": {"user": {"id": "12345"}}})
    )

    assert login.check_login() is True
    assert login.user_id == "12345"


def test_unknown_username_fails_the_login_instead_of_crashing(monkeypatch, caplog):
    # Issue #401: Twitch returns a null user for a login name it doesn't know.
    login = make_login(monkeypatch, FakeResponse(200, {"data": {"user": None}}))

    assert login.check_login() is False
    assert login.user_id is None
    assert "no account with the username 'miner'" in caplog.text


@pytest.mark.parametrize(
    "payload",
    [{"data": None}, {}, {"data": {}}, {"data": {"user": {"id": None}}}],
)
def test_incomplete_responses_fail_the_login(monkeypatch, payload):
    login = make_login(monkeypatch, FakeResponse(200, payload))

    assert login.check_login() is False
    assert login.user_id is None
