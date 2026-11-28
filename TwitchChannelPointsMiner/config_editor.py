# -*- coding: utf-8 -*-

"""Source-preserving edits for dashboard-managed Python configuration."""

import ast
import contextlib
import errno
import functools
import io
import json
import os
import re
import stat
import tempfile
import threading
import tokenize
from pathlib import Path

# Held by every public writer of config.py for its whole read-modify-write,
# so one can't write over another's change with an older copy. Reentrant:
# update_managed_web_config runs migrate_web_config while holding it.
CONFIG_FILE_MUTEX = threading.RLock()
# The config.py bytes of the edit this thread has open (see _config_edit).
_STAGED_CONFIG = threading.local()
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
    "streak_only": False,
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
SOURCE_NAMES = {"streamers", "followers", "categories", "badges", "wildcard_categories"}
# Ordered UI-name -> StreamerSource enum-name mapping, shared between the
# read side (building source_order) and the write side (update_sources) so
# the two can't silently drift out of sync.
SOURCE_ENUM_BY_NAME = {
    "streamers": "STREAMERS",
    "followers": "FOLLOWERS",
    "categories": "CATEGORIES",
    "badges": "BADGES",
    "wildcard_categories": "WILDCARD_CATEGORIES",
}
DEFAULT_SOURCE_ORDER = tuple(SOURCE_ENUM_BY_NAME)
NOTIFICATION_SCHEMAS = {
    "telegram": {
        "fields": ("chat_id", "message_thread_id", "disable_notification", "events"),
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
    "ntfy": {
        "fields": ("server_url", "priority", "tags", "events"),
        "secrets": ("topic", "token"),
    },
}
NOTIFICATION_REQUIRED = {
    "telegram": {"chat_id", "token"},
    "discord": {"webhook_api"},
    "webhook": {"endpoint"},
    "email": {"host", "port", "sender", "recipients"},
    "matrix": {"username", "password", "homeserver", "room_id"},
    "pushover": {"userkey", "token"},
    "gotify": {"endpoint"},
    "ntfy": {"topic"},
}
# Every constructor argument without a default. Enabling a notifier without
# one would write a call that raises TypeError, so the miner wouldn't start.
# Unlike NOTIFICATION_REQUIRED, an empty events list counts as given here.
NOTIFICATION_CONSTRUCTOR_REQUIRED = {
    "telegram": {"chat_id", "token", "events"},
    "discord": {"webhook_api", "events"},
    "webhook": {"endpoint", "method", "events"},
    "email": {"host", "port", "sender", "recipients", "events"},
    "matrix": {"username", "password", "homeserver", "room_id", "events"},
    "pushover": {"userkey", "token", "priority", "sound", "events"},
    "gotify": {"endpoint", "priority", "events"},
    "ntfy": {"topic", "events"},
}
# Constructor defaults the dashboard must show when a config omits the field.
# The form renders a missing boolean as unchecked and saves every checkbox,
# so re-saving an Email notifier would otherwise write starttls=False and
# send SMTP credentials in cleartext.
NOTIFICATION_FIELD_DEFAULTS = {
    "email": {"use_ssl": False, "starttls": True},
}
NOTIFICATION_POSITIONAL_FIELDS = {
    "telegram": (
        "chat_id",
        "token",
        "events",
        "disable_notification",
        "message_thread_id",
    ),
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
    "ntfy": (
        "topic",
        "events",
        "server_url",
        "token",
        "priority",
        "tags",
        "timeout",
    ),
}


def _holding_config_mutex(function):
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        with CONFIG_FILE_MUTEX:
            return function(*args, **kwargs)

    return wrapper


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
    """The value of a literal dict entry, or None without one.

    Python keeps the last of repeated keys, so this returns the last entry
    for key_name: the one the miner uses, and the one edits must change.
    """
    if not isinstance(node, ast.Dict):
        return None
    for key, value in reversed(list(zip(node.keys, node.values))):
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
        try:
            return {
                _simple_value(key): _simple_value(value)
                for key, value in zip(node.keys, node.values)
            }
        except TypeError:
            # A key the reader can't hash, e.g. a tuple, read as a list.
            return None
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


def read_config_literal(source, assignment_name, key=None, default=None):
    """Read a literal config value without executing the configuration.

    Optionally select a key from a literal dictionary assignment. Missing
    values and non-literal expressions return default; invalid Python raises
    SyntaxError so callers can distinguish an unreadable configuration.
    """
    node = _assignment(ast.parse(source), assignment_name)
    if key is not None:
        node = _dict_item(node, key)
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError):
        return default


def _base_web_config(config_path):
    from TwitchChannelPointsMiner.classes.Settings import Events

    source = _read_config_source(config_path)
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
    update_interval = miner.get("update_check_interval_hours", 24)
    startup_only = update_interval == "inf" or (
        isinstance(update_interval, dict)
        and update_interval.get("__call__") == "float"
        and update_interval.get("__args__") == ["inf"]
    )

    streamers = []
    for node in streamer_nodes.elts:
        username = _streamer_value(node)
        if username is None:
            continue
        # Streamer() strips the username, so the dashboard shows it stripped.
        username = username.strip()
        settings = dict(effective_streamer_defaults)
        parsed = _simple_value(node) if isinstance(node, ast.Call) else {}
        explicit_settings = parsed.get("settings")
        if explicit_settings is None and len(parsed.get("__args__", [])) > 1:
            explicit_settings = parsed["__args__"][1]
        if not isinstance(explicit_settings, dict):
            # A variable or other expression (e.g. Streamer("x", SETTINGS)):
            # the dashboard can't read it, and edits to it are refused.
            explicit_settings = {}
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
    source_priority = miner.get("streamer_source_priority") or list(
        ("STREAMERS", "FOLLOWERS", "CATEGORIES", "BADGES", "WILDCARD_CATEGORIES")
    )
    sources = {
        "streamers": "STREAMERS" in source_priority,
        "followers": "FOLLOWERS" in source_priority
        and bool(mine.get("followers", False)),
        "categories": "CATEGORIES" in source_priority and bool(categories),
        "badges": "BADGES" in source_priority
        and bool(mine.get("auto_mine_badge_drops", False)),
        "wildcard_categories": "WILDCARD_CATEGORIES" in source_priority
        and bool(mine.get("wildcard_categories", False)),
    }
    ui_name_by_enum = {enum: name for name, enum in SOURCE_ENUM_BY_NAME.items()}
    source_order = [
        ui_name_by_enum[member]
        for member in source_priority
        if member in ui_name_by_enum
    ]
    source_order += [name for name in DEFAULT_SOURCE_ORDER if name not in source_order]
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
        for name, default in NOTIFICATION_FIELD_DEFAULTS.get(provider, {}).items():
            fields.setdefault(name, default)
        notifications[provider] = {
            "enabled": bool(configured),
            "fields": fields,
            "secrets": {name: bool(configured.get(name)) for name in schema["secrets"]},
        }
        available = {
            name for name, value in fields.items() if value not in (None, "", [])
        }
        available.update(
            name
            for name, present in notifications[provider]["secrets"].items()
            if present
        )
        notifications[provider]["test_available"] = bool(configured) and (
            NOTIFICATION_REQUIRED[provider].issubset(available)
        )

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
        "source_order": source_order,
        "category_drops_override_streamers": mine.get(
            "category_drops_override_streamers", False
        )
        is True,
        "logging": {
            "console_level": logger_settings.get("console_level", "INFO"),
            "file_level": logger_settings.get("file_level", "DEBUG"),
            "daily_report": logger_settings.get("daily_report", False),
            "daily_report_time": logger_settings.get("daily_report_time", "00:00"),
        },
        "updates": {
            "enabled": miner.get("update_check", True),
            "interval_hours": 24 if startup_only else update_interval,
            "startup_only": startup_only,
        },
        "login": {"android": miner.get("android_login", False) is True},
        "notifications": notifications,
        "notification_schemas": NOTIFICATION_SCHEMAS,
        # WATCH_STATUS is never sent to notifiers, so it isn't offered.
        "notification_event_options": [
            event.name for event in Events if event is not Events.WATCH_STATUS
        ],
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


def read_managed_web_config(config_path):
    return _base_web_config(config_path)


