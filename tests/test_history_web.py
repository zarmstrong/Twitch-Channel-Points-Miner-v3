import sqlite3
import time

import pytest
from flask import Flask

from TwitchChannelPointsMiner.history import web as history_web
from TwitchChannelPointsMiner.history.store import HistoryStore

NOW = time.time()


@pytest.fixture
def store(tmp_path):
    instance = HistoryStore(tmp_path / "history.sqlite3")
    instance.initialize()
    instance.record_points("alice", NOW, 100, 40, "WATCH")
    instance.record_event("alice", NOW + 60, "watch_streak", "+50 - WATCH_STREAK", 50)
    instance.start_watch_session("alice", "points", NOW)
    instance.upsert_stream_session("alice", "b1", NOW)
    return instance


@pytest.fixture
def client(monkeypatch, store):
    monkeypatch.setattr(history_web, "get_store", lambda: store)
    app = Flask(__name__)
    history_web.register_history(app)
    return app.test_client()


def test_history_endpoints_return_data(client):
    response = client.get("/history/overview?days=30")
    assert response.status_code == 200
    assert response.get_json()["window"]["points_gained"] == 40

    response = client.get("/history/streamers?days=30")
    assert response.status_code == 200
    assert response.get_json()["streamers"][0]["streamer"] == "alice"

    response = client.get("/history/daily?days=7&streamer=alice")
    assert response.status_code == 200
    assert len(response.get_json()["daily"]) == 7

    response = client.get("/history/streamer/alice?days=30")
    assert response.status_code == 200
    assert response.get_json()["streamer"] == "alice"

    response = client.get("/history/sessions?kind=watch&days=30")
    assert response.status_code == 200
    assert response.get_json()["total"] == 1

    response = client.get("/history/sessions?kind=stream&days=30")
    assert response.status_code == 200
    assert response.get_json()["sessions"][0]["active"] is True

    response = client.get("/history/events?days=30")
    assert response.status_code == 200
    assert response.get_json()["events"][0]["type"] == "watch_streak"

    response = client.get("/history/activity?days=7")
    assert response.status_code == 200
    assert len(response.get_json()["day_list"]) == 7


def test_history_endpoints_return_503_until_recorder_is_ready(monkeypatch):
    monkeypatch.setattr(history_web, "get_store", lambda: None)
    app = Flask(__name__)
    history_web.register_history(app)
    response = app.test_client().get("/history/overview")
    assert response.status_code == 503
    assert "not available" in response.get_json()["error"]


def test_history_rejects_invalid_arguments(client):
    response = client.get("/history/overview?days=not-a-number")
    assert response.status_code == 400

    response = client.get("/history/sessions?kind=invalid")
    assert response.status_code == 400


def test_history_delete_streamer(client):
    response = client.delete("/history/streamer/alice")
    assert response.status_code == 200
    assert response.get_json()["deleted"]["points"] == 1

    response = client.get("/history/streamers?days=30")
    assert response.get_json()["streamers"] == []


def test_history_returns_503_when_the_database_fails(client, monkeypatch, store):
    def fail(**kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "overview", fail)
    response = client.get("/history/overview?days=30")
    assert response.status_code == 503
    assert "temporarily unavailable" in response.get_json()["error"]
