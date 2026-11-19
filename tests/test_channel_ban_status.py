from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import requests

from TwitchChannelPointsMiner.classes.ClientSession import ClientSession
from TwitchChannelPointsMiner.classes.entities.ChannelBanStatus import (
    ChannelBanStatus,
)
from TwitchChannelPointsMiner.classes.gql.Integration import (
    CHANNEL_BAN_BATCH_SIZE,
    GQL,
    parse_channel_ban_status,
)

EXPIRES = "2026-11-17T23:00:00Z"
EXPIRES_AT = datetime(2026, 11, 17, 23, 0, tzinfo=timezone.utc).timestamp()


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)

    def json(self):
        return self.payload


def client_session():
    login = SimpleNamespace(get_auth_token=lambda: "token", get_user_id=lambda: "1")
    return ClientSession(
        login=login,
        user_agent="test-agent",
        version="test-version",
        device_id="test-device",
        session_id="test-session",
    )


def answer(channel_id, ban_status):
    return {"data": {"user": {"id": channel_id, "self": {"banStatus": ban_status}}}}


def test_a_null_ban_status_means_not_banned():
    status = parse_channel_ban_status(answer("1", None), "1")

    assert status.banned is False
    assert status.active(0) is False


def test_a_permanent_ban_is_active():
    status = parse_channel_ban_status(
        answer("1", {"isPermanent": True, "expiresAt": None}), "1"
    )

    assert (status.banned, status.permanent, status.expires_at) == (True, True, None)
    assert status.active(EXPIRES_AT + 10**9) is True


def test_a_timeout_is_active_only_until_it_expires():
    status = parse_channel_ban_status(
        answer("1", {"isPermanent": False, "expiresAt": EXPIRES}), "1"
    )

    assert (status.banned, status.permanent) == (True, False)
    assert status.expires_at == EXPIRES_AT
    assert status.active(EXPIRES_AT - 1) is True
    assert status.active(EXPIRES_AT) is False


@pytest.mark.parametrize(
    "expires_at, expected",
    [
        ("2026-11-17T23:00:00.123456Z", EXPIRES_AT + 0.123456),
        ("2026-11-17T23:00:00", EXPIRES_AT),
        ("2026-11-17T23:00:00+00:00", EXPIRES_AT),
        ("not a time", None),
        (12345, None),
    ],
)
def test_timeout_end_times_are_read_as_utc(expires_at, expected):
    status = parse_channel_ban_status(
        answer("1", {"isPermanent": False, "expiresAt": expires_at}), "1"
    )

    assert status.banned is True
    if expected is None:
        # Banned with no known end: stays active until checked again.
        assert status.expires_at is None
        assert status.active(10**12) is True
    else:
        assert status.expires_at == pytest.approx(expected)


@pytest.mark.parametrize(
    "response",
    [
        {"errors": [{"message": "service error"}], "data": None},
        {
            "errors": [{"message": "partial"}],
            "data": {"user": {"id": "1", "self": {"banStatus": None}}},
        },
        {"data": {"user": None}},
        {"data": {"user": {"id": "1", "self": None}}},
        {"data": {"user": {"id": "1", "self": {}}}},
        {"data": {"user": {"id": "1"}}},
        {"data": {"user": {"id": "2", "self": {"banStatus": None}}}},
        {"data": {"user": {"self": {"banStatus": None}}}},
        {"data": {"user": {"id": "1", "self": {"banStatus": "yes"}}}},
        {"data": None},
        {"data": []},
        {},
        [],
        None,
    ],
    ids=[
        "error",
        "error with data",
        "no user",
        "no self",
        "self without banStatus",
        "user without self",
        "another user",
        "user without id",
        "banStatus not an object",
        "no data",
        "data not an object",
        "empty",
        "list",
        "none",
    ],
)
def test_an_unclear_answer_is_unknown_not_banned(response):
    assert parse_channel_ban_status(response, "1") is None


def test_channel_ban_statuses_batches_below_the_twitch_limit():
    calls = []

    def post(url, json, headers):
        calls.append(json)
        assert len(json) <= 35
        return FakeResponse(
            [answer(item["variables"]["channelID"], None) for item in json]
        )

    ids = [str(n) for n in range(1, 2 * CHANNEL_BAN_BATCH_SIZE + 6)]
    statuses = GQL(client_session(), post_request=post).channel_ban_statuses(ids)

    assert [len(chunk) for chunk in calls] == [30, 30, 5]
    assert all(
        item["operationName"] == "ChannelBanStatus" and "query" in item
        for chunk in calls
        for item in chunk
    )
    assert set(statuses) == set(ids)
    assert not any(status.banned for status in statuses.values())


def test_channel_ban_statuses_keeps_only_clear_answers():
    def post(url, json, headers):
        return FakeResponse(
            [
                answer("1", {"isPermanent": True, "expiresAt": None}),
                {"errors": [{"message": "nope"}], "data": None},
                answer("3", None),
            ]
        )

    statuses = GQL(client_session(), post_request=post).channel_ban_statuses(
        ["1", "2", "3"]
    )

    assert set(statuses) == {"1", "3"}
    assert statuses["1"].permanent is True
    assert statuses["3"].banned is False


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(
            {
                "errors": [
                    {
                        "message": "Invalid GraphQL request",
                        "extensions": {"code": "BATCH_LIMIT_EXCEEDED"},
                    }
                ]
            }
        ),
        FakeResponse({}, status_code=503),
        FakeResponse([answer("1", None)]),
    ],
    ids=["batch rejected", "server error", "wrong number of answers"],
)
def test_a_failed_request_is_tried_once_and_stops_the_remaining_chunks(response):
    calls = []

    def post(url, json, headers):
        calls.append(json)
        return response

    ids = [str(n) for n in range(1, CHANNEL_BAN_BATCH_SIZE + 3)]
    statuses = GQL(client_session(), post_request=post).channel_ban_statuses(ids)

    assert statuses == {}
    assert len(calls) == 1


def test_same_ban_compares_every_field():
    ban = ChannelBanStatus(True, expires_at=1.0)

    assert ban.same_ban(ChannelBanStatus(True, expires_at=1.0))
    assert not ban.same_ban(ChannelBanStatus(True, expires_at=2.0))
    assert not ban.same_ban(ChannelBanStatus(True, permanent=True))
    assert not ban.same_ban(None)
