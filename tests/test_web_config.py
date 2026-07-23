import json
import stat
import threading
from types import SimpleNamespace

import pytest
from flask import Flask

from TwitchChannelPointsMiner.TwitchChannelPointsMiner import TwitchChannelPointsMiner
from TwitchChannelPointsMiner.classes.Chat import ChatPresence
from TwitchChannelPointsMiner.classes.AnalyticsServer import web_config
from TwitchChannelPointsMiner.classes.Settings import Settings
from TwitchChannelPointsMiner.classes.WebSocketsPool import WebSocketsPool
from TwitchChannelPointsMiner.classes.entities.Streamer import Streamer
from TwitchChannelPointsMiner.config_editor import (
    ConfigEditError,
    apply_web_overrides,
    read_managed_web_config,
    update_managed_web_config,
)
from TwitchChannelPointsMiner.logger import LoggerSettings
from TwitchChannelPointsMiner.runner import _config_digest, _load_config


def write_config(path):
    path.write_text(
        """\
import logging
from TwitchChannelPointsMiner.classes.Chat import ChatPresence
from TwitchChannelPointsMiner.classes.Discord import Discord
from TwitchChannelPointsMiner.classes.Settings import CategorySort, StreamerSource
from TwitchChannelPointsMiner.classes.entities.Streamer import Streamer, StreamerSettings
from TwitchChannelPointsMiner.logger import LoggerSettings

MINER_CONFIG = {
    "username": "example",
    "streamer_source_priority": [StreamerSource.STREAMERS, StreamerSource.CATEGORIES],
    "streamer_settings": StreamerSettings(favorite=False, chat=ChatPresence.ONLINE),
    "logger_settings": LoggerSettings(
        console_level=logging.INFO,
        file_level=logging.DEBUG,
        daily_report=False,
        discord=Discord(webhook_api="https://secret.example/hook", events=[]),
    ),
}
STREAMERS = [Streamer("one", settings=StreamerSettings(favorite=True)), "two"]
MINE_CONFIG = {
    "followers": False,
    "categories": ["alpha", "beta"],
    "category_limit": 5,
    "category_sort": CategorySort.VIEWERS_DESC,
    "category_refresh_interval_hours": 3,
    "category_drops_enabled": True,
    "auto_mine_badge_drops": False,
}
ANALYTICS_CONFIG = None
""",
        encoding="utf-8",
    )


def test_managed_web_config_masks_credentials_and_reads_effective_settings(tmp_path):
    config = tmp_path / "config.py"
    write_config(config)

    result = read_managed_web_config(config)

    assert [item["username"] for item in result["streamers"]] == ["one", "two"]
    assert result["streamers"][0]["settings"]["favorite"] is True
    assert result["streamers"][1]["settings"]["chat"] == "ONLINE"
    assert result["notifications"]["discord"] == {
        "enabled": True,
        "fields": {"events": []},
        "secrets": {"webhook_api": True},
    }
    assert "secret.example" not in json.dumps(result)


def test_managed_web_config_updates_lists_settings_and_permissions(tmp_path):
    config = tmp_path / "config.py"
    write_config(config)
    original_source = config.read_text(encoding="utf-8")

    update_managed_web_config(
        config, {"action": "remove", "kind": "streamers", "value": "two"}
    )
    update_managed_web_config(
        config,
        {"action": "reorder_categories", "categories": ["beta", "alpha"]},
    )
    result = update_managed_web_config(
        config,
        {
            "action": "update_streamer",
            "username": "one",
            "settings": {
                "favorite": False,
                "make_predictions": False,
                "follow_raid": False,
                "claim_drops": True,
                "claim_moments": True,
                "chat": "NEVER",
                "points_limit": 25000,
            },
        },
    )

    assert [item["username"] for item in result["streamers"]] == ["one"]
    assert result["categories"] == ["beta", "alpha"]
    assert result["streamers"][0]["settings"]["points_limit"] == 25000
    override = tmp_path / "web-config.json"
    assert stat.S_IMODE(override.stat().st_mode) == 0o600
    assert "https://secret.example/hook" not in override.read_text(encoding="utf-8")
    assert config.read_text(encoding="utf-8") == original_source


def test_managed_categories_support_display_names_urls_and_forced_streamers(tmp_path):
    config = tmp_path / "config.py"
    write_config(config)
    categories = [
        "Call of Duty: Warzone|streamer_name",
        "https://www.twitch.tv/directory/category/pokemon-go?filter=drops",
        "beta",
        "alpha",
    ]
    update_managed_web_config(
        config,
        {
            "action": "add",
            "kind": "categories",
            "value": categories[0],
        },
    )
    update_managed_web_config(
        config,
        {
            "action": "add",
            "kind": "categories",
            "value": categories[1],
        },
    )

    result = update_managed_web_config(
        config, {"action": "reorder_categories", "categories": categories}
    )

    assert result["categories"] == categories


def test_notification_secrets_are_write_only_and_blank_values_are_not_saved(tmp_path):
    config = tmp_path / "config.py"
    write_config(config)

    result = update_managed_web_config(
        config,
        {
            "action": "update_notification",
            "provider": "discord",
            "values": {"enabled": True, "events": ["DROP_CLAIM"], "webhook_api": ""},
        },
    )

    saved = json.loads((tmp_path / "web-config.json").read_text(encoding="utf-8"))
    assert saved["notifications"]["discord"].get("secrets", {}) == {}
    assert result["notifications"]["discord"]["secrets"]["webhook_api"] is True
    assert "secret.example" not in json.dumps(result)


