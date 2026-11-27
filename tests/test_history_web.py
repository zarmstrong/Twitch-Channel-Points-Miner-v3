import sqlite3
import time
from types import SimpleNamespace

import pytest
from flask import Flask

from TwitchChannelPointsMiner.history import web as history_web
from TwitchChannelPointsMiner.history.importer import JournalImporter
from TwitchChannelPointsMiner.history.journal import Journal
from TwitchChannelPointsMiner.history.store import HistoryStore

NOW = time.time()


def _alice():
    return SimpleNamespace(
        username="alice",
        is_online=True,
        explicitly_configured=True,
        from_followers=False,
        from_category=False,
        from_badge_campaign=False,
        from_wildcard_category=False,
        stream=SimpleNamespace(broadcast_id="b1", game={"name": "Game"}, title="T"),
    )


@pytest.fixture
def store(tmp_path):
    folder = str(tmp_path / "history")
    clock = {"wall": NOW, "mono": 0.0}
    journal = Journal(clock=lambda: clock["wall"], monotonic=lambda: clock["mono"])
    journal.start(folder)
    alice = _alice()
    journal.record_live(alice)
    journal.watch_selection({"alice": (alice, "points")})
    clock["wall"] += 20
    clock["mono"] += 20
    journal.watch_attempt(alice, True)
    journal.record_points_message(
        alice,
        "points-earned",
        {
            "timestamp": "2026-11-23T07:01:25Z",
            "point_gain": {"total_points": 40, "reason_code": "WATCH"},
            "balance": {"balance": 100},
        },
    )
    journal.record_event(alice, "WIN", "A prediction", 50)
    instance = HistoryStore(tmp_path / "history.sqlite3")
    instance.initialize()
    instance.active_run = journal.run
    JournalImporter(instance, folder).poll()
    return instance


@pytest.fixture
def client(monkeypatch, store):
    monkeypatch.setattr(history_web, "get_store", lambda: store)
    app = Flask(__name__)
    history_web.register_history(app)
    return app.test_client()


def test_history_endpoints_return_data(client):
    response = client.get("/history/overview?days=3650")
    assert response.status_code == 200
    assert response.get_json()["lifetime"]["points_gained"] == 40
    assert response.get_json()["active_watch_sessions"] == 1
    assert response.get_json()["live_streams"] == 1

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
    assert response.get_json()["sessions"][0]["watch_seconds"] == 20

    response = client.get("/history/sessions?kind=stream&days=30")
    assert response.status_code == 200
    assert response.get_json()["sessions"][0]["active"] is True

    response = client.get("/history/events?days=30")
    assert response.status_code == 200
    assert response.get_json()["events"][0]["type"] == "win"

    response = client.get("/history/activity?days=7")
    assert response.status_code == 200
    assert len(response.get_json()["day_list"]) == 7

    response = client.get("/history/health")
    assert response.status_code == 200
    assert response.get_json()["journal"]["gaps"] == 0


def test_history_endpoints_return_503_until_history_is_ready(monkeypatch):
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
