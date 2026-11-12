"""Opt-in Android app login (issue #229)."""

import copy
import importlib
import json
import logging
import signal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from TwitchChannelPointsMiner.classes import TwitchLogin as twitch_login_module
from TwitchChannelPointsMiner.classes.ClientSession import ClientSession
from TwitchChannelPointsMiner.classes.Exceptions import WrongCookiesException
from TwitchChannelPointsMiner.classes.gql.Integration import GQL
from TwitchChannelPointsMiner.classes.Twitch import Twitch
from TwitchChannelPointsMiner.classes.TwitchLogin import (
    TwitchLogin,
    parse_android_login,
)
from TwitchChannelPointsMiner.logger import (
    ANDROID_LOGIN_COLOR,
    ANDROID_LOGIN_ERROR_COLOR,
    LoggerSettings,
)
from TwitchChannelPointsMiner.constants import (
    ANDROID_CLIENT_ID,
    ANDROID_LOGIN_ENV_VAR,
    CLIENT_ID,
    USER_AGENTS,
    GQLOperations,
)

TOKEN = "abcdefghijklmnopqrstuvwxyz0123"
REDIRECT = (
    f"https://www.twitch.tv/#access_token={TOKEN}&scope=channel_read&token_type=bearer"
)


class FakeValidate:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self.payload = payload or {}
        self.content = b"x"

    def json(self):
        return self.payload


def android_login(monkeypatch, response, username="Miner"):
    calls = []

    def fake_get(url, headers, timeout):
        calls.append((url, headers))
        return response

    monkeypatch.setattr(twitch_login_module.requests, "get", fake_get)
    monkeypatch.delenv(ANDROID_LOGIN_ENV_VAR, raising=False)
    monkeypatch.setattr(twitch_login_module, "_running_in_container", lambda: False)
    return TwitchLogin(ANDROID_CLIENT_ID, "device", username, "agent"), calls


def valid_response(login="miner", client_id=ANDROID_CLIENT_ID):
    return FakeValidate(
        payload={"client_id": client_id, "login": login, "user_id": "42"}
    )


@pytest.mark.parametrize(
    "pasted",
    [
        REDIRECT,
        f"  {REDIRECT}\n",
        f"https://www.twitch.tv/?access_token={TOKEN}",
        TOKEN,
    ],
)
def test_parse_android_login_reads_url_or_bare_token(pasted):
    assert parse_android_login(pasted) == TOKEN


@pytest.mark.parametrize(
    "pasted",
    ["", None, "https://www.twitch.tv/", "not a token!", "#access_token=x"],
)
def test_parse_android_login_rejects_text_without_a_token(pasted):
    assert parse_android_login(pasted) is None


def test_pasted_redirect_url_logs_in_without_logging_the_token(monkeypatch, caplog):
    login, calls = android_login(monkeypatch, valid_response())

    with caplog.at_level(logging.DEBUG):
        assert login.android_login_flow(read_input=lambda prompt: REDIRECT) is True

    assert login.token == TOKEN
    assert login.user_id == "42"
    assert calls[0][1] == {"Authorization": f"OAuth {TOKEN}"}
    assert TOKEN not in caplog.text


def test_login_flow_uses_the_android_flow_for_the_android_client(monkeypatch):
    login, _ = android_login(monkeypatch, valid_response())
    monkeypatch.setattr("builtins.input", lambda prompt: REDIRECT)

    assert login.login_flow() is True


def test_environment_variable_logs_in_without_a_prompt(monkeypatch):
    login, _ = android_login(monkeypatch, valid_response())
    monkeypatch.setenv(ANDROID_LOGIN_ENV_VAR, REDIRECT)

    def no_prompt(prompt):
        raise AssertionError("prompted although the environment had a login")

    assert login.android_login_flow(read_input=no_prompt) is True


@pytest.mark.parametrize(
    "response",
    [
        valid_response(client_id=CLIENT_ID),  # a TV token
        valid_response(login="someone_else"),
        FakeValidate(status_code=401),
    ],
)
def test_token_that_is_not_this_accounts_android_login_is_refused(
    monkeypatch, response
):
    login, _ = android_login(monkeypatch, response)
    pasted = iter([REDIRECT] * 3)

    assert login.android_login_flow(read_input=lambda prompt: next(pasted)) is False
    assert login.token is None


