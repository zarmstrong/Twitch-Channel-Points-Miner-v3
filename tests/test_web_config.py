import ast
import errno
import json
import logging
import math
import os
import stat
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask

import TwitchChannelPointsMiner.config_editor as config_editor
from TwitchChannelPointsMiner.TwitchChannelPointsMiner import TwitchChannelPointsMiner
from TwitchChannelPointsMiner.classes.Chat import ChatPresence
from TwitchChannelPointsMiner.classes.Matrix import Matrix
from TwitchChannelPointsMiner.classes.AnalyticsServer import (
    test_web_notification as send_web_notification_test,
    web_config,
)
from TwitchChannelPointsMiner.classes.Settings import Settings, StreamerSource
from TwitchChannelPointsMiner.classes.WebSocketsPool import WebSocketsPool
from TwitchChannelPointsMiner.classes.entities.PubsubTopic import PubsubTopic
from TwitchChannelPointsMiner.classes.entities.Streamer import Streamer
from TwitchChannelPointsMiner.config_editor import (
    ConfigEditError,
    enable_analytics_dashboard,
    load_web_overrides,
    migrate_web_config,
    read_managed_web_config,
    read_config_literal,
    set_miner_username,
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
    assert result["notifications"]["discord"]["secrets"] == {"webhook_api": True}
    assert result["notifications"]["discord"]["test_available"] is True
    assert "DROP_CLAIM" in result["notification_event_options"]
    assert "DAILY_REPORT" in result["notification_event_options"]
    # Never sent to notifiers, so not offered as a notification event.
    assert "WATCH_STATUS" not in result["notification_event_options"]
    assert "secret.example" not in json.dumps(result)
    assert result["updates"] == {
        "enabled": True,
        "interval_hours": 24,
        "startup_only": False,
    }
    assert result["login"] == {"android": False}


def test_managed_web_config_updates_android_login(tmp_path):
    config = tmp_path / "config.py"
    write_config(config)

    result = update_managed_web_config(
        config, {"action": "update_login", "values": {"android": True}}
    )

    assert result["login"] == {"android": True}
    assert _load_config(config).MINER_CONFIG["android_login"] is True

    update_managed_web_config(
        config, {"action": "update_login", "values": {"android": False}}
    )
    assert _load_config(config).MINER_CONFIG["android_login"] is False


@pytest.mark.parametrize(
    "values",
    [{"android": "yes"}, {"android": 1}, {"token": "abc"}, ["android"]],
    ids=["string", "int", "unknown-key", "not-a-dict"],
)
def test_managed_web_config_rejects_invalid_login_settings(tmp_path, values):
    config = tmp_path / "config.py"
    write_config(config)
    before = config.read_text(encoding="utf-8")

    with pytest.raises(ConfigEditError):
        update_managed_web_config(config, {"action": "update_login", "values": values})

    assert config.read_text(encoding="utf-8") == before


def test_managed_web_config_updates_release_check_settings(tmp_path):
    config = tmp_path / "config.py"
    write_config(config)

    result = update_managed_web_config(
        config,
        {
            "action": "update_updates",
            "values": {
                "enabled": False,
                "interval_hours": 12,
                "startup_only": False,
            },
        },
    )

    assert result["updates"] == {
        "enabled": False,
        "interval_hours": 12,
        "startup_only": False,
    }

    loaded = _load_config(config)
    assert loaded.MINER_CONFIG["update_check"] is False
    assert loaded.MINER_CONFIG["update_check_interval_hours"] == 12

    update_managed_web_config(
        config,
        {
            "action": "update_updates",
            "values": {
                "enabled": True,
                "interval_hours": 24,
                "startup_only": True,
            },
        },
    )

    loaded = _load_config(config)
    assert loaded.MINER_CONFIG["update_check"] is True
    assert loaded.MINER_CONFIG["update_check_interval_hours"] == math.inf


def test_managed_web_config_updates_wildcard_category_source(tmp_path):
    config = tmp_path / "config.py"
    write_config(config)

    result = update_managed_web_config(
        config,
        {"action": "update_sources", "values": {"wildcard_categories": True}},
    )

    assert result["sources"]["wildcard_categories"] is True
    loaded = _load_config(config)
    assert (
        StreamerSource.WILDCARD_CATEGORIES
        in loaded.MINER_CONFIG["streamer_source_priority"]
    )
    assert loaded.MINE_CONFIG["wildcard_categories"] is True

    result = update_managed_web_config(
        config,
        {"action": "update_sources", "values": {"wildcard_categories": False}},
    )

    assert result["sources"]["wildcard_categories"] is False
    loaded = _load_config(config)
    assert (
        StreamerSource.WILDCARD_CATEGORIES
        not in loaded.MINER_CONFIG["streamer_source_priority"]
    )
    assert loaded.MINE_CONFIG["wildcard_categories"] is False


def test_managed_web_config_reorders_streamers_without_touching_settings(tmp_path):
    config = tmp_path / "config.py"
    write_config(config)

    result = update_managed_web_config(
        config,
        {"action": "reorder_streamers", "usernames": ["two", "one"]},
    )

    assert [item["username"] for item in result["streamers"]] == ["two", "one"]
    one_settings = next(
        item["settings"] for item in result["streamers"] if item["username"] == "one"
    )
    assert one_settings["favorite"] is True
    loaded = _load_config(config)
    assert [
        getattr(streamer, "username", streamer) for streamer in loaded.STREAMERS
    ] == ["two", "one"]


def test_managed_web_config_rejects_incomplete_streamer_order(tmp_path):
    config = tmp_path / "config.py"
    write_config(config)

    with pytest.raises(ConfigEditError, match="must contain every configured streamer"):
        update_managed_web_config(
            config,
            {"action": "reorder_streamers", "usernames": ["one"]},
        )


def test_managed_web_config_rejects_streamer_order_with_unknown_username(tmp_path):
    config = tmp_path / "config.py"
    write_config(config)

    with pytest.raises(ConfigEditError, match="must contain every configured streamer"):
        update_managed_web_config(
            config,
            {"action": "reorder_streamers", "usernames": ["one", "two", "extra"]},
        )


def test_managed_web_config_update_sources_enables_and_reorders_in_one_call(tmp_path):
    config = tmp_path / "config.py"
    write_config(config)

    result = update_managed_web_config(
        config,
        {
            "action": "update_sources",
            "values": {"wildcard_categories": True},
            "order": [
                "wildcard_categories",
                "streamers",
                "followers",
                "categories",
                "badges",
            ],
        },
    )

    assert result["sources"]["wildcard_categories"] is True
    assert result["source_order"][0] == "wildcard_categories"
    loaded = _load_config(config)
    priority = loaded.MINER_CONFIG["streamer_source_priority"]
    assert priority[0] == StreamerSource.WILDCARD_CATEGORIES
    assert loaded.MINE_CONFIG["wildcard_categories"] is True


def test_managed_web_config_update_sources_without_order_keeps_legacy_behavior(
    tmp_path,
):
    config = tmp_path / "config.py"
    write_config(config)

    result = update_managed_web_config(
        config,
        {"action": "update_sources", "values": {"followers": True}},
    )

    # Baseline priority is [STREAMERS, CATEGORIES]; the legacy no-order path
    # appends the newly-enabled source at the end rather than rebuilding
    # from a posted order.
    assert result["sources"]["followers"] is True
    assert result["source_order"].index("followers") == 2


def test_managed_web_config_rejects_incomplete_source_order(tmp_path):
    config = tmp_path / "config.py"
    write_config(config)

    with pytest.raises(ConfigEditError, match="Invalid stream source order"):
        update_managed_web_config(
            config,
            {
                "action": "update_sources",
                "values": {},
                "order": ["streamers", "followers"],
            },
        )


def test_managed_web_config_reads_math_inf_as_startup_only(tmp_path):
    config = tmp_path / "config.py"
    write_config(config)
    source = config.read_text(encoding="utf-8")
    source = source.replace("import logging\n", "import logging\nimport math\n", 1)
    source = source.replace(
        '    "username": "example",\n',
        '    "username": "example",\n' '    "update_check_interval_hours": math.inf,\n',
        1,
    )
    config.write_text(source, encoding="utf-8")

    result = read_managed_web_config(config)

    assert result["updates"] == {
        "enabled": True,
        "interval_hours": 24,
        "startup_only": True,
    }


@pytest.mark.parametrize(
    "values",
    [
        {"enabled": "yes"},
        {"startup_only": "yes"},
        {"interval_hours": 2},
        {"interval_hours": 3.5},
        {"unknown": True},
    ],
)
def test_managed_web_config_rejects_invalid_release_check_settings(tmp_path, values):
    config = tmp_path / "config.py"
    write_config(config)

    with pytest.raises(ConfigEditError):
        update_managed_web_config(
            config,
            {"action": "update_updates", "values": values},
        )


def test_managed_web_config_ignores_interval_when_checking_only_at_startup(
    tmp_path,
):
    config = tmp_path / "config.py"
    write_config(config)

    result = update_managed_web_config(
        config,
        {
            "action": "update_updates",
            "values": {"interval_hours": 0, "startup_only": True},
        },
    )

    assert result["updates"]["startup_only"] is True
    loaded = _load_config(config)
    assert loaded.MINER_CONFIG["update_check_interval_hours"] == math.inf


def test_managed_web_config_updates_lists_settings_and_permissions(tmp_path):
    config = tmp_path / "config.py"
    write_config(config)
    config.chmod(0o640)
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
                "streak_only": True,
            },
        },
    )

    assert [item["username"] for item in result["streamers"]] == ["one"]
    assert result["categories"] == ["beta", "alpha"]
    assert result["streamers"][0]["settings"]["points_limit"] == 25000
    assert result["streamers"][0]["settings"]["streak_only"] is True
    assert not (tmp_path / "web-config.json").exists()
    assert config.read_text(encoding="utf-8") != original_source
    loaded = _load_config(config)
    assert loaded.MINE_CONFIG["categories"] == ["beta", "alpha"]
    # Windows has no POSIX permission bits; chmod only toggles read-only.
    if os.name == "posix":
        assert stat.S_IMODE(config.stat().st_mode) == 0o640


def test_managed_web_config_preserves_an_integer_streak_only_window(tmp_path):
    config = tmp_path / "config.py"
    write_config(config)

    result = update_managed_web_config(
        config,
        {
            "action": "update_streamer",
            "username": "one",
            "settings": {"streak_only": 5},
        },
    )

    assert result["streamers"][0]["settings"]["streak_only"] == 5
    loaded = _load_config(config)
    assert loaded.STREAMERS[0].settings.streak_only == 5


def test_managed_web_config_rejects_an_invalid_streak_only_window(tmp_path):
    config = tmp_path / "config.py"
    write_config(config)
    before = config.read_text(encoding="utf-8")

    with pytest.raises(ConfigEditError):
        update_managed_web_config(
            config,
            {
                "action": "update_streamer",
                "username": "one",
                "settings": {"streak_only": 0},
            },
        )

    assert config.read_text(encoding="utf-8") == before


def test_managed_web_config_updates_categories_when_replace_is_ebusy(
    tmp_path, monkeypatch
):
    config = tmp_path / "config.py"
    write_config(config)

    real_replace = os.replace

    def maybe_busy(source, destination):
        if destination == config:
            raise OSError(errno.EBUSY, "Device or resource busy")
        return real_replace(source, destination)

    monkeypatch.setattr("TwitchChannelPointsMiner.config_editor.os.replace", maybe_busy)

    result = update_managed_web_config(
        config,
        {"action": "reorder_categories", "categories": ["beta", "alpha"]},
    )

    assert result["categories"] == ["beta", "alpha"]
    monkeypatch.undo()
    loaded = _load_config(config)
    assert loaded.MINE_CONFIG["categories"] == ["beta", "alpha"]


def test_managed_web_config_updates_categories_after_non_ascii_source(tmp_path):
    config = tmp_path / "config.py"
    write_config(config)
    source = config.read_text(encoding="utf-8").replace(
        "MINE_CONFIG = {", "# Pokémon\nMINE_CONFIG = {"
    )
    config.write_text(source, encoding="utf-8")

    update_managed_web_config(
        config,
        {"action": "reorder_categories", "categories": ["beta", "alpha"]},
    )

    assert "# Pokémon" in config.read_text(encoding="utf-8")
    loaded = _load_config(config)
    assert loaded.MINE_CONFIG["categories"] == ["beta", "alpha"]


def test_managed_web_config_updates_categories_when_chmod_is_unsupported(
    tmp_path, monkeypatch
):
    config = tmp_path / "config.py"
    write_config(config)
    real_chmod = os.chmod

    def unsupported_chmod(path, *args, **kwargs):
        if str(path).startswith(str(tmp_path / ".config.py.")):
            raise OSError("unsupported")
        return real_chmod(path, *args, **kwargs)

    monkeypatch.setattr(
        "TwitchChannelPointsMiner.config_editor.os.chmod", unsupported_chmod
    )

    result = update_managed_web_config(
        config,
        {"action": "reorder_categories", "categories": ["beta", "alpha"]},
    )

    assert result["categories"] == ["beta", "alpha"]
    monkeypatch.undo()
    loaded = _load_config(config)
    assert loaded.MINE_CONFIG["categories"] == ["beta", "alpha"]


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


def test_remove_streamer_with_name_manual_config_would_reject_on_add(tmp_path):
    # STREAMERS entered directly in config.py aren't required to satisfy the
    # add-time username format (e.g. a hyphenated or over-length name). Remove
    # must still work for such an entry instead of re-validating its format.
    config = tmp_path / "config.py"
    write_config(config)
    update_managed_web_config(
        config,
        {"action": "add", "kind": "streamers", "value": "legacy_placeholder"},
    )
    source = config.read_text(encoding="utf-8")
    config.write_text(
        source.replace("legacy_placeholder", "legacy-name-with-hyphen"),
        encoding="utf-8",
    )

    result = update_managed_web_config(
        config,
        {"action": "remove", "kind": "streamers", "value": "legacy-name-with-hyphen"},
    )

    assert "legacy-name-with-hyphen" not in [
        streamer["username"] for streamer in result["streamers"]
    ]


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

    assert config.read_text(encoding="utf-8").count("https://secret.example/hook") == 1
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

    assert read_managed_web_config(config)["notifications"]["matrix"]["fields"] == {}


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


def test_ntfy_dashboard_accepts_tags_and_uses_default_server(tmp_path):
    config = tmp_path / "config.py"
    write_config(config)

    result = update_managed_web_config(
        config,
        {
            "action": "update_notification",
            "provider": "ntfy",
            "values": {
                "enabled": True,
                "topic": "private-topic",
                "tags": ["twitch", "gift"],
                "events": ["DROP_CLAIM"],
            },
        },
    )
    loaded = _load_config(config)
    ntfy = loaded.MINER_CONFIG["logger_settings"].ntfy

    assert result["notifications"]["ntfy"]["test_available"] is True
    assert result["notifications"]["ntfy"]["secrets"] == {
        "topic": True,
        "token": False,
    }
    assert ntfy.server_url == "https://ntfy.sh"
    assert ntfy.tags == ["twitch", "gift"]
    assert ntfy.events == ["DROP_CLAIM"]


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
                "message_thread_id": 987,
                "disable_notification": True,
                "events": ["DROP_CLAIM"],
            },
        },
    )
    loaded = _load_config(config)
    assert (
        "super-secret-token"
        in loaded.MINER_CONFIG["logger_settings"].telegram.telegram_api
    )
    assert result["notifications"]["telegram"]["fields"]["message_thread_id"] == 987
    assert result["notifications"]["telegram"]["secrets"] == {"token": True}
    assert "super-secret-token" not in json.dumps(result)


def test_telegram_message_thread_id_must_be_integer(tmp_path):
    config = tmp_path / "config.py"
    write_config(config)

    with pytest.raises(ConfigEditError, match="message_thread_id must be an integer"):
        update_managed_web_config(
            config,
            {
                "action": "update_notification",
                "provider": "telegram",
                "values": {"message_thread_id": "topic"},
            },
        )


def test_telegram_message_thread_id_can_be_cleared(tmp_path):
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
                "message_thread_id": None,
                "events": ["DROP_CLAIM"],
            },
        },
    )

    assert result["notifications"]["telegram"]["fields"]["message_thread_id"] is None


def test_other_notification_numeric_fields_cannot_be_none(tmp_path):
    config = tmp_path / "config.py"
    write_config(config)

    with pytest.raises(ConfigEditError, match="port must be an integer"):
        update_managed_web_config(
            config,
            {
                "action": "update_notification",
                "provider": "email",
                "values": {"port": None},
            },
        )


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


def test_canonical_config_edits_build_effective_runtime_config(tmp_path):
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
    module = _load_config(config_path)

    assert [streamer.username for streamer in module.STREAMERS] == ["one"]
    assert module.STREAMERS[0].settings.favorite is False
    assert module.STREAMERS[0].settings.chat is ChatPresence.NEVER
    assert module.MINE_CONFIG["category_limit"] == 9
    assert module.MINE_CONFIG["category_sort"] == "RANDOM"
    assert module.MINE_CONFIG["category_drops_enabled"] is False


