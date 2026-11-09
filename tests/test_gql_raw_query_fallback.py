import copy
import logging
from types import SimpleNamespace

import pytest
import requests

from TwitchChannelPointsMiner.classes.ClientSession import ClientSession
from TwitchChannelPointsMiner.classes.gql.Errors import (
    RetryError,
    is_persisted_query_not_found_message,
)
from TwitchChannelPointsMiner.classes.gql.Integration import GQL
from TwitchChannelPointsMiner.constants import GQLOperations
from TwitchChannelPointsMiner.utils import AttemptStrategy


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError(str(self.status_code))
            error.response = self
            raise error

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


def scripted_post(responses):
    """Returns a post function that replies with `responses` in order and records each request."""
    calls = []
    queue = list(responses)

    def post(url, json, headers):
        calls.append(copy.deepcopy(json))
        return FakeResponse(queue.pop(0))

    return post, calls


def persisted_not_found():
    return {"errors": [{"message": "PersistedQueryNotFound"}]}


def dashboard_request():
    return copy.deepcopy(GQLOperations.ViewerDropsDashboard)


DASHBOARD_DATA = {"data": {"currentUser": {"dropCampaigns": []}}}


def test_persisted_query_not_found_resends_raw_query_and_returns_data(caplog):
    post, calls = scripted_post([persisted_not_found(), DASHBOARD_DATA])
    gql = GQL(client_session(), post_request=post)

    with caplog.at_level(logging.WARNING):
        response = gql.post_gql_request_raw("ViewerDropsDashboard", dashboard_request())

    assert response == DASHBOARD_DATA
    assert len(calls) == 2
    assert "extensions" in calls[0]
    assert "extensions" not in calls[1]
    assert calls[1]["query"] == GQLOperations.raw_queries["ViewerDropsDashboard"]
    assert calls[1]["operationName"] == "ViewerDropsDashboard"
    assert caplog.text.count("Persisted query hash for ViewerDropsDashboard") == 1


def test_persisted_query_not_found_match_ignores_case_and_position(caplog):
    post, calls = scripted_post(
        [
            {"errors": [{"message": "persistedQueryNotFound: unknown hash"}]},
            DASHBOARD_DATA,
        ]
    )
    gql = GQL(client_session(), post_request=post)

    with caplog.at_level(logging.WARNING):
        response = gql.post_gql_request_raw("ViewerDropsDashboard", dashboard_request())

    assert response == DASHBOARD_DATA
    assert len(calls) == 2
    assert "extensions" in calls[0]
    assert "extensions" not in calls[1]
    assert calls[1]["query"] == GQLOperations.raw_queries["ViewerDropsDashboard"]
    assert calls[1]["operationName"] == "ViewerDropsDashboard"
    assert caplog.text.count("Persisted query hash for ViewerDropsDashboard") == 1


def test_persisted_query_not_found_message_helper():
    assert is_persisted_query_not_found_message("PersistedQueryNotFound") is True
    assert is_persisted_query_not_found_message("persistedQueryNotFound: x") is True
    assert is_persisted_query_not_found_message("service timeout") is False
    assert is_persisted_query_not_found_message(None) is False
    assert is_persisted_query_not_found_message(123) is False


def test_rejected_operation_is_sent_raw_first_on_later_calls(caplog):
    post, calls = scripted_post([persisted_not_found(), DASHBOARD_DATA, DASHBOARD_DATA])
    gql = GQL(client_session(), post_request=post)

    with caplog.at_level(logging.WARNING):
        gql.post_gql_request_raw("ViewerDropsDashboard", dashboard_request())
        response = gql.post_gql_request_raw("ViewerDropsDashboard", dashboard_request())

    assert response == DASHBOARD_DATA
    # Second call goes straight to the raw query: one request, no persisted attempt
    assert len(calls) == 3
    assert "extensions" not in calls[2]
    assert calls[2]["query"] == GQLOperations.raw_queries["ViewerDropsDashboard"]
    # The warning is logged once per operation, not on every call
    assert caplog.text.count("Persisted query hash for ViewerDropsDashboard") == 1


def test_operation_without_raw_query_keeps_previous_error_handling(caplog):
    # GetIDFromLogin has no raw query: the rejection is returned as before
    # and surfaces as a non-retried RetryError after a single request
    post, calls = scripted_post([persisted_not_found()])
    gql = GQL(
        client_session(),
        attempt_strategy=AttemptStrategy(attempts=3, attempt_interval_seconds=0),
        post_request=post,
    )

    with caplog.at_level(logging.WARNING):
        with pytest.raises(RetryError):
            gql.get_id_from_login("example")

    assert len(calls) == 1
    assert "extensions" in calls[0]
    assert "no raw query is available" in caplog.text
    assert "GetIDFromLogin" in caplog.text
    assert gql.raw_query_operations == set()


def test_raw_resend_of_mutation_after_persisted_query_rejection():
    # PersistedQueryNotFound means Twitch did not run the mutation, so the raw
    # resend cannot claim the same drop twice. Only the rejected request is
    # repeated, and only once.
    claim = {
        "operationName": "DropsPage_ClaimDropRewards",
        "variables": {"input": {"dropInstanceID": "drop-1"}},
        "extensions": copy.deepcopy(
            GQLOperations.DropsPage_ClaimDropRewards["extensions"]
        ),
    }
    claimed = {"data": {"claimDropRewards": {"status": "ELIGIBLE_FOR_ALL"}}}
    post, calls = scripted_post([persisted_not_found(), claimed])
    gql = GQL(client_session(), post_request=post)

    response = gql.post_gql_request_raw("DropsPage_ClaimDropRewards", claim)

    assert response == claimed
    assert len(calls) == 2
    assert calls[1]["query"].startswith("mutation DropsPage_ClaimDropRewards")
    assert "extensions" not in calls[1]
    assert calls[1]["variables"] == {"input": {"dropInstanceID": "drop-1"}}


def test_batch_with_raw_queries_resends_whole_batch_raw():
    batch = [
        dashboard_request(),
        copy.deepcopy(GQLOperations.ModViewChannelQuery),
    ]
    batch[1]["variables"] = {"channelLogin": "example"}
    batch_data = [DASHBOARD_DATA, {"data": {"user": None}}]
    post, calls = scripted_post([persisted_not_found(), batch_data])
    gql = GQL(client_session(), post_request=post)

    response = gql.post_gql_request_batch_raw("ViewerDropsDashboard", batch)

    assert response == batch_data
    assert len(calls) == 2
    assert all("extensions" not in item for item in calls[1])
    assert [item["query"] for item in calls[1]] == [
        GQLOperations.raw_queries["ViewerDropsDashboard"],
        GQLOperations.raw_queries["ModViewChannelQuery"],
    ]