@_holding_config_mutex
def migrate_web_config(config_path):
    path = _overrides_path(config_path)
    if not path.is_file():
        return False
    # Move everything into config.py in one write before retiring the file,
    # so a refusal part-way leaves both files as they were.
    with _config_edit(config_path):
        overrides = load_web_overrides(config_path)
        if "streamers" in overrides:
            records = overrides["streamers"]
            if not isinstance(records, list):
                raise ConfigEditError("Managed streamers must be a list.")
            for record in records:
                if not isinstance(record, dict):
                    raise ConfigEditError("Each managed streamer must be an object.")
                username = record.get("username")
                if (
                    not isinstance(username, str)
                    or STREAMER_RE.fullmatch(username) is None
                ):
                    raise ConfigEditError(
                        "Each managed streamer needs a valid username."
                    )
                _validate_streamer_settings(record.get("settings", {}))
            try:
                always_write = {}
                for record in records:
                    key = _username_key(record["username"])
                    # _write_streamers uses the first record for each username.
                    if key not in always_write:
                        always_write[key] = _require_editable_settings(
                            config_path, key, record.get("settings") or {}
                        )
                _write_streamers(
                    config_path,
                    records,
                    {
                        _username_key(str(record.get("username", "")))
                        for record in records
                    },
                    always_write=always_write,
                )
            except ConfigEditError as error:
                raise ConfigEditError(
                    f"Couldn't move the streamers saved in {path.name} into "
                    f"config.py. {error} To discard them instead, delete "
                    f"{path.name}."
                ) from error
        if "categories" in overrides:
            categories = overrides["categories"]
            if not isinstance(categories, list) or any(
                not _valid_managed_category(category) for category in categories
            ):
                raise ConfigEditError(
                    "Managed categories must be a list of valid values."
                )
            _write_config_categories(config_path, categories)
        actions = {
            "category": "update_category",
            "sources": "update_sources",
            "logging": "update_logging",
            "updates": "update_updates",
        }
        for name, action in actions.items():
            if name in overrides:
                if not isinstance(overrides[name], dict):
                    messages = {
                        "category": "Invalid managed category settings.",
                        "sources": "Managed stream sources must be Boolean values.",
                        "logging": "Invalid managed logging settings.",
                        "updates": "Invalid managed update settings.",
                    }
                    raise ConfigEditError(messages[name])
                _update_managed_web_config(
                    config_path, {"action": action, "values": overrides[name]}
                )
        notifications = overrides.get("notifications", {})
        if not isinstance(notifications, dict):
            raise ConfigEditError("Managed notifications must be an object.")
        for provider, update in notifications.items():
            if provider not in NOTIFICATION_SCHEMAS or not isinstance(update, dict):
                raise ConfigEditError("Invalid managed notification provider.")
            if not isinstance(update.get("fields", {}), dict) or not isinstance(
                update.get("secrets", {}), dict
            ):
                raise ConfigEditError(
                    "Managed notification fields and secrets must be objects."
                )
            values = dict(update.get("fields", {}))
            values.update(
                {
                    name: value
                    for name, value in update.get("secrets", {}).items()
                    if name in NOTIFICATION_SCHEMAS.get(provider, {}).get("secrets", ())
                }
            )
            if "enabled" in update:
                values["enabled"] = update["enabled"]
            _update_managed_web_config(
                config_path,
                {
                    "action": "update_notification",
                    "provider": provider,
                    "values": values,
                },
            )
    os.replace(path, path.with_name(f".{WEB_CONFIG_FILENAME}.migrated.bak"))
    return True


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


def _write_config_categories(config_path, categories):
    path = Path(config_path)
    encoded = _read_config_bytes(path)
    tree, _streamers, category_node = _config_lists(encoded.decode("utf-8"))
    _require_dict_edit_allowed(
        _assignment(tree, "MINE_CONFIG"), "MINE_CONFIG", "categories"
    )
    if not all(
        isinstance(item, ast.Constant) and isinstance(item.value, str)
        for item in category_node.elts
    ):
        # The list is rewritten from the names the dashboard reads, so a
        # variable or *unpacking in it would be dropped.
        raise ConfigEditError(
            "MINE_CONFIG['categories'] in config.py has entries that aren't "
            "plain strings, so the dashboard can't change the list without "
            "losing them. Edit the categories in config.py instead."
        )
    encoded_lines = encoded.splitlines(keepends=True)
    start = _byte_offset(encoded_lines, category_node.lineno, category_node.col_offset)
    end = _byte_offset(
        encoded_lines, category_node.end_lineno, category_node.end_col_offset
    )
    line = encoded_lines[category_node.lineno - 1]
    indentation = line[: len(line) - len(line.lstrip())].decode("utf-8")
    if categories:
        newline = _newline(encoded)
        rendered = (
            "["
            + newline
            + "".join(
                f"{indentation}    {category!r},{newline}" for category in categories
            )
            + f"{indentation}]"
        )
    else:
        rendered = "[]"
    updated = encoded[:start] + rendered.encode("utf-8") + encoded[end:]
    _require_parses(updated)
    _store_config_bytes(path, updated)


def _replace_config_node(config_path, node, rendered):
    path = Path(config_path)
    # Read bytes, not text: read_text() translates CRLF to LF, which would
    # rewrite every line ending in the user's file.
    encoded = _read_config_bytes(path)
    # Split bytes, not str: str.splitlines() also breaks on U+2028, a form
    # feed and similar characters, which would shift the offsets and splice
    # the value into the wrong place.
    encoded_lines = encoded.splitlines(keepends=True)
    start = _byte_offset(encoded_lines, node.lineno, node.col_offset)
    end = _byte_offset(encoded_lines, node.end_lineno, node.end_col_offset)
    updated = encoded[:start] + rendered.encode("utf-8") + encoded[end:]
    _require_parses(updated)
    _store_config_bytes(path, updated)


def _staged_key(config_path):
    return os.path.abspath(os.fspath(config_path))


def _read_config_bytes(config_path):
    """config.py as this thread's open edit has it, or as it is on disk."""
    staged = getattr(_STAGED_CONFIG, "files", None)
    if staged is not None:
        key = _staged_key(config_path)
        if key in staged:
            return staged[key]
    return Path(config_path).read_bytes()


def _read_config_source(config_path):
    # Python's parser reads \r\n and \r line breaks itself, so the text
    # doesn't need read_text()'s newline translation.
    return _read_config_bytes(config_path).decode("utf-8")


def _store_config_bytes(path, updated):
    """Write config.py, or keep the bytes for the open edit to write later."""
    staged = getattr(_STAGED_CONFIG, "files", None)
    key = _staged_key(path)
    if staged is not None and key in staged:
        staged[key] = updated
        return
    path = Path(path)
    _replace_config_bytes(path, updated, stat.S_IMODE(path.stat().st_mode))


@contextlib.contextmanager
def _config_edit(config_path):
    """Write all of an edit's changes to config.py at once, or none of them.

    A dashboard action can change several parts of config.py, for example
    MINER_CONFIG and then MINE_CONFIG. Writing each part as it goes would
    leave the first change saved when a later part is refused, while the
    dashboard reports the whole action as failed. Inside this block every
    read sees the changes made so far and every write is kept in memory; the
    result is written once when the block finishes without an error. A block
    opened inside another one for the same file joins it.
    """
    key = _staged_key(config_path)
    outer = getattr(_STAGED_CONFIG, "files", None)
    if outer is not None and key in outer:
        yield
        return
    path = Path(config_path)
    original = path.read_bytes()
    _STAGED_CONFIG.files = {**(outer or {}), key: original}
    try:
        yield
        updated = _STAGED_CONFIG.files[key]
    finally:
        _STAGED_CONFIG.files = outer
    if updated != original:
        _replace_config_bytes(path, updated, stat.S_IMODE(path.stat().st_mode))


def _replace_config_bytes(path, updated, mode):
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=str(path.parent), text=False
    )
    try:
        with os.fdopen(descriptor, "wb") as temporary:
            temporary.write(updated)
            temporary.flush()
            os.fsync(temporary.fileno())
        try:
            os.chmod(temporary_name, mode)
        except OSError:
            pass
        try:
            os.replace(temporary_name, path)
        except OSError as error:
            if error.errno != errno.EBUSY:
                raise
            with path.open("r+b") as handle:
                handle.seek(0)
                handle.write(updated)
                handle.truncate()
                handle.flush()
                os.fsync(handle.fileno())
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _expression(source):
    return ast.parse(source, mode="eval").body


def _unpacked_dict_keys(node):
    """The keys a ``**`` entry is known to set, or None when they can't be read."""
    if not isinstance(node, ast.Dict):
        return None
    keys = set()
    for key in node.keys:
        if key is None or not (
            isinstance(key, ast.Constant) and isinstance(key.value, str)
        ):
            return None
        keys.add(key.value)
    return keys


def _require_dict_edit_allowed(dictionary, assignment_name, name):
    """Refuse a dict edit unless the miner uses the literal entry it changes.

    Python applies each entry in order, so a later ``**`` mapping or computed
    key replaces an earlier literal entry. The dashboard could then report the
    saved value while the miner keeps using another one. The edit is allowed
    only when the last literal entry for name comes after every entry that
    might set it, or when nothing else can set it and the edit adds the entry
    at the end. An entry that might set it is a ``**`` mapping whose keys
    can't all be read or that names it, or a key that isn't a constant.
    """
    index = next(
        (
            position
            for position in reversed(range(len(dictionary.keys)))
            if isinstance(dictionary.keys[position], ast.Constant)
            and dictionary.keys[position].value == name
        ),
        None,
    )
    for position, key in enumerate(dictionary.keys):
        if index is not None and position < index:
            # The literal entry after this one wins.
            continue
        if isinstance(key, ast.Constant):
            continue
        if key is not None:
            raise ConfigEditError(
                f"{assignment_name} in config.py has a key that isn't a plain "
                "string, so the dashboard can't tell whether it sets "
                f"{name!r}. Edit {name!r} in config.py instead."
            )
        unpacked = _unpacked_dict_keys(dictionary.values[position])
        if unpacked is None:
            raise ConfigEditError(
                f"{assignment_name} in config.py unpacks a mapping whose keys "
                "cannot be read, so the dashboard cannot edit it reliably. "
                f"Edit {name!r} in config.py instead."
            )
        if name in unpacked:
            if index is None:
                raise ConfigEditError(
                    f"{assignment_name} in config.py sets {name!r} by "
                    "unpacking a mapping, so the dashboard cannot edit it "
                    f"reliably. Edit {name!r} in config.py instead."
                )
            raise ConfigEditError(
                f"{assignment_name} in config.py unpacks a mapping after "
                f"{name!r} that would override it. Edit {name!r} in "
                "config.py instead."
            )