def test_canonical_config_edits_add_required_symbol_imports(tmp_path):
    config = tmp_path / "config.py"
    config.write_text(
        """\
MINER_CONFIG = {"streamer_settings": {}}
STREAMERS = ["one"]
MINE_CONFIG = {"categories": [], "category_sort": "ORDER"}
ANALYTICS_CONFIG = None
""",
        encoding="utf-8",
    )

    update_managed_web_config(
        config,
        {
            "action": "update_streamer",
            "username": "one",
            "settings": {"favorite": True, "chat": "NEVER"},
        },
    )
    update_managed_web_config(
        config,
        {"action": "update_category", "values": {"sort": "RANDOM"}},
    )
    update_managed_web_config(
        config,
        {"action": "update_sources", "values": {"followers": True}},
    )

    source = config.read_text(encoding="utf-8")
    assert "from TwitchChannelPointsMiner.classes.Chat import ChatPresence" in source
    assert (
        "from TwitchChannelPointsMiner.classes.entities.Streamer import "
        "Streamer, StreamerSettings"
    ) in source
    assert (
        "from TwitchChannelPointsMiner.classes.Settings import CategorySort" in source
    )
    assert (
        "from TwitchChannelPointsMiner.classes.Settings import StreamerSource" in source
    )
    namespace = {}
    exec(compile(source, config.name, "exec"), namespace)
    assert namespace["STREAMERS"][0].settings.favorite is True
    assert namespace["STREAMERS"][0].settings.chat is ChatPresence.NEVER
    assert namespace["MINE_CONFIG"]["category_sort"] == "RANDOM"
    assert any(
        source.name == "FOLLOWERS"
        for source in namespace["MINER_CONFIG"]["streamer_source_priority"]
    )


def test_enable_analytics_dashboard_preserves_formatting_and_comments(tmp_path):
    # Used by the Windows desktop shell's one-time "enable the dashboard?"
    # consent prompt and its on-demand "Enable dashboard" button - a
    # deliberate, in-the-moment user action, so (unlike the shell's silent
    # first-run bootstrap) it must go through this source-preserving path
    # and leave the rest of the file - comments included - untouched.
    config = tmp_path / "config.py"
    config.write_text(
        """\
# my custom header comment
MINER_CONFIG = {
    "username": "someone",  # inline comment
    "enable_analytics": False,
}
STREAMERS = []
MINE_CONFIG = {}
ANALYTICS_CONFIG = None
""",
        encoding="utf-8",
    )

    enable_analytics_dashboard(config, password="generated-secret")

    source = config.read_text(encoding="utf-8")
    assert "# my custom header comment" in source
    assert '"username": "someone",  # inline comment' in source

    module = _load_config(config)
    assert module.MINER_CONFIG["enable_analytics"] is True
    assert module.ANALYTICS_CONFIG == {
        "host": "127.0.0.1",
        "port": 54455,
        "refresh": 5,
        "days_ago": 7,
        "password": "generated-secret",
        "log_poll_interval": 5,
        "trust_proxy_auth": False,
        "allowed_hosts": None,
        "history_retention_days": 730,
    }


def test_enable_analytics_dashboard_respects_existing_analytics_config(tmp_path):
    # A user may have pre-configured analytics settings while leaving it
    # switched off; enabling it must not clobber those with fresh defaults
    # and an unrelated generated password.
    from TwitchChannelPointsMiner.config_migration import CONFIG_VERSION

    config = tmp_path / "config.py"
    # CONFIG_VERSION matches the current schema so _load_config's own,
    # unrelated schema migration (which legitimately backfills missing
    # ANALYTICS_CONFIG keys like refresh/days_ago on an *old*-schema config)
    # is a verified no-op - isolating enable_analytics_dashboard's own
    # behavior, which is what this test is about.
    config.write_text(
        f"""\
CONFIG_VERSION = {CONFIG_VERSION}
MINER_CONFIG = {{
    "enable_analytics": False,
}}
STREAMERS = []
MINE_CONFIG = {{}}
ANALYTICS_CONFIG = {{
    "host": "127.0.0.1",
    "port": 9999,
    "password": "already-set-by-user",
}}
""",
        encoding="utf-8",
    )

    enable_analytics_dashboard(config, password="should-not-be-used")

    module = _load_config(config)
    assert module.MINER_CONFIG["enable_analytics"] is True
    assert module.ANALYTICS_CONFIG == {
        "host": "127.0.0.1",
        "port": 9999,
        "password": "already-set-by-user",
    }


def test_enable_analytics_dashboard_requires_analytics_config_assignment(tmp_path):
    config = tmp_path / "config.py"
    config.write_text(
        'MINER_CONFIG = {"enable_analytics": False}\n',
        encoding="utf-8",
    )

    original = config.read_bytes()
    with pytest.raises(ConfigEditError, match="ANALYTICS_CONFIG"):
        enable_analytics_dashboard(config, password="secret")
    assert config.read_bytes() == original


def test_enable_analytics_dashboard_leaves_invalid_python_unchanged(tmp_path):
    config = tmp_path / "config.py"
    original = b'MINER_CONFIG = {"enable_analytics": False}\nANALYTICS_CONFIG = {\n'
    config.write_bytes(original)
    with pytest.raises(SyntaxError):
        enable_analytics_dashboard(config, password="secret")
    assert config.read_bytes() == original


def test_read_config_literal_distinguishes_none_from_missing_and_expressions():
    missing = object()
    assert (
        read_config_literal(
            "ANALYTICS_CONFIG = None", "ANALYTICS_CONFIG", default=missing
        )
        is None
    )
    assert read_config_literal("", "ANALYTICS_CONFIG", default=missing) is missing
    assert (
        read_config_literal(
            "ANALYTICS_CONFIG = load_settings()", "ANALYTICS_CONFIG", default=missing
        )
        is missing
    )
    assert (
        read_config_literal(
            "MINER_CONFIG = {'enable_analytics': False}",
            "MINER_CONFIG",
            "enable_analytics",
        )
        is False
    )
    assert (
        read_config_literal(
            "MINER_CONFIG = {}", "MINER_CONFIG", "username", default=missing
        )
        is missing
    )


def test_set_miner_username_preserves_formatting_and_comments(tmp_path):
    # Used by the Windows desktop shell's first-run setup panel, which
    # collects a real Twitch username before mining starts (see
    # windows_launcher.py's _needs_username/WindowApi.submit_username) - a
    # deliberate, in-the-moment user action, so this must preserve the rest
    # of the file, comments included, like every other dashboard-driven edit.
    config = tmp_path / "config.py"
    config.write_text(
        """\
# my custom header comment
MINER_CONFIG = {
    "username": "your-twitch-username",
    "enable_analytics": False,  # inline comment
}
STREAMERS = []
MINE_CONFIG = {}
ANALYTICS_CONFIG = None
""",
        encoding="utf-8",
    )

    result = set_miner_username(config, "  real_twitch_user  ")

    assert result == "real_twitch_user"
    source = config.read_text(encoding="utf-8")
    assert "# my custom header comment" in source
    assert '"enable_analytics": False,  # inline comment' in source

    module = _load_config(config)
    assert module.MINER_CONFIG["username"] == "real_twitch_user"


def test_set_miner_username_rejects_invalid_characters(tmp_path):
    config = tmp_path / "config.py"
    config.write_text(
        'MINER_CONFIG = {"username": "your-twitch-username"}\n', encoding="utf-8"
    )

    with pytest.raises(ConfigEditError, match="valid Twitch username"):
        set_miner_username(config, "not a valid username!!!")

    assert (
        config.read_text(encoding="utf-8")
        == 'MINER_CONFIG = {"username": "your-twitch-username"}\n'
    )


def test_legacy_streamers_migrate_in_one_source_rewrite(tmp_path, monkeypatch):
    config = tmp_path / "config.py"
    write_config(config)
    records = [
        {"username": "one", "settings": {"favorite": False, "chat": "NEVER"}},
        {"username": "two", "settings": {"favorite": True, "chat": "ALWAYS"}},
    ]
    (tmp_path / "web-config.json").write_text(
        json.dumps({"streamers": records}), encoding="utf-8"
    )
    calls = []
    write_streamers = config_editor._write_streamers

    def record_write(config_path, migrated_records, updated_usernames=None, **kwargs):
        calls.append((migrated_records, updated_usernames))
        return write_streamers(
            config_path, migrated_records, updated_usernames, **kwargs
        )

    monkeypatch.setattr(config_editor, "_write_streamers", record_write)

    migrate_web_config(config)

    assert len(calls) == 1
    assert calls[0][1] == {"one", "two"}
    loaded = _load_config(config)
    assert loaded.STREAMERS[0].settings.chat is ChatPresence.NEVER
    assert loaded.STREAMERS[1].settings.favorite is True
    assert loaded.STREAMERS[1].settings.chat is ChatPresence.ALWAYS


def test_remove_streamers_unsubscribes_only_purely_explicit_streamers():
    explicit = Streamer("explicit", explicitly_configured=True)
    category = Streamer("category", from_category=True, explicitly_configured=True)
    websocket_pool = SimpleNamespace(removed=[])
    websocket_pool.remove_streamer_topics = websocket_pool.removed.append
    retired = []
    miner = SimpleNamespace(
        running=True,
        ws_pool=websocket_pool,
        config_reload_lock=threading.Lock(),
        streamers=[explicit, category],
        original_streamers=[100, 200],
        _record_daily_report_retirements=retired.append,
    )

    TwitchChannelPointsMiner.remove_streamers(miner, ["explicit", "category"])

    assert retired == [[category]]
    assert miner.streamers == [category]
    assert miner.original_streamers == [200]
    assert category.explicitly_configured is False
    assert websocket_pool.removed == [explicit]


def test_add_streamers_marks_already_tracked_streamer_configured():
    category = Streamer("category", from_category=True)
    miner = SimpleNamespace(streamers=[category], original_streamers=[100])

    TwitchChannelPointsMiner._add_streamers(miner, ["Category"])

    assert miner.streamers == [category]
    assert category.explicitly_configured is True


def test_add_streamers_attaches_the_watch_streak_cache(monkeypatch):
    from TwitchChannelPointsMiner.classes.entities.Streamer import StreamerSettings

    defaults = StreamerSettings(chat=ChatPresence.NEVER)
    defaults.default()
    defaults.chat = ChatPresence.NEVER
    monkeypatch.setattr(Settings, "streamer_settings", defaults, raising=False)
    cache = object()
    cache_when_checked = []
    twitch = SimpleNamespace(
        get_channel_id=lambda username: "channel-1",
        load_channel_points_context=lambda streamer: None,
        check_streamer_online=lambda streamer: cache_when_checked.append(
            streamer.watch_streak_cache
        ),
    )
    miner = SimpleNamespace(
        streamers=[],
        original_streamers=[],
        twitch=twitch,
        watch_streak_cache=cache,
        ws_pool=SimpleNamespace(submit=lambda topic: None),
        _record_daily_report_streamers=lambda streamers: None,
        _TwitchChannelPointsMiner__subscribe_user_drop_events=lambda: None,
    )

    TwitchChannelPointsMiner._add_streamers(miner, ["newcomer"])

    assert [streamer.username for streamer in miner.streamers] == ["newcomer"]
    # Attached before the first online check, which can call set_online().
    assert cache_when_checked == [cache]
    assert miner.streamers[0].watch_streak_cache is cache


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


def test_websocket_pool_does_not_relisten_an_existing_topic():
    topic = SimpleNamespace(streamer=Streamer("target"))
    websocket = SimpleNamespace(
        topics=[topic],
        pending_topics=[],
        is_opened=True,
        listen=lambda _topic, _token: pytest.fail("unexpected duplicate listen"),
        unlisten=lambda _topic, _token: pytest.fail("unexpected unlisten"),
    )
    pool = WebSocketsPool(
        SimpleNamespace(
            twitch_login=SimpleNamespace(
                get_auth_token=lambda: pytest.fail("unexpected token request")
            )
        ),
        [],
        {},
    )
    pool.ws = [websocket]

    pool.submit(topic)

    assert websocket.topics == [topic]
    assert websocket.pending_topics == []


def test_websocket_pool_reuses_capacity_in_older_socket():
    topic = SimpleNamespace(streamer=Streamer("target"))
    available = SimpleNamespace(
        index=0,
        topics=[object() for _index in range(49)],
        pending_topics=[],
        is_opened=False,
    )
    full = SimpleNamespace(
        index=1,
        topics=[object() for _index in range(50)],
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
    pool.ws = [available, full]

    pool.submit(topic)

    assert len(pool.ws) == 2
    assert available.topics[-1] is topic
    assert available.pending_topics == [topic]
    assert topic not in full.topics


def test_websocket_pool_finds_existing_topic_on_older_socket():
    streamer = Streamer("target")
    streamer.channel_id = 100
    existing_topic = PubsubTopic("video-playback-by-id", streamer=streamer)
    duplicate_topic = PubsubTopic("video-playback-by-id", streamer=streamer)
    existing = SimpleNamespace(
        index=0,
        topics=[existing_topic],
        pending_topics=[],
        is_opened=True,
        listen=lambda _topic, _token: pytest.fail("unexpected duplicate listen"),
    )
    available = SimpleNamespace(
        index=1,
        topics=[],
        pending_topics=[],
        is_opened=False,
    )
    pool = WebSocketsPool(
        SimpleNamespace(
            twitch_login=SimpleNamespace(
                get_auth_token=lambda: pytest.fail("unexpected token request")
            )
        ),
        [],
        {},
    )
    pool.ws = [existing, available]

    pool.submit(duplicate_topic)

    assert existing.topics == [existing_topic]
    assert available.topics == []


def test_websocket_pool_retires_empty_trailing_sockets():
    target = Streamer("target")
    retained = Streamer("retained")
    closed = []
    first = SimpleNamespace(
        index=0,
        topics=[SimpleNamespace(streamer=retained)],
        pending_topics=[],
        is_opened=True,
    )
    trailing = SimpleNamespace(
        index=1,
        topics=[SimpleNamespace(streamer=target)],
        pending_topics=[],
        is_opened=True,
        forced_close=False,
        close=lambda: closed.append(True),
    )
    pool = WebSocketsPool(
        SimpleNamespace(
            twitch_login=SimpleNamespace(
                get_auth_token=lambda: pytest.fail("unexpected token request")
            )
        ),
        [],
        {},
    )
    pool.ws = [first, trailing]

    pool.remove_streamer_topics(target)

    assert pool.ws == [first]
    assert trailing.forced_close is True
    assert closed == [True]


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
        index=0,
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
        forced_close=False,
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

    monkeypatch.setitem(web_config.__globals__, "read_managed_web_config", fail_read)
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
        MINER_CONFIG={"logger_settings": SimpleNamespace(discord=notification)}
    )
    monkeypatch.setattr(
        "TwitchChannelPointsMiner.runner._load_config", lambda _path: loaded
    )
    app = Flask(__name__)

    with app.test_request_context("/config/notifications/discord/test", method="POST"):
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

    with app.test_request_context("/config/notifications/telegram/test", method="POST"):
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

    with app.test_request_context("/config/notifications/discord/test", method="POST"):
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
        MINER_CONFIG={"logger_settings": SimpleNamespace(discord=notification)}
    )
    monkeypatch.setattr(
        "TwitchChannelPointsMiner.runner._load_config", lambda _path: loaded
    )
    app = Flask(__name__)

    with app.test_request_context("/config/notifications/discord/test", method="POST"):
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

    migrate_web_config(config)
    result = read_managed_web_config(config)

    assert result["notifications"]["gotify"]["secrets"] == {"endpoint": False}
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

    assert "email=" not in config.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("provider", "values", "message"),
    [
        ("webhook", {"method": None}, "method must be a string"),
        ("matrix", {"room_id": None}, "room_id must be a string"),
        ("discord", {"webhook_api": 123}, "webhook_api must be a string"),
    ],
)
def test_notification_text_fields_require_strings(tmp_path, provider, values, message):
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

    with pytest.raises(ConfigEditError, match="(?i)managed streamer"):
        migrate_web_config(config)


@pytest.mark.parametrize("categories", ["alpha", {"alpha": True}, [None]])
def test_malformed_managed_categories_fail_cleanly(tmp_path, categories):
    config = tmp_path / "config.py"
    write_config(config)
    (tmp_path / "web-config.json").write_text(
        json.dumps({"categories": categories}), encoding="utf-8"
    )

    with pytest.raises(ConfigEditError, match="Managed categories"):
        migrate_web_config(config)


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

    with pytest.raises(ConfigEditError, match="(?i)stream source"):
        migrate_web_config(config)


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

    with pytest.raises(ConfigEditError, match="(?i)log|daily report"):
        migrate_web_config(config)


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

    with pytest.raises(ConfigEditError, match="(?i)notification"):
        migrate_web_config(config)


