from types import SimpleNamespace

import pytest
import requests

from TwitchChannelPointsMiner.classes.ClientSession import ClientSession
from TwitchChannelPointsMiner.classes.gql.Errors import RetryError
from TwitchChannelPointsMiner.classes.gql.Integration import GQL
from TwitchChannelPointsMiner.classes.Twitch import Twitch
from TwitchChannelPointsMiner.utils import AttemptStrategy


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

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


def test_get_id_from_login_parses_typed_response_and_session_headers():
    calls = []

    def post(url, json, headers):
        calls.append((url, json, headers))
        return FakeResponse(
            {
                "data": {"user": {"id": "123"}},
                "extensions": {"operationName": "GetIDFromLogin"},
            }
        )

    response = GQL(client_session(), post_request=post).get_id_from_login("example")

    assert response.id == "123"
    assert calls[0][2]["Client-Session-Id"] == "test-session"
    assert calls[0][2]["Client-Version"] == "test-version"


def test_retries_transport_errors_then_returns_typed_response():
    attempts = 0

    def post(url, json, headers):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise requests.ConnectionError("temporary")
        return FakeResponse(
            {
                "data": {"user": {"id": "123"}},
                "extensions": {"operationName": "GetIDFromLogin"},
            }
        )

    gql = GQL(
        client_session(),
        attempt_strategy=AttemptStrategy(attempts=2, attempt_interval_seconds=0),
        post_request=post,
    )

    assert gql.get_id_from_login("example").id == "123"
    assert attempts == 2


def test_exhausted_transport_retries_raise_retry_error():
    def post(url, json, headers):
        raise requests.ConnectionError("offline")

    gql = GQL(
        client_session(),
        attempt_strategy=AttemptStrategy(attempts=2, attempt_interval_seconds=0),
        post_request=post,
    )

    with pytest.raises(RetryError) as error:
        gql.get_id_from_login("example")

    assert len(error.value.errors) == 2


def twitch_with_gql(gql):
    twitch = Twitch.__new__(Twitch)
    twitch.gql = gql
    return twitch


def test_load_channel_points_context_adapts_typed_response(monkeypatch):
    claim = SimpleNamespace(id="claim-1")
    points = SimpleNamespace(
        balance=120,
        active_multipliers=[SimpleNamespace(factor=1.2)],
        available_claim=claim,
    )
    goal = SimpleNamespace(
        id="goal-1",
        title="Goal",
        is_in_stock=True,
        points_contributed=10,
        amount_needed=100,
        per_stream_user_maximum_contribution=20,
        status="STARTED",
    )
    channel = SimpleNamespace(
        edge=SimpleNamespace(community_points=points),
        community_points_settings=SimpleNamespace(goals=[goal]),
    )
    gql = SimpleNamespace(
        get_channel_points_context=lambda username: SimpleNamespace(
            community=SimpleNamespace(channel=channel)
        )
    )
    twitch = twitch_with_gql(gql)
    claimed = []
    contributed = []
    monkeypatch.setattr(
        Twitch,
        "claim_bonus",
        lambda self, streamer, claim_id: claimed.append(claim_id),
    )
    monkeypatch.setattr(
        Twitch,
        "contribute_to_community_goals",
        lambda self, streamer: contributed.append(streamer),
    )
    streamer = SimpleNamespace(
        username="example",
        channel_points=0,
        activeMultipliers=None,
        community_goals={},
        settings=SimpleNamespace(community_goals=True),
    )

    twitch.load_channel_points_context(streamer)

    assert streamer.channel_points == 120
    assert streamer.activeMultipliers == [{"factor": 1.2}]
    assert streamer.community_goals["goal-1"].title == "Goal"
    assert claimed == ["claim-1"]
    assert contributed == [streamer]


def test_get_followers_keeps_legacy_empty_list_on_retry_failure():
    retry_error = RetryError("ChannelFollows", [])

    def fail(limit, order):
        raise retry_error

    twitch = twitch_with_gql(SimpleNamespace(channel_follows=fail))

    assert twitch.get_followers() == []


def test_get_stream_info_adapts_typed_game_and_tags_for_stream_entity():
    typed_game = SimpleNamespace(
        id="game-1", display_name="Example Game", name="Example Game"
    )
    typed_stream = SimpleNamespace(
        id="broadcast-1",
        viewers_count=42,
        tags=[SimpleNamespace(id="tag-1", localized_name="Drops Enabled")],
    )
    typed_user = SimpleNamespace(
        stream=typed_stream,
        broadcast_settings=SimpleNamespace(title="Example", game=typed_game),
    )
    twitch = twitch_with_gql(
        SimpleNamespace(
            video_player_stream_info_overlay_channel=lambda username: SimpleNamespace(
                user=typed_user
            )
        )
    )

    stream_info = twitch.get_stream_info(SimpleNamespace(username="example"))

    assert stream_info["broadcastSettings"]["game"] == {
        "id": "game-1",
        "displayName": "Example Game",
        "name": "Example Game",
    }
    assert stream_info["stream"]["tags"] == [
        {"id": "tag-1", "localizedName": "Drops Enabled"}
    ]


def test_channel_points_context_accepts_current_query_shape_without_user_ids():
    payload = {
        "data": {
            "community": {
                "channel": {
                    "self": {
                        "communityPoints": {
                            "balance": 100,
                            "activeMultipliers": [],
                            "availableClaim": None,
                        }
                    },
                    "communityPointsSettings": {"goals": []},
                }
            }
        },
        "extensions": {"operationName": "ChannelPointsContext"},
    }
    gql = GQL(client_session(), post_request=lambda *args, **kwargs: FakeResponse(payload))

    response = gql.get_channel_points_context("example")

    assert response.community.id is None
    assert response.community.display_name is None
    assert response.community.channel.id is None
    assert response.community.channel.edge.community_points.balance == 100