@pytest.mark.parametrize(
    "error",
    [EOFError, RuntimeError("input(): lost sys.stdin")],
)
def test_no_console_tells_the_user_about_the_environment_variable(
    monkeypatch, caplog, error
):
    login, calls = android_login(monkeypatch, valid_response())

    def no_console(prompt):
        raise error

    with caplog.at_level(logging.ERROR):
        assert login.android_login_flow(read_input=no_console) is False

    assert ANDROID_LOGIN_ENV_VAR in caplog.text
    assert calls == []


def test_outside_a_container_the_user_is_asked_to_paste(monkeypatch, caplog):
    login, _ = android_login(monkeypatch, valid_response())

    with caplog.at_level(logging.INFO):
        assert login.android_login_flow(read_input=lambda prompt: REDIRECT) is True

    assert "3. Paste it below." in caplog.text
    assert "docker" not in caplog.text.lower()


def test_in_a_container_the_steps_point_at_the_environment_variable(
    monkeypatch, caplog
):
    login, _ = android_login(monkeypatch, valid_response())
    monkeypatch.setattr(twitch_login_module, "_running_in_container", lambda: True)

    def no_console(prompt):
        raise EOFError

    with caplog.at_level(logging.INFO):
        assert login.android_login_flow(read_input=no_console) is False

    assert "Paste it below" not in caplog.text
    assert f"set the {ANDROID_LOGIN_ENV_VAR} environment variable" in caplog.text
    assert "docker attach" in caplog.text
    assert "recreate the container." in caplog.text
    assert "start the miner again" not in caplog.text


def test_empty_environment_variable_is_reported_and_the_user_is_asked(
    monkeypatch, caplog
):
    # docker compose sets "${TWITCH_ANDROID_LOGIN}" to "" when it's unset.
    login, _ = android_login(monkeypatch, valid_response())
    monkeypatch.setenv(ANDROID_LOGIN_ENV_VAR, "  ")

    with caplog.at_level(logging.INFO):
        assert login.android_login_flow(read_input=lambda prompt: REDIRECT) is True

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert f"{ANDROID_LOGIN_ENV_VAR} is set but empty" in errors[0].getMessage()
    assert "does not hold a valid Android login" not in caplog.text


def test_android_login_messages_are_colored(monkeypatch, caplog):
    login, _ = android_login(monkeypatch, valid_response(login="someone-else"))
    pasted = iter([REDIRECT, "", ""])

    with caplog.at_level(logging.INFO):
        login.android_login_flow(read_input=lambda prompt: next(pasted))

    records = [r for r in caplog.records if r.name == twitch_login_module.__name__]
    infos = [r for r in records if r.levelno == logging.INFO]
    errors = [r for r in records if r.levelno == logging.ERROR]
    assert infos and errors
    assert all(r.default_color == ANDROID_LOGIN_COLOR for r in infos)
    assert all(r.default_color == ANDROID_LOGIN_ERROR_COLOR for r in errors)


def test_prompt_is_logged_after_the_steps_not_printed_by_input(monkeypatch, caplog):
    # input()'s own prompt bypasses the logging queue and printed before the
    # sign-in steps; the prompt is logged instead.
    login, _ = android_login(monkeypatch, valid_response())
    prompts = []

    def read_input(prompt):
        prompts.append(prompt)
        return REDIRECT

    with caplog.at_level(logging.INFO):
        assert login.android_login_flow(read_input=read_input) is True

    assert prompts == [""]
    messages = [r.getMessage() for r in caplog.records]
    waiting = messages.index("Waiting for the redirect URL or access token...")
    assert waiting > next(i for i, m in enumerate(messages) if m.startswith("3. "))


def test_paste_without_a_token_is_asked_again(monkeypatch):
    login, _ = android_login(monkeypatch, valid_response())
    pasted = iter(["https://www.twitch.tv/", REDIRECT])

    assert login.android_login_flow(read_input=lambda prompt: next(pasted)) is True