def test_managed_web_config_updates_category_drops_override_streamers(tmp_path):
    config = tmp_path / "config.py"
    write_config(config)

    assert read_managed_web_config(config)["category_drops_override_streamers"] is False

    result = update_managed_web_config(
        config,
        {
            "action": "update_sources",
            "values": {},
            "category_drops_override_streamers": True,
        },
    )

    assert result["category_drops_override_streamers"] is True
    assert _load_config(config).MINE_CONFIG["category_drops_override_streamers"] is True

    result = update_managed_web_config(
        config,
        {
            "action": "update_sources",
            "values": {},
            "category_drops_override_streamers": False,
        },
    )

    assert result["category_drops_override_streamers"] is False
    assert (
        _load_config(config).MINE_CONFIG["category_drops_override_streamers"] is False
    )


def test_managed_web_config_rejects_non_boolean_category_drops_override(tmp_path):
    config = tmp_path / "config.py"
    write_config(config)

    with pytest.raises(ConfigEditError):
        update_managed_web_config(
            config,
            {
                "action": "update_sources",
                "values": {},
                "category_drops_override_streamers": "yes",
            },
        )


def _example_config(tmp_path):
    config = tmp_path / "config.py"
    example = Path(__file__).resolve().parent.parent / "config.example.py"
    config.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    return config


def test_inserting_after_commented_last_item_keeps_config_valid(tmp_path):
    # The example's Email(...) ends with "starttls=True,  # Use use_ssl=..."
    # and has no use_ssl keyword, so saving use_ssl inserts after a comment.
    config = _example_config(tmp_path)

    update_managed_web_config(
        config,
        {
            "action": "update_notification",
            "provider": "email",
            "values": {"enabled": True, "use_ssl": True, "starttls": False},
        },
    )

    email = _load_config(config).MINER_CONFIG["logger_settings"].email
    assert email.use_ssl is True
    assert email.starttls is False


def test_insert_config_item_handles_comment_without_trailing_comma(tmp_path):
    config = tmp_path / "config.py"
    config.write_text(
        "VALUES = {\n    'a': 1,\n    'b': 2  # note, with a comma\n}\n",
        encoding="utf-8",
    )

    config_editor._set_dict_items(config, "VALUES", {"c": "3"})

    namespace = {}
    exec(config.read_text(encoding="utf-8"), namespace)
    assert namespace["VALUES"] == {"a": 1, "b": 2, "c": 3}


def test_email_tls_defaults_are_reported_when_omitted(tmp_path):
    config = _example_config(tmp_path)
    source = config.read_text(encoding="utf-8")
    config.write_text(
        source.replace(
            "            starttls=True,  # Use use_ssl=True and starttls=False "
            "for implicit TLS (usually port 465)\n",
            "",
        ),
        encoding="utf-8",
    )
    assert "starttls" not in config.read_text(encoding="utf-8").split("email=")[1][:400]

    fields = read_managed_web_config(config)["notifications"]["email"]["fields"]

    assert fields["starttls"] is True
    assert fields["use_ssl"] is False


@pytest.mark.parametrize(
    "body",
    [
        "{'a': (1)}",
        "{'a': ((1))}",
        "{'a': (1),}",
        "{\n    'a': (\n        1\n    )  # trailing, comment\n}",
        "{\n    'a': (1)  # c\n    ,\n}",
        "{'a': (1, 2)}",
    ],
)
def test_insert_config_item_handles_parenthesized_last_value(tmp_path, body):
    config = tmp_path / "config.py"
    config.write_text(f"VALUES = {body}\n", encoding="utf-8")
    namespace = {}
    exec(config.read_text(encoding="utf-8"), namespace)
    expected = {**namespace["VALUES"], "c": 3}

    config_editor._set_dict_items(config, "VALUES", {"c": "3"})

    namespace = {}
    exec(config.read_text(encoding="utf-8"), namespace)
    assert namespace["VALUES"] == expected


def test_insert_config_item_refuses_to_write_unparseable_config(tmp_path, monkeypatch):
    config = tmp_path / "config.py"
    original = "VALUES = {'a': 1}\n"
    config.write_text(original, encoding="utf-8")
    monkeypatch.setattr(config_editor, "_scan_item_tail", lambda segment: (0, True))

    with pytest.raises(ConfigEditError, match="would not parse"):
        config_editor._set_dict_items(config, "VALUES", {"c": "3"})

    assert config.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    "provider, module_name",
    [
        ("telegram", "Telegram"),
        ("discord", "Discord"),
        ("webhook", "Webhook"),
        ("email", "Email"),
        ("matrix", "Matrix"),
        ("pushover", "Pushover"),
        ("gotify", "Gotify"),
        ("ntfy", "Ntfy"),
    ],
)
def test_notification_constructor_required_matches_signatures(provider, module_name):
    import importlib
    import inspect

    module = importlib.import_module(f"TwitchChannelPointsMiner.classes.{module_name}")
    parameters = inspect.signature(getattr(module, module_name)).parameters
    required = {
        name
        for name, parameter in parameters.items()
        if parameter.default is inspect.Parameter.empty
    }

    assert config_editor.NOTIFICATION_CONSTRUCTOR_REQUIRED[provider] == required


@pytest.mark.parametrize(
    "provider, values, missing",
    [
        (
            "webhook",
            {"endpoint": "https://hooks.example/x", "events": ["DROP_CLAIM"]},
            "method",
        ),
        (
            "pushover",
            {"userkey": "u", "token": "t", "events": ["DROP_CLAIM"], "priority": 0},
            "sound",
        ),
        (
            "gotify",
            {"endpoint": "https://gotify.example", "events": ["DROP_CLAIM"]},
            "priority",
        ),
        ("ntfy", {"topic": "miner-alerts"}, "events"),
    ],
)
def test_enabling_notification_requires_constructor_arguments(
    tmp_path, provider, values, missing
):
    config = tmp_path / "config.py"
    write_config(config)
    original = config.read_text(encoding="utf-8")

    with pytest.raises(ConfigEditError, match=f"{provider} requires: {missing}"):
        update_managed_web_config(
            config,
            {
                "action": "update_notification",
                "provider": provider,
                "values": {"enabled": True, **values},
            },
        )

    assert config.read_text(encoding="utf-8") == original


def test_enabled_webhook_saved_from_dashboard_loads(tmp_path):
    config = tmp_path / "config.py"
    write_config(config)

    update_managed_web_config(
        config,
        {
            "action": "update_notification",
            "provider": "webhook",
            "values": {
                "enabled": True,
                "endpoint": "https://hooks.example/x",
                "method": "POST",
                "events": [],
            },
        },
    )

    webhook = _load_config(config).MINER_CONFIG["logger_settings"].webhook
    assert webhook.endpoint == "https://hooks.example/x"
    assert webhook.method == "POST"
    assert webhook.events == []


COMMENTED_STREAMERS = """\
STREAMERS = [
    # Favourites first.
    Streamer("one", settings=StreamerSettings(favorite=True)),  # main channel
    "two",  # second
    # Weekend streams.
    "three",
    EXTRA_STREAMER,
]
"""


def write_commented_config(path, streamers=COMMENTED_STREAMERS):
    write_config(path)
    source = path.read_text(encoding="utf-8")
    source = source.replace(
        'STREAMERS = [Streamer("one", settings=StreamerSettings(favorite=True)), "two"]\n',
        'EXTRA_STREAMER = "extra"\n' + streamers,
    )
    path.write_text(source, encoding="utf-8")


def _usernames(config):
    return [
        getattr(streamer, "username", streamer)
        for streamer in _load_config(config).STREAMERS
    ]


def test_streamer_edits_keep_comments_and_unmanaged_entries(tmp_path):
    config = tmp_path / "config.py"
    write_commented_config(config)

    update_managed_web_config(
        config, {"action": "add", "kind": "streamers", "value": "four"}
    )
    update_managed_web_config(
        config,
        {"action": "reorder_streamers", "usernames": ["three", "one", "two", "four"]},
    )
    update_managed_web_config(
        config, {"action": "remove", "kind": "streamers", "value": "two"}
    )
    update_managed_web_config(
        config,
        {
            "action": "update_streamer",
            "username": "three",
            "settings": {"chat": "NEVER"},
        },
    )

    source = config.read_text(encoding="utf-8")
    for comment in ("# Favourites first.", "# main channel", "# Weekend streams."):
        assert comment in source
    assert "EXTRA_STREAMER," in source
    # A comment after an entry on its line moves with the entry.
    assert (
        'Streamer("one", settings=StreamerSettings(favorite=True)),  # main channel'
        in source
    )
    assert '    "four",\n]' in source
    assert _usernames(config) == ["three", "one", "extra", "four"]
    loaded = _load_config(config)
    assert loaded.STREAMERS[0].settings.chat is ChatPresence.NEVER
    assert loaded.STREAMERS[1].settings.favorite is True


def test_update_streamer_writes_only_changed_settings(tmp_path):
    config = tmp_path / "config.py"
    write_config(config)

    update_managed_web_config(
        config,
        {
            "action": "update_streamer",
            "username": "two",
            "settings": {"favorite": True},
        },
    )

    assert (
        'STREAMERS = [Streamer("one", settings=StreamerSettings(favorite=True)), '
        'Streamer("two", settings=StreamerSettings(favorite=True))]'
    ) in config.read_text(encoding="utf-8")


def test_update_streamer_edits_positional_settings_without_duplicating(tmp_path):
    config = tmp_path / "config.py"
    write_commented_config(
        config,
        'STREAMERS = [Streamer("one", StreamerSettings(favorite=True)), "two"]\n',
    )

    update_managed_web_config(
        config,
        {
            "action": "update_streamer",
            "username": "one",
            "settings": {"chat": "ALWAYS"},
        },
    )

    streamers_line = next(
        line
        for line in config.read_text(encoding="utf-8").splitlines()
        if line.startswith("STREAMERS = ")
    )
    assert streamers_line.count("StreamerSettings(") == 1
    assert "settings=" not in streamers_line
    loaded = _load_config(config)
    assert loaded.STREAMERS[0].settings.favorite is True
    assert loaded.STREAMERS[0].settings.chat is ChatPresence.ALWAYS


def test_update_streamer_refuses_settings_defined_elsewhere(tmp_path):
    config = tmp_path / "config.py"
    write_commented_config(
        config,
        "ONE_SETTINGS = StreamerSettings(favorite=True, make_predictions=False)\n"
        'STREAMERS = [Streamer("one", settings=ONE_SETTINGS), "two"]\n',
    )
    before = config.read_text(encoding="utf-8")

    with pytest.raises(ConfigEditError, match="Edit them in config.py"):
        update_managed_web_config(
            config,
            {
                "action": "update_streamer",
                "username": "one",
                "settings": {"chat": "NEVER"},
            },
        )

    assert config.read_text(encoding="utf-8") == before


@pytest.mark.parametrize(
    "streamers, removed, expected",
    [
        ('STREAMERS = ["one", "two", "three"]\n', "one", ["two", "three"]),
        ('STREAMERS = ["one", "two", "three"]\n', "two", ["one", "three"]),
        ('STREAMERS = ["one", "two", "three"]\n', "three", ["one", "two"]),
        ('STREAMERS = ["one",]\n', "one", []),
        ('STREAMERS = [\n    "one",\n    "two",\n]\n', "two", ["one"]),
        ('STREAMERS = [\n    "one",\n    "two",\n]\n', "one", ["two"]),
    ],
)
def test_removing_streamers_keeps_the_list_valid(
    tmp_path, streamers, removed, expected
):
    config = tmp_path / "config.py"
    write_commented_config(config, streamers)

    update_managed_web_config(
        config, {"action": "remove", "kind": "streamers", "value": removed}
    )

    assert _usernames(config) == expected


@pytest.mark.parametrize(
    "streamers, removed, expected, kept, dropped",
    [
        # A comment above the next entry belongs to that entry.
        (
            'STREAMERS = [\n    "one",\n    # about two\n    "two",\n]\n',
            "one",
            ["two"],
            ["# about two"],
            [],
        ),
        # The previous entry's trailing comment stays when the last goes.
        (
            'STREAMERS = [\n    "one",  # one note\n    "two",\n]\n',
            "two",
            ["one"],
            ["# one note"],
            [],
        ),
        (
            'STREAMERS = [\n    "one",  # one note\n    "two"\n]\n',
            "two",
            ["one"],
            ["# one note"],
            [],
        ),
        # The closing bracket on the removed entry's line.
        (
            'STREAMERS = [\n    "one",  # one note\n    "two"]\n',
            "two",
            ["one"],
            ["# one note"],
            [],
        ),
        # The first entry shares its line with the opening bracket.
        (
            'STREAMERS = ["one",  # one note\n    # about two\n    "two",\n]\n',
            "one",
            ["two"],
            ["# about two"],
            ["# one note"],
        ),
        (
            'STREAMERS = ["one", "two", "three"]  # list note\n',
            "two",
            ["one", "three"],
            ["# list note"],
            [],
        ),
        # A multi-line entry takes its inner comments with it.
        (
            'STREAMERS = [\n    Streamer(\n        "one",  # inner\n    ),\n'
            '    # about two\n    "two",\n]\n',
            "one",
            ["two"],
            ["# about two"],
            ["# inner"],
        ),
        # The removed entry's own trailing comment goes with it.
        (
            'STREAMERS = [\n    "one",  # one note\n    "two",  # two note\n]\n',
            "one",
            ["two"],
            ["# two note"],
            ["# one note"],
        ),
    ],
)
def test_removing_a_streamer_keeps_other_entries_comments(
    tmp_path, streamers, removed, expected, kept, dropped
):
    config = tmp_path / "config.py"
    write_commented_config(config, streamers)

    update_managed_web_config(
        config, {"action": "remove", "kind": "streamers", "value": removed}
    )

    source = config.read_text(encoding="utf-8")
    assert _usernames(config) == expected
    for comment in kept:
        assert comment in source
    for comment in dropped:
        assert comment not in source


def test_positional_settings_variable_can_be_read_but_not_edited(tmp_path):
    config = tmp_path / "config.py"
    write_commented_config(
        config,
        "ONE_SETTINGS = StreamerSettings(favorite=True)\n"
        'STREAMERS = [Streamer("one", ONE_SETTINGS), "two"]\n',
    )

    managed = read_managed_web_config(config)
    assert [item["username"] for item in managed["streamers"]] == ["one", "two"]
    update_managed_web_config(
        config, {"action": "add", "kind": "streamers", "value": "three"}
    )
    assert _usernames(config) == ["one", "two", "three"]

    before = config.read_text(encoding="utf-8")
    with pytest.raises(ConfigEditError, match="Edit them in config.py"):
        update_managed_web_config(
            config,
            {
                "action": "update_streamer",
                "username": "one",
                "settings": {"chat": "NEVER"},
            },
        )
    assert config.read_text(encoding="utf-8") == before


def test_refused_streamer_edit_does_not_add_imports(tmp_path):
    config = tmp_path / "config.py"
    write_commented_config(
        config,
        "ONE_SETTINGS = StreamerSettings(favorite=True)\n"
        'STREAMERS = [Streamer("one", settings=ONE_SETTINGS), "two"]\n',
    )
    import_line = (
        "from TwitchChannelPointsMiner.classes.entities.Streamer import "
        "Streamer, StreamerSettings\n"
    )
    source = config.read_text(encoding="utf-8")
    assert import_line in source
    config.write_text(
        source.replace(
            import_line,
            "from TwitchChannelPointsMiner.classes."
            "entities.Streamer import (\n    Streamer,\n    StreamerSettings,\n)\n",
        ),
        encoding="utf-8",
    )
    before = config.read_text(encoding="utf-8")

    with pytest.raises(ConfigEditError, match="Edit them in config.py"):
        update_managed_web_config(
            config,
            {
                "action": "update_streamer",
                "username": "one",
                "settings": {"chat": "NEVER"},
            },
        )

    assert config.read_text(encoding="utf-8") == before


@pytest.mark.parametrize(
    "streamers",
    [
        'OPTS = {}\nSTREAMERS = [Streamer("one", **OPTS), "two"]\n',
        'ARGS = ()\nSTREAMERS = [Streamer("one", *ARGS), "two"]\n',
        'KW = {}\nSTREAMERS = [Streamer("one", settings=StreamerSettings(**KW)), "two"]\n',
        'STREAMERS = [Streamer("one", settings=StreamerSettings(True)), "two"]\n',
    ],
)
def test_update_streamer_refuses_unpacked_or_positional_arguments(tmp_path, streamers):
    # Adding a keyword next to ** unpacking can repeat one the unpacked dict
    # already supplies, which fails when config.py loads.
    config = tmp_path / "config.py"
    write_commented_config(config, streamers)
    before = config.read_text(encoding="utf-8")

    with pytest.raises(ConfigEditError, match="config.py instead"):
        update_managed_web_config(
            config,
            {
                "action": "update_streamer",
                "username": "one",
                "settings": {"chat": "NEVER"},
            },
        )

    assert config.read_text(encoding="utf-8") == before


