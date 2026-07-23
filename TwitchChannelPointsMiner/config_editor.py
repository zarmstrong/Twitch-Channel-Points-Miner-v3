# -*- coding: utf-8 -*-

"""Small, source-preserving edits for dashboard-managed configuration lists."""

import ast
import json
import logging
import os
import re
import tempfile
import threading
from pathlib import Path
from urllib.parse import quote, unquote

CONFIG_FILE_MUTEX = threading.Lock()
STREAMER_RE = re.compile(r"^[A-Za-z0-9_]{1,25}$")
WEB_CONFIG_FILENAME = "web-config.json"
STREAMER_SETTING_DEFAULTS = {
    "favorite": False,
    "make_predictions": True,
    "follow_raid": True,
    "claim_drops": True,
    "claim_moments": True,
    "chat": "ONLINE",
    "points_limit": None,
}
STREAMER_SETTING_NAMES = set(STREAMER_SETTING_DEFAULTS)
CATEGORY_SORTS = {
    "ORDER",
    "VIEWERS_DESC",
    "VIEWERS_ASC",
    "STARTED_AT_DESC",
    "STARTED_AT_ASC",
    "RANDOM",
}
CHAT_PRESENCES = {"ALWAYS", "NEVER", "ONLINE", "OFFLINE"}
LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
SOURCE_NAMES = {"streamers", "followers", "categories", "badges"}
NOTIFICATION_SCHEMAS = {
    "telegram": {
        "fields": ("chat_id", "disable_notification", "events"),
        "secrets": ("token",),
    },
    "discord": {"fields": ("events",), "secrets": ("webhook_api",)},
    "webhook": {"fields": ("method", "events"), "secrets": ("endpoint",)},
    "email": {
        "fields": (
            "host",
            "port",
            "username",
            "sender",
            "recipients",
            "use_ssl",
            "starttls",
            "events",
        ),
        "secrets": ("password",),
    },
    "matrix": {
        "fields": ("username", "homeserver", "room_id", "events"),
        "secrets": ("password",),
    },
    "pushover": {
        "fields": ("priority", "sound", "events"),
        "secrets": ("userkey", "token"),
    },
    "gotify": {"fields": ("priority", "events"), "secrets": ("endpoint",)},
}
NOTIFICATION_REQUIRED = {
    "telegram": {"chat_id", "token"},
    "discord": {"webhook_api"},
    "webhook": {"endpoint"},
    "email": {"host", "port", "sender", "recipients"},
    "matrix": {"username", "password", "homeserver", "room_id"},
    "pushover": {"userkey", "token"},
    "gotify": {"endpoint"},
}
NOTIFICATION_POSITIONAL_FIELDS = {
    "telegram": ("chat_id", "token", "events", "disable_notification"),
    "discord": ("webhook_api", "events"),
    "webhook": ("endpoint", "method", "events", "timeout"),
    "email": (
        "host",
        "port",
        "sender",
        "recipients",
        "events",
        "username",
        "password",
        "use_ssl",
        "starttls",
        "timeout",
    ),
    "matrix": ("username", "password", "homeserver", "room_id", "events"),
    "pushover": ("userkey", "token", "priority", "sound", "events"),
    "gotify": ("endpoint", "priority", "events"),
}


class ConfigEditError(ValueError):
    pass


def _assignment(tree, name):
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        ):
            return node.value
    return None


def _dict_item(node, key_name):
    if not isinstance(node, ast.Dict):
        return None
    for key, value in zip(node.keys, node.values):
        if isinstance(key, ast.Constant) and key.value == key_name:
            return value
    return None


def _config_lists(source):
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        raise ConfigEditError(f"Configuration cannot be parsed: {error.msg}") from error
    streamers = _assignment(tree, "STREAMERS")
    categories = _dict_item(_assignment(tree, "MINE_CONFIG"), "categories")
    if not isinstance(streamers, ast.List):
        raise ConfigEditError(
            "STREAMERS must be a literal list to edit it in the web UI."
        )
    if not isinstance(categories, ast.List):
        raise ConfigEditError(
            "MINE_CONFIG['categories'] must be a literal list to edit it in the web UI."
        )
    return tree, streamers, categories