def test_enabling_new_notification_requires_credentials(tmp_path):
    config = tmp_path / "config.py"
    write_config(config)

    with pytest.raises(ConfigEditError, match="telegram requires: token"):
        update_managed_web_config(
            config,
            {
                "action": "update_notification",
                "provider": "telegram",
                "values": {"enabled": True, "chat_id": 123, "events": []},
            },
        )


def test_new_notification_secret_is_applied_but_never_returned(tmp_path):
    config = tmp_path / "config.py"
    write_config(config)
    result = update_managed_web_config(
        config,
        {
            "action": "update_notification",
            "provider": "telegram",
            "values": {
                "enabled": True,
                "chat_id": 123,
                "token": "super-secret-token",
                "disable_notification": True,
                "events": ["DROP_CLAIM"],
            },
        },
    )
    logger_settings = LoggerSettings()
    module = SimpleNamespace(
        STREAMERS=[], MINE_CONFIG={}, MINER_CONFIG={"logger_settings": logger_settings}
    )

    apply_web_overrides(module, config)

    assert "super-secret-token" in logger_settings.telegram.telegram_api
    assert result["notifications"]["telegram"]["secrets"] == {"token": True}
    assert "super-secret-token" not in json.dumps(result)


def test_apply_web_overrides_builds_effective_runtime_config(tmp_path):
    config_path = tmp_path / "config.py"
    write_config(config_path)
    update_managed_web_config(
        config_path,
        {"action": "remove", "kind": "streamers", "value": "two"},
    )
    update_managed_web_config(
        config_path,
        {
            "action": "update_streamer",
            "username": "one",
            "settings": {"favorite": False, "chat": "NEVER"},
        },
    )
    update_managed_web_config(
        config_path,
        {
            "action": "update_category",
            "values": {"limit": 9, "sort": "RANDOM", "drops_enabled": False},
        },
    )
    module = SimpleNamespace(
        STREAMERS=[Streamer("one"), Streamer("two")],
        MINE_CONFIG={"categories": ["alpha", "beta"]},
        MINER_CONFIG={"logger_settings": None},
    )

    apply_web_overrides(module, config_path)

    assert [streamer.username for streamer in module.STREAMERS] == ["one"]
    assert module.STREAMERS[0].settings.favorite is False
    assert module.STREAMERS[0].settings.chat is ChatPresence.NEVER
    assert module.MINE_CONFIG["category_limit"] == 9
    assert module.MINE_CONFIG["category_sort"] == "RANDOM"
    assert module.MINE_CONFIG["category_drops_enabled"] is False


def test_remove_streamers_unsubscribes_only_purely_explicit_streamers():
    explicit = Streamer("explicit", explicitly_configured=True)
    category = Streamer(
        "category", from_category=True, explicitly_configured=True
    )
    websocket_pool = SimpleNamespace(removed=[])
    websocket_pool.remove_streamer_topics = websocket_pool.removed.append
    miner = SimpleNamespace(
        running=True,
        ws_pool=websocket_pool,
        config_reload_lock=threading.Lock(),
        streamers=[explicit, category],
        original_streamers=[100, 200],
    )

    TwitchChannelPointsMiner.remove_streamers(miner, ["explicit", "category"])

    assert miner.streamers == [category]
    assert miner.original_streamers == [200]
    assert category.explicitly_configured is False
    assert websocket_pool.removed == [explicit]


def test_websocket_pool_removes_and_unlistens_streamer_topics():
    target = Streamer("target")
    retained = Streamer("retained")
    target_topic = SimpleNamespace(streamer=target)
    retained_topic = SimpleNamespace(streamer=retained)
    websocket = SimpleNamespace(
        topics=[target_topic, retained_topic],
        pending_topics=[target_topic],
        is_opened=True,
        removed=[],
    )
    websocket.unlisten = websocket.removed.append
    pool = SimpleNamespace(ws=[websocket])

    WebSocketsPool.remove_streamer_topics(pool, target)

    assert websocket.topics == [retained_topic]
    assert websocket.pending_topics == []
    assert websocket.removed == [target_topic]


def test_config_endpoint_never_returns_notification_secrets(tmp_path, monkeypatch):
    config = tmp_path / "config.py"
    write_config(config)
    monkeypatch.setattr(Settings, "config_path", str(tmp_path), raising=False)
    app = Flask(__name__)

    with app.test_request_context("/config", method="GET"):
        response = web_config()

    assert response.status_code == 200
    assert b"secret.example" not in response.data
    assert response.get_json()["notifications"]["discord"]["secrets"] == {
        "webhook_api": True
    }


def test_runner_loads_and_digests_dashboard_overrides(tmp_path):
    config = tmp_path / "config.py"
    write_config(config)
    original_digest = _config_digest(config)
    update_managed_web_config(
        config,
        {"action": "remove", "kind": "streamers", "value": "two"},
    )

    loaded = _load_config(config)

    assert [streamer.username for streamer in loaded.STREAMERS] == ["one"]
    assert _config_digest(config) != original_digest