def test_settings_insertion_indents_from_the_right_line_after_a_form_feed(tmp_path):
    config = tmp_path / "config.py"
    write_commented_config(
        config,
        "# a\x0cform feed and a \u2028 line separator\n"
        'STREAMERS = [\n    Streamer(\n        "one"\n    ),\n    "two",\n]\n',
    )

    update_managed_web_config(
        config,
        {
            "action": "update_streamer",
            "username": "one",
            "settings": {"chat": "NEVER"},
        },
    )

    source = config.read_text(encoding="utf-8")
    assert "        settings=StreamerSettings(chat=ChatPresence.NEVER)," in source
    assert _load_config(config).STREAMERS[0].settings.chat is ChatPresence.NEVER


@pytest.mark.parametrize("marker", ["\u2028", "\x0c", "\x1c"])
def test_replaced_value_keeps_its_offsets_after_a_non_line_break(tmp_path, marker):
    # str.splitlines() treats these as line breaks while Python's tokenizer
    # does not, so the old offset arithmetic spliced the value in the wrong
    # place and left config.py unparseable.
    config = tmp_path / "config.py"
    config.write_text(
        f"# a {marker} note\n"
        "MINER_CONFIG = {\n"
        '    "username": "x",\n'
        '    "update_check": True,\n'
        "}\n"
        "STREAMERS = []\n"
        'MINE_CONFIG = {"categories": []}\n'
        "ANALYTICS_CONFIG = None\n",
        encoding="utf-8",
    )

    update_managed_web_config(
        config, {"action": "update_updates", "values": {"enabled": False}}
    )

    loaded = _load_config(config)
    assert loaded.MINER_CONFIG["username"] == "x"
    assert loaded.MINER_CONFIG["update_check"] is False


def test_replaced_call_keyword_keeps_its_offsets_after_a_non_line_break(tmp_path):
    config = tmp_path / "config.py"
    write_config(config)
    source = config.read_text(encoding="utf-8")
    config.write_text("# a\u2028note and a \x0c form feed\n" + source, encoding="utf-8")

    update_managed_web_config(
        config, {"action": "update_logging", "values": {"console_level": "DEBUG"}}
    )

    assert _load_config(config).MINER_CONFIG["logger_settings"].console_level == (
        logging.DEBUG
    )


def test_analytics_enable_keeps_its_offsets_after_a_non_line_break(tmp_path):
    config = tmp_path / "config.py"
    config.write_text(
        "# a\u2028note and a \x0c form feed\n"
        'MINER_CONFIG = {\n    "username": "x",\n}\n'
        "STREAMERS = []\n"
        'MINE_CONFIG = {"categories": []}\n'
        "ANALYTICS_CONFIG = None\n",
        encoding="utf-8",
    )

    config_editor.enable_analytics_dashboard(config, "secret")

    loaded = _load_config(config)
    assert loaded.MINER_CONFIG["enable_analytics"] is True
    assert loaded.ANALYTICS_CONFIG["port"] == 54455


def test_replace_config_node_refuses_to_write_unparseable_config(tmp_path):
    config = tmp_path / "config.py"
    original = 'VALUES = {"a": 1}\n'
    config.write_text(original, encoding="utf-8")
    node = config_editor._assignment(ast.parse(original), "VALUES").values[0]

    with pytest.raises(ConfigEditError, match="would not parse"):
        config_editor._replace_config_node(config, node, "(")

    assert config.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "add", "kind": "categories", "value": "gamma"},
        {"action": "remove", "kind": "categories", "value": "alpha"},
        {"action": "reorder_categories", "categories": ["beta", "alpha"]},
    ],
    ids=["add", "remove", "reorder"],
)
@pytest.mark.parametrize("marker", ["\u2028", "\x0c"])
def test_category_save_keeps_its_offsets_after_a_non_line_break(
    tmp_path, marker, payload
):
    # The category list is rewritten whole rather than node by node, but its
    # offsets were computed the same broken way.
    config = tmp_path / "config.py"
    config.write_text(
        f"# a {marker} note\n"
        'MINER_CONFIG = {\n    "username": "x",\n}\n'
        "STREAMERS = []\n"
        'MINE_CONFIG = {\n    "categories": ["alpha", "beta"],\n}\n'
        "ANALYTICS_CONFIG = None\n",
        encoding="utf-8",
    )

    update_managed_web_config(config, payload)

    categories = _load_config(config).MINE_CONFIG["categories"]
    if payload["action"] == "add":
        assert categories == ["alpha", "beta", "gamma"]
    elif payload["action"] == "remove":
        assert categories == ["beta"]
    else:
        assert categories == ["beta", "alpha"]


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "update_updates", "values": {"enabled": False}},
        {"action": "update_login", "values": {"android": True}},
    ],
    ids=["replace", "insert"],
)
def test_dict_edits_keep_crlf_line_breaks(tmp_path, payload):
    config = tmp_path / "config.py"
    write_config(config)
    source = config.read_text(encoding="utf-8")
    config.write_bytes(source.replace("\n", "\r\n").encode("utf-8"))

    update_managed_web_config(config, payload)

    written = config.read_bytes()
    assert b"\n" in written
    assert written.count(b"\n") == written.count(b"\r\n")
    assert b"\r\r" not in written


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "add", "kind": "categories", "value": "gamma"},
        {"action": "remove", "kind": "categories", "value": "alpha"},
        {"action": "reorder_categories", "categories": ["beta", "alpha"]},
    ],
    ids=["add", "remove", "reorder"],
)
def test_category_save_keeps_crlf_line_breaks(tmp_path, payload):
    config = tmp_path / "config.py"
    write_config(config)
    source = config.read_text(encoding="utf-8")
    config.write_bytes(source.replace("\n", "\r\n").encode("utf-8"))

    update_managed_web_config(config, payload)

    written = config.read_bytes()
    assert b"\n" in written
    assert written.count(b"\n") == written.count(b"\r\n")
    assert b"\r\r" not in written


def test_analytics_enable_keeps_crlf_line_breaks(tmp_path):
    config = tmp_path / "config.py"
    write_config(config)
    source = config.read_text(encoding="utf-8")
    config.write_bytes(source.replace("\n", "\r\n").encode("utf-8"))

    config_editor.enable_analytics_dashboard(config, "secret")

    written = config.read_bytes()
    assert b"\n" in written
    assert written.count(b"\n") == written.count(b"\r\n")
    assert b"\r\r" not in written


def test_import_insertion_goes_after_a_future_import(tmp_path):
    # An import above a __future__ import parses but does not compile.
    from TwitchChannelPointsMiner.config_migration import CONFIG_VERSION

    config = tmp_path / "config.py"
    config.write_text(
        "from __future__ import annotations\n"
        f"CONFIG_VERSION = {CONFIG_VERSION}\n"
        'MINER_CONFIG = {"username": "x"}\n'
        "STREAMERS = []\n"
        'MINE_CONFIG = {"categories": []}\n'
        "ANALYTICS_CONFIG = None\n",
        encoding="utf-8",
    )

    update_managed_web_config(
        config, {"action": "update_sources", "values": {"streamers": True}}
    )

    assert config.read_text(encoding="utf-8").startswith(
        "from __future__ import annotations\n"
        "from TwitchChannelPointsMiner.classes.Settings import StreamerSource\n"
    )
    assert StreamerSource.STREAMERS in (
        _load_config(config).MINER_CONFIG["streamer_source_priority"]
    )


def test_import_insertion_after_a_non_line_break_in_the_first_line(tmp_path):
    # The first line is treated as a coding declaration when it mentions
    # "coding". Splitting it on U+2028 put the import inside the comment.
    config = tmp_path / "config.py"
    config.write_text(
        "# coding: utf-8 \u2028 pointer\n"
        'MINER_CONFIG = {"username": "x"}\n'
        "STREAMERS = []\n"
        'MINE_CONFIG = {"categories": []}\n'
        "ANALYTICS_CONFIG = None\n",
        encoding="utf-8",
    )

    update_managed_web_config(
        config, {"action": "update_sources", "values": {"streamers": True}}
    )

    source = config.read_text(encoding="utf-8")
    assert config.read_bytes().splitlines()[1] == (
        b"from TwitchChannelPointsMiner.classes.Settings import StreamerSource"
    )
    namespace = {}
    exec(compile(source, str(config), "exec"), namespace)
    assert (
        StreamerSource.STREAMERS
        in namespace["MINER_CONFIG"]["streamer_source_priority"]
    )


def test_entry_with_a_comment_after_its_redundant_parenthesis_can_be_removed(
    tmp_path,
):
    config = tmp_path / "config.py"
    write_commented_config(
        config,
        "STREAMERS = [\n"
        "    # keep me\n"
        '    (  # why\n        "one"\n    ),\n'
        '    "two",  # two\n'
        "]\n",
    )

    update_managed_web_config(
        config, {"action": "remove", "kind": "streamers", "value": "one"}
    )

    source = config.read_text(encoding="utf-8")
    assert 'STREAMERS = [\n    # keep me\n    "two",  # two\n]\n' in source
    assert _usernames(config) == ["two"]


def test_unremovable_entry_is_refused_with_a_clear_message(tmp_path):
    config = tmp_path / "config.py"
    write_commented_config(
        config, 'STREAMERS = [\n    "one"\n    , "two"\n    , "three"\n]\n'
    )
    before = config.read_bytes()

    with pytest.raises(ConfigEditError, match="Remove it in config.py instead"):
        update_managed_web_config(
            config, {"action": "remove", "kind": "streamers", "value": "one"}
        )

    assert config.read_bytes() == before


def test_parenthesis_in_a_comment_does_not_block_removal(tmp_path):
    config = tmp_path / "config.py"
    write_commented_config(
        config,
        'STREAMERS = [\n    (\n        "one"  # note (x) )\n    ),\n    "two",\n]\n',
    )

    update_managed_web_config(
        config, {"action": "remove", "kind": "streamers", "value": "one"}
    )

    assert 'STREAMERS = [\n    "two",\n]\n' in config.read_text(encoding="utf-8")


def test_update_to_a_shown_default_is_refused_for_a_variable_setting(tmp_path):
    config = tmp_path / "config.py"
    write_commented_config(
        config,
        "FAV = True\n"
        'STREAMERS = [Streamer("one", settings=StreamerSettings(favorite=FAV)), "two"]\n',
    )
    before = config.read_bytes()

    with pytest.raises(ConfigEditError, match="Edit it in config.py"):
        update_managed_web_config(
            config,
            {
                "action": "update_streamer",
                "username": "one",
                "settings": {"favorite": False},
            },
        )
    assert config.read_bytes() == before

    # Other settings of the same streamer can still be changed.
    update_managed_web_config(
        config,
        {
            "action": "update_streamer",
            "username": "one",
            "settings": {"chat": "NEVER"},
        },
    )
    assert "favorite=FAV" in config.read_text(encoding="utf-8")
    assert _load_config(config).STREAMERS[0].settings.chat is ChatPresence.NEVER


def test_legacy_migration_refuses_settings_it_cant_write(tmp_path):
    # The old dashboard applied these settings over config.py at runtime;
    # dropping them would change what the miner does.
    config = tmp_path / "config.py"
    write_commented_config(
        config,
        "ONE_SETTINGS = StreamerSettings(favorite=True)\n"
        'STREAMERS = [Streamer("one", settings=ONE_SETTINGS), "two"]\n',
    )
    shown = read_managed_web_config(config)["streamers"]
    (tmp_path / "web-config.json").write_text(
        json.dumps({"streamers": shown}), encoding="utf-8"
    )
    before = config.read_bytes()

    with pytest.raises(ConfigEditError, match="delete web-config.json"):
        migrate_web_config(config)

    assert config.read_bytes() == before
    assert (tmp_path / "web-config.json").exists()


def test_refused_legacy_migration_names_the_overrides_file(tmp_path):
    config = tmp_path / "config.py"
    write_commented_config(
        config,
        "ONE_SETTINGS = StreamerSettings(favorite=True)\n"
        'STREAMERS = [Streamer("one", settings=ONE_SETTINGS), "two"]\n',
    )
    (tmp_path / "web-config.json").write_text(
        json.dumps(
            {
                "streamers": [
                    {"username": "one", "settings": {"chat": "NEVER"}},
                    {"username": "two"},
                ]
            }
        ),
        encoding="utf-8",
    )
    before = config.read_bytes()

    with pytest.raises(ConfigEditError, match="delete web-config.json"):
        migrate_web_config(config)

    assert config.read_bytes() == before


def test_comma_in_a_comment_is_not_taken_for_the_separator(tmp_path):
    config = tmp_path / "config.py"
    write_commented_config(
        config, 'STREAMERS = ["one"  # note, EXTRA_STREAMER\n    , "two"]\n'
    )

    update_managed_web_config(
        config, {"action": "remove", "kind": "streamers", "value": "one"}
    )

    assert 'STREAMERS = ["two"]\n' in config.read_text(encoding="utf-8")
    assert _usernames(config) == ["two"]


@pytest.mark.parametrize(
    ("streamers", "expected"),
    [
        (
            'STREAMERS = [\n    "one"  # one note\n    , "two"\n]\n',
            'STREAMERS = [\n    "one"  # one note\n]\n',
        ),
        (
            'STREAMERS = [\n    "one"  # one note\n    , "two"]\n',
            'STREAMERS = [\n    "one"  # one note\n    ]\n',
        ),
    ],
)
def test_removing_the_last_entry_keeps_the_previous_entrys_comment(
    tmp_path, streamers, expected
):
    config = tmp_path / "config.py"
    write_commented_config(config, streamers)

    update_managed_web_config(
        config, {"action": "remove", "kind": "streamers", "value": "two"}
    )

    assert expected in config.read_text(encoding="utf-8")
    assert _usernames(config) == ["one"]


@pytest.mark.parametrize(
    "defaults",
    ["DEFAULTS", "StreamerSettings(favorite=FAVORITE)", "dict(favorite=FAVORITE)"],
)
def test_update_is_written_when_the_inherited_default_cant_be_read(tmp_path, defaults):
    # The dashboard shows favorite=False for these defaults, though the
    # miner inherits True, so saving False must still be written.
    config = tmp_path / "config.py"
    write_commented_config(config, 'STREAMERS = ["one", "two"]\n')
    source = config.read_text(encoding="utf-8")
    source = source.replace(
        '"streamer_settings": StreamerSettings(favorite=False, chat=ChatPresence.ONLINE)',
        f'"streamer_settings": {defaults}',
    )
    source = source.replace(
        "MINER_CONFIG = {",
        "FAVORITE = True\nDEFAULTS = StreamerSettings(favorite=FAVORITE)\n"
        "MINER_CONFIG = {",
    )
    config.write_text(source, encoding="utf-8")
    assert f'"streamer_settings": {defaults}' in source

    update_managed_web_config(
        config,
        {
            "action": "update_streamer",
            "username": "one",
            "settings": {"favorite": False},
        },
    )

    assert 'Streamer("one", settings=StreamerSettings(favorite=False))' in (
        config.read_text(encoding="utf-8")
    )
    assert _load_config(config).STREAMERS[0].settings.favorite is False


@pytest.mark.parametrize(
    ("streamers", "value"),
    [
        ('STREAMERS = [\n    "a"\n    , "b"\n    # keep, me\n    , "c"\n]\n', "b"),
        ('STREAMERS = [\n    "a"\n    , "b"\n    , "c"\n]\n', "a"),
    ],
)
def test_removal_that_would_lose_another_comment_or_break_the_list_is_refused(
    tmp_path, streamers, value
):
    config = tmp_path / "config.py"
    write_commented_config(config, streamers)
    before = config.read_bytes()

    with pytest.raises(ConfigEditError, match="Remove it in config.py instead"):
        update_managed_web_config(
            config, {"action": "remove", "kind": "streamers", "value": value}
        )

    assert config.read_bytes() == before


@pytest.mark.parametrize(
    "miner_tail", ["    **BASE,\n", '    "streamer_settings": {**BASE},\n']
)
def test_update_is_written_when_unpacking_may_supply_the_defaults(tmp_path, miner_tail):
    config = tmp_path / "config.py"
    write_commented_config(config, 'STREAMERS = ["one", "two"]\n')
    source = config.read_text(encoding="utf-8")
    source = source.replace(
        '    "streamer_settings": StreamerSettings(favorite=False, chat=ChatPresence.ONLINE),\n',
        miner_tail,
    )
    source = source.replace(
        "MINER_CONFIG = {",
        'BASE = {"streamer_settings": StreamerSettings(favorite=True)}\n'
        "MINER_CONFIG = {",
    )
    config.write_text(source, encoding="utf-8")
    assert miner_tail in source

    update_managed_web_config(
        config,
        {
            "action": "update_streamer",
            "username": "one",
            "settings": {"favorite": False},
        },
    )

    assert 'Streamer("one", settings=StreamerSettings(favorite=False))' in (
        config.read_text(encoding="utf-8")
    )