def test_android_twitch_uses_its_own_client_cookie_file_and_user_agent(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)

    twitch = Twitch("someone", "agent", gql_factory=MagicMock(), android_login=True)

    assert twitch.twitch_login.client_id == ANDROID_CLIENT_ID
    assert twitch.cookies_file.endswith("someone-android.pkl")
    assert twitch.user_agent == USER_AGENTS["Android"]["App"]


def test_tv_login_is_unchanged_by_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    twitch = Twitch("someone", "agent", gql_factory=MagicMock())

    assert twitch.twitch_login.client_id == CLIENT_ID
    assert twitch.cookies_file.endswith("someone.pkl")
    assert twitch.user_agent == "agent"


def test_failed_android_login_stops_instead_of_running_without_a_token(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    twitch = Twitch("someone", "agent", gql_factory=MagicMock(), android_login=True)
    monkeypatch.setattr(TwitchLogin, "login_flow", lambda self: False)

    with pytest.raises(WrongCookiesException):
        twitch.login()


def test_gql_requests_send_the_login_client_id():
    headers = []

    def post(url, json, headers_arg=None, **kwargs):
        headers.append(kwargs.get("headers", headers_arg))
        return SimpleNamespace(
            status_code=200,
            text="{}",
            json=lambda: {"data": {"currentUser": {"dropCampaigns": []}}},
            raise_for_status=lambda: None,
        )

    login = SimpleNamespace(
        client_id=ANDROID_CLIENT_ID,
        get_auth_token=lambda: "token",
        get_user_id=lambda: "1",
    )
    session = ClientSession(
        login=login,
        user_agent="agent",
        version="version",
        device_id="device",
        session_id="session",
    )
    gql = GQL(session, post_request=lambda url, json, headers: post(url, json, headers))

    gql.post_gql_request_raw(
        "ViewerDropsDashboard", copy.deepcopy(GQLOperations.ViewerDropsDashboard)
    )

    assert headers[0]["Client-Id"] == ANDROID_CLIENT_ID


def write_cookie(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            [
                {"name": "auth-token", "value": TOKEN},
                {"name": "persistent", "value": "42"},
            ]
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize("android", [True, False], ids=["android", "tv"])
def test_saved_android_login_is_announced_without_the_token(
    tmp_path, monkeypatch, caplog, android
):
    monkeypatch.chdir(tmp_path)
    twitch = Twitch("someone", "agent", gql_factory=MagicMock(), android_login=android)
    monkeypatch.setattr(Twitch, "update_client_version", lambda self: None)
    write_cookie(tmp_path / "cookies" / Path(twitch.cookies_file).name)

    with caplog.at_level(logging.INFO):
        twitch.login()

    announced = [r for r in caplog.records if "saved Android app login" in r.msg]
    assert len(announced) == (1 if android else 0)
    if android:
        assert announced[0].default_color == ANDROID_LOGIN_COLOR
        assert "someone-android.pkl" in announced[0].getMessage()
    assert TOKEN not in caplog.text


def test_startup_reports_the_android_login_after_loggers_are_ready(
    tmp_path, monkeypatch, capsys
):
    # Logged before configure_loggers, the line had no handler and was lost.
    monkeypatch.chdir(tmp_path)
    miner_module = importlib.import_module(
        "TwitchChannelPointsMiner.TwitchChannelPointsMiner"
    )
    root_logger = logging.getLogger()
    existing_handlers = set(root_logger.handlers)
    existing_level = root_logger.level
    signals = (signal.SIGINT, signal.SIGSEGV, signal.SIGTERM)
    existing_signal_handlers = {sign: signal.getsignal(sign) for sign in signals}

    try:
        miner = miner_module.TwitchChannelPointsMiner(
            "someone",
            android_login=True,
            update_check=False,
            logger_settings=LoggerSettings(save=False),
        )
        miner.queue_listener.stop()
        for handler in miner.queue_listener.handlers:
            handler.close()
    finally:
        # The miner configures the root logger and installs signal handlers;
        # undo both so later tests see the defaults.
        for handler in set(root_logger.handlers) - existing_handlers:
            root_logger.removeHandler(handler)
            handler.close()
        root_logger.setLevel(existing_level)
        for sign, handler in existing_signal_handlers.items():
            signal.signal(sign, handler)

    output = capsys.readouterr()
    assert "Using the Android app login" in output.out + output.err