def test_claim_drop_uses_typed_claim_status(monkeypatch):
    drop = SimpleNamespace(
        drop_instance_id="instance-1",
        id="drop-1",
        dt_match=True,
        is_claimed=False,
    )
    gql = SimpleNamespace(
        claim_drop_rewards=lambda drop_instance_id: SimpleNamespace(
            status="ELIGIBLE_FOR_ALL", errors=[]
        )
    )
    twitch = twitch_with_gql(gql)
    twitch.completed_drop_campaigns = set()
    saved = []
    monkeypatch.setattr(
        Twitch, "_Twitch__drop_variant_entries_from_drop", lambda self, drop: [{}]
    )
    monkeypatch.setattr(
        Twitch,
        "_Twitch__save_drop_claim_analytics",
        lambda self, drop, **kwargs: saved.append(drop.id),
    )

    assert twitch.claim_drop(drop) is True
    assert saved == ["drop-1"]


def test_claim_drop_returns_false_for_typed_errors():
    drop = SimpleNamespace(drop_instance_id="instance-1")
    twitch = twitch_with_gql(
        SimpleNamespace(
            claim_drop_rewards=lambda drop_instance_id: SimpleNamespace(
                status=None, errors=["not eligible"]
            )
        )
    )

    assert twitch.claim_drop(drop) is False


def test_get_broadcast_id_uses_typed_stream_info(monkeypatch):
    twitch = twitch_with_gql(SimpleNamespace())
    monkeypatch.setattr(
        Twitch,
        "get_stream_info",
        lambda self, streamer: {"stream": {"id": "broadcast-1"}},
    )

    assert twitch.get_broadcast_id(SimpleNamespace(username="example")) == "broadcast-1"


def test_inventory_typed_response_retains_richer_raw_payload():
    inventory = {
        "dropCampaignsInProgress": None,
        "gameEventDrops": [{"id": "reward-1", "name": "Reward"}],
        "completedRewardCampaigns": [{"id": "campaign-1"}],
    }
    payload = {
        "data": {"currentUser": {"inventory": inventory}},
        "extensions": {"operationName": "Inventory"},
    }
    gql = GQL(client_session(), post_request=lambda *args, **kwargs: FakeResponse(payload))

    response = gql.get_inventory()

    assert response.campaigns is None
    assert response.inventory == inventory


def test_inventory_typed_parsing_does_not_replace_raw_campaign_dicts():
    campaign = {
        "id": "campaign-1",
        "name": "Campaign",
        "game": {"id": "game-1", "displayName": "Game"},
        "timeBasedDrops": [
            {
                "id": "drop-1",
                "name": "Reward",
                "endAt": "2099-01-01T00:00:00Z",
                "startAt": "2020-01-01T00:00:00Z",
                "benefitEdges": [{"benefit": {"name": "Badge"}}],
                "requiredMinutesWatched": 10,
                "requiredSubs": 0,
                "self": {
                    "hasPreconditionsMet": True,
                    "currentMinutesWatched": 5,
                    "currentSubs": 0,
                    "dropInstanceID": "instance-1",
                    "isClaimed": False,
                },
            }
        ],
    }
    payload = {
        "data": {
            "currentUser": {
                "inventory": {"dropCampaignsInProgress": [campaign]}
            }
        },
        "extensions": {"operationName": "Inventory"},
    }
    gql = GQL(client_session(), post_request=lambda *args, **kwargs: FakeResponse(payload))

    response = gql.get_inventory()

    assert response.campaigns[0].id == "campaign-1"
    assert isinstance(response.inventory["dropCampaignsInProgress"][0], dict)
    assert response.inventory["dropCampaignsInProgress"][0]["game"]["displayName"] == "Game"
    assert isinstance(
        payload["data"]["currentUser"]["inventory"]["dropCampaignsInProgress"][0],
        dict,
    )


def test_inventory_uses_partial_data_when_optional_field_returns_gql_error():
    inventory = {
        "dropCampaignsInProgress": None,
        "gameEventDrops": None,
        "completedRewardCampaigns": [],
    }
    payload = {
        "data": {"currentUser": {"inventory": inventory}},
        "errors": [
            {
                "message": "service error",
                "path": ["currentUser", "inventory", "gameEventDrops"],
            }
        ],
        "extensions": {"operationName": "Inventory"},
    }
    gql = GQL(client_session(), post_request=lambda *args, **kwargs: FakeResponse(payload))

    response = gql.get_inventory()

    assert response.inventory == inventory
    assert len(response.errors) == 1
    assert response.errors[0].message == "service error"


def test_dashboard_typed_response_retains_full_raw_payload():
    payload = {
        "data": {
            "currentUser": {
                "dropCampaigns": None,
                "rewardCampaigns": [{"id": "campaign-1", "status": "ACTIVE"}],
            },
            "rewardCampaignsAvailableToUser": [{"id": "campaign-2"}],
        },
        "extensions": {"operationName": "ViewerDropsDashboard"},
    }
    gql = GQL(client_session(), post_request=lambda *args, **kwargs: FakeResponse(payload))

    response = gql.get_viewer_drops_dashboard()

    assert response.campaigns is None
    assert response.raw_response == payload


def test_mod_view_channel_parses_moderator_status():
    payload = {
        "data": {"user": {"self": {"isModerator": True}}},
        "extensions": {"operationName": "ModViewChannelQuery"},
    }
    gql = GQL(client_session(), post_request=lambda *args, **kwargs: FakeResponse(payload))

    assert gql.mod_view_channel("example").is_moderator is True