@pytest.mark.parametrize(
    "assignment_name", ["MINER_CONFIG", "MINE_CONFIG", "ANALYTICS_CONFIG"]
)
def test_dict_edit_refuses_an_unpacking_that_would_override_the_value(
    tmp_path, assignment_name
):
    config = tmp_path / "config.py"
    config.write_text(
        f'{assignment_name} = {{\n    "value": 1,\n    **{{"value": 2}},\n}}\n',
        encoding="utf-8",
    )
    before = config.read_text(encoding="utf-8")

    with pytest.raises(ConfigEditError, match="Edit 'value' in config.py"):
        config_editor._set_dict_items(config, assignment_name, {"value": "3"})

    assert config.read_text(encoding="utf-8") == before


def test_update_refuses_an_unreadable_unpacking_that_may_supply_the_key(tmp_path):
    config = tmp_path / "config.py"
    config.write_text(
        "BASE = {}\n"
        "MINER_CONFIG = {\n"
        '    "username": "x",\n'
        "    **BASE,\n"
        "}\n"
        "STREAMERS = []\n"
        'MINE_CONFIG = {"categories": []}\n'
        "ANALYTICS_CONFIG = None\n",
        encoding="utf-8",
    )
    before = config.read_text(encoding="utf-8")

    with pytest.raises(ConfigEditError, match="Edit 'update_check' in config.py"):
        update_managed_web_config(
            config, {"action": "update_updates", "values": {"enabled": False}}
        )

    assert config.read_text(encoding="utf-8") == before


@pytest.mark.parametrize(
    "miner_body",
    [
        # A literal unpacking after the value that doesn't name it is safe.
        '    "update_check": True,\n    **{"other": 1},\n',
        # The value comes after the unpacking, so it wins at runtime.
        '    **BASE,\n    "update_check": True,\n',
        # The new entry is inserted after the unpacking, so it wins too.
        '    **{"username": "y"},\n',
    ],
)
def test_update_edits_when_unpacking_cannot_override_the_value(tmp_path, miner_body):
    config = tmp_path / "config.py"
    config.write_text(
        'BASE = {"update_check": True}\n'
        "MINER_CONFIG = {\n"
        f"{miner_body}"
        "}\n"
        "STREAMERS = []\n"
        'MINE_CONFIG = {"categories": []}\n'
        "ANALYTICS_CONFIG = None\n",
        encoding="utf-8",
    )

    update_managed_web_config(
        config, {"action": "update_updates", "values": {"enabled": False}}
    )

    assert _load_config(config).MINER_CONFIG["update_check"] is False


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "update_logging", "values": {"console_level": "DEBUG"}},
        {
            "action": "update_notification",
            "provider": "discord",
            "values": {
                "enabled": True,
                "webhook_api": "https://example/hook",
                "events": [],
            },
        },
    ],
    ids=["logging", "notification"],
)
def test_nested_logger_settings_edits_refuse_an_override(tmp_path, payload):
    # logger_settings is edited through _write_call_keywords, not
    # _set_dict_items, but a later unpacking overrides it the same way.
    config = tmp_path / "config.py"
    config.write_text(
        "import logging\n"
        "from TwitchChannelPointsMiner.logger import LoggerSettings\n"
        "BASE = {}\n"
        "MINER_CONFIG = {\n"
        '    "username": "x",\n'
        '    "logger_settings": LoggerSettings(console_level=logging.INFO),\n'
        "    **BASE,\n"
        "}\n"
        "STREAMERS = []\n"
        'MINE_CONFIG = {"categories": []}\n'
        "ANALYTICS_CONFIG = None\n",
        encoding="utf-8",
    )
    before = config.read_text(encoding="utf-8")

    with pytest.raises(ConfigEditError, match="Edit 'logger_settings' in config.py"):
        update_managed_web_config(config, payload)

    assert config.read_text(encoding="utf-8") == before


def test_nested_logger_settings_edits_when_unpacking_cannot_override(tmp_path):
    config = tmp_path / "config.py"
    config.write_text(
        "import logging\n"
        "from TwitchChannelPointsMiner.logger import LoggerSettings\n"
        "MINER_CONFIG = {\n"
        '    "username": "x",\n'
        '    "logger_settings": LoggerSettings(console_level=logging.INFO),\n'
        '    **{"other": 1},\n'
        "}\n"
        "STREAMERS = []\n"
        'MINE_CONFIG = {"categories": []}\n'
        "ANALYTICS_CONFIG = None\n",
        encoding="utf-8",
    )

    update_managed_web_config(
        config, {"action": "update_logging", "values": {"console_level": "DEBUG"}}
    )

    assert _load_config(config).MINER_CONFIG["logger_settings"].console_level == (
        logging.DEBUG
    )


def test_reorder_leaves_code_after_the_closing_bracket_alone(tmp_path):
    config = tmp_path / "config.py"
    write_commented_config(
        config,
        'STREAMERS = [\n    "one",  # keep"\n    "two"]; LABEL = "#label"\n',
    )

    update_managed_web_config(
        config, {"action": "reorder_streamers", "usernames": ["two", "one"]}
    )

    source = config.read_text(encoding="utf-8")
    assert 'STREAMERS = [\n    "two",\n    "one"  # keep"\n]; LABEL = "#label"\n' in (
        source
    )
    assert _load_config(config).LABEL == "#label"


def test_reorder_leaves_entries_that_stay_put_and_comment_spacing_alone(tmp_path):
    config = tmp_path / "config.py"
    write_commented_config(
        config,
        'EXTRA_STREAMER = "extra"\n'
        "STREAMERS = [\n"
        '    "one",           # first\n'
        "    EXTRA_STREAMER,      # keep spacing   \n"
        '    "two",           # second\n'
        "]\n",
    )

    update_managed_web_config(
        config, {"action": "reorder_streamers", "usernames": ["two", "one"]}
    )

    source = config.read_text(encoding="utf-8")
    assert (
        "STREAMERS = [\n"
        '    "two",           # second\n'
        "    EXTRA_STREAMER,      # keep spacing   \n"
        '    "one",           # first\n'
        "]\n"
    ) in source


def test_migrated_new_streamer_gets_settings_its_unreadable_defaults_hide(
    tmp_path,
):
    config = tmp_path / "config.py"
    write_commented_config(config, 'STREAMERS = ["one"]\n')
    source = config.read_text(encoding="utf-8")
    source = source.replace(
        '"streamer_settings": StreamerSettings(favorite=False, chat=ChatPresence.ONLINE)',
        '"streamer_settings": DEFAULTS',
    ).replace(
        "MINER_CONFIG = {",
        "DEFAULTS = StreamerSettings(favorite=True)\nMINER_CONFIG = {",
    )
    config.write_text(source, encoding="utf-8")
    (tmp_path / "web-config.json").write_text(
        json.dumps(
            {
                "streamers": [
                    {"username": "one"},
                    {"username": "new", "settings": {"favorite": False}},
                ]
            }
        ),
        encoding="utf-8",
    )

    migrate_web_config(config)

    streamers = _load_config(config).STREAMERS
    assert [getattr(item, "username", item) for item in streamers] == ["one", "new"]
    assert streamers[1].settings.favorite is False


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "add", "kind": "streamers", "value": "three"},
        {"action": "remove", "kind": "streamers", "value": "two"},
        {"action": "reorder_streamers", "usernames": ["two", "one", "one"]},
        {
            "action": "update_streamer",
            "username": "one",
            "settings": {"favorite": False},
        },
    ],
    ids=["add", "remove", "reorder", "update"],
)
def test_streamer_edits_are_refused_while_a_username_is_duplicated(tmp_path, payload):
    config = tmp_path / "config.py"
    write_commented_config(
        config,
        "STREAMERS = [\n"
        '    Streamer("one", settings=StreamerSettings(favorite=True)),\n'
        '    "one",\n'
        '    "two",\n'
        "]\n",
    )
    before = config.read_bytes()

    with pytest.raises(ConfigEditError, match="listed more than once"):
        update_managed_web_config(config, payload)

    assert config.read_bytes() == before


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {"action": "add", "kind": "streamers", "value": "three"},
            'STREAMERS = [Streamer(" one ", settings=StreamerSettings(favorite=True)), '
            '"two", "three"]\n',
        ),
        (
            {"action": "reorder_streamers", "usernames": ["two", "one"]},
            'STREAMERS = ["two", '
            'Streamer(" one ", settings=StreamerSettings(favorite=True))]\n',
        ),
        (
            {
                "action": "update_streamer",
                "username": "one",
                "settings": {"chat": "NEVER"},
            },
            'STREAMERS = [Streamer(" one ", settings=StreamerSettings(favorite=True, '
            'chat=ChatPresence.NEVER)), "two"]\n',
        ),
    ],
    ids=["add", "reorder", "update"],
)
def test_username_with_spaces_keeps_its_entry(tmp_path, payload, expected):
    config = tmp_path / "config.py"
    write_commented_config(
        config,
        'STREAMERS = [Streamer(" one ", settings=StreamerSettings(favorite=True)), '
        '"two"]\n',
    )
    assert [
        item["username"] for item in read_managed_web_config(config)["streamers"]
    ] == [
        "one",
        "two",
    ]

    update_managed_web_config(config, payload)

    assert expected in config.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "later",
    [
        'MINER_CONFIG["streamer_settings"] = StreamerSettings(favorite=True)\n',
        "MINER_CONFIG.update(streamer_settings=StreamerSettings(favorite=True))\n",
        'if True:\n    MINER_CONFIG["streamer_settings"] = StreamerSettings(favorite=True)\n',
        "dict.update(MINER_CONFIG, streamer_settings=StreamerSettings(favorite=True))\n",
        'D = MINER_CONFIG["streamer_settings"]\nD.favorite = True\n',
        'D = MINER_CONFIG.get("streamer_settings")\nD.favorite = True\n',
        'D = dict(MINER_CONFIG)\nD["streamer_settings"].favorite = True\n',
        'globals()["MINER_CONFIG"]["streamer_settings"] = StreamerSettings(favorite=True)\n',
    ],
)
def test_update_is_written_when_miner_config_changes_after_the_dict(tmp_path, later):
    config = tmp_path / "config.py"
    write_commented_config(config, 'STREAMERS = ["one", "two"]\n' + later)

    update_managed_web_config(
        config,
        {
            "action": "update_streamer",
            "username": "one",
            "settings": {"favorite": False},
        },
    )

    assert 'Streamer("one", settings=StreamerSettings(favorite=False))' in (
        config.read_text(encoding="utf-8")
    )


@pytest.mark.parametrize(
    "streamers",
    [
        'STREAMERS = [\n    "a", "b",  # ab\n    "c", "d",  # cd\n]\n',
        'STREAMERS = [\n    (\n        "a"\n    ), "b",  # ab\n    "c", "d",\n]\n',
    ],
)
def test_removing_an_entry_keeps_a_comment_it_shares_with_another(tmp_path, streamers):
    config = tmp_path / "config.py"
    write_commented_config(config, streamers)

    update_managed_web_config(
        config, {"action": "remove", "kind": "streamers", "value": "b"}
    )

    source = config.read_text(encoding="utf-8")
    assert "# ab" in source
    assert _usernames(config) == ["a", "c", "d"]


def test_update_is_written_when_streamer_settings_is_given_twice(tmp_path):
    config = tmp_path / "config.py"
    write_commented_config(config, 'STREAMERS = ["one", "two"]\n')
    source = config.read_text(encoding="utf-8")
    source = source.replace(
        '"streamer_settings": StreamerSettings(favorite=False, chat=ChatPresence.ONLINE),',
        '"streamer_settings": StreamerSettings(favorite=False),\n'
        '    "streamer_settings": StreamerSettings(favorite=FAV),',
    ).replace("MINER_CONFIG = {", "FAV = True\nMINER_CONFIG = {")
    config.write_text(source, encoding="utf-8")
    assert "favorite=FAV" in source

    update_managed_web_config(
        config,
        {
            "action": "update_streamer",
            "username": "one",
            "settings": {"favorite": False},
        },
    )

    assert _load_config(config).STREAMERS[0].settings.favorite is False


@pytest.mark.parametrize(
    ("assignment", "later"),
    [
        ("MINER_CONFIG = ALIAS = {", 'ALIAS["streamer_settings"].favorite = True\n'),
        ("MINER_CONFIG = {", "S.favorite = True\n"),
    ],
)
def test_update_is_written_when_the_defaults_have_another_name(
    tmp_path, assignment, later
):
    config = tmp_path / "config.py"
    write_commented_config(config, 'STREAMERS = ["one", "two"]\n' + later)
    source = config.read_text(encoding="utf-8").replace("MINER_CONFIG = {", assignment)
    source = (
        source.replace(
            '"streamer_settings": StreamerSettings(favorite=False, chat=ChatPresence.ONLINE)',
            '"streamer_settings": (S := StreamerSettings(favorite=False))',
        )
        if "S." in later
        else source
    )
    config.write_text(source, encoding="utf-8")

    update_managed_web_config(
        config,
        {
            "action": "update_streamer",
            "username": "one",
            "settings": {"favorite": False},
        },
    )

    assert _load_config(config).STREAMERS[0].settings.favorite is False


@pytest.mark.parametrize(
    ("later", "readable"),
    [
        ("", True),
        ('X = MINER_CONFIG["username"]\n', True),
        ('X = MINER_CONFIG.get("username", 1)\n', True),
        ("MINER_CONFIG = {}\n", False),
        ('MINER_CONFIG["username"] = "x"\n', False),
        ("X = MINER_CONFIG.get()\n", False),
        ("X = MINER_CONFIG.copy()\n", False),
        ("X = MINER_CONFIG[0]\n", False),
        ("import os as MINER_CONFIG\n", False),
        ("from os.path import *\n", False),
        ("def MINER_CONFIG():\n    pass\n", False),
        ("class MINER_CONFIG:\n    pass\n", False),
        ("try:\n    pass\nexcept Exception as MINER_CONFIG:\n    pass\n", False),
        ("match 1:\n    case MINER_CONFIG:\n        pass\n", False),
        ("match {}:\n    case {**MINER_CONFIG}:\n        pass\n", False),
        ("match []:\n    case [*MINER_CONFIG]:\n        pass\n", False),
        ("def f():\n    global MINER_CONFIG\n", False),
        ('X = "MINER_CONFIG"\n', True),
        ("import builtins\nbuiltins.exec('')\n", False),
        ("from builtins import exec as run\n", False),
        ("def f(MINER_CONFIG):\n    pass\n", False),
        ("del MINER_CONFIG\n", False),
        ("for MINER_CONFIG in []:\n    pass\n", False),
        *(
            (f"{name}()\n", False)
            for name in (
                "globals",
                "vars",
                "locals",
                "exec",
                "eval",
                "setattr",
                "delattr",
            )
        ),
    ],
)
def test_miner_config_readability(later, readable):
    source = 'MINER_CONFIG = {"username": "a", "streamer_settings": None}\n' + later
    assert config_editor._miner_config_readable(ast.parse(source)) is readable


def test_miner_config_bound_to_something_else_is_unreadable():
    assert (
        config_editor._miner_config_readable(ast.parse("MINER_CONFIG = dict()\n"))
        is False
    )


def test_explicit_none_setting_still_inherits_an_unreadable_default(tmp_path):
    config = tmp_path / "config.py"
    write_commented_config(
        config,
        'STREAMERS = [Streamer("one", settings=StreamerSettings(favorite=None))]\n',
    )
    source = (
        config.read_text(encoding="utf-8")
        .replace(
            '"streamer_settings": StreamerSettings(favorite=False, chat=ChatPresence.ONLINE)',
            '"streamer_settings": DEFAULTS',
        )
        .replace(
            "MINER_CONFIG = {",
            "DEFAULTS = StreamerSettings(favorite=True)\nMINER_CONFIG = {",
        )
    )
    config.write_text(source, encoding="utf-8")

    update_managed_web_config(
        config,
        {
            "action": "update_streamer",
            "username": "one",
            "settings": {"favorite": False},
        },
    )

    assert _load_config(config).STREAMERS[0].settings.favorite is False


def test_reading_a_settings_expression_with_tuple_keys_does_not_crash(tmp_path):
    config = tmp_path / "config.py"
    write_commented_config(
        config,
        "def pick(options):\n    return next(iter(options.values()))\n"
        'STREAMERS = [Streamer("one", settings=pick({("g", "one"): '
        'StreamerSettings(favorite=True)})), "two"]\n',
    )

    managed = read_managed_web_config(config)
    assert [item["username"] for item in managed["streamers"]] == ["one", "two"]
    before = config.read_bytes()
    with pytest.raises(ConfigEditError, match="Edit them in config.py"):
        update_managed_web_config(
            config,
            {
                "action": "update_streamer",
                "username": "one",
                "settings": {"favorite": False},
            },
        )
    assert config.read_bytes() == before


