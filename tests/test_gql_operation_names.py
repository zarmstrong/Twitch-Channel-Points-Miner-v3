"""Every hand-written GraphQL payload must name the operation it sends.

Twitch rejects a request whose ``operationName`` doesn't match a named
operation in its ``query`` text ("operation with name '...' not found"), and
the miner only logs that at DEBUG level (issue #296).
"""

import re
from types import SimpleNamespace

import pytest

from TwitchChannelPointsMiner.classes.Twitch import Twitch
from TwitchChannelPointsMiner.constants import GQLOperations

_OPERATION_RE = re.compile(
    r"(?:^|[^_0-9A-Za-z])(?:query|mutation|subscription)\s+([_A-Za-z][_0-9A-Za-z]*)"
)


def named_operations(query_text):
    return set(_OPERATION_RE.findall(query_text))


def constant_payloads():
    for name, value in vars(GQLOperations).items():
        if isinstance(value, dict) and "operationName" in value and "query" in value:
            yield pytest.param(value["operationName"], value["query"], id=name)
    for name, query_text in GQLOperations.raw_queries.items():
        # The persisted-hash fallback sends this text under ``name``.
        yield pytest.param(name, query_text, id=f"raw_queries.{name}")


def test_named_operations_helper_rejects_anonymous_queries():
    assert named_operations("query{currentUser{id}}") == set()
    assert named_operations("query Foo{currentUser{id}}") == {"Foo"}
    assert named_operations("mutation Bar($x: ID!) { a }") == {"Bar"}


@pytest.mark.parametrize("operation_name,query_text", list(constant_payloads()))
def test_constant_payload_names_its_operation(operation_name, query_text):
    assert operation_name in named_operations(query_text)


def bare_twitch(response):
    twitch = object.__new__(Twitch)
    twitch.log_drop_checks = False
    twitch.available_badge_names = None
    sent = []

    def post_gql_request_raw(operation_name, payload):
        sent.append((operation_name, payload))
        return response

    twitch.gql = SimpleNamespace(post_gql_request_raw=post_gql_request_raw)
    return twitch, sent


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(
            lambda twitch: twitch._Twitch__get_reward_campaigns_raw_query(),
            id="MinerRewardCampaignsRaw",
        ),
        pytest.param(
            lambda twitch: twitch._Twitch__get_available_badge_names(),
            id="AvailableBadges",
        ),
    ],
)
def test_sent_raw_payload_names_its_operation(call):
    twitch, sent = bare_twitch({"data": {}})

    call(twitch)

    assert sent
    for operation_name, payload in sent:
        assert payload["operationName"] == operation_name
        assert operation_name in named_operations(payload["query"])


def test_reward_campaigns_raw_query_returns_tagged_campaigns():
    campaigns = [
        {"id": "reward-1", "name": "Sub reward", "game": None},
        {"id": "reward-2", "name": "Game reward", "game": {"name": "Example"}},
    ]
    twitch, _ = bare_twitch({"data": {"rewardCampaignsAvailableToUser": campaigns}})

    result, debug = twitch._Twitch__get_reward_campaigns_raw_query()

    assert [campaign["id"] for campaign in result] == ["reward-1", "reward-2"]
    assert all(campaign["_is_reward_campaign"] is True for campaign in result)
    assert debug["total_unique"] == 2
    assert debug["variants"][0]["errors"] == []


def test_reward_campaigns_raw_query_records_errors():
    error = {"message": "operation with name 'MinerRewardCampaignsRaw' not found"}
    twitch, _ = bare_twitch({"errors": [error]})

    result, debug = twitch._Twitch__get_reward_campaigns_raw_query()

    assert result == []
    assert debug["variants"][0]["errors"] == [error]
