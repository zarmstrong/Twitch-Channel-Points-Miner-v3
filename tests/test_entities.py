import base64
import json

import pytest

from TwitchChannelPointsMiner.classes.entities.Drop import Drop, parse_datetime
from TwitchChannelPointsMiner.classes.entities.Message import Message
from TwitchChannelPointsMiner.classes.entities.PubsubTopic import PubsubTopic
from TwitchChannelPointsMiner.classes.entities.Stream import Stream


def drop_data():
    return {
        "id": "drop-1",
        "name": "Reward",
        "benefitEdges": [
            {"benefit": {"name": "Badge", "imageAssetURL": "https://image.test/a.png"}}
        ],
        "requiredMinutesWatched": 100,
        "startAt": "2020-01-01T00:00:00Z",
        "endAt": "2099-01-01T00:00:00.000Z",
    }


def test_drop_parses_benefit_and_updates_progress():
    drop = Drop(drop_data())

    drop.update(
        {
            "hasPreconditionsMet": True,
            "currentMinutesWatched": 1,
            "dropInstanceID": "instance-1",
            "isClaimed": False,
        }
    )

    assert drop.benefit == "Badge"
    assert drop.item_art_url == "https://image.test/a.png"
    assert drop.percentage_progress == 1
    assert drop.is_printable is True
    assert drop.is_claimable is True


@pytest.mark.parametrize(
    "benefit_edges",
    [None, [], [{}], [{"benefit": None}], [{"benefit": {}}]],
)
def test_drop_accepts_missing_or_incomplete_benefits(benefit_edges):
    data = drop_data()
    data["benefitEdges"] = benefit_edges

    drop = Drop(data)

    expected_edges = [] if benefit_edges is None else benefit_edges
    assert drop.benefit_edges == expected_edges
    assert drop.benefit == ""
    assert drop.item_art_url is None


def test_drop_accepts_missing_benefit_edges():
    data = drop_data()
    del data["benefitEdges"]

    drop = Drop(data)

    assert drop.benefit_edges == []
    assert drop.benefit == ""
    assert drop.item_art_url is None


def test_drop_becomes_unclaimable_after_claim():
    drop = Drop(drop_data())
    drop.update(
        {
            "hasPreconditionsMet": True,
            "currentMinutesWatched": 100,
            "dropInstanceID": "instance-1",
            "isClaimed": True,
        }
    )

    assert drop.is_claimed is True
    assert drop.is_claimable is False


def test_parse_datetime_accepts_seconds_and_fractional_seconds():
    assert parse_datetime("2024-01-02T03:04:05Z").microsecond == 0
    assert parse_datetime("2024-01-02T03:04:05.123Z").microsecond == 123000


def test_parse_datetime_rejects_unknown_format():
    with pytest.raises(ValueError, match="does not match format"):
        parse_datetime("January 2, 2024")


def test_stream_update_normalizes_title_and_detects_drop_tag(monkeypatch):
    monkeypatch.setattr("TwitchChannelPointsMiner.classes.entities.Stream.DROP_ID", "drops")
    stream = Stream()

    stream.update(
        "broadcast-1",
        "  A stream  ",
        {"id": "game-1", "name": "Game", "displayName": "Game"},
        [{"id": "drops", "localizedName": "Drops Enabled"}],
        42,
    )

    assert stream.title == "A stream"
    assert stream.game_name() == "Game"
    assert stream.game_id() == "game-1"
    assert stream.drops_tags is True
    assert stream.update_required() is False


def test_stream_payload_is_compact_base64_encoded_json():
    stream = Stream()
    stream.payload = {"event": "minute-watched", "value": 1}

    encoded = stream.encode_payload()["data"]

    assert json.loads(base64.b64decode(encoded)) == stream.payload


def test_message_prefers_nested_prediction_channel_and_timestamp():
    message = Message(
        {
            "topic": "predictions-channel-v1.100",
            "message": json.dumps(
                {
                    "type": "event-created",
                    "data": {
                        "timestamp": "2024-01-02T03:04:05Z",
                        "prediction": {"channel_id": "200"},
                    },
                }
            ),
        }
    )

    assert message.channel_id == "200"
    assert message.timestamp == "2024-01-02T03:04:05Z"
    assert message.identifier == "event-created.predictions-channel-v1.200"


def test_pubsub_topic_uses_user_or_streamer_channel():
    assert str(PubsubTopic("user-topic", user_id="100")) == "user-topic.100"
    streamer = type("Streamer", (), {"channel_id": "200"})()
    assert str(PubsubTopic("channel-topic", streamer=streamer)) == "channel-topic.200"