def test_removal_is_refused_when_a_shared_comment_precedes_the_comma(tmp_path):
    config = tmp_path / "config.py"
    write_commented_config(config, 'STREAMERS = [\n    "a", "b" # ab\n    , "c",\n]\n')
    before = config.read_bytes()

    with pytest.raises(ConfigEditError, match="Remove it in config.py instead"):
        update_managed_web_config(
            config, {"action": "remove", "kind": "streamers", "value": "b"}
        )

    assert config.read_bytes() == before


@pytest.mark.parametrize(
    "later",
    [
        'X = MINER_CONFIG.get("username")\n',
        'print(MINER_CONFIG["username"])\n',
    ],
)
def test_reading_miner_config_later_keeps_inherited_settings_inherited(tmp_path, later):
    config = tmp_path / "config.py"
    write_commented_config(config, 'STREAMERS = ["one", "two"]\n' + later)

    update_managed_web_config(
        config,
        {
            "action": "update_streamer",
            "username": "one",
            "settings": {"favorite": False, "chat": "NEVER"},
        },
    )

    # favorite=False is the readable inherited value, so it isn't written.
    assert 'Streamer("one", settings=StreamerSettings(chat=ChatPresence.NEVER))' in (
        config.read_text(encoding="utf-8")
    )


def test_usernames_differing_only_in_spaces_count_as_duplicates(tmp_path):
    config = tmp_path / "config.py"
    write_commented_config(
        config,
        'STREAMERS = [Streamer("one", settings=StreamerSettings(favorite=True)), '
        '" One ", "two"]\n',
    )
    before = config.read_bytes()

    with pytest.raises(ConfigEditError, match="listed more than once"):
        update_managed_web_config(
            config,
            {
                "action": "update_streamer",
                "username": "one",
                "settings": {"favorite": False},
            },
        )

    assert config.read_bytes() == before


def test_migration_refuses_a_duplicated_saved_username(tmp_path):
    config = tmp_path / "config.py"
    write_commented_config(config, 'STREAMERS = ["one", "two"]\n')
    (tmp_path / "web-config.json").write_text(
        json.dumps({"streamers": [{"username": "two"}, {"username": "Two"}]}),
        encoding="utf-8",
    )
    before = config.read_bytes()

    with pytest.raises(ConfigEditError, match="listed only once"):
        migrate_web_config(config)

    assert config.read_bytes() == before
    assert (tmp_path / "web-config.json").exists()


@pytest.mark.parametrize(
    "entry", ['Streamer("one", ONE_SETTINGS)', 'Streamer("one", **OPTIONS)']
)
def test_legacy_record_without_settings_migrates_past_unreadable_ones(tmp_path, entry):
    config = tmp_path / "config.py"
    write_commented_config(
        config,
        "ONE_SETTINGS = StreamerSettings()\nOPTIONS = {}\n"
        f'STREAMERS = [{entry}, "two"]\n',
    )
    (tmp_path / "web-config.json").write_text(
        json.dumps({"streamers": [{"username": "two"}, {"username": "one"}]}),
        encoding="utf-8",
    )

    migrate_web_config(config)

    assert _usernames(config) == ["two", "one"]
    assert entry in config.read_text(encoding="utf-8")


def test_removal_is_refused_when_the_file_cant_be_tokenized(tmp_path, monkeypatch):
    import tokenize

    config = tmp_path / "config.py"
    write_commented_config(config, 'STREAMERS = ["one", "two"]\n')
    before = config.read_bytes()

    def fail(_encoded):
        raise tokenize.TokenError("boom", (1, 0))

    monkeypatch.setattr(config_editor, "_comments", fail)
    with pytest.raises(ConfigEditError, match="Remove it in config.py instead"):
        update_managed_web_config(
            config, {"action": "remove", "kind": "streamers", "value": "one"}
        )

    assert config.read_bytes() == before


def test_import_is_added_below_a_future_import(tmp_path):
    config = tmp_path / "config.py"
    write_commented_config(config, 'STREAMERS = ["one", "two"]\n')
    source = config.read_text(encoding="utf-8").replace(
        "from TwitchChannelPointsMiner.classes.Chat import ChatPresence\n", ""
    )
    config.write_text("from __future__ import annotations\n" + source, encoding="utf-8")

    update_managed_web_config(
        config,
        {
            "action": "update_streamer",
            "username": "one",
            "settings": {"chat": "NEVER"},
        },
    )

    written = config.read_text(encoding="utf-8")
    assert written.startswith(
        "from __future__ import annotations\n"
        "from TwitchChannelPointsMiner.classes.Chat import ChatPresence\n"
    )
    assert "chat=ChatPresence.NEVER" in written
    # Loading it would run the schema migration, which puts CONFIG_VERSION
    # above the __future__ import (a separate bug), so only compile it.
    compile(written, "config.py", "exec", dont_inherit=True)


@pytest.mark.parametrize(
    "entry", ['Streamer("one", settings=ONE_SETTINGS)', 'Streamer("one", ONE_SETTINGS)']
)
def test_update_to_a_shown_default_is_refused_for_unreadable_settings(tmp_path, entry):
    # The dashboard shows favorite=False for ONE_SETTINGS, but config.py says
    # True, so saving False would otherwise look like a no-op.
    config = tmp_path / "config.py"
    write_commented_config(
        config,
        "ONE_SETTINGS = StreamerSettings(favorite=True)\n"
        f'STREAMERS = [{entry}, "two"]\n',
    )
    shown = next(
        item["settings"]
        for item in read_managed_web_config(config)["streamers"]
        if item["username"] == "one"
    )
    assert shown["favorite"] is False
    before = config.read_bytes()

    with pytest.raises(ConfigEditError, match="Edit them in config.py"):
        update_managed_web_config(
            config,
            {
                "action": "update_streamer",
                "username": "one",
                "settings": {"favorite": False},
            },
        )

    assert config.read_bytes() == before


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        (
            'Streamer("one", settings=None)',
            'Streamer("one", settings=StreamerSettings(chat=ChatPresence.NEVER))',
        ),
        (
            'Streamer("one", None)',
            'Streamer("one", StreamerSettings(chat=ChatPresence.NEVER))',
        ),
    ],
)
def test_settings_none_is_replaced_by_the_changed_settings(tmp_path, entry, expected):
    config = tmp_path / "config.py"
    write_commented_config(config, f'STREAMERS = [{entry}, "two"]\n')

    update_managed_web_config(
        config,
        {
            "action": "update_streamer",
            "username": "one",
            "settings": {"chat": "NEVER"},
        },
    )

    assert f'STREAMERS = [{expected}, "two"]\n' in config.read_text(encoding="utf-8")
    assert _load_config(config).STREAMERS[0].settings.chat is ChatPresence.NEVER


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "add", "kind": "streamers", "value": "three"},
        {"action": "remove", "kind": "streamers", "value": "one"},
        {"action": "reorder_streamers", "usernames": ["two", "one"]},
        {
            "action": "update_streamer",
            "username": "one",
            "settings": {"chat": "NEVER"},
        },
    ],
    ids=["add", "remove", "reorder", "update"],
)
def test_streamer_edits_keep_crlf_line_breaks(tmp_path, payload):
    config = tmp_path / "config.py"
    write_commented_config(
        config,
        "STREAMERS = [\n"
        '    "one",  # one\n'
        "    # about two\n"
        '    Streamer(\n        "two"\n    ),\n'
        "]\n",
    )
    source = config.read_text(encoding="utf-8")
    source = source.replace(
        "from TwitchChannelPointsMiner.classes.Chat import ChatPresence\n", ""
    )
    config.write_bytes(source.replace("\n", "\r\n").encode("utf-8"))

    update_managed_web_config(config, payload)

    written = config.read_bytes()
    assert b"\n" in written
    assert written.count(b"\n") == written.count(b"\r\n")
    assert b"\r\r" not in written


def _edited_config(path, *replacements):
    write_config(path)
    source = path.read_text(encoding="utf-8")
    for old, new in replacements:
        assert old in source
        source = source.replace(old, new)
    path.write_text(source, encoding="utf-8")
    return path.read_bytes()


def test_refused_source_update_leaves_the_earlier_part_unwritten(tmp_path):
    # update_sources writes MINER_CONFIG's priority before MINE_CONFIG's
    # followers. A refusal of the second part must not keep the first.
    config = tmp_path / "config.py"
    before = _edited_config(
        config,
        ('"followers": False,', '"followers": True,\n    **{"followers": True},'),
        (
            "[StreamerSource.STREAMERS, StreamerSource.CATEGORIES]",
            "[StreamerSource.STREAMERS, StreamerSource.FOLLOWERS, "
            "StreamerSource.CATEGORIES]",
        ),
    )

    with pytest.raises(ConfigEditError, match="Edit 'followers' in config.py"):
        update_managed_web_config(
            config, {"action": "update_sources", "values": {"followers": False}}
        )

    assert config.read_bytes() == before
    assert StreamerSource.FOLLOWERS in (
        _load_config(config).MINER_CONFIG["streamer_source_priority"]
    )


def test_refused_category_setting_leaves_the_import_unwritten(tmp_path):
    config = tmp_path / "config.py"
    before = _edited_config(
        config,
        (
            "from TwitchChannelPointsMiner.classes.Settings import "
            "CategorySort, StreamerSource\n",
            "from TwitchChannelPointsMiner.classes.Settings import StreamerSource\n",
        ),
        (
            '    "category_sort": CategorySort.VIEWERS_DESC,\n',
            "    **BASE,\n",
        ),
        ("MINER_CONFIG = {", "BASE = {}\nMINER_CONFIG = {"),
    )

    with pytest.raises(ConfigEditError, match="Edit 'category_sort' in config.py"):
        update_managed_web_config(
            config, {"action": "update_category", "values": {"sort": "VIEWERS_ASC"}}
        )

    assert config.read_bytes() == before


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "add", "kind": "categories", "value": "gamma"},
        {"action": "remove", "kind": "categories", "value": "alpha"},
        {"action": "reorder_categories", "categories": ["beta", "alpha"]},
    ],
    ids=["add", "remove", "reorder"],
)
@pytest.mark.parametrize(
    "unpacking",
    ['**{"categories": ["zeta"]}', "**BASE", "KEY: []"],
    ids=["overriding", "unreadable", "computed-key"],
)
def test_category_edits_refuse_a_later_override(tmp_path, payload, unpacking):
    config = tmp_path / "config.py"
    before = _edited_config(
        config,
        ('    "category_limit": 5,\n', f'    "category_limit": 5,\n    {unpacking},\n'),
        ("MINER_CONFIG = {", "BASE = {}\nKEY = 'categories'\nMINER_CONFIG = {"),
    )

    with pytest.raises(ConfigEditError, match="(?i)categories"):
        update_managed_web_config(config, payload)

    assert config.read_bytes() == before


def test_category_migration_refuses_a_later_override_and_keeps_both_files(tmp_path):
    config = tmp_path / "config.py"
    before = _edited_config(
        config,
        (
            '    "category_limit": 5,\n',
            '    "category_limit": 5,\n    **{"categories": ["zeta"]},\n',
        ),
    )
    overrides = tmp_path / "web-config.json"
    overrides.write_text(json.dumps({"categories": ["gamma"]}), encoding="utf-8")

    with pytest.raises(ConfigEditError, match="Edit 'categories' in config.py"):
        migrate_web_config(config)

    assert config.read_bytes() == before
    assert overrides.is_file()


def test_failed_migration_step_leaves_earlier_steps_unwritten(tmp_path):
    # Categories move first, then sources; a refused source change must not
    # leave the categories moved while web-config.json still holds them.
    config = tmp_path / "config.py"
    before = _edited_config(
        config,
        ('"followers": False,', '"followers": True,\n    **{"followers": True},'),
    )
    overrides = tmp_path / "web-config.json"
    overrides.write_text(
        json.dumps({"categories": ["gamma"], "sources": {"followers": False}}),
        encoding="utf-8",
    )

    with pytest.raises(ConfigEditError, match="Edit 'followers' in config.py"):
        migrate_web_config(config)

    assert config.read_bytes() == before
    assert overrides.is_file()


@pytest.mark.parametrize(
    "categories",
    ['["alpha", *MORE]', '["alpha", NAME]'],
    ids=["starred", "variable"],
)
def test_category_edits_refuse_entries_they_would_drop(tmp_path, categories):
    config = tmp_path / "config.py"
    before = _edited_config(
        config,
        ('"categories": ["alpha", "beta"]', f'"categories": {categories}'),
        ("MINER_CONFIG = {", "MORE = ['beta']\nNAME = 'beta'\nMINER_CONFIG = {"),
    )

    with pytest.raises(ConfigEditError, match="aren't plain strings"):
        update_managed_web_config(
            config, {"action": "add", "kind": "categories", "value": "gamma"}
        )

    assert config.read_bytes() == before


def test_category_edit_changes_the_entry_the_miner_uses(tmp_path):
    config = tmp_path / "config.py"
    _edited_config(
        config,
        (
            '    "categories": ["alpha", "beta"],\n',
            '    "categories": ["old"],\n'
            "    **BASE,\n"
            '    "categories": ["alpha", "beta"],\n',
        ),
        ("MINER_CONFIG = {", "BASE = {}\nMINER_CONFIG = {"),
    )

    result = update_managed_web_config(
        config, {"action": "add", "kind": "categories", "value": "gamma"}
    )

    assert result["categories"] == ["alpha", "beta", "gamma"]
    assert _load_config(config).MINE_CONFIG["categories"] == [
        "alpha",
        "beta",
        "gamma",
    ]


_DISCORD_LINE = (
    '        discord=Discord(webhook_api="https://secret.example/hook", events=[]),\n'
)


@pytest.mark.parametrize("enabled", [False, True])
def test_notification_save_refuses_a_notifier_set_by_a_later_unpacking(
    tmp_path, enabled
):
    # The literal LoggerSettings has no Discord, so turning it off looked
    # like a no-op, while the unpacked one the miner uses keeps it on.
    config = tmp_path / "config.py"
    before = _edited_config(
        config,
        (_DISCORD_LINE, ""),
        (
            "    ),\n}\nSTREAMERS",
            "    ),\n"
            '    **{"logger_settings": LoggerSettings(discord=Discord('
            'webhook_api="https://secret.example/hook", events=[]))},\n'
            "}\nSTREAMERS",
        ),
    )

    with pytest.raises(ConfigEditError, match="Edit 'logger_settings' in config.py"):
        update_managed_web_config(
            config,
            {
                "action": "update_notification",
                "provider": "discord",
                "values": {
                    "enabled": enabled,
                    "webhook_api": "https://other.example/hook",
                    "events": [],
                },
            },
        )

    assert config.read_bytes() == before


@pytest.mark.parametrize(
    "arguments",
    ['**{"discord": None}', "**OPTIONS"],
    ids=["names-it", "unreadable"],
)
def test_notification_save_refuses_logger_settings_unpacking(tmp_path, arguments):
    config = tmp_path / "config.py"
    before = _edited_config(
        config,
        (_DISCORD_LINE, f"        {arguments},\n"),
        ("MINER_CONFIG = {", "OPTIONS = {}\nMINER_CONFIG = {"),
    )

    with pytest.raises(ConfigEditError, match=r"\*\* arguments that may set 'discord'"):
        update_managed_web_config(
            config,
            {
                "action": "update_notification",
                "provider": "discord",
                "values": {"enabled": False},
            },
        )

    assert config.read_bytes() == before


@pytest.mark.parametrize(
    "arguments",
    ['**{"console_level": logging.INFO}', "**OPTIONS", "*ARGS"],
    ids=["names-it", "unreadable", "starred"],
)
def test_logging_save_refuses_logger_settings_arguments_it_may_repeat(
    tmp_path, arguments
):
    # Adding console_level= next to one passed through ** makes LoggerSettings
    # raise TypeError when config.py runs.
    config = tmp_path / "config.py"
    before = _edited_config(
        config,
        ("        console_level=logging.INFO,\n", f"        {arguments},\n"),
        ("MINER_CONFIG = {", "OPTIONS = {}\nARGS = ()\nMINER_CONFIG = {"),
    )

    with pytest.raises(ConfigEditError, match="(?i)arguments"):
        update_managed_web_config(
            config,
            {"action": "update_logging", "values": {"console_level": "WARNING"}},
        )

    assert config.read_bytes() == before