def _require_call_keywords_editable(call, names, label):
    """Refuse keyword edits that * or ** arguments of the call may conflict with.

    A keyword also passed through ``**`` makes the call raise TypeError when
    config.py runs, and arguments the dashboard can't read may supply any
    keyword, so it can't show or change them reliably.
    """
    if any(isinstance(argument, ast.Starred) for argument in call.args):
        raise ConfigEditError(
            f"{label} in config.py uses * arguments, so the dashboard cannot "
            "edit it reliably. Edit it in config.py instead."
        )
    for keyword in call.keywords:
        if keyword.arg is not None:
            continue
        unpacked = _unpacked_dict_keys(keyword.value)
        conflicts = sorted(names if unpacked is None else set(names) & unpacked)
        if conflicts:
            raise ConfigEditError(
                f"{label} in config.py passes ** arguments that may set "
                f"{', '.join(map(repr, conflicts))}, so the dashboard cannot "
                "edit it reliably. Edit it in config.py instead."
            )


def _require_logger_settings_editable(config_path, names=()):
    """Refuse a LoggerSettings edit the miner wouldn't see as saved.

    MINER_CONFIG's logger_settings entry must be the one the miner uses, and
    the LoggerSettings(...) call must not take names through * or **.
    """
    dictionary = _assignment(
        ast.parse(_read_config_source(config_path)), "MINER_CONFIG"
    )
    if not isinstance(dictionary, ast.Dict):
        raise ConfigEditError("MINER_CONFIG must be a literal dictionary.")
    _require_dict_edit_allowed(dictionary, "MINER_CONFIG", "logger_settings")
    logger_settings = _dict_item(dictionary, "logger_settings")
    if isinstance(logger_settings, ast.Call) and not _is_constructor_call(
        logger_settings, "LoggerSettings"
    ):
        # A helper such as make_logger() doesn't take LoggerSettings'
        # arguments, so adding one would raise TypeError when config.py runs.
        raise ConfigEditError(
            "MINER_CONFIG['logger_settings'] in config.py isn't written as "
            "LoggerSettings(...), so the dashboard can't change it. Edit it "
            "in config.py instead."
        )
    if isinstance(logger_settings, ast.Call) and names:
        _require_call_keywords_editable(
            logger_settings, names, "MINER_CONFIG['logger_settings']"
        )