def _streamer_value(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "Streamer"
    ):
        if (
            node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            return node.args[0].value
        for keyword in node.keywords:
            if (
                keyword.arg == "username"
                and isinstance(keyword.value, ast.Constant)
                and isinstance(keyword.value.value, str)
            ):
                return keyword.value.value
    return None


def _simple_value(node):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_simple_value(item) for item in node.elts]
    if isinstance(node, ast.Dict):
        return {
            _simple_value(key): _simple_value(value)
            for key, value in zip(node.keys, node.values)
        }
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return {
            "__call__": getattr(node.func, "id", getattr(node.func, "attr", "")),
            "__args__": [_simple_value(item) for item in node.args],
            **{
                keyword.arg: _simple_value(keyword.value)
                for keyword in node.keywords
                if keyword.arg is not None
            },
        }
    return None


def _base_web_config(config_path):
    from TwitchChannelPointsMiner.classes.Settings import Events

    source = Path(config_path).read_text(encoding="utf-8")
    tree, streamer_nodes, category_nodes = _config_lists(source)
    miner = _simple_value(_assignment(tree, "MINER_CONFIG")) or {}
    mine = _simple_value(_assignment(tree, "MINE_CONFIG")) or {}
    global_streamer_settings = miner.get("streamer_settings") or {}
    effective_streamer_defaults = dict(STREAMER_SETTING_DEFAULTS)
    effective_streamer_defaults.update(
        {
            name: global_streamer_settings[name]
            for name in STREAMER_SETTING_NAMES
            if name in global_streamer_settings
            and global_streamer_settings[name] is not None
        }
    )
    logger_settings = miner.get("logger_settings") or {}

    streamers = []
    for node in streamer_nodes.elts:
        username = _streamer_value(node)
        if username is None:
            continue
        settings = dict(effective_streamer_defaults)
        parsed = _simple_value(node) if isinstance(node, ast.Call) else {}
        explicit_settings = parsed.get("settings") or (
            parsed.get("__args__", [None, {}])[1]
            if len(parsed.get("__args__", [])) > 1
            else {}
        )
        settings.update(
            {
                name: explicit_settings[name]
                for name in STREAMER_SETTING_NAMES
                if name in explicit_settings and explicit_settings[name] is not None
            }
        )
        streamers.append({"username": username, "settings": settings})

    categories = [
        node.value
        for node in category_nodes.elts
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    sources = {
        "streamers": True,
        "followers": bool(mine.get("followers", False)),
        "categories": bool(categories),
        "badges": bool(mine.get("auto_mine_badge_drops", False)),
    }
    notifications = {}
    for provider, schema in NOTIFICATION_SCHEMAS.items():
        configured = logger_settings.get(provider)
        if not isinstance(configured, dict):
            configured = {}
        else:
            configured = dict(configured)
            for name, value in zip(
                NOTIFICATION_POSITIONAL_FIELDS[provider],
                configured.get("__args__", []),
            ):
                configured.setdefault(name, value)
        fields = {
            name: configured.get(name)
            for name in schema["fields"]
            if name in configured
        }
        notifications[provider] = {
            "enabled": bool(configured),
            "fields": fields,
            "secrets": {name: bool(configured.get(name)) for name in schema["secrets"]},
        }

    return {
        "streamers": streamers,
        "streamer_defaults": effective_streamer_defaults,
        "categories": categories,
        "category": {
            "limit": mine.get("category_limit", 30),
            "sort": mine.get("category_sort", "VIEWERS_DESC"),
            "refresh_interval_hours": mine.get("category_refresh_interval_hours", 6),
            "drops_enabled": mine.get("category_drops_enabled", True),
        },
        "sources": sources,
        "logging": {
            "console_level": logger_settings.get("console_level", "INFO"),
            "file_level": logger_settings.get("file_level", "DEBUG"),
            "daily_report": logger_settings.get("daily_report", False),
            "daily_report_time": logger_settings.get("daily_report_time", "00:00"),
        },
        "notifications": notifications,
        "notification_schemas": NOTIFICATION_SCHEMAS,
        "notification_event_options": [event.name for event in Events],
    }


def _overrides_path(config_path):
    return Path(config_path).with_name(WEB_CONFIG_FILENAME)


def load_web_overrides(config_path):
    path = _overrides_path(config_path)
    if not path.is_file():
        return {}
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        raise
    try:
        data = json.loads(source)
    except json.JSONDecodeError as error:
        raise ConfigEditError(
            f"{WEB_CONFIG_FILENAME} contains invalid JSON."
        ) from error
    if not isinstance(data, dict):
        raise ConfigEditError(f"{WEB_CONFIG_FILENAME} must contain a JSON object.")
    return data


def _merge_web_config(base, overrides):
    result = json.loads(json.dumps(base))
    for name in ("streamers", "categories"):
        if name in overrides:
            result[name] = overrides[name]
    for name in ("category", "sources", "logging"):
        result[name].update(overrides.get(name, {}))
    if "categories" not in overrides.get("sources", {}):
        result["sources"]["categories"] = bool(result["categories"])
    for provider, update in overrides.get("notifications", {}).items():
        if provider not in result["notifications"]:
            continue
        result["notifications"][provider]["enabled"] = update.get(
            "enabled", result["notifications"][provider]["enabled"]
        )
        result["notifications"][provider]["fields"].update(update.get("fields", {}))
        known_secrets = set(NOTIFICATION_SCHEMAS[provider]["secrets"])
        for secret in set(update.get("secrets", {})) & known_secrets:
            result["notifications"][provider]["secrets"][secret] = True
    for provider, state in result["notifications"].items():
        available = {
            name
            for name, value in state["fields"].items()
            if value not in (None, "", [])
        }
        available.update(
            name for name, configured in state["secrets"].items() if configured
        )
        state["test_available"] = state["enabled"] and NOTIFICATION_REQUIRED[
            provider
        ].issubset(available)
    return result


def read_managed_web_config(config_path):
    return _merge_web_config(
        _base_web_config(config_path), load_web_overrides(config_path)
    )


def _write_web_overrides(config_path, data):
    path = _overrides_path(config_path)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=str(path.parent), text=True
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
            json.dump(data, temporary, indent=2, sort_keys=True)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        try:
            os.chmod(temporary_name, 0o600)
        except OSError:
            # Some mounted and Windows filesystems do not expose POSIX modes.
            pass
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _validate_streamer_settings(settings):
    if not isinstance(settings, dict) or set(settings) - STREAMER_SETTING_NAMES:
        raise ConfigEditError("Unsupported per-streamer setting.")
    for name in STREAMER_SETTING_NAMES - {"chat", "points_limit"}:
        if name in settings and not isinstance(settings[name], bool):
            raise ConfigEditError(f"{name} must be true or false.")
    if "chat" in settings and settings["chat"] not in CHAT_PRESENCES:
        raise ConfigEditError("Invalid chat presence.")
    points_limit = settings.get("points_limit")
    if points_limit is not None and (
        not isinstance(points_limit, int)
        or isinstance(points_limit, bool)
        or points_limit < 0
    ):
        raise ConfigEditError("Points limit must be a non-negative integer or null.")


def _valid_managed_category(value):
    return (
        isinstance(value, str)
        and 1 <= len(value.strip()) <= 300
        and not any(character in value for character in "\r\n\x00")
    )


def update_managed_web_config(config_path, payload):
    with CONFIG_FILE_MUTEX:
        return _update_managed_web_config(config_path, payload)


def _update_managed_web_config(config_path, payload):
    if not isinstance(payload, dict):
        raise ConfigEditError("The configuration update must be a JSON object.")
    action = payload.get("action")
    current = read_managed_web_config(config_path)
    overrides = load_web_overrides(config_path)

    if action in {"add", "remove"}:
        kind = payload.get("kind")
        value = str(payload.get("value", "")).strip()
        valid = (
            STREAMER_RE.fullmatch(value) is not None
            if kind == "streamers"
            else _valid_managed_category(value)
            if kind == "categories"
            else False
        )
        if not valid:
            raise ConfigEditError("Invalid streamer username or category value.")
        items = list(current[kind])
        names = [item["username"] if kind == "streamers" else item for item in items]
        matching = {name.lower() for name in names}
        if action == "add":
            if value.lower() in matching:
                raise ConfigEditError(f"{value} is already configured.")
            items.append(
                {
                    "username": value,
                    "settings": dict(current["streamer_defaults"]),
                }
                if kind == "streamers"
                else value
            )
        else:
            if value.lower() not in matching:
                raise ConfigEditError(f"{value} is not configured.")
            items = [
                item
                for item in items
                if (item["username"] if kind == "streamers" else item).lower()
                != value.lower()
            ]
        overrides[kind] = items
    elif action == "reorder_categories":
        categories = payload.get("categories")
        if not isinstance(categories, list) or any(
            not _valid_managed_category(item) for item in categories
        ):
            raise ConfigEditError("Invalid category order.")
        if sorted(map(str.lower, categories)) != sorted(
            map(str.lower, current["categories"])
        ):
            raise ConfigEditError(
                "Category order must contain every configured category."
            )
        overrides["categories"] = categories
    elif action == "update_streamer":
        username = str(payload.get("username", "")).lower().strip()
        settings = payload.get("settings")
        _validate_streamer_settings(settings)
        streamers = list(current["streamers"])
        for streamer in streamers:
            if streamer["username"].lower() == username:
                streamer["settings"].update(settings)
                break
        else:
            raise ConfigEditError("Streamer is not configured.")
        overrides["streamers"] = streamers
    elif action == "update_category":
        values = payload.get("values") or {}
        allowed = {"limit", "sort", "refresh_interval_hours", "drops_enabled"}
        if not isinstance(values, dict) or set(values) - allowed:
            raise ConfigEditError("Unsupported category setting.")
        if "limit" in values and (
            not isinstance(values["limit"], int)
            or isinstance(values["limit"], bool)
            or not 1 <= values["limit"] <= 100
        ):
            raise ConfigEditError("Category limit must be between 1 and 100.")
        if "sort" in values and values["sort"] not in CATEGORY_SORTS:
            raise ConfigEditError("Invalid category sort.")
        if "refresh_interval_hours" in values and (
            not isinstance(values["refresh_interval_hours"], (int, float))
            or isinstance(values["refresh_interval_hours"], bool)
            or not 0 <= values["refresh_interval_hours"] <= 168
        ):
            raise ConfigEditError("Refresh interval must be between 0 and 168 hours.")
        if "drops_enabled" in values and not isinstance(values["drops_enabled"], bool):
            raise ConfigEditError("Drops-only behavior must be true or false.")
        overrides.setdefault("category", {}).update(values)
    elif action == "update_sources":
        values = payload.get("values") or {}
        if (
            not isinstance(values, dict)
            or set(values) - SOURCE_NAMES
            or any(not isinstance(value, bool) for value in values.values())
        ):
            raise ConfigEditError("Invalid stream source controls.")
        overrides.setdefault("sources", {}).update(values)
    elif action == "update_logging":
        values = payload.get("values") or {}
        allowed = {"console_level", "file_level", "daily_report", "daily_report_time"}
        if not isinstance(values, dict) or set(values) - allowed:
            raise ConfigEditError("Unsupported logging setting.")
        for name in ("console_level", "file_level"):
            if name in values and values[name] not in LOG_LEVELS:
                raise ConfigEditError("Invalid logging level.")
        if "daily_report" in values and not isinstance(values["daily_report"], bool):
            raise ConfigEditError("Daily report must be true or false.")
        if "daily_report_time" in values and not re.fullmatch(
            r"(?:[01]\d|2[0-3]):[0-5]\d", str(values["daily_report_time"])
        ):
            raise ConfigEditError("Daily report time must use HH:MM.")
        overrides.setdefault("logging", {}).update(values)
    elif action == "update_notification":
        provider = payload.get("provider")
        schema = NOTIFICATION_SCHEMAS.get(provider)
        values = payload.get("values") or {}
        if schema is None or not isinstance(values, dict):
            raise ConfigEditError("Invalid notification provider.")
        values = dict(values)
        allowed = {"enabled", *schema["fields"], *schema["secrets"]}
        if set(values) - allowed or (
            "enabled" in values and not isinstance(values["enabled"], bool)
        ):
            raise ConfigEditError("Unsupported notification setting.")
        for list_name in ("events", "recipients"):
            if list_name in values and (
                not isinstance(values[list_name], list)
                or any(not isinstance(item, str) for item in values[list_name])
            ):
                raise ConfigEditError(f"{list_name} must be a list of strings.")
        for number_name in ("chat_id", "port", "priority"):
            if values.get(number_name) == "":
                values.pop(number_name)
                continue
            if number_name in values and (
                not isinstance(values[number_name], int)
                or isinstance(values[number_name], bool)
            ):
                raise ConfigEditError(f"{number_name} must be an integer.")
        for bool_name in ("disable_notification", "use_ssl", "starttls"):
            if bool_name in values and not isinstance(values[bool_name], bool):
                raise ConfigEditError(f"{bool_name} must be true or false.")
        list_fields = {"events", "recipients"}
        number_fields = {"chat_id", "port", "priority"}
        bool_fields = {"disable_notification", "use_ssl", "starttls"}
        text_fields = set(schema["fields"]) - list_fields - number_fields - bool_fields
        for text_name in text_fields | set(schema["secrets"]):
            if text_name in values and not isinstance(values[text_name], str):
                raise ConfigEditError(f"{text_name} must be a string.")
        if "method" in values and values["method"].upper() not in {"GET", "POST"}:
            raise ConfigEditError("Webhook method must be GET or POST.")
        if values.get("use_ssl") is True and values.get("starttls") is True:
            raise ConfigEditError("Email SSL and STARTTLS cannot both be enabled.")
        if values.get("enabled") is True:
            state = current["notifications"][provider]
            available = {
                name
                for name, value in {**state["fields"], **values}.items()
                if value not in (None, "", [])
            }
            available.update(
                name for name, configured in state["secrets"].items() if configured
            )
            available.update(
                name
                for name in schema["secrets"]
                if isinstance(values.get(name), str) and values[name]
            )
            missing = sorted(NOTIFICATION_REQUIRED[provider] - available)
            if missing:
                raise ConfigEditError(f"{provider} requires: {', '.join(missing)}.")
        update = overrides.setdefault("notifications", {}).setdefault(provider, {})
        if "enabled" in values:
            update["enabled"] = values.pop("enabled")
        fields = {name: values[name] for name in schema["fields"] if name in values}
        secrets = {
            name: values[name]
            for name in schema["secrets"]
            if isinstance(values.get(name), str) and values[name]
        }
        if secrets:
            for name, value in current["notifications"][provider]["fields"].items():
                fields.setdefault(name, value)
        update.setdefault("fields", {}).update(fields)
        update.setdefault("secrets", {}).update(secrets)
    else:
        raise ConfigEditError("Unsupported configuration action.")

    _write_web_overrides(config_path, overrides)
    return read_managed_web_config(config_path)


def apply_web_overrides(config, config_path):
    """Apply dashboard overrides to an executed configuration module."""
    overrides = load_web_overrides(config_path)
    if not overrides:
        return config

    from TwitchChannelPointsMiner.classes.Chat import ChatPresence
    from TwitchChannelPointsMiner.classes.Discord import Discord
    from TwitchChannelPointsMiner.classes.Email import Email
    from TwitchChannelPointsMiner.classes.entities.Streamer import (
        Streamer,
        StreamerSettings,
    )
    from TwitchChannelPointsMiner.classes.Gotify import Gotify
    from TwitchChannelPointsMiner.classes.Matrix import Matrix
    from TwitchChannelPointsMiner.classes.Pushover import Pushover
    from TwitchChannelPointsMiner.classes.Telegram import Telegram
    from TwitchChannelPointsMiner.classes.Webhook import Webhook

    if "streamers" in overrides:
        existing = {
            str(getattr(item, "username", item)).lower().strip(): item
            for item in config.STREAMERS
        }
        configured = []
        records = overrides["streamers"]
        if not isinstance(records, list):
            raise ConfigEditError("Managed streamers must be a list.")
        for record in records:
            if not isinstance(record, dict):
                raise ConfigEditError("Each managed streamer must be an object.")
            username_value = record.get("username")
            settings_value = record.get("settings", {})
            if (
                not isinstance(username_value, str)
                or STREAMER_RE.fullmatch(username_value.strip()) is None
            ):
                raise ConfigEditError("Each managed streamer needs a valid username.")
            _validate_streamer_settings(settings_value)
            username = username_value.lower().strip()
            streamer = existing.get(username)
            if not isinstance(streamer, Streamer):
                streamer = Streamer(username)
            settings = streamer.settings or StreamerSettings()
            for name, value in settings_value.items():
                if name == "chat":
                    value = ChatPresence[value]
                setattr(settings, name, value)
            streamer.settings = settings
            configured.append(streamer)
        config.STREAMERS = configured

    if "categories" in overrides:
        categories = overrides["categories"]
        if not isinstance(categories, list) or any(
            not _valid_managed_category(category) for category in categories
        ):
            raise ConfigEditError("Managed categories must be a list of valid values.")

    sources = overrides.get("sources", {})
    if sources.get("streamers") is False:
        config.STREAMERS = []
    if "followers" in sources:
        config.MINE_CONFIG["followers"] = sources["followers"]
    if sources.get("categories") is False:
        config.MINE_CONFIG["categories"] = []
    elif "categories" in overrides:
        config.MINE_CONFIG["categories"] = list(categories)
    if "badges" in sources:
        config.MINE_CONFIG["auto_mine_badge_drops"] = sources["badges"]

    category = overrides.get("category", {})
    category_mapping = {
        "limit": "category_limit",
        "sort": "category_sort",
        "refresh_interval_hours": "category_refresh_interval_hours",
        "drops_enabled": "category_drops_enabled",
    }
    for source_name, target_name in category_mapping.items():
        if source_name in category:
            config.MINE_CONFIG[target_name] = category[source_name]

    logger_settings = config.MINER_CONFIG.get("logger_settings")
    logging_overrides = overrides.get("logging", {})
    if logger_settings is not None:
        for name in ("console_level", "file_level"):
            if name in logging_overrides:
                setattr(
                    logger_settings,
                    name,
                    getattr(logging, logging_overrides[name]),
                )
        for name in ("daily_report", "daily_report_time"):
            if name in logging_overrides:
                setattr(logger_settings, name, logging_overrides[name])

        constructors = {
            "telegram": Telegram,
            "discord": Discord,
            "webhook": Webhook,
            "email": Email,
            "matrix": Matrix,
            "pushover": Pushover,
            "gotify": Gotify,
        }
        for provider, update in overrides.get("notifications", {}).items():
            existing_notification = getattr(logger_settings, provider, None)
            if update.get("enabled") is False:
                setattr(logger_settings, provider, None)
                continue
            fields = dict(update.get("fields", {}))
            secrets = dict(update.get("secrets", {}))
            if existing_notification is not None and not secrets:
                for name, value in fields.items():
                    if name == "events":
                        value = [str(event) for event in value]
                    if provider == "matrix" and name == "room_id":
                        value = quote(value)
                    if hasattr(existing_notification, name):
                        setattr(existing_notification, name, value)
                continue
            if update.get("enabled") is not True and not secrets:
                continue
            kwargs = _notification_constructor_kwargs(
                provider, existing_notification, fields, secrets
            )
            setattr(logger_settings, provider, constructors[provider](**kwargs))
    return config


def _notification_constructor_kwargs(provider, existing, fields, secrets):
    def current(name, default=None):
        return getattr(existing, name, default) if existing is not None else default

    events = fields.get("events", current("events", []))
    if provider == "telegram":
        token = secrets.get("token")
        if token is None and existing is not None:
            token = existing.telegram_api.split("/bot", 1)[-1].rsplit(
                "/sendMessage", 1
            )[0]
        return {
            "chat_id": fields.get("chat_id", current("chat_id")),
            "token": token,
            "events": events,
            "disable_notification": fields.get(
                "disable_notification", current("disable_notification", False)
            ),
        }
    if provider == "discord":
        return {
            "webhook_api": secrets.get("webhook_api", current("webhook_api")),
            "events": events,
        }
    if provider == "webhook":
        return {
            "endpoint": secrets.get("endpoint", current("endpoint")),
            "method": fields.get("method", current("method", "POST")),
            "events": events,
        }
    if provider == "gotify":
        return {
            "endpoint": secrets.get("endpoint", current("endpoint")),
            "priority": fields.get("priority", current("priority", 0)),
            "events": events,
        }
    if provider == "pushover":
        return {
            "userkey": secrets.get("userkey", current("userkey")),
            "token": secrets.get("token", current("token")),
            "priority": fields.get("priority", current("priority", 0)),
            "sound": fields.get("sound", current("sound", "pushover")),
            "events": events,
        }
    if provider == "email":
        return {
            "host": fields.get("host", current("host")),
            "port": fields.get("port", current("port", 587)),
            "username": fields.get("username", current("username")),
            "password": secrets.get("password", current("password")),
            "sender": fields.get("sender", current("sender")),
            "recipients": fields.get("recipients", current("recipients", [])),
            "events": events,
            "use_ssl": fields.get("use_ssl", current("use_ssl", False)),
            "starttls": fields.get("starttls", current("starttls", True)),
        }
    if provider == "matrix":
        room_id = fields.get("room_id")
        if room_id is None:
            room_id = unquote(current("room_id"))
        return {
            "username": fields.get("username"),
            "password": secrets.get("password"),
            "homeserver": fields.get("homeserver", current("homeserver")),
            "room_id": room_id,
            "events": events,
        }
    raise ConfigEditError("Unsupported notification provider.")