def test_logging_save_keeps_logger_settings_unpacking_it_cannot_repeat(tmp_path):
    config = tmp_path / "config.py"
    _edited_config(
        config,
        (
            "        console_level=logging.INFO,\n",
            "        console_level=logging.INFO,\n"
            '        **{"file_level": logging.INFO},\n',
        ),
        ("        file_level=logging.DEBUG,\n", ""),
    )

    update_managed_web_config(
        config, {"action": "update_logging", "values": {"console_level": "WARNING"}}
    )

    logger_settings = _load_config(config).MINER_CONFIG["logger_settings"]
    assert logger_settings.console_level == logging.WARNING
    assert logger_settings.file_level == logging.INFO


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "update_logging", "values": {"console_level": "WARNING"}},
        {
            "action": "update_notification",
            "provider": "discord",
            "values": {"enabled": False},
        },
    ],
    ids=["logging", "notification"],
)
def test_logger_settings_edit_changes_the_entry_the_miner_uses(tmp_path, payload):
    # Python keeps the last logger_settings; editing the first one would be
    # saved without any effect.
    config = tmp_path / "config.py"
    _edited_config(
        config,
        (
            '    "logger_settings": LoggerSettings(\n',
            '    "logger_settings": LoggerSettings(),\n'
            "    **BASE,\n"
            '    "logger_settings": LoggerSettings(\n',
        ),
        ("MINER_CONFIG = {", "BASE = {}\nMINER_CONFIG = {"),
    )

    update_managed_web_config(config, payload)

    assert "LoggerSettings()," in config.read_text(encoding="utf-8")
    logger_settings = _load_config(config).MINER_CONFIG["logger_settings"]
    if payload["action"] == "update_logging":
        assert logger_settings.console_level == logging.WARNING
    else:
        assert logger_settings.discord is None


def test_source_update_refuses_a_priority_set_only_by_unpacking(tmp_path):
    # Starting from the default order would change which source comes first.
    config = tmp_path / "config.py"
    before = _edited_config(
        config,
        (
            '    "streamer_source_priority": '
            "[StreamerSource.STREAMERS, StreamerSource.CATEGORIES],\n",
            '    **{"streamer_source_priority": '
            "[StreamerSource.CATEGORIES, StreamerSource.STREAMERS]},\n",
        ),
    )

    with pytest.raises(
        ConfigEditError, match="sets 'streamer_source_priority' by unpacking"
    ):
        update_managed_web_config(
            config, {"action": "update_sources", "values": {"followers": True}}
        )

    assert config.read_bytes() == before


def test_dict_edit_refuses_a_later_computed_key(tmp_path):
    config = tmp_path / "config.py"
    before = _edited_config(
        config,
        (
            '    "username": "example",\n',
            '    "username": "example",\n    KEY: True,\n',
        ),
        ("MINER_CONFIG = {", "KEY = 'update_check'\nMINER_CONFIG = {"),
    )

    with pytest.raises(ConfigEditError, match="key that isn't a plain string"):
        update_managed_web_config(
            config, {"action": "update_updates", "values": {"enabled": False}}
        )

    assert config.read_bytes() == before


def test_notification_edit_replaces_positional_arguments(tmp_path):
    config = tmp_path / "config.py"
    _edited_config(
        config,
        (
            'Discord(webhook_api="https://secret.example/hook", events=[])',
            'Discord("https://secret.example/hook", [])',
        ),
    )

    update_managed_web_config(
        config,
        {
            "action": "update_notification",
            "provider": "discord",
            "values": {
                "enabled": True,
                "webhook_api": "https://other.example/hook",
                "events": ["DROP_CLAIM"],
            },
        },
    )

    discord = _load_config(config).MINER_CONFIG["logger_settings"].discord
    assert discord.webhook_api == "https://other.example/hook"
    assert [str(event) for event in discord.events] == ["DROP_CLAIM"]


def test_logging_edit_replaces_positional_arguments(tmp_path):
    config = tmp_path / "config.py"
    _edited_config(
        config,
        (
            '    "logger_settings": LoggerSettings(\n'
            "        console_level=logging.INFO,\n",
            '    "logger_settings": LoggerSettings(\n'
            "        True,\n"
            "        False,\n"
            "        logging.INFO,\n",
        ),
    )

    update_managed_web_config(
        config, {"action": "update_logging", "values": {"console_level": "WARNING"}}
    )

    assert (
        _load_config(config).MINER_CONFIG["logger_settings"].console_level
        == logging.WARNING
    )


_EMAIL_CONFIG = (
    "from TwitchChannelPointsMiner.classes.Email import Email\n"
    "from TwitchChannelPointsMiner.logger import LoggerSettings\n"
    "MINER_CONFIG = {\n"
    '    "username": "x",\n'
    '    "logger_settings": LoggerSettings(\n'
    '        email=Email("smtp.example", 465, "a@example.com", [], [], {tls}),\n'
    "    ),\n"
    "}\n"
    "STREAMERS = []\n"
    'MINE_CONFIG = {"categories": []}\n'
    "ANALYTICS_CONFIG = None\n"
)


@pytest.mark.parametrize(
    "tls",
    [
        "use_ssl=True, starttls=False",
        'starttls=False, **{"use_ssl": True}',
        "None, None, True, False",
    ],
    ids=["keyword", "unpacked", "positional"],
)
def test_email_save_refuses_turning_on_starttls_next_to_ssl(tmp_path, tls):
    # Email() raises ValueError for both, so config.py would stop loading.
    config = tmp_path / "config.py"
    config.write_text(_EMAIL_CONFIG.replace("{tls}", tls), encoding="utf-8")
    before = config.read_bytes()

    with pytest.raises(ConfigEditError, match="(?i)SSL and STARTTLS|starttls"):
        update_managed_web_config(
            config,
            {
                "action": "update_notification",
                "provider": "email",
                "values": {"starttls": True},
            },
        )

    assert config.read_bytes() == before


def test_new_email_notifier_refuses_ssl_with_the_starttls_default(tmp_path):
    config = tmp_path / "config.py"
    before = _edited_config(config)

    with pytest.raises(ConfigEditError, match="SSL and STARTTLS"):
        update_managed_web_config(
            config,
            {
                "action": "update_notification",
                "provider": "email",
                "values": {
                    "enabled": True,
                    "host": "smtp.example",
                    "port": 465,
                    "sender": "a@example.com",
                    "recipients": ["b@example.com"],
                    "events": [],
                    "use_ssl": True,
                },
            },
        )

    assert config.read_bytes() == before


_POSITIONAL_DISCORD = (
    '    "logger_settings": LoggerSettings(\n'
    "        True, False, logging.INFO, False, None, 'dd/mm/yy', logging.DEBUG,\n"
    "        False, False, ColorPalette(), True, 7, None,\n"
    '        Discord("https://secret.example/hook", []),\n'
    "    ),\n"
)


def _positional_discord_config(path):
    write_config(path)
    source = path.read_text(encoding="utf-8")
    start = source.index('    "logger_settings": LoggerSettings(\n')
    end = source.index("    ),\n", start) + len("    ),\n")
    source = source[:start] + _POSITIONAL_DISCORD + source[end:]
    source = source.replace(
        "from TwitchChannelPointsMiner.logger import LoggerSettings",
        "from TwitchChannelPointsMiner.logger import ColorPalette, LoggerSettings",
    )
    path.write_text(source, encoding="utf-8")


def test_notification_disable_reaches_a_notifier_passed_positionally(tmp_path):
    config = tmp_path / "config.py"
    _positional_discord_config(config)

    update_managed_web_config(
        config,
        {
            "action": "update_notification",
            "provider": "discord",
            "values": {"enabled": False},
        },
    )

    assert _load_config(config).MINER_CONFIG["logger_settings"].discord is None


def test_notification_update_reaches_a_notifier_passed_positionally(tmp_path):
    config = tmp_path / "config.py"
    _positional_discord_config(config)

    update_managed_web_config(
        config,
        {
            "action": "update_notification",
            "provider": "discord",
            "values": {"events": ["DROP_CLAIM"]},
        },
    )

    discord = _load_config(config).MINER_CONFIG["logger_settings"].discord
    assert [str(event) for event in discord.events] == ["DROP_CLAIM"]


def _variable_discord_config(path):
    return _edited_config(
        path,
        (
            'discord=Discord(webhook_api="https://secret.example/hook", events=[])',
            "discord=SAVED_DISCORD",
        ),
        (
            "MINER_CONFIG = {",
            'SAVED_DISCORD = Discord(webhook_api="https://secret.example/hook", '
            "events=[])\nMINER_CONFIG = {",
        ),
    )


def test_notification_disable_replaces_a_notifier_variable(tmp_path):
    config = tmp_path / "config.py"
    _variable_discord_config(config)

    update_managed_web_config(
        config,
        {
            "action": "update_notification",
            "provider": "discord",
            "values": {"enabled": False},
        },
    )

    assert _load_config(config).MINER_CONFIG["logger_settings"].discord is None


@pytest.mark.parametrize(
    "values",
    [
        {"events": ["DROP_CLAIM"]},
        {
            "enabled": True,
            "webhook_api": "https://other.example/hook",
            "events": [],
        },
    ],
    ids=["update", "enable"],
)
def test_notification_update_refuses_a_notifier_variable(tmp_path, values):
    # Its settings can't be read, and writing a new Discord(...) over it
    # would lose them.
    config = tmp_path / "config.py"
    before = _variable_discord_config(config)

    with pytest.raises(ConfigEditError, match=r"isn't written as Discord\(\.\.\.\)"):
        update_managed_web_config(
            config,
            {"action": "update_notification", "provider": "discord", "values": values},
        )

    assert config.read_bytes() == before


@pytest.mark.parametrize(
    "priority", ["PRIORITY", "list(PRIORITY)", '["STREAMERS"]'], ids=str
)
def test_source_update_refuses_a_priority_it_cannot_read(tmp_path, priority):
    config = tmp_path / "config.py"
    before = _edited_config(
        config,
        (
            "[StreamerSource.STREAMERS, StreamerSource.CATEGORIES]",
            priority,
        ),
        (
            "MINER_CONFIG = {",
            "PRIORITY = [StreamerSource.CATEGORIES, StreamerSource.STREAMERS]\n"
            "MINER_CONFIG = {",
        ),
    )

    with pytest.raises(ConfigEditError, match="streamer_source_priority"):
        update_managed_web_config(
            config, {"action": "update_sources", "values": {"followers": True}}
        )

    assert config.read_bytes() == before


def test_source_update_starts_from_the_default_for_a_none_priority(tmp_path):
    config = tmp_path / "config.py"
    _edited_config(
        config,
        ("[StreamerSource.STREAMERS, StreamerSource.CATEGORIES]", "None"),
    )

    update_managed_web_config(
        config, {"action": "update_sources", "values": {"badges": False}}
    )

    # Loading config.py would migrate it, so check what was written.
    assert (
        '"streamer_source_priority": [StreamerSource.STREAMERS, '
        "StreamerSource.FOLLOWERS, StreamerSource.CATEGORIES, "
        "StreamerSource.WILDCARD_CATEGORIES]"
    ) in config.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "call",
    [
        lambda config: migrate_web_config(config),
        lambda config: enable_analytics_dashboard(config, password="secret"),
        lambda config: set_miner_username(config, "someone"),
        lambda config: update_managed_web_config(
            config, {"action": "update_updates", "values": {"enabled": False}}
        ),
    ],
    ids=["migrate", "enable-dashboard", "username", "update"],
)
def test_public_config_writers_hold_the_config_mutex_while_writing(
    tmp_path, monkeypatch, call
):
    # Without it, a migration started by the runner could write its older
    # copy of config.py over a Config tab save that finished in between.
    config = tmp_path / "config.py"
    write_config(config)
    (tmp_path / "web-config.json").write_text(
        json.dumps({"categories": ["gamma"]}), encoding="utf-8"
    )
    original = config_editor._replace_config_bytes
    held = []

    def record(path, updated, mode):
        result = []
        probe = threading.Thread(
            target=lambda: result.append(
                config_editor.CONFIG_FILE_MUTEX.acquire(blocking=False)
            )
        )
        probe.start()
        probe.join(timeout=5)
        if result[0]:
            config_editor.CONFIG_FILE_MUTEX.release()
        held.append(not result[0])
        return original(path, updated, mode)

    monkeypatch.setattr(config_editor, "_replace_config_bytes", record)

    call(config)

    assert held and all(held)


@pytest.mark.parametrize(
    "analytics",
    ["dict(port=9000, password='existing')", "SAVED_ANALYTICS", "False"],
    ids=["dict-call", "variable", "false"],
)
def test_enable_dashboard_refuses_analytics_settings_it_would_replace(
    tmp_path, analytics
):
    config = tmp_path / "config.py"
    before = _edited_config(
        config,
        ("ANALYTICS_CONFIG = None", f"ANALYTICS_CONFIG = {analytics}"),
        ("MINER_CONFIG = {", "SAVED_ANALYTICS = {}\nMINER_CONFIG = {"),
    )

    with pytest.raises(ConfigEditError, match="ANALYTICS_CONFIG"):
        enable_analytics_dashboard(config, password="secret")

    assert config.read_bytes() == before


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "update_logging", "values": {"daily_report": True}},
        {
            "action": "update_notification",
            "provider": "discord",
            "values": {
                "enabled": True,
                "webhook_api": "https://other.example/hook",
                "events": [],
            },
        },
    ],
    ids=["logging", "notification"],
)
def test_logger_settings_edits_refuse_a_helper_call(tmp_path, payload):
    # make_logger(daily_report=True) would raise TypeError when config.py runs.
    config = tmp_path / "config.py"
    write_config(config)
    source = config.read_text(encoding="utf-8")
    start = source.index('    "logger_settings": LoggerSettings(\n')
    end = source.index("    ),\n", start) + len("    ),\n")
    source = (
        source[:start] + '    "logger_settings": make_logger(),\n' + source[end:]
    ).replace(
        "MINER_CONFIG = {",
        "def make_logger():\n    return LoggerSettings()\n\n\nMINER_CONFIG = {",
    )
    config.write_text(source, encoding="utf-8")
    before = config.read_bytes()

    with pytest.raises(ConfigEditError, match=r"isn't written as LoggerSettings"):
        update_managed_web_config(config, payload)

    assert config.read_bytes() == before


def test_logging_level_save_adds_the_logging_import(tmp_path):
    from TwitchChannelPointsMiner.config_migration import CONFIG_VERSION

    # Current schema, so loading it doesn't migrate (and add imports to) it.
    config = tmp_path / "config.py"
    config.write_text(
        f"CONFIG_VERSION = {CONFIG_VERSION}\n"
        "from TwitchChannelPointsMiner.logger import LoggerSettings\n"
        'MINER_CONFIG = {"username": "x", "logger_settings": LoggerSettings()}\n'
        "STREAMERS = []\n"
        'MINE_CONFIG = {"categories": []}\n'
        "ANALYTICS_CONFIG = None\n",
        encoding="utf-8",
    )

    update_managed_web_config(
        config, {"action": "update_logging", "values": {"console_level": "WARNING"}}
    )

    assert (
        _load_config(config).MINER_CONFIG["logger_settings"].console_level
        == logging.WARNING
    )


@pytest.mark.parametrize(
    "import_line",
    [
        "from TwitchChannelPointsMiner.classes.Settings import CategorySort as Sort",
        "# from TwitchChannelPointsMiner.classes.Settings import CategorySort",
    ],
    ids=["aliased", "commented"],
)
def test_category_sort_save_adds_an_import_the_text_only_resembles(
    tmp_path, import_line
):
    config = tmp_path / "config.py"
    _edited_config(
        config,
        (
            "from TwitchChannelPointsMiner.classes.Settings import "
            "CategorySort, StreamerSource\n",
            f"{import_line}\n"
            "from TwitchChannelPointsMiner.classes.Settings import StreamerSource\n",
        ),
        ('    "category_sort": CategorySort.VIEWERS_DESC,\n', ""),
    )

    update_managed_web_config(
        config, {"action": "update_category", "values": {"sort": "VIEWERS_ASC"}}
    )

    assert _load_config(config).MINE_CONFIG["category_sort"].name == "VIEWERS_ASC"


def test_logging_level_save_adds_an_import_a_later_one_would_bind_too_late(tmp_path):
    from TwitchChannelPointsMiner.config_migration import CONFIG_VERSION

    config = tmp_path / "config.py"
    config.write_text(
        f"CONFIG_VERSION = {CONFIG_VERSION}\n"
        "from TwitchChannelPointsMiner.logger import LoggerSettings\n"
        'MINER_CONFIG = {"username": "x", "logger_settings": LoggerSettings()}\n'
        "STREAMERS = []\n"
        'MINE_CONFIG = {"categories": []}\n'
        "ANALYTICS_CONFIG = None\n"
        "import logging\n",
        encoding="utf-8",
    )

    update_managed_web_config(
        config, {"action": "update_logging", "values": {"console_level": "WARNING"}}
    )

    assert (
        _load_config(config).MINER_CONFIG["logger_settings"].console_level
        == logging.WARNING
    )