def _set_dict_items(config_path, assignment_name, values):
    if not values:
        return
    # Check every value before writing any of them, so a refusal leaves
    # config.py exactly as it was.
    dictionary = _assignment(
        ast.parse(_read_config_source(config_path)), assignment_name
    )
    if not isinstance(dictionary, ast.Dict):
        raise ConfigEditError(f"{assignment_name} must be a literal dictionary.")
    for name in values:
        _require_dict_edit_allowed(dictionary, assignment_name, name)
    for name, rendered in values.items():
        source = _read_config_source(config_path)
        dictionary = _assignment(ast.parse(source), assignment_name)
        existing = {
            key.value: index
            for index, key in enumerate(dictionary.keys)
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        if name in existing:
            _replace_config_node(
                config_path, dictionary.values[existing[name]], rendered
            )
        else:
            _insert_config_item(config_path, dictionary, f"{name!r}: {rendered}")


def _byte_offset(encoded_lines, lineno, col_offset):
    # ast columns are UTF-8 byte offsets, and bytes.splitlines() breaks lines
    # exactly where the tokenizer does (unlike str.splitlines()).
    return sum(len(line) for line in encoded_lines[: lineno - 1]) + col_offset


def _container_children(container):
    if isinstance(container, ast.Dict):
        return [node for node in container.values if node is not None]
    if isinstance(container, ast.Call):
        return [*container.args, *container.keywords]
    return list(getattr(container, "elts", []))


def _scan_item_tail(segment):
    """Scan the bytes between a container's last element and its closer.

    Only redundant closing parentheses of that element (ast positions exclude
    them), a separating comma, whitespace and comments can appear there.
    Returns (offset just past the element's own parentheses, has_comma).
    """
    item_end = 0
    has_comma = False
    index = 0
    while index < len(segment):
        char = segment[index : index + 1]
        if char == b"#":
            newline = segment.find(b"\n", index)
            index = len(segment) if newline == -1 else newline
            continue
        if char == b",":
            has_comma = True
        elif char == b")" and not has_comma:
            item_end = index + 1
        index += 1
    return item_end, has_comma


def _insert_config_item(config_path, container, rendered):
    path = Path(config_path)
    # Read bytes, so the inserted line uses the file's own line endings.
    encoded = _read_config_bytes(path)
    updated = _insert_item_bytes(encoded, container, rendered)
    _require_parses(updated)
    _store_config_bytes(path, updated)


def _require_parses(encoded):
    try:
        # compile, not ast.parse: only the compiler rejects some errors, such
        # as a __future__ import that no longer comes first.
        compile(encoded, "config.py", "exec", dont_inherit=True)
    except (SyntaxError, ValueError) as error:
        raise ConfigEditError(
            f"Refusing to save a configuration that would not parse: {error}"
        ) from error


def _insert_item_bytes(encoded, container, rendered, inline=False):
    """Return encoded with rendered appended to container's items.

    The item goes on its own line. With inline=True, a container written on
    a single line keeps that layout and gets the item inline instead.
    """
    encoded_lines = encoded.splitlines(keepends=True)
    closing = (
        _byte_offset(encoded_lines, container.end_lineno, container.end_col_offset) - 1
    )
    children = _container_children(container)
    comma_at = None
    if children:
        last = max(children, key=lambda node: (node.end_lineno, node.end_col_offset))
        last_end = _byte_offset(encoded_lines, last.end_lineno, last.end_col_offset)
        item_end, has_comma = _scan_item_tail(encoded[last_end:closing])
        if not has_comma:
            comma_at = last_end + item_end
    if inline and container.lineno == container.end_lineno:
        separator = b"" if not children else b" " if comma_at is None else b", "
        if comma_at is not None:
            # Insert right after the last item (and its own parentheses).
            closing = comma_at
        return (
            encoded[:closing] + separator + rendered.encode("utf-8") + encoded[closing:]
        )
    # bytes.splitlines() breaks only on CR and LF, so the line number lines up
    # with ast's even when the file has form feeds or U+2028 in it.
    container_line = encoded_lines[container.lineno - 1].decode("utf-8")
    base_indent = container_line[: len(container_line) - len(container_line.lstrip())]
    indentation = base_indent + "    "
    newline = _newline(encoded)
    line_start = len(encoded[:closing].rstrip(b" \t"))
    if encoded[:line_start].endswith((b"\n", b"\r")):
        # The closing bracket has its own line: add the item above it.
        closing = line_start
        insertion = f"{indentation}{rendered},{newline}".encode("utf-8")
    else:
        insertion = f"{newline}{indentation}{rendered},{newline}{base_indent}".encode(
            "utf-8"
        )
    updated = encoded[:closing] + insertion + encoded[closing:]
    if comma_at is not None:
        updated = updated[:comma_at] + b"," + updated[comma_at:]
    return updated


def _newline(encoded):
    """The line break config.py uses, so edits don't mix CRLF and LF."""
    return "\r\n" if b"\r\n" in encoded else "\n"


def _call_keyword(call, name):
    return next((item for item in call.keywords if item.arg == name), None)


def _set_call_keywords(call, values):
    for name, rendered in values.items():
        keyword = _call_keyword(call, name)
        if rendered is None:
            if keyword is not None:
                call.keywords.remove(keyword)
            continue
        value = _expression(rendered)
        if keyword is None:
            call.keywords.append(ast.keyword(arg=name, value=value))
        else:
            keyword.value = value


def _logger_settings_positional_fields():
    """LoggerSettings' parameters in the order it takes them positionally."""
    import inspect

    from TwitchChannelPointsMiner.logger import LoggerSettings

    return tuple(inspect.signature(LoggerSettings.__init__).parameters)[1:]


def _write_call_keywords(
    config_path, find_call, values, label="A constructor call", positional=()
):
    """Set keyword arguments of a call in config.py.

    positional names the call's parameters in order. A value already passed
    positionally is replaced where it is: adding it as a keyword too would
    make the call raise TypeError when config.py runs.
    """
    if not values:
        return
    call = find_call(ast.parse(_read_config_source(config_path)))
    if not isinstance(call, ast.Call):
        raise ConfigEditError("Expected a constructor call in config.py.")
    _require_call_keywords_editable(call, set(values), label)
    for name, rendered in values.items():
        source = _read_config_source(config_path)
        call = find_call(ast.parse(source))
        keyword = _call_keyword(call, name)
        given_positionally = tuple(positional)[: len(call.args)]
        if keyword is not None:
            _replace_config_node(config_path, keyword.value, rendered)
        elif name in given_positionally:
            _replace_config_node(
                config_path, call.args[given_positionally.index(name)], rendered
            )
        else:
            _insert_config_item(config_path, call, f"{name}={rendered}")


def _write_streamers(config_path, records, updated_usernames=None, always_write=None):
    if isinstance(updated_usernames, str):
        updated_usernames = {_username_key(updated_usernames)}
    else:
        updated_usernames = {
            _username_key(username) for username in updated_usernames or ()
        }
    # Write only the settings that differ from what the streamer has now, so
    # values it inherits from the global defaults stay inherited.
    path = Path(config_path)
    # Read bytes, so CRLF line breaks are kept.
    original = _read_config_bytes(path)
    # The miner uses only one entry for a duplicated username, so edits to
    # it can't be applied reliably.
    keys = [
        key
        for key in map(_streamer_key, _streamers_list(original).elts)
        if key is not None
    ]
    duplicate = next((key for key in keys if keys.count(key) > 1), None)
    if duplicate is not None:
        raise ConfigEditError(
            f"{duplicate} is listed more than once in STREAMERS in config.py. "
            "Remove the extra entry in config.py, then try again."
        )
    names = [_username_key(record["username"]) for record in records]
    duplicate = next((name for name in names if names.count(name) > 1), None)
    if duplicate is not None:
        raise ConfigEditError(f"{duplicate} can be listed only once.")
    base = _base_web_config(config_path)
    # With a duplicated username, the first entry is the one that is edited.
    current = {}
    for item in base["streamers"]:
        current.setdefault(_username_key(item["username"]), item["settings"])
    # always_write maps a username to settings whose current value can't be
    # read, so they're written even when they look unchanged.
    always_write = always_write or {}
    settings_changes = {}
    seen = set()
    for record in records:
        key = _username_key(record["username"])
        if key not in updated_usernames or key in seen:
            continue
        seen.add(key)
        effective = current.get(key, base["streamer_defaults"])
        forced = always_write.get(key, ())
        changed = {
            name: value
            for name, value in (record.get("settings") or {}).items()
            if effective.get(name) != value or name in forced
        }
        if changed:
            settings_changes[key] = changed
    encoded = original
    wanted = [record["username"] for record in records]
    wanted_keys = [_username_key(username) for username in wanted]

    # Edit STREAMERS in place instead of regenerating it, so comments,
    # formatting and entries the dashboard doesn't understand survive.
    # Remove entries that are no longer wanted.
    while True:
        streamers = _streamers_list(encoded)
        index = next(
            (
                index
                for index, node in enumerate(streamers.elts)
                if _streamer_key(node) is not None
                and _streamer_key(node) not in wanted_keys
            ),
            None,
        )
        if index is None:
            break
        removed_key = _streamer_key(streamers.elts[index])
        # Only comments on the removed entry's own lines may go.
        try:
            updated, (first, last) = _remove_list_item(encoded, streamers, index)
            ast.parse(updated)
            kept = [
                text for line, text in _comments(encoded) if not first <= line <= last
            ]
            remaining = [text for _line, text in _comments(updated)]
            for text in kept:
                remaining.remove(text)
        except (SyntaxError, ValueError, tokenize.TokenError):
            # e.g. a comma that starts the next entry's line.
            raise ConfigEditError(
                f"{removed_key} can't be removed from the dashboard because of "
                "how its entry is written in config.py. Remove it in config.py "
                "instead."
            ) from None
        encoded = updated

    # Append new entries as plain usernames.
    present = {_streamer_key(node) for node in _streamers_list(encoded).elts}
    for username in wanted:
        if _username_key(username) not in present:
            # STREAMER_RE limits usernames to [A-Za-z0-9_], so this is a
            # valid literal in the double quotes Black and the example use.
            encoded = _insert_item_bytes(
                encoded, _streamers_list(encoded), f'"{username}"', inline=True
            )
            present.add(_username_key(username))

    # Reorder by moving each entry's source text into its new slot.
    streamers = _streamers_list(encoded)
    slots = [
        index
        for index, node in enumerate(streamers.elts)
        if _streamer_key(node) is not None
    ]
    # Duplicate usernames each keep their own entry: the first wanted
    # occurrence takes the first remaining slot with that name.
    slots_by_key = {}
    for index in slots:
        slots_by_key.setdefault(_streamer_key(streamers.elts[index]), []).append(index)
    desired = [slots_by_key[key].pop(0) for key in wanted_keys if slots_by_key.get(key)]
    if len(desired) == len(slots) and desired != slots:
        order = list(range(len(streamers.elts)))
        for slot, source_index in zip(slots, desired):
            order[slot] = source_index
        encoded = _reorder_list_items(encoded, streamers, order)

    for key, changed in settings_changes.items():
        encoded = _set_streamer_settings(encoded, key, changed)

    # Imports go in last: every edit above can still refuse, and a refused
    # edit must leave config.py untouched.
    if settings_changes:
        if any("chat" in changed for changed in settings_changes.values()):
            encoded = _with_config_import(
                encoded,
                "from TwitchChannelPointsMiner.classes.Chat import ChatPresence",
            )
        encoded = _with_config_import(
            encoded,
            "from TwitchChannelPointsMiner.classes.entities.Streamer import "
            "Streamer, StreamerSettings",
        )

    if encoded != original:
        _require_parses(encoded)
        _store_config_bytes(path, encoded)


def _username_key(username):
    """How Streamer() and the miner compare usernames."""
    return username.strip().lower()


def _streamer_key(node):
    username = _streamer_value(node)
    return _username_key(username) if username is not None else None


def _streamers_list(encoded):
    return _config_lists(encoded.decode("utf-8"))[1]


def _node_span(encoded_lines, node):
    return (
        _byte_offset(encoded_lines, node.lineno, node.col_offset),
        _byte_offset(encoded_lines, node.end_lineno, node.end_col_offset),
    )


_ITEM_LINE_TAIL_RE = re.compile(rb",?")


def _next_comma(encoded, position):
    """The offset of the first comma at or after position outside comments.

    Between an entry and its separating comma there can only be
    parentheses, whitespace and comments, so no string can hide a comma.
    """
    while True:
        char = encoded[position : position + 1]
        if char == b",":
            return position
        if char == b"#":
            position += len(_line_tail(encoded, position))
            continue
        if not char:
            raise ConfigEditError("Expected a comma after the entry in config.py.")
        position += 1


def _redundant_paren_start(encoded, boundary, start, parens):
    """Move start back over an entry's redundant opening parentheses, e.g.
    the "(" of ("name"); ast positions exclude them.

    boundary is where the previous entry, or the list's opening bracket, ends.
    Only whitespace, a comma, comments and these parentheses lie between it
    and start.
    """
    if not parens:
        return start
    openings = []
    index = boundary
    while index < start:
        char = encoded[index : index + 1]
        if char == b"#":
            newline = encoded.find(b"\n", index, start)
            index = start if newline == -1 else newline
            continue
        if char == b"(":
            openings.append(index)
        index += 1
    return openings[-parens] if len(openings) >= parens else start


def _own_lines_span(encoded, start, end, closing, is_last, has_comma):
    """The span to delete for an entry that has its lines to itself.

    That means only indentation before the entry, and after it only its comma,
    whitespace and its own comment. Returns those whole lines, so comments on
    other lines (another entry's own-line comment, or the previous entry's
    trailing comment) are kept. When the list's closing bracket follows the
    last entry on its line, the entry is cut up to the bracket instead.
    Returns None for other layouts. start and end include the entry's
    redundant parentheses.
    """
    line_start = encoded.rfind(b"\n", 0, start) + 1
    if encoded[line_start:start].strip(b" \t"):
        return None
    newline = encoded.find(b"\n", end)
    line_end = len(encoded) if newline == -1 else newline + 1
    if closing < line_end:
        if not is_last:
            return None
        cut_end = _next_comma(encoded, end) + 1 if has_comma else end
        return line_start, cut_end
    tail = encoded[end:line_end]
    comment = tail.find(b"#")
    code = tail if comment == -1 else tail[:comment]
    if not _ITEM_LINE_TAIL_RE.fullmatch(re.sub(rb"\s", b"", code)):
        return None
    return line_start, line_end


def _comments(encoded):
    """(line, text) for every comment in encoded."""
    return [
        (token.start[0], token.string)
        for token in tokenize.tokenize(io.BytesIO(encoded).readline)
        if token.type == tokenize.COMMENT
    ]


def _remove_list_item(encoded, container, index):
    """Return (encoded without the entry, (first, last) line of the entry)."""
    encoded_lines = encoded.splitlines(keepends=True)
    elements = container.elts
    is_last = index + 1 == len(elements)
    start, end = _node_span(encoded_lines, elements[index])
    closing = (
        _byte_offset(encoded_lines, container.end_lineno, container.end_col_offset) - 1
    )
    segment_end = (
        closing if is_last else _node_span(encoded_lines, elements[index + 1])[0]
    )
    item_end, has_comma = _scan_item_tail(encoded[end:segment_end])
    boundary = (
        _node_span(encoded_lines, elements[index - 1])[1]
        if index
        else _byte_offset(encoded_lines, container.lineno, container.col_offset) + 1
    )
    start = _redundant_paren_start(
        encoded,
        boundary,
        start,
        re.sub(rb"#[^\r\n]*", b"", encoded[end : end + item_end]).count(b")"),
    )
    end += item_end
    lines = (encoded.count(b"\n", 0, start) + 1, encoded.count(b"\n", 0, end) + 1)

    own_lines = _own_lines_span(encoded, start, end, closing, is_last, has_comma)
    if own_lines is not None:
        return encoded[: own_lines[0]] + encoded[own_lines[1] :], lines
    if not is_last:
        # The next entry follows on the same line, or this one shares its
        # line with the opening bracket: drop the entry, its comma and the
        # spaces after it, but not the line break or anything after it.
        shares_line = False
        if index > 0:
            # Include the previous entry's redundant closing parentheses.
            previous_end = _node_span(encoded_lines, elements[index - 1])[1]
            previous_item_end, _ = _scan_item_tail(encoded[previous_end:start])
            shares_line = b"\n" not in encoded[previous_end + previous_item_end : start]
        comma = _next_comma(encoded, end)
        if shares_line and b"#" in encoded[end:comma]:
            # A comment of the line shared with another entry comes before
            # this entry's comma, so cutting through the comma would drop it.
            raise ValueError("shared comment before the comma")
        end = comma + 1
        while encoded[end : end + 1] in (b" ", b"\t"):
            end += 1
        if encoded[end : end + 1] == b"#" and not shares_line:
            # This entry's own trailing comment. One on a line it shares with
            # another entry belongs to that line, so it stays.
            end += len(_line_tail(encoded, end))
    elif index > 0:
        # The last entry shares its line with the previous one: drop it from
        # the end of the previous entry, so that entry's comma state is kept.
        previous_end = _node_span(encoded_lines, elements[index - 1])[1]
        previous_item_end, _ = _scan_item_tail(encoded[previous_end:start])
        cut = previous_end + previous_item_end
        if b"#" in encoded[cut:start]:
            # The previous entry's comment sits between it and this entry,
            # e.g. a comma that starts this entry's line: cut from the
            # comma, or from this entry's line when the comment follows it.
            comma = _next_comma(encoded, cut)
            if b"#" in encoded[comma:start]:
                cut = encoded.rfind(b"\n", 0, start) + 1
            else:
                cut = comma
                line_start = encoded.rfind(b"\n", 0, comma) + 1
                rest = _line_tail(encoded, end)
                if not encoded[line_start:comma].strip() and not rest.strip():
                    # The comma's line held only this entry: drop the line.
                    cut = line_start
                    end += len(rest)
                    end += 2 if encoded[end : end + 2] == b"\r\n" else 1
        start = cut
    elif has_comma:
        end = _next_comma(encoded, end) + 1
    return encoded[:start] + encoded[end:], lines


def _line_tail(encoded, end):
    """The rest of the line after end, without its line break."""
    line_end = len(encoded)
    for newline in (b"\n", b"\r"):
        position = encoded.find(newline, end)
        if position != -1:
            line_end = min(line_end, position)
    return encoded[end:line_end]


def _reorder_list_items(encoded, container, order):
    encoded_lines = encoded.splitlines(keepends=True)
    elements = container.elts
    spans = [_node_span(encoded_lines, node) for node in elements]
    texts = [encoded[start:end] for start, end in spans]
    # When every entry ends its own line, a comment after an entry on that
    # line describes the entry, so it moves with it. Between an entry and
    # the line end there can only be parentheses, a comma, spaces and a
    # comment, so the first "#" starts the comment.
    own_lines = all(
        index + 1 == len(elements)
        or elements[index + 1].lineno > elements[index].end_lineno
        for index in range(len(elements))
    )
    closing = (
        _byte_offset(encoded_lines, container.end_lineno, container.end_col_offset) - 1
    )
    # Stop at the list's closing bracket: code after it on the same line
    # isn't part of any entry.
    tails = [_line_tail(encoded, end)[: max(closing - end, 0)] for _start, end in spans]
    # Each comment keeps the spacing in front of it, so aligned comments
    # stay aligned.
    comments = [
        tail[len(tail[: tail.index(b"#")].rstrip()) :].rstrip() if b"#" in tail else b""
        for tail in tails
    ]
    for slot in reversed(range(len(spans))):
        start, end = spans[slot]
        source = order[slot]
        if source == slot:
            # An entry that stays put keeps its line byte for byte.
            continue
        if own_lines:
            tail = tails[slot]
            separator = tail[: tail.index(b"#")].rstrip() if b"#" in tail else tail
            comment = comments[source]
            replacement = texts[source] + separator.rstrip() + comment
            end += len(tail)
            if comment and end == closing:
                # The closing bracket follows on this line: move it to its
                # own line, so the comment doesn't swallow it.
                line = encoded_lines[container.lineno - 1].decode("utf-8")
                indent = line[: len(line) - len(line.lstrip())]
                replacement += f"{_newline(encoded)}{indent}".encode("utf-8")
        else:
            replacement = texts[source]
        encoded = encoded[:start] + replacement + encoded[end:]
    return encoded


def _has_unpacking(call):
    return any(isinstance(arg, ast.Starred) for arg in call.args) or any(
        keyword.arg is None for keyword in call.keywords
    )


def _settings_argument(node):
    """The settings argument of a Streamer(...) entry, or None without one.

    Raises ConfigEditError when the dashboard can't edit it safely. A literal
    None counts as no settings.
    """
    if _has_unpacking(node):
        # Streamer("x", **OPTIONS) may already carry settings; adding a
        # settings keyword could repeat it.
        raise ConfigEditError(
            f"The entry for {_streamer_value(node)} in config.py uses * or ** "
            "arguments, so the dashboard can't change its settings safely. "
            "Edit them in config.py instead."
        )
    settings_keyword = _call_keyword(node, "settings")
    if settings_keyword is not None:
        settings = settings_keyword.value
    else:
        # Streamer(username, settings) also takes settings positionally.
        settings = node.args[1] if len(node.args) > 1 else None
    if settings is None or (
        isinstance(settings, ast.Constant) and settings.value is None
    ):
        return settings
    if not (
        isinstance(settings, ast.Call)
        and isinstance(settings.func, ast.Name)
        and settings.func.id == "StreamerSettings"
        and not settings.args
        and not _has_unpacking(settings)
    ):
        raise ConfigEditError(
            f"The settings for {_streamer_value(node)} in config.py aren't "
            "written as StreamerSettings(...), so the dashboard can't change "
            "them without losing settings. Edit them in config.py instead."
        )
    return settings


def _readable_setting(value):
    """Whether the dashboard shows this StreamerSettings value as written."""
    if isinstance(value, ast.Constant):
        return True
    return (
        isinstance(value, ast.Attribute)
        and isinstance(value.value, ast.Name)
        and value.value.id == "ChatPresence"
    )


_DYNAMIC_NAMES = {"globals", "vars", "locals", "exec", "eval", "setattr", "delattr"}


def _miner_config_readable(tree):
    """Whether MINER_CONFIG is exactly its literal dict when the miner runs.

    That holds when it's bound once, to a dict literal with plain string keys
    and at most one streamer_settings, and the rest of the file only reads
    other keys from it (MINER_CONFIG["username"]). Anything else, such as a
    later change, an alias, or globals() and exec, may change it in ways the
    dashboard can't follow.
    """
    miner = _assignment(tree, "MINER_CONFIG")
    if not isinstance(miner, ast.Dict) or not all(
        isinstance(key, ast.Constant) and isinstance(key.value, str)
        for key in miner.keys
    ):
        return False
    if [key.value for key in miner.keys].count("streamer_settings") > 1:
        return False
    # MINER_CONFIG = ALIAS = {...} or a walrus inside the dict gives another
    # name to the dict or its defaults.
    statement = next(
        node
        for node in tree.body
        if isinstance(node, ast.Assign) and node.value is miner
    )
    if len(statement.targets) != 1 or any(
        isinstance(node, ast.NamedExpr) for node in ast.walk(miner)
    ):
        return False
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    bindings = 0
    for node in ast.walk(tree):
        if (
            (isinstance(node, ast.Name) and node.id in _DYNAMIC_NAMES)
            or (isinstance(node, ast.Attribute) and node.attr in _DYNAMIC_NAMES)
            or (isinstance(node, ast.alias) and node.name in _DYNAMIC_NAMES)
        ):
            # globals(), builtins.exec(...), from builtins import exec as run
            return False
        if isinstance(node, ast.alias) and node.name == "*":
            # A star import can bind any name, MINER_CONFIG included.
            return False
        if not isinstance(node, (ast.Name, ast.Constant)) and any(
            value == "MINER_CONFIG"
            or (isinstance(value, list) and "MINER_CONFIG" in value)
            for _field, value in ast.iter_fields(node)
        ):
            # Any other node naming MINER_CONFIG binds or declares it: an
            # import alias, def, class, argument, except or match capture
            # (including {**MINER_CONFIG}), global or nonlocal.
            return False
        if not (isinstance(node, ast.Name) and node.id == "MINER_CONFIG"):
            continue
        if not isinstance(node.ctx, ast.Load):
            bindings += 1
            continue
        parent = parents.get(node)
        if (
            isinstance(parent, ast.Subscript)
            and parent.value is node
            and isinstance(parent.ctx, ast.Load)
        ):
            key = parent.slice
        elif (
            isinstance(parent, ast.Attribute)
            and parent.attr == "get"
            and isinstance(parents.get(parent), ast.Call)
            and parents[parent].func is parent
            and parents[parent].args
        ):
            # MINER_CONFIG.get("username")
            key = parents[parent].args[0]
        else:
            return False
        if not (
            isinstance(key, ast.Constant)
            and isinstance(key.value, str)
            and key.value != "streamer_settings"
        ):
            return False
    return bindings == 1


def _inherited_setting_unknown(tree, name):
    """Whether the global default for a streamer setting can't be read.

    The dashboard then shows the built-in default, which may be wrong.
    """
    if not _miner_config_readable(tree):
        return True
    defaults = _dict_item(_assignment(tree, "MINER_CONFIG"), "streamer_settings")
    if defaults is None or (
        isinstance(defaults, ast.Constant) and defaults.value is None
    ):
        return False
    if isinstance(defaults, ast.Dict):
        if None in defaults.keys:
            return True
        value = _dict_item(defaults, name)
        return value is not None and not _readable_setting(value)
    if not (
        isinstance(defaults, ast.Call)
        and isinstance(defaults.func, ast.Name)
        and defaults.func.id in ("StreamerSettings", "dict")
        and not defaults.args
        and not _has_unpacking(defaults)
    ):
        return True
    keyword = _call_keyword(defaults, name)
    return keyword is not None and not _readable_setting(keyword.value)


def _require_editable_settings(config_path, username, names):
    """Refuse a settings update the dashboard can't apply faithfully.

    The dashboard shows defaults for settings it can't read, so a submitted
    value that looks unchanged may still differ from config.py. names are
    the settings the dashboard submitted. Returns the names whose inherited
    value can't be read, which must be written even when they look
    unchanged.
    """
    if not names:
        return set()
    encoded = _read_config_bytes(config_path)
    node = next(
        (
            node
            for node in _streamers_list(encoded).elts
            if _streamer_key(node) == username
        ),
        None,
    )
    tree = ast.parse(encoded)
    if node is None:
        # A streamer the edit adds inherits every setting.
        return {name for name in names if _inherited_setting_unknown(tree, name)}
    settings = _settings_argument(node) if isinstance(node, ast.Call) else None
    explicit = set()
    if isinstance(settings, ast.Call):
        for name in names:
            keyword = _call_keyword(settings, name)
            if keyword is None:
                continue
            if not _readable_setting(keyword.value):
                raise ConfigEditError(
                    f"{name} for {_streamer_value(node)} in config.py isn't a "
                    "plain value, so the dashboard can't change it safely. "
                    "Edit it in config.py instead."
                )
            if not (
                isinstance(keyword.value, ast.Constant) and keyword.value.value is None
            ):
                explicit.add(name)
    return {
        name
        for name in names
        if name not in explicit and _inherited_setting_unknown(tree, name)
    }


def _set_streamer_settings(encoded, key, changed):
    for name, value in changed.items():
        rendered = f"ChatPresence.{value}" if name == "chat" else repr(value)
        streamers = _streamers_list(encoded)
        node = next(node for node in streamers.elts if _streamer_key(node) == key)
        encoded_lines = encoded.splitlines(keepends=True)
        if isinstance(node, ast.Constant):
            start, end = _node_span(encoded_lines, node)
            # Keep the username literal exactly as written.
            replacement = (
                b"Streamer("
                + encoded[start:end]
                + f", settings=StreamerSettings({name}={rendered}))".encode("utf-8")
            )
            encoded = encoded[:start] + replacement + encoded[end:]
            continue
        settings = _settings_argument(node)
        if isinstance(settings, ast.Constant):
            # settings=None: replace the None, keyword or positional alike.
            start, end = _node_span(encoded_lines, settings)
            encoded = (
                encoded[:start]
                + f"StreamerSettings({name}={rendered})".encode("utf-8")
                + encoded[end:]
            )
            continue
        if settings is None:
            encoded = _insert_item_bytes(
                encoded,
                node,
                f"settings=StreamerSettings({name}={rendered})",
                inline=True,
            )
            continue
        keyword = _call_keyword(settings, name)
        if keyword is not None:
            start, end = _node_span(encoded_lines, keyword.value)
            encoded = encoded[:start] + rendered.encode("utf-8") + encoded[end:]
        else:
            encoded = _insert_item_bytes(
                encoded, settings, f"{name}={rendered}", inline=True
            )
    return encoded


@_holding_config_mutex
def enable_analytics_dashboard(
    config_path,
    password,
    host="127.0.0.1",
    port=54455,
    refresh=5,
    days_ago=7,
    log_poll_interval=5,
):
    """Turn on the analytics dashboard for an *existing* configuration.

    Used for an explicit, in-the-moment user action - the Windows desktop
    shell's one-time "enable the dashboard?" prompt, or its on-demand
    "Enable dashboard" button, both in windows_launcher.py - rather than the
    silent first-run bootstrap in that module's
    ensure_windows_analytics_defaults(), which only ever touches a config it
    just created from the template. Because this path is a deliberate user
    choice, it goes through the same source-preserving AST edit machinery as
    every other dashboard-driven config change, keeping the rest of the
    user's file - formatting, comments, unrelated settings - untouched.

    Leaves ANALYTICS_CONFIG alone if it is already a literal dict (the user
    has existing analytics settings, just switched off - respect them
    rather than overwriting host/port/password with fresh defaults); only
    replaces it when it is still unset (e.g. `ANALYTICS_CONFIG = None`).
    """
    with _config_edit(config_path):
        source = _read_config_source(config_path)
        analytics_config_node = _assignment(ast.parse(source), "ANALYTICS_CONFIG")
        if analytics_config_node is None:
            raise ConfigEditError("ANALYTICS_CONFIG assignment not found in config.py.")
        if not isinstance(analytics_config_node, ast.Dict) and not (
            isinstance(analytics_config_node, ast.Constant)
            and analytics_config_node.value is None
        ):
            # Only an unset value gets the defaults: anything else, such as
            # dict(port=...), holds settings the defaults would replace.
            raise ConfigEditError(
                "ANALYTICS_CONFIG in config.py isn't a literal dictionary or "
                "None, so the dashboard can't turn it on without replacing "
                "its settings. Turn it on in config.py instead."
            )
        _set_dict_items(config_path, "MINER_CONFIG", {"enable_analytics": "True"})
        if isinstance(analytics_config_node, ast.Dict):
            return

        # The MINER_CONFIG edit may have shifted the assignment's source offsets.
        source = _read_config_source(config_path)
        analytics_config_node = _assignment(ast.parse(source), "ANALYTICS_CONFIG")

        rendered = (
            "{\n"
            f"    'host': {host!r},\n"
            f"    'port': {port},\n"
            f"    'refresh': {refresh},\n"
            f"    'days_ago': {days_ago},\n"
            f"    'password': {password!r},\n"
            f"    'log_poll_interval': {log_poll_interval},\n"
            "}"
        ).replace("\n", _newline(_read_config_bytes(config_path)))
        _replace_config_node(config_path, analytics_config_node, rendered)


@_holding_config_mutex
def set_miner_username(config_path, username):
    """Set MINER_CONFIG['username'] - the Twitch account the miner logs in
    as - on an existing configuration.

    Used by the Windows desktop shell's first-run setup prompt (see
    windows_launcher.py), shown when a config's username is still the
    bundled template's placeholder. Twitch account usernames follow the
    same character rules as a streamer username, so STREAMER_RE is reused
    for validation. Writes through the normal AST edit path, preserving
    the rest of the file untouched.
    """
    username = username.strip()
    if not STREAMER_RE.fullmatch(username):
        raise ConfigEditError("Enter a valid Twitch username.")
    _set_dict_items(config_path, "MINER_CONFIG", {"username": repr(username)})
    return username


def _require_readable_source_priority(node):
    """Refuse a partial source update that would start from a guessed order.

    The update keeps the sources it isn't given in their current order, so
    that order must be readable: no entry (or None, which the miner also
    replaces with the default order), or a literal list of StreamerSource
    members. Anything else, such as a variable, would be read as missing and
    replaced with the default order.
    """
    if node is None or (isinstance(node, ast.Constant) and node.value is None):
        return
    if isinstance(node, (ast.List, ast.Tuple)) and all(
        isinstance(item, ast.Attribute)
        and isinstance(item.value, ast.Name)
        and item.value.id == "StreamerSource"
        for item in node.elts
    ):
        return
    raise ConfigEditError(
        "MINER_CONFIG['streamer_source_priority'] in config.py isn't a plain "
        "list of StreamerSource values, so the dashboard can't change the "
        "stream sources without changing their order. Edit it in config.py "
        "instead."
    )


def _write_logger_settings(config_path, values):
    rendered = {
        name: (
            f"logging.{value}"
            if name in {"console_level", "file_level"}
            else repr(value)
        )
        for name, value in values.items()
    }
    _require_logger_settings_editable(config_path)
    if {"console_level", "file_level"} & set(values):
        # The levels are written as logging.<LEVEL>.
        _ensure_config_import(config_path, "import logging")
    _write_call_keywords(
        config_path,
        lambda tree: _dict_item(_assignment(tree, "MINER_CONFIG"), "logger_settings"),
        rendered,
        "MINER_CONFIG['logger_settings']",
        _logger_settings_positional_fields(),
    )


def _notification_expression(provider, values, existing):
    constructors = {
        "telegram": "Telegram",
        "discord": "Discord",
        "webhook": "Webhook",
        "email": "Email",
        "matrix": "Matrix",
        "pushover": "Pushover",
        "gotify": "Gotify",
        "ntfy": "Ntfy",
    }
    call = (
        existing
        if isinstance(existing, ast.Call)
        else _expression(f"{constructors[provider]}()")
    )
    rendered = {}
    for name, value in values.items():
        if name == "enabled" or value == "":
            continue
        if name == "events":
            rendered[name] = repr(_runtime_notification_events(value))
        else:
            rendered[name] = repr(value)
    _set_call_keywords(call, rendered)
    return call


def _call_argument(call, name, positional=()):
    """The argument a call passes for name, by keyword or position, or None."""
    keyword = _call_keyword(call, name)
    if keyword is not None:
        return keyword.value
    given = tuple(positional)[: len(call.args)]
    if name in given:
        return call.args[given.index(name)]
    return None


def _call_may_supply(call, name):
    """Whether * or ** arguments of the call may pass name."""
    if any(isinstance(argument, ast.Starred) for argument in call.args):
        return True
    for keyword in call.keywords:
        if keyword.arg is None:
            unpacked = _unpacked_dict_keys(keyword.value)
            if unpacked is None or name in unpacked:
                return True
    return False


def _logger_settings_argument(tree, name):
    logger_settings = _dict_item(_assignment(tree, "MINER_CONFIG"), "logger_settings")
    if not isinstance(logger_settings, ast.Call):
        return None
    return _call_argument(logger_settings, name, _logger_settings_positional_fields())


def _is_constructor_call(node, constructor):
    if not isinstance(node, ast.Call):
        return False
    function = node.func
    return (isinstance(function, ast.Name) and function.id == constructor) or (
        isinstance(function, ast.Attribute) and function.attr == constructor
    )


def _require_consistent_email_tls(existing, values):
    """Refuse an Email save that would enable both SSL and STARTTLS.

    Email() raises ValueError for that, which would stop config.py loading.
    A setting the save doesn't change keeps its value in config.py, or the
    constructor default when it isn't given.
    """
    if existing is not None and not {"use_ssl", "starttls"} & set(values):
        return
    final = {}
    for name, default in (("use_ssl", False), ("starttls", True)):
        if name in values:
            final[name] = values[name]
            continue
        node = (
            None
            if existing is None
            else _call_argument(existing, name, NOTIFICATION_POSITIONAL_FIELDS["email"])
        )
        if node is None and existing is not None and _call_may_supply(existing, name):
            node = existing
        if node is None:
            final[name] = default
        elif isinstance(node, ast.Constant):
            final[name] = node.value
        else:
            raise ConfigEditError(
                f"The email notification's {name} in config.py isn't a plain "
                "value, so the dashboard can't tell whether SSL and STARTTLS "
                "would both be on. Edit it in config.py instead."
            )
    if final["use_ssl"] and final["starttls"]:
        raise ConfigEditError("Email SSL and STARTTLS cannot both be enabled.")


def _write_notification(config_path, provider, values):
    # Check before reading the notifier: a LoggerSettings entry or argument
    # the miner doesn't use as written would make the read below, and the
    # decision that there's nothing to change, wrong.
    _require_logger_settings_editable(config_path, {provider})
    tree = ast.parse(_read_config_source(config_path))
    logger_settings = _dict_item(_assignment(tree, "MINER_CONFIG"), "logger_settings")
    if not isinstance(logger_settings, ast.Call):
        raise ConfigEditError(
            "MINER_CONFIG['logger_settings'] must be LoggerSettings(...)."
        )
    existing = _logger_settings_argument(tree, provider)
    if isinstance(existing, ast.Constant) and existing.value is None:
        existing = None
    logger_settings_arguments = {
        "find_call": lambda tree: _dict_item(
            _assignment(tree, "MINER_CONFIG"), "logger_settings"
        ),
        "label": "MINER_CONFIG['logger_settings']",
        "positional": _logger_settings_positional_fields(),
    }
    if values.get("enabled") is False:
        if existing is not None:
            _write_call_keywords(
                config_path, values={provider: "None"}, **logger_settings_arguments
            )
        return
    constructor = provider.title()
    if existing is not None and not _is_constructor_call(existing, constructor):
        # A variable or helper call: the dashboard can't read its settings,
        # and writing over it would lose them.
        raise ConfigEditError(
            f"The {provider} notification in config.py isn't written as "
            f"{constructor}(...), so the dashboard can't change it. Edit it "
            "in config.py instead."
        )
    if provider == "email":
        _require_consistent_email_tls(existing, values)
    if existing is None:
        if values.get("enabled") is not True:
            return
        _ensure_config_import(
            config_path,
            f"from TwitchChannelPointsMiner.classes.{constructor} import {constructor}",
        )
        rendered = ast.unparse(_notification_expression(provider, values, None))
        _write_call_keywords(
            config_path, values={provider: rendered}, **logger_settings_arguments
        )
        return
    rendered_values = {}
    for name, value in values.items():
        if name == "enabled" or value == "":
            continue
        rendered_values[name] = (
            repr(_runtime_notification_events(value))
            if name == "events"
            else repr(value)
        )
    _write_call_keywords(
        config_path,
        lambda tree: _logger_settings_argument(tree, provider),
        rendered_values,
        f"The {provider} notification settings",
        NOTIFICATION_POSITIONAL_FIELDS[provider],
    )


def _imports_binding(tree, statement):
    """Whether config.py's top level already runs this import statement.

    Matched on the parsed import, not the text: the same text with "as"
    after it, or in a comment, doesn't bind the name the edit writes.
    """
    wanted = ast.parse(statement).body[0]
    wanted_names = {(alias.name, alias.asname or alias.name) for alias in wanted.names}
    for node in tree.body:
        if type(node) is not type(wanted):
            continue
        if isinstance(node, ast.ImportFrom) and (
            node.module != wanted.module or node.level != wanted.level
        ):
            continue
        names = {(alias.name, alias.asname or alias.name) for alias in node.names}
        if wanted_names <= names:
            return True
    return False


def _with_config_import(encoded, statement):
    if _imports_binding(ast.parse(encoded), statement):
        return encoded
    # Split bytes, so a U+2028 or form feed in the first line doesn't look
    # like a line break and push the import into the comment.
    lines = encoded.splitlines(keepends=True)
    insertion = 1 if lines and b"coding" in lines[0] else 0
    lines.insert(
        insertion, statement.encode("utf-8") + _newline(encoded).encode("utf-8")
    )
    return b"".join(lines)


def _ensure_config_import(config_path, statement):
    path = Path(config_path)
    # Read bytes, so adding an import doesn't rewrite the file's line endings.
    encoded = _read_config_bytes(path)
    updated = _with_config_import(encoded, statement)
    if updated != encoded:
        _require_parses(updated)
        _store_config_bytes(path, updated)


def _validate_streamer_settings(settings):
    if not isinstance(settings, dict) or set(settings) - STREAMER_SETTING_NAMES:
        raise ConfigEditError("Unsupported per-streamer setting.")
    for name in STREAMER_SETTING_NAMES - {"chat", "points_limit", "streak_only"}:
        if name in settings and not isinstance(settings[name], bool):
            raise ConfigEditError(f"{name} must be true or false.")
    if "streak_only" in settings:
        streak_only = settings["streak_only"]
        if not isinstance(streak_only, bool) and (
            not isinstance(streak_only, int) or streak_only <= 0
        ):
            raise ConfigEditError(
                "streak_only must be true, false, or a positive integer "
                "number of minutes."
            )
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


def _require_reorder_is_permutation(new_order, current_names, item_label):
    if sorted(map(str.lower, new_order)) != sorted(map(str.lower, current_names)):
        raise ConfigEditError(
            f"{item_label} order must contain every configured {item_label.lower()}."
        )


def _runtime_notification_events(values):
    from TwitchChannelPointsMiner.classes.Settings import Events

    if not isinstance(values, (list, tuple)):
        raise ConfigEditError("events must be a list of valid event names.")
    normalized = []
    for value in values:
        if isinstance(value, Events):
            normalized.append(str(value))
            continue
        if not isinstance(value, str):
            raise ConfigEditError("events must be a list of valid event names.")
        name = value.removeprefix("Events.")
        try:
            normalized.append(str(Events[name]))
        except KeyError as error:
            raise ConfigEditError(f"Unknown notification event: {value}.") from error
    return normalized


def update_managed_web_config(config_path, payload):
    with CONFIG_FILE_MUTEX:
        migrate_web_config(config_path)
        # One write for the whole action, so a refused part leaves config.py
        # exactly as it was.
        with _config_edit(config_path):
            return _update_managed_web_config(config_path, payload)


def _update_managed_web_config(config_path, payload):
    if not isinstance(payload, dict):
        raise ConfigEditError("The configuration update must be a JSON object.")
    action = payload.get("action")
    current = read_managed_web_config(config_path)

    if action in {"add", "remove"}:
        kind = payload.get("kind")
        raw_value = payload.get("value")
        value = raw_value.strip() if isinstance(raw_value, str) else ""
        if action == "add":
            valid = (
                STREAMER_RE.fullmatch(value) is not None
                if kind == "streamers"
                else _valid_managed_category(value) if kind == "categories" else False
            )
        else:
            valid = bool(value) and kind in {"streamers", "categories"}
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
        if kind == "categories":
            _write_config_categories(config_path, items)
        else:
            _write_streamers(config_path, items)
    elif action == "reorder_categories":
        categories = payload.get("categories")
        if not isinstance(categories, list) or any(
            not _valid_managed_category(item) for item in categories
        ):
            raise ConfigEditError("Invalid category order.")
        _require_reorder_is_permutation(categories, current["categories"], "Category")
        _write_config_categories(config_path, categories)
    elif action == "reorder_streamers":
        usernames = payload.get("usernames")
        if not isinstance(usernames, list) or any(
            not isinstance(item, str) for item in usernames
        ):
            raise ConfigEditError("Invalid streamer order.")
        current_usernames = [item["username"] for item in current["streamers"]]
        _require_reorder_is_permutation(usernames, current_usernames, "Streamer")
        by_username = {
            item["username"].lower(): item["username"] for item in current["streamers"]
        }
        reordered = [
            {"username": by_username[username.lower()]} for username in usernames
        ]
        _write_streamers(config_path, reordered)
    elif action == "update_streamer":
        username = str(payload.get("username", "")).lower().strip()
        settings = payload.get("settings")
        _validate_streamer_settings(settings)
        unknown = _require_editable_settings(config_path, username, settings)
        streamers = list(current["streamers"])
        for streamer in streamers:
            if streamer["username"].lower() == username:
                streamer["settings"].update(settings)
                break
        else:
            raise ConfigEditError("Streamer is not configured.")
        _write_streamers(
            config_path, streamers, username, always_write={username: unknown}
        )
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
        mapping = {
            "limit": "category_limit",
            "sort": "category_sort",
            "refresh_interval_hours": "category_refresh_interval_hours",
            "drops_enabled": "category_drops_enabled",
        }
        rendered = {
            mapping[name]: (f"CategorySort.{value}" if name == "sort" else repr(value))
            for name, value in values.items()
        }
        if "sort" in values:
            _ensure_config_import(
                config_path,
                "from TwitchChannelPointsMiner.classes.Settings import CategorySort",
            )
        _set_dict_items(config_path, "MINE_CONFIG", rendered)
    elif action == "update_sources":
        values = payload.get("values") or {}
        if (
            not isinstance(values, dict)
            or set(values) - SOURCE_NAMES
            or any(not isinstance(value, bool) for value in values.values())
        ):
            raise ConfigEditError("Invalid stream source controls.")
        order = payload.get("order")
        if order is not None and (
            not isinstance(order, list) or sorted(order) != sorted(SOURCE_NAMES)
        ):
            raise ConfigEditError("Invalid stream source order.")
        override_streamer_drops = payload.get("category_drops_override_streamers")
        if override_streamer_drops is not None and not isinstance(
            override_streamer_drops, bool
        ):
            raise ConfigEditError("Category Drops override must be true or false.")
        source_names = SOURCE_ENUM_BY_NAME
        source = _read_config_source(config_path)
        miner_node = _assignment(ast.parse(source), "MINER_CONFIG")
        _require_readable_source_priority(
            _dict_item(miner_node, "streamer_source_priority")
        )
        miner = _simple_value(miner_node) or {}
        priority = list(
            miner.get("streamer_source_priority")
            or ("STREAMERS", "FOLLOWERS", "CATEGORIES", "BADGES", "WILDCARD_CATEGORIES")
        )
        if order is not None:
            # A single payload carries both enablement and position: rebuild
            # the priority list from the posted order, falling back to each
            # source's currently-saved enabled state for any name this
            # particular payload didn't include in `values` (mirroring how a
            # `values`-only payload is already allowed to be partial).
            currently_enabled = {
                name: source_names[name] in priority for name in SOURCE_NAMES
            }
            priority = [
                source_names[name]
                for name in order
                if values.get(name, currently_enabled[name]) is True
            ]
        else:
            for name, enabled in values.items():
                member = source_names[name]
                if enabled and member not in priority:
                    priority.append(member)
                elif not enabled and member in priority:
                    priority.remove(member)
        _ensure_config_import(
            config_path,
            "from TwitchChannelPointsMiner.classes.Settings import StreamerSource",
        )
        _set_dict_items(
            config_path,
            "MINER_CONFIG",
            {
                "streamer_source_priority": "["
                + ", ".join(f"StreamerSource.{name}" for name in priority)
                + "]"
            },
        )
        mine_values = {}
        if "followers" in values:
            mine_values["followers"] = repr(values["followers"])
        if "badges" in values:
            mine_values["auto_mine_badge_drops"] = repr(values["badges"])
        if "wildcard_categories" in values:
            mine_values["wildcard_categories"] = repr(values["wildcard_categories"])
        if override_streamer_drops is not None:
            mine_values["category_drops_override_streamers"] = repr(
                override_streamer_drops
            )
        if mine_values:
            _set_dict_items(config_path, "MINE_CONFIG", mine_values)
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
        _write_logger_settings(config_path, values)
    elif action == "update_updates":
        values = payload.get("values") or {}
        _validate_update_settings(values)
        miner_values = {}
        if "enabled" in values:
            miner_values["update_check"] = repr(values["enabled"])
        if values.get("startup_only") is True:
            miner_values["update_check_interval_hours"] = 'float("inf")'
        elif "interval_hours" in values:
            miner_values["update_check_interval_hours"] = repr(values["interval_hours"])
        _set_dict_items(config_path, "MINER_CONFIG", miner_values)
    elif action == "update_login":
        values = payload.get("values") or {}
        if not isinstance(values, dict) or set(values) - {"android"}:
            raise ConfigEditError("Unsupported login setting.")
        if "android" in values:
            if not isinstance(values["android"], bool):
                raise ConfigEditError("android must be true or false.")
            _set_dict_items(
                config_path,
                "MINER_CONFIG",
                {"android_login": repr(values["android"])},
            )
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
        for list_name in ("events", "recipients", "tags"):
            if list_name in values and (
                not isinstance(values[list_name], list)
                or any(not isinstance(item, str) for item in values[list_name])
            ):
                raise ConfigEditError(f"{list_name} must be a list of strings.")
        if "events" in values:
            _runtime_notification_events(values["events"])
        for number_name in ("chat_id", "message_thread_id", "port", "priority"):
            if values.get(number_name) == "":
                if number_name == "message_thread_id":
                    values[number_name] = None
                else:
                    values.pop(number_name)
                continue
            if number_name in values:
                if number_name == "message_thread_id" and values[number_name] is None:
                    continue
                if not isinstance(values[number_name], int) or isinstance(
                    values[number_name], bool
                ):
                    raise ConfigEditError(f"{number_name} must be an integer.")
        for bool_name in ("disable_notification", "use_ssl", "starttls"):
            if bool_name in values and not isinstance(values[bool_name], bool):
                raise ConfigEditError(f"{bool_name} must be true or false.")
        list_fields = {"events", "recipients", "tags"}
        number_fields = {"chat_id", "message_thread_id", "port", "priority"}
        bool_fields = {"disable_notification", "use_ssl", "starttls"}
        text_fields = set(schema["fields"]) - list_fields - number_fields - bool_fields
        for text_name in text_fields | set(schema["secrets"]):
            if text_name in values and not isinstance(values[text_name], str):
                raise ConfigEditError(f"{text_name} must be a string.")
        for text_name in text_fields:
            if values.get(text_name) == "":
                values.pop(text_name)
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
            # An empty list is still an argument the constructor accepts.
            given = available | {
                name
                for name, value in {**state["fields"], **values}.items()
                if isinstance(value, list)
            }
            missing = sorted(
                (NOTIFICATION_REQUIRED[provider] - available)
                | (NOTIFICATION_CONSTRUCTOR_REQUIRED[provider] - given)
            )
            if missing:
                raise ConfigEditError(f"{provider} requires: {', '.join(missing)}.")
        _write_notification(config_path, provider, values)
    else:
        raise ConfigEditError("Unsupported configuration action.")

    return read_managed_web_config(config_path)


def _validate_update_settings(values):
    allowed = {"enabled", "interval_hours", "startup_only"}
    if not isinstance(values, dict) or set(values) - allowed:
        raise ConfigEditError("Unsupported update-check setting.")
    for name in ("enabled", "startup_only"):
        if name in values and not isinstance(values[name], bool):
            raise ConfigEditError(f"{name} must be true or false.")
    if (
        values.get("startup_only") is not True
        and "interval_hours" in values
        and (
            not isinstance(values["interval_hours"], int)
            or isinstance(values["interval_hours"], bool)
            or values["interval_hours"] < 3
        )
    ):
        raise ConfigEditError(
            "Update-check interval must be a whole number of at least 3 hours."
        )
