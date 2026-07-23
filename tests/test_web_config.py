import json
import stat
import threading
from types import SimpleNamespace

import pytest
from flask import Flask

from TwitchChannelPointsMiner.TwitchChannelPointsMiner import TwitchChannelPointsMiner
from TwitchChannelPointsMiner.classes.Chat import ChatPresence
from TwitchChannelPointsMiner.classes.Matrix import Matrix
from TwitchChannelPointsMiner.classes.AnalyticsServer import (
    test_web_notification as send_web_notification_test,
    web_config,
)
from TwitchChannelPointsMiner.classes.Settings import Settings
from TwitchChannelPointsMiner.classes.WebSocketsPool import WebSocketsPool
from TwitchChannelPointsMiner.classes.entities.Streamer import Streamer
from TwitchChannelPointsMiner.config_editor import (
    ConfigEditError,
    _notification_constructor_kwargs,
    apply_web_overrides,
    load_web_overrides,
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
    assert result["notifications"]["discord"]["enabled"] is True
    assert result["notifications"]["discord"]["fields"] == {"events": []}
    assert result["notifications"]["discord"]["secrets"] == {
        "webhook_api": True
    }
    assert result["notifications"]["discord"]["test_available"] is True
    assert "DROP_CLAIM" in result["notification_event_options"]
    assert "DAILY_REPORT" in result["notification_event_options"]
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


def test_invalid_category_error_uses_supported_value_terminology(tmp_path):
    config = tmp_path / "config.py"
    write_config(config)

    with pytest.raises(ConfigEditError, match="category value"):
        update_managed_web_config(
            config,
            {"action": "add", "kind": "categories", "value": "bad\ncategory"},
        )


@pytest.mark.parametrize("kind", ["streamers", "categories"])
@pytest.mark.parametrize("action", ["add", "remove"])
@pytest.mark.parametrize("value", [None, 123, True, [], {}])
def test_add_remove_rejects_non_string_values(tmp_path, action, kind, value):
    config = tmp_path / "config.py"
    write_config(config)

    with pytest.raises(
        ConfigEditError, match="Invalid streamer username or category value"
    ):
        update_managed_web_config(
            config,
            {"action": action, "kind": kind, "value": value},
        )


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


def test_blank_notification_text_fields_preserve_existing_values(tmp_path):
    config = tmp_path / "config.py"
    write_config(config)
    update_managed_web_config(
        config,
        {
            "action": "update_notification",
            "provider": "matrix",
            "values": {
                "enabled": False,
                "username": "miner",
                "homeserver": "matrix.example",
                "room_id": "!room:matrix.example",
            },
        },
    )

    update_managed_web_config(
        config,
        {
            "action": "update_notification",
            "provider": "matrix",
            "values": {"username": "", "homeserver": "", "room_id": ""},
        },
    )

    saved = json.loads((tmp_path / "web-config.json").read_text(encoding="utf-8"))
    assert saved["notifications"]["matrix"]["fields"] == {
        "username": "miner",
        "homeserver": "matrix.example",
        "room_id": "!room:matrix.example",
    }


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


def test_notification_events_are_validated_and_normalized_for_runtime(tmp_path):
    config = tmp_path / "config.py"
    write_config(config)
    update_managed_web_config(
        config,
        {
            "action": "update_notification",
            "provider": "discord",
            "values": {"enabled": True, "events": ["DROP_CLAIM"]},
        },
    )

    loaded = _load_config(config)

    assert loaded.MINER_CONFIG["logger_settings"].discord.events == ["DROP_CLAIM"]
    reconstructed = _notification_constructor_kwargs(
        "discord",
        loaded.MINER_CONFIG["logger_settings"].discord,
        {"events": ["DROP_CLAIM"]},
        {"webhook_api": "https://new.example/hook"},
    )
    assert reconstructed["events"] == ["DROP_CLAIM"]
    with pytest.raises(ConfigEditError, match="Unknown notification event"):
        update_managed_web_config(
            config,
            {
                "action": "update_notification",
                "provider": "discord",
                "values": {"events": ["NOT_A_REAL_EVENT"]},
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


def test_secret_only_update_does_not_enable_disabled_notification(tmp_path):
    config = tmp_path / "config.py"
    write_config(config)
    update_managed_web_config(
        config,
        {
            "action": "update_notification",
            "provider": "gotify",
            "values": {"endpoint": "https://gotify.example/message?token=secret"},
        },
    )

    loaded = _load_config(config)

    assert loaded.MINER_CONFIG["logger_settings"].gotify is None


def test_secret_only_update_keeps_enabled_notification_enabled(tmp_path):
    config = tmp_path / "config.py"
    write_config(config)
    update_managed_web_config(
        config,
        {
            "action": "update_notification",
            "provider": "discord",
            "values": {"webhook_api": "https://new.example/hook"},
        },
    )

    loaded = _load_config(config)

    assert loaded.MINER_CONFIG["logger_settings"].discord.webhook_api == (
        "https://new.example/hook"
    )


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

    class RecordingLock:
        active = False

        def __enter__(self):
            self.active = True

        def __exit__(self, *_args):
            self.active = False

    topic_lock = RecordingLock()
    removed = []

    def unlisten(topic, auth_token):
        assert topic_lock.active is False
        removed.append((topic, auth_token))

    websocket = SimpleNamespace(
        topics=[target_topic, retained_topic],
        pending_topics=[target_topic],
        is_opened=True,
        removed=removed,
    )
    websocket.unlisten = unlisten
    untouched_websocket = SimpleNamespace(
        topics=[retained_topic],
        pending_topics=[],
        is_opened=True,
        unlisten=lambda _topic, _token: pytest.fail("unexpected unlisten"),
    )
    token_requests = []

    def get_auth_token():
        token_requests.append(True)
        return "oauth-token"

    pool = SimpleNamespace(
        ws=[websocket, untouched_websocket],
        topic_lock=topic_lock,
        twitch=SimpleNamespace(
            twitch_login=SimpleNamespace(get_auth_token=get_auth_token)
        ),
    )

    WebSocketsPool.remove_streamer_topics(pool, target)

    assert websocket.topics == [retained_topic]
    assert websocket.pending_topics == []
    assert websocket.removed == [(target_topic, "oauth-token")]
    assert token_requests == [True]


def test_websocket_pool_listens_after_releasing_topic_lock():
    class RecordingLock:
        depth = 0

        def __enter__(self):
            self.depth += 1

        def __exit__(self, *_args):
            self.depth -= 1

    topic_lock = RecordingLock()
    listened = []

    def listen(topic, token):
        assert topic_lock.depth == 0
        listened.append((topic, token))

    websocket = SimpleNamespace(
        topics=[],
        pending_topics=[],
        is_opened=True,
        listen=listen,
        unlisten=lambda _topic, _token: pytest.fail("unexpected unlisten"),
    )
    pool = WebSocketsPool(
        SimpleNamespace(
            twitch_login=SimpleNamespace(get_auth_token=lambda: "oauth-token")
        ),
        [],
        {},
    )
    pool.ws = [websocket]
    pool.topic_lock = topic_lock
    topic = SimpleNamespace(streamer=Streamer("target"))

    pool.submit(topic)

    assert listened == [(topic, "oauth-token")]


def test_websocket_selection_and_registration_share_one_lock_scope():
    class RecordingLock:
        depth = 0
        max_depth = 0

        def __enter__(self):
            self.depth += 1
            self.max_depth = max(self.max_depth, self.depth)

        def __exit__(self, *_args):
            self.depth -= 1

    topic_lock = RecordingLock()
    websocket = SimpleNamespace(
        topics=[object() for _index in range(49)],
        pending_topics=[],
        is_opened=False,
    )
    pool = WebSocketsPool(
        SimpleNamespace(
            twitch_login=SimpleNamespace(get_auth_token=lambda: "oauth-token")
        ),
        [],
        {},
    )
    pool.ws = [websocket]
    pool.topic_lock = topic_lock
    topic = SimpleNamespace(streamer=Streamer("target"))

    pool.submit(topic)

    assert topic_lock.max_depth == 1
    assert len(websocket.topics) == 50
    assert websocket.pending_topics == [topic]


def test_websocket_on_open_drains_pending_topics_outside_lock(monkeypatch):
    class RecordingLock:
        depth = 0

        def __enter__(self):
            self.depth += 1

        def __exit__(self, *_args):
            self.depth -= 1

    class ImmediateThread:
        daemon = False

        def __init__(self, target):
            self.target = target

        def start(self):
            self.target()

    topic_lock = RecordingLock()
    topic = SimpleNamespace(streamer=Streamer("target"))
    listened = []
    pool = WebSocketsPool(
        SimpleNamespace(
            twitch_login=SimpleNamespace(get_auth_token=lambda: "oauth-token")
        ),
        [],
        {},
    )
    pool.topic_lock = topic_lock

    def listen(opened_topic, token):
        assert topic_lock.depth == 0
        listened.append((opened_topic, token))

    websocket = SimpleNamespace(
        parent_pool=pool,
        topics=[topic],
        pending_topics=[topic],
        is_opened=False,
        is_closed=True,
        ping=lambda: None,
        listen=listen,
        unlisten=lambda _topic, _token: pytest.fail("unexpected unlisten"),
    )
    pool.ws = [websocket]
    monkeypatch.setattr(
        "TwitchChannelPointsMiner.classes.WebSocketsPool.Thread", ImmediateThread
    )

    WebSocketsPool.on_open(websocket)

    assert websocket.pending_topics == []
    assert listened == [(topic, "oauth-token")]


@pytest.mark.parametrize("replacement_opened", [True, False])
def test_websocket_reconnection_replays_topics_after_releasing_lock(
    monkeypatch, replacement_opened
):
    class RecordingLock:
        depth = 0

        def __enter__(self):
            self.depth += 1

        def __exit__(self, *_args):
            self.depth -= 1

    topic_lock = RecordingLock()
    topic = SimpleNamespace(streamer=Streamer("target"))
    listened = []

    def listen(replayed_topic, token):
        assert topic_lock.depth == 0
        listened.append((replayed_topic, token))

    new_websocket = SimpleNamespace(
        topics=[],
        pending_topics=[],
        is_opened=replacement_opened,
        listen=listen,
        unlisten=lambda _topic, _token: pytest.fail("unexpected unlisten"),
    )
    pool = WebSocketsPool(
        SimpleNamespace(
            twitch_login=SimpleNamespace(get_auth_token=lambda: "oauth-token")
        ),
        [],
        {},
    )
    pool.topic_lock = topic_lock
    old_websocket = SimpleNamespace(
        index=0,
        parent_pool=pool,
        is_reconnecting=False,
        forced_close=False,
        topics=[topic],
        is_closed=False,
        keep_running=True,
    )
    pool.ws = [old_websocket]
    def new_websocket_while_locked(_self, _index):
        assert topic_lock.depth > 0
        return new_websocket

    monkeypatch.setattr(
        WebSocketsPool, "_WebSocketsPool__new", new_websocket_while_locked
    )
    monkeypatch.setattr(
        WebSocketsPool, "_WebSocketsPool__start", lambda _self, _index: None
    )
    monkeypatch.setattr(
        "TwitchChannelPointsMiner.classes.WebSocketsPool.time.sleep",
        lambda _seconds: None,
    )
    monkeypatch.setattr(
        "TwitchChannelPointsMiner.classes.WebSocketsPool.internet_connection_available",
        lambda: True,
    )

    WebSocketsPool.handle_reconnection(old_websocket)

    assert new_websocket.topics == [topic]
    if replacement_opened:
        assert new_websocket.pending_topics == []
        assert listened == [(topic, "oauth-token")]
    else:
        assert new_websocket.pending_topics == [topic]
        assert listened == []


def test_matrix_reconstruction_does_not_double_encode_room_id(monkeypatch):
    existing = SimpleNamespace(
        homeserver="matrix.example",
        room_id="%21room%3Amatrix.example",
        events=[],
    )
    kwargs = _notification_constructor_kwargs(
        "matrix",
        existing,
        {"username": "miner", "homeserver": "matrix.example", "events": []},
        {"password": "secret"},
    )
    monkeypatch.setattr(
        "TwitchChannelPointsMiner.classes.Matrix.requests.post",
        lambda **_kwargs: SimpleNamespace(json=lambda: {"access_token": "token"}),
    )

    notification = Matrix(**kwargs)

    assert notification.room_id == "%21room%3Amatrix.example"


def test_notification_reconstruction_preserves_custom_timeouts():
    webhook = SimpleNamespace(
        endpoint="https://old.example/hook",
        method="POST",
        events=[],
        timeout=37,
    )
    email = SimpleNamespace(
        host="smtp.example",
        port=587,
        username="miner",
        password="old-password",
        sender="miner@example.com",
        recipients=["alerts@example.com"],
        events=[],
        use_ssl=False,
        starttls=True,
        timeout=41,
    )

    webhook_kwargs = _notification_constructor_kwargs(
        "webhook", webhook, {}, {"endpoint": "https://new.example/hook"}
    )
    email_kwargs = _notification_constructor_kwargs(
        "email", email, {}, {"password": "new-password"}
    )

    assert webhook_kwargs["timeout"] == 37
    assert email_kwargs["timeout"] == 41


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


def test_config_endpoint_does_not_expose_filesystem_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(Settings, "config_path", str(tmp_path), raising=False)

    def fail_read(_path):
        raise OSError("/private/config/path: permission denied")

    monkeypatch.setitem(
        web_config.__globals__, "read_managed_web_config", fail_read
    )
    app = Flask(__name__)

    with app.test_request_context("/config", method="GET"):
        response = web_config()

    assert response.status_code == 500
    assert response.get_json() == {"error": "Unable to access configuration."}
    assert b"/private/config/path" not in response.data


def test_notification_test_endpoint_uses_saved_provider(tmp_path, monkeypatch):
    config = tmp_path / "config.py"
    write_config(config)
    monkeypatch.setattr(Settings, "config_path", str(tmp_path), raising=False)
    sent = []
    notification = SimpleNamespace(events=[])
    notification.send = lambda message, event: sent.append((message, event))
    loaded = SimpleNamespace(
        MINER_CONFIG={
            "logger_settings": SimpleNamespace(discord=notification)
        }
    )
    monkeypatch.setattr(
        "TwitchChannelPointsMiner.runner._load_config", lambda _path: loaded
    )
    app = Flask(__name__)

    with app.test_request_context(
        "/config/notifications/discord/test", method="POST"
    ):
        response = send_web_notification_test("discord")

    assert response.status_code == 200
    assert response.get_json() == {"message": "Test notification sent."}
    assert len(sent) == 1
    assert "test notification" in sent[0][0].lower()


def test_notification_test_endpoint_requires_complete_enabled_provider(
    tmp_path, monkeypatch
):
    config = tmp_path / "config.py"
    write_config(config)
    monkeypatch.setattr(Settings, "config_path", str(tmp_path), raising=False)
    loaded = SimpleNamespace(
        MINER_CONFIG={"logger_settings": SimpleNamespace(telegram=None)}
    )
    monkeypatch.setattr(
        "TwitchChannelPointsMiner.runner._load_config", lambda _path: loaded
    )
    app = Flask(__name__)

    with app.test_request_context(
        "/config/notifications/telegram/test", method="POST"
    ):
        response = send_web_notification_test("telegram")

    assert response.status_code == 409
    assert response.get_json() == {
        "error": "Configure and enable this notification first."
    }


def test_notification_test_endpoint_sanitizes_config_load_failures(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(Settings, "config_path", str(tmp_path), raising=False)

    def fail_load(_path):
        raise RuntimeError("Unable to parse /private/config/web-config.json")

    monkeypatch.setattr("TwitchChannelPointsMiner.runner._load_config", fail_load)
    app = Flask(__name__)

    with app.test_request_context(
        "/config/notifications/discord/test", method="POST"
    ):
        response = send_web_notification_test("discord")

    assert response.status_code == 500
    assert response.get_json() == {"error": "Unable to send test notification."}
    assert b"/private/config" not in response.data


def test_notification_test_endpoint_returns_sanitized_delivery_failure(
    tmp_path, monkeypatch
):
    config = tmp_path / "config.py"
    write_config(config)
    monkeypatch.setattr(Settings, "config_path", str(tmp_path), raising=False)
    notification = SimpleNamespace(
        events=[],
        send=lambda _message, _event: (
            False,
            "SMTP authentication failed. Check the username and password.",
        ),
    )
    loaded = SimpleNamespace(
        MINER_CONFIG={
            "logger_settings": SimpleNamespace(discord=notification)
        }
    )
    monkeypatch.setattr(
        "TwitchChannelPointsMiner.runner._load_config", lambda _path: loaded
    )
    app = Flask(__name__)

    with app.test_request_context(
        "/config/notifications/discord/test", method="POST"
    ):
        response = send_web_notification_test("discord")

    assert response.status_code == 502
    assert response.get_json() == {
        "error": "SMTP authentication failed. Check the username and password."
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


def test_web_override_read_errors_do_not_expose_paths(tmp_path, monkeypatch):
    config = tmp_path / "config.py"
    override = tmp_path / "web-config.json"
    override.write_text("{}", encoding="utf-8")
    original_read_text = type(override).read_text

    def fail_override_read(path, *args, **kwargs):
        if path == override:
            raise OSError(f"permission denied: {override}")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(type(override), "read_text", fail_override_read)

    with pytest.raises(OSError) as raised:
        load_web_overrides(config)

    assert str(override) in str(raised.value)


def test_invalid_web_override_json_returns_sanitized_error(tmp_path):
    config = tmp_path / "config.py"
    (tmp_path / "web-config.json").write_text("{broken", encoding="utf-8")

    with pytest.raises(
        ConfigEditError, match=r"web-config\.json contains invalid JSON"
    ) as raised:
        load_web_overrides(config)

    assert str(tmp_path) not in str(raised.value)


def test_unknown_notification_secrets_are_not_exposed(tmp_path):
    config = tmp_path / "config.py"
    write_config(config)
    (tmp_path / "web-config.json").write_text(
        json.dumps(
            {
                "notifications": {
                    "gotify": {
                        "secrets": {
                            "endpoint": "https://example.invalid",
                            "unexpected_secret": "do-not-expose",
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    result = read_managed_web_config(config)

    assert result["notifications"]["gotify"]["secrets"] == {"endpoint": True}
    assert "unexpected_secret" not in json.dumps(result)


def test_empty_notification_number_is_treated_as_unset(tmp_path):
    config = tmp_path / "config.py"
    write_config(config)

    update_managed_web_config(
        config,
        {
            "action": "update_notification",
            "provider": "email",
            "values": {"enabled": False, "port": ""},
        },
    )

    saved = json.loads((tmp_path / "web-config.json").read_text(encoding="utf-8"))
    assert "port" not in saved["notifications"]["email"].get("fields", {})


@pytest.mark.parametrize(
    ("provider", "values", "message"),
    [
        ("webhook", {"method": None}, "method must be a string"),
        ("matrix", {"room_id": None}, "room_id must be a string"),
        ("discord", {"webhook_api": 123}, "webhook_api must be a string"),
    ],
)
def test_notification_text_fields_require_strings(
    tmp_path, provider, values, message
):
    config = tmp_path / "config.py"
    write_config(config)

    with pytest.raises(ConfigEditError, match=message):
        update_managed_web_config(
            config,
            {
                "action": "update_notification",
                "provider": provider,
                "values": values,
            },
        )


@pytest.mark.parametrize(
    "streamers",
    [None, [None], [{}], [{"username": None}], [{"username": "bad name"}]],
)
def test_malformed_managed_streamers_fail_cleanly(tmp_path, streamers):
    config = tmp_path / "config.py"
    write_config(config)
    (tmp_path / "web-config.json").write_text(
        json.dumps({"streamers": streamers}), encoding="utf-8"
    )
    module = SimpleNamespace(
        STREAMERS=[], MINE_CONFIG={}, MINER_CONFIG={"logger_settings": None}
    )

    with pytest.raises(ConfigEditError, match="(?i)managed streamer"):
        apply_web_overrides(module, config)


@pytest.mark.parametrize("categories", ["alpha", {"alpha": True}, [None]])
def test_malformed_managed_categories_fail_cleanly(tmp_path, categories):
    config = tmp_path / "config.py"
    write_config(config)
    (tmp_path / "web-config.json").write_text(
        json.dumps({"categories": categories}), encoding="utf-8"
    )
    module = SimpleNamespace(
        STREAMERS=[], MINE_CONFIG={}, MINER_CONFIG={"logger_settings": None}
    )

    with pytest.raises(ConfigEditError, match="Managed categories"):
        apply_web_overrides(module, config)


@pytest.mark.parametrize(
    "sources",
    [None, [], {"followers": "yes"}, {"unknown_source": True}],
)
def test_malformed_managed_sources_fail_cleanly(tmp_path, sources):
    config = tmp_path / "config.py"
    write_config(config)
    (tmp_path / "web-config.json").write_text(
        json.dumps({"sources": sources}), encoding="utf-8"
    )
    module = SimpleNamespace(
        STREAMERS=[], MINE_CONFIG={}, MINER_CONFIG={"logger_settings": None}
    )

    with pytest.raises(ConfigEditError, match="Managed stream sources"):
        apply_web_overrides(module, config)


@pytest.mark.parametrize(
    "logging_overrides",
    [
        None,
        [],
        {"unknown": True},
        {"console_level": None},
        {"file_level": "VERBOSE"},
        {"daily_report": "yes"},
        {"daily_report_time": "25:00"},
    ],
)
def test_malformed_managed_logging_fails_cleanly(tmp_path, logging_overrides):
    config = tmp_path / "config.py"
    write_config(config)
    (tmp_path / "web-config.json").write_text(
        json.dumps({"logging": logging_overrides}), encoding="utf-8"
    )
    module = SimpleNamespace(
        STREAMERS=[],
        MINE_CONFIG={},
        MINER_CONFIG={"logger_settings": LoggerSettings()},
    )

    with pytest.raises(ConfigEditError, match="(?i)managed.*log|daily report"):
        apply_web_overrides(module, config)


@pytest.mark.parametrize(
    "notifications",
    [
        None,
        [],
        {"discord": None},
        {"unknown": {}},
        {"discord": {"enabled": "yes"}},
        {"discord": {"fields": []}},
        {"discord": {"secrets": []}},
    ],
)
def test_malformed_managed_notifications_fail_cleanly(tmp_path, notifications):
    config = tmp_path / "config.py"
    write_config(config)
    (tmp_path / "web-config.json").write_text(
        json.dumps({"notifications": notifications}), encoding="utf-8"
    )
    module = SimpleNamespace(
        STREAMERS=[],
        MINE_CONFIG={},
        MINER_CONFIG={"logger_settings": LoggerSettings()},
    )

    with pytest.raises(ConfigEditError, match="(?i)notification"):
        apply_web_overrides(module, config)