def test_category_sort_save_adds_an_import_a_later_one_would_bind_too_late(tmp_path):
    from TwitchChannelPointsMiner.config_migration import CONFIG_VERSION

    config = tmp_path / "config.py"
    config.write_text(
        f"CONFIG_VERSION = {CONFIG_VERSION}\n"
        'MINER_CONFIG = {"username": "x"}\n'
        "STREAMERS = []\n"
        'MINE_CONFIG = {"categories": []}\n'
        "ANALYTICS_CONFIG = None\n"
        "from TwitchChannelPointsMiner.classes.Settings import CategorySort\n",
        encoding="utf-8",
    )

    update_managed_web_config(
        config, {"action": "update_category", "values": {"sort": "VIEWERS_ASC"}}
    )

    assert _load_config(config).MINE_CONFIG["category_sort"].name == "VIEWERS_ASC"


_REPEATED_CATEGORIES = (
    "[StreamerSource.STREAMERS, StreamerSource.CATEGORIES]",
    "[StreamerSource.CATEGORIES, StreamerSource.STREAMERS, "
    "StreamerSource.CATEGORIES]",
)


def test_source_disable_removes_every_copy_of_a_repeated_source(tmp_path):
    config = tmp_path / "config.py"
    _edited_config(config, _REPEATED_CATEGORIES)

    result = update_managed_web_config(
        config, {"action": "update_sources", "values": {"categories": False}}
    )

    assert result["sources"]["categories"] is False
    assert '"streamer_source_priority": [StreamerSource.STREAMERS]' in config.read_text(
        encoding="utf-8"
    )


def test_source_migration_removes_every_copy_of_a_repeated_source(tmp_path):
    config = tmp_path / "config.py"
    _edited_config(config, _REPEATED_CATEGORIES)
    (tmp_path / "web-config.json").write_text(
        json.dumps({"sources": {"categories": False}}), encoding="utf-8"
    )

    migrate_web_config(config)

    assert read_managed_web_config(config)["sources"]["categories"] is False


@pytest.mark.parametrize("migrate", [False, True], ids=["save", "migration"])
def test_source_update_keeps_the_order_after_an_unreadable_key(tmp_path, migrate):
    # str("x") as a key makes the whole MINER_CONFIG unreadable to the
    # reader, which must not make the save start from the default order.
    config = tmp_path / "config.py"
    _edited_config(
        config,
        (
            '    "username": "example",\n',
            '    str("other"): 1,\n    "username": "example",\n',
        ),
        (
            "[StreamerSource.STREAMERS, StreamerSource.CATEGORIES]",
            "[StreamerSource.CATEGORIES, StreamerSource.STREAMERS]",
        ),
    )
    if migrate:
        (tmp_path / "web-config.json").write_text(
            json.dumps({"sources": {"followers": True}}), encoding="utf-8"
        )
        migrate_web_config(config)
    else:
        update_managed_web_config(
            config, {"action": "update_sources", "values": {"followers": True}}
        )

    assert (
        '"streamer_source_priority": [StreamerSource.CATEGORIES, '
        "StreamerSource.STREAMERS, StreamerSource.FOLLOWERS]"
    ) in config.read_text(encoding="utf-8")


def test_source_update_keeps_an_empty_priority_empty(tmp_path):
    config = tmp_path / "config.py"
    _edited_config(
        config, ("[StreamerSource.STREAMERS, StreamerSource.CATEGORIES]", "[]")
    )

    update_managed_web_config(
        config, {"action": "update_sources", "values": {"followers": True}}
    )

    assert '"streamer_source_priority": [StreamerSource.FOLLOWERS]' in (
        config.read_text(encoding="utf-8")
    )


@pytest.mark.parametrize(
    "between",
    ["del logging", "logging = None", "from os import path as logging"],
    ids=["deleted", "rebound", "other-import"],
)
def test_logging_level_save_refuses_a_logging_name_changed_before_use(
    tmp_path, between
):
    # An import added at the top would be undone before MINER_CONFIG runs.
    from TwitchChannelPointsMiner.config_migration import CONFIG_VERSION

    config = tmp_path / "config.py"
    config.write_text(
        f"CONFIG_VERSION = {CONFIG_VERSION}\n"
        "import logging\n"
        "from TwitchChannelPointsMiner.logger import LoggerSettings\n"
        f"{between}\n"
        "MINER_CONFIG = {\n"
        '    "username": "x",\n'
        '    "logger_settings": LoggerSettings(console_level=20),\n'
        "}\n"
        "STREAMERS = []\n"
        'MINE_CONFIG = {"categories": []}\n'
        "ANALYTICS_CONFIG = None\n",
        encoding="utf-8",
    )
    before = config.read_bytes()

    with pytest.raises(ConfigEditError, match="binds or removes logging"):
        update_managed_web_config(
            config,
            {"action": "update_logging", "values": {"console_level": "WARNING"}},
        )

    assert config.read_bytes() == before


def test_logging_level_save_accepts_an_import_aliased_to_its_own_name(tmp_path):
    from TwitchChannelPointsMiner.config_migration import CONFIG_VERSION

    config = tmp_path / "config.py"
    config.write_text(
        f"CONFIG_VERSION = {CONFIG_VERSION}\n"
        "import logging as logging\n"
        "from TwitchChannelPointsMiner.logger import LoggerSettings\n"
        'MINER_CONFIG = {"username": "x", "logger_settings": LoggerSettings()}\n'
        "STREAMERS = []\n"
        'MINE_CONFIG = {"categories": []}\n'
        "ANALYTICS_CONFIG = None\n",
        encoding="utf-8",
    )

    update_managed_web_config(
        config, {"action": "update_logging", "values": {"console_level": "WARNING"}}
    )

    assert config.read_text(encoding="utf-8").count("import logging") == 1
    assert (
        _load_config(config).MINER_CONFIG["logger_settings"].console_level
        == logging.WARNING
    )


def test_streamer_add_accepts_its_imports_split_across_statements(tmp_path):
    config = tmp_path / "config.py"
    _edited_config(
        config,
        (
            "from TwitchChannelPointsMiner.classes.entities.Streamer import "
            "Streamer, StreamerSettings\n",
            "from TwitchChannelPointsMiner.classes.entities.Streamer import Streamer\n"
            "from TwitchChannelPointsMiner.classes.entities.Streamer import "
            "StreamerSettings\n",
        ),
    )

    update_managed_web_config(
        config,
        {
            "action": "update_streamer",
            "username": "two",
            "settings": {"favorite": True},
        },
    )

    assert _load_config(config).STREAMERS[1].settings.favorite is True


@pytest.mark.parametrize("migrate", [False, True], ids=["save", "migration"])
def test_category_sort_save_refuses_a_name_rebound_between_settings(tmp_path, migrate):
    from TwitchChannelPointsMiner.config_migration import CONFIG_VERSION

    config = tmp_path / "config.py"
    config.write_text(
        f"CONFIG_VERSION = {CONFIG_VERSION}\n"
        'MINER_CONFIG = {"username": "x"}\n'
        "CategorySort = None\n"
        "STREAMERS = []\n"
        'MINE_CONFIG = {"categories": []}\n'
        "ANALYTICS_CONFIG = None\n",
        encoding="utf-8",
    )
    before = config.read_bytes()
    overrides = tmp_path / "web-config.json"

    with pytest.raises(ConfigEditError, match="binds or removes CategorySort"):
        if migrate:
            overrides.write_text(
                json.dumps({"category": {"sort": "VIEWERS_ASC"}}), encoding="utf-8"
            )
            migrate_web_config(config)
        else:
            update_managed_web_config(
                config,
                {"action": "update_category", "values": {"sort": "VIEWERS_ASC"}},
            )

    assert config.read_bytes() == before
    assert overrides.is_file() is migrate


def test_logging_level_save_refuses_a_conflicting_alias_in_the_same_import(tmp_path):
    from TwitchChannelPointsMiner.config_migration import CONFIG_VERSION

    config = tmp_path / "config.py"
    config.write_text(
        f"CONFIG_VERSION = {CONFIG_VERSION}\n"
        "import logging, math as logging\n"
        "from TwitchChannelPointsMiner.logger import LoggerSettings\n"
        "MINER_CONFIG = {\n"
        '    "username": "x",\n'
        '    "logger_settings": LoggerSettings(console_level=20),\n'
        "}\n"
        "STREAMERS = []\n"
        'MINE_CONFIG = {"categories": []}\n'
        "ANALYTICS_CONFIG = None\n",
        encoding="utf-8",
    )
    before = config.read_bytes()

    with pytest.raises(ConfigEditError, match="binds or removes logging"):
        update_managed_web_config(
            config,
            {"action": "update_logging", "values": {"console_level": "WARNING"}},
        )

    assert config.read_bytes() == before


@pytest.mark.parametrize(
    "between",
    [
        "from os.path import *",
        "if True:\n    from os.path import *",
        "try:\n    raise RuntimeError()\nexcept RuntimeError as logging:\n    pass",
        "match None:\n    case logging:\n        pass",
        "class Helper:\n    global logging\n    logging = None",
        "def helper(value=(logging := None)):\n    pass",
        "class Helper(metaclass=(logging := type)):\n    pass",
        "@(logging := (lambda function: function))\ndef helper():\n    pass",
    ],
    ids=[
        "star",
        "nested-star",
        "except",
        "match",
        "class-global",
        "default",
        "class-keyword",
        "decorator",
    ],
)
def test_logging_level_save_refuses_names_bound_at_definition_or_by_star(
    tmp_path, between
):
    from TwitchChannelPointsMiner.config_migration import CONFIG_VERSION

    config = tmp_path / "config.py"
    config.write_text(
        f"CONFIG_VERSION = {CONFIG_VERSION}\n"
        "import logging\n"
        "from TwitchChannelPointsMiner.logger import LoggerSettings\n"
        f"{between}\n"
        "MINER_CONFIG = {\n"
        '    "username": "x",\n'
        '    "logger_settings": LoggerSettings(console_level=20),\n'
        "}\n"
        "STREAMERS = []\n"
        'MINE_CONFIG = {"categories": []}\n'
        "ANALYTICS_CONFIG = None\n",
        encoding="utf-8",
    )
    before = config.read_bytes()

    with pytest.raises(
        ConfigEditError, match="binds or removes (logging|LoggerSettings)"
    ):
        update_managed_web_config(
            config,
            {"action": "update_logging", "values": {"console_level": "WARNING"}},
        )

    assert config.read_bytes() == before


@pytest.mark.parametrize("migrate", [False, True], ids=["save", "migration"])
def test_logging_import_is_not_added_inside_a_leading_docstring(tmp_path, migrate):
    # The first line mentions "coding", which used to put the import on the
    # second line: inside the docstring.
    from TwitchChannelPointsMiner.config_migration import CONFIG_VERSION

    config = tmp_path / "config.py"
    config.write_text(
        '"""My coding configuration.\n'
        "\n"
        'More notes."""\n'
        f"CONFIG_VERSION = {CONFIG_VERSION}\n"
        "from TwitchChannelPointsMiner.logger import LoggerSettings\n"
        "MINER_CONFIG = {\n"
        '    "username": "x",\n'
        '    "logger_settings": LoggerSettings(console_level=20),\n'
        "}\n"
        "STREAMERS = []\n"
        'MINE_CONFIG = {"categories": []}\n'
        "ANALYTICS_CONFIG = None\n",
        encoding="utf-8",
    )
    if migrate:
        (tmp_path / "web-config.json").write_text(
            json.dumps({"logging": {"console_level": "WARNING"}}), encoding="utf-8"
        )
        migrate_web_config(config)
    else:
        update_managed_web_config(
            config,
            {"action": "update_logging", "values": {"console_level": "WARNING"}},
        )

    loaded = _load_config(config)
    assert loaded.__doc__.startswith("My coding configuration.")
    assert loaded.MINER_CONFIG["logger_settings"].console_level == logging.WARNING


@pytest.mark.parametrize(
    "payload",
    [
        {
            "action": "update_notification",
            "provider": "discord",
            "values": {"events": ["DROP_CLAIM"]},
        },
        {"action": "update_logging", "values": {"daily_report": True}},
    ],
    ids=["notifier", "logger-settings"],
)
def test_edits_refuse_a_qualified_helper_named_like_the_constructor(tmp_path, payload):
    # Factory.Discord() may take no arguments at all.
    config = tmp_path / "config.py"
    write_config(config)
    source = config.read_text(encoding="utf-8")
    if payload["action"] == "update_notification":
        source = source.replace(
            'discord=Discord(webhook_api="https://secret.example/hook", events=[])',
            "discord=Factory.Discord()",
        )
    else:
        start = source.index('    "logger_settings": LoggerSettings(\n')
        end = source.index("    ),\n", start) + len("    ),\n")
        source = (
            source[:start]
            + '    "logger_settings": Factory.LoggerSettings(),\n'
            + source[end:]
        )
    source = source.replace(
        "MINER_CONFIG = {",
        "import types\n"
        "Factory = types.SimpleNamespace(\n"
        '    Discord=lambda: Discord(webhook_api="https://x.example/h", events=[]),\n'
        "    LoggerSettings=lambda: LoggerSettings(),\n"
        ")\n"
        "MINER_CONFIG = {",
    )
    config.write_text(source, encoding="utf-8")
    before = config.read_bytes()

    with pytest.raises(ConfigEditError, match="isn't written as"):
        update_managed_web_config(config, payload)

    assert config.read_bytes() == before


def test_import_insertion_refuses_a_statement_sharing_the_docstring_line(tmp_path):
    from TwitchChannelPointsMiner.config_migration import CONFIG_VERSION

    config = tmp_path / "config.py"
    config.write_text(
        '"""example"""; from TwitchChannelPointsMiner.logger import LoggerSettings\n'
        f"CONFIG_VERSION = {CONFIG_VERSION}\n"
        "MINER_CONFIG = {\n"
        '    "username": __doc__,\n'
        '    "logger_settings": LoggerSettings(console_level=20),\n'
        "}\n"
        "STREAMERS = []\n"
        'MINE_CONFIG = {"categories": []}\n'
        "ANALYTICS_CONFIG = None\n",
        encoding="utf-8",
    )
    before = config.read_bytes()

    with pytest.raises(ConfigEditError, match="couldn't add the import"):
        update_managed_web_config(
            config,
            {"action": "update_logging", "values": {"console_level": "WARNING"}},
        )

    assert config.read_bytes() == before


@pytest.mark.parametrize("migrate", [False, True], ids=["save", "migration"])
def test_startup_only_update_check_refuses_a_shadowed_float(tmp_path, migrate):
    config = tmp_path / "config.py"
    before = _edited_config(
        config, ("MINER_CONFIG = {", "float = None\nMINER_CONFIG = {")
    )
    overrides = tmp_path / "web-config.json"

    with pytest.raises(ConfigEditError, match="binds float"):
        if migrate:
            overrides.write_text(
                json.dumps({"updates": {"startup_only": True}}), encoding="utf-8"
            )
            migrate_web_config(config)
        else:
            update_managed_web_config(
                config,
                {"action": "update_updates", "values": {"startup_only": True}},
            )

    assert config.read_bytes() == before
    assert overrides.is_file() is migrate


@pytest.mark.parametrize(
    "payload, replacement",
    [
        (
            {"action": "update_logging", "values": {"console_level": "WARNING"}},
            (
                "        console_level=logging.INFO,\n",
                "        console_level=(LEVEL := logging.INFO),\n",
            ),
        ),
        (
            {"action": "update_updates", "values": {"enabled": False}},
            (
                '    "username": "example",\n',
                '    "update_check": (LEVEL := True),\n    "username": "example",\n',
            ),
        ),
    ],
    ids=["call-keyword", "dict-value"],
)
def test_edits_refuse_replacing_a_walrus_other_settings_use(
    tmp_path, payload, replacement
):
    config = tmp_path / "config.py"
    before = _edited_config(
        config,
        replacement,
        ('    "category_limit": 5,\n', '    "category_limit": LEVEL and 5,\n'),
        # Another LEVEL := in a function must not hide the removed one.
        (
            "MINER_CONFIG = {",
            "def unused():\n    return (LEVEL := 0)\n\n\nMINER_CONFIG = {",
        ),
    )

    with pytest.raises(ConfigEditError, match="assigns LEVEL with :="):
        update_managed_web_config(config, payload)

    assert config.read_bytes() == before


@pytest.mark.skipif(
    not hasattr(ast, "TypeVar"), reason="type parameters need Python 3.12+"
)
@pytest.mark.parametrize(
    "payload, name",
    [
        ({"action": "update_updates", "values": {"startup_only": True}}, "float"),
        (
            {"action": "update_logging", "values": {"console_level": "WARNING"}},
            "logging",
        ),
    ],
    ids=["builtin", "import"],
)
def test_name_checks_include_type_parameters(tmp_path, payload, name):
    config = tmp_path / "config.py"
    before = _edited_config(
        config,
        ("MINER_CONFIG = {", f"def helper[{name}]():\n    pass\n\n\nMINER_CONFIG = {{"),
    )

    with pytest.raises(ConfigEditError, match=f"binds (or removes )?{name}"):
        update_managed_web_config(config, payload)

    assert config.read_bytes() == before
