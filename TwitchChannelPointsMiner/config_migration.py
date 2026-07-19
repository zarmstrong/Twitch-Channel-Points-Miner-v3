# -*- coding: utf-8 -*-

"""Convert a conventional user-owned run.py into a declarative config.py."""

import ast
import builtins
import hashlib
import os
import shutil
import stat
from pathlib import Path

CONFIG_VERSION = 1
STREAMER_SETTINGS_DEFAULTS = (
    ("make_predictions", "True"),
    ("follow_raid", "True"),
    ("claim_drops", "True"),
    ("claim_moments", "True"),
    ("watch_streak", "True"),
    ("favorite", "False"),
    ("points_limit", "None"),
    ("community_goals", "False"),
    ("bet", "None"),
    ("chat", "None"),
)
CONFIG_PRIORITY_ADDITIONS = ("Priority.FAVORITE",)


class ConfigMigrationError(ValueError):
    pass


def _assignment_value(tree, name):
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else []
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
        if any(
            isinstance(target, ast.Name) and target.id == name for target in targets
        ):
            return node.value
    return None


def _config_dict(tree):
    value = _assignment_value(tree, "MINER_CONFIG")
    return value if isinstance(value, ast.Dict) else None


def _dict_value(node, name):
    if not isinstance(node, ast.Dict):
        return None
    for key, value in zip(node.keys, node.values):
        if isinstance(key, ast.Constant) and key.value == name:
            return value
    return None


def _resolve_value(tree, node):
    if isinstance(node, ast.Name):
        return _assignment_value(tree, node.id)
    return node


def _line_start_offsets(source):
    offsets = []
    offset = 0
    for line in source.splitlines(keepends=True):
        offsets.append(offset)
        offset += len(line)
    if not offsets or offset == len(source):
        offsets.append(offset)
    return offsets


def _source_offset(line_offsets, lineno, col_offset):
    return line_offsets[lineno - 1] + col_offset


def _closing_line_insertion(source, line_offsets, node, lines):
    closing_offset = _source_offset(
        line_offsets, node.end_lineno, node.end_col_offset - 1
    )
    closing_line_start = line_offsets[node.end_lineno - 1]
    closing_prefix = source[closing_line_start:closing_offset]
    candidates = [
        *getattr(node, "args", []),
        *getattr(node, "keywords", []),
        *getattr(node, "elts", []),
    ]
    if closing_prefix.strip():
        trimmed = closing_prefix.rstrip()
        if candidates and trimmed.endswith(","):
            separator = "" if closing_prefix.endswith((" ", "\t")) else " "
        else:
            separator = ", " if candidates else ""
        return closing_offset, separator + ", ".join(lines)

    if candidates:
        indent = source[
            line_offsets[candidates[0].lineno - 1] : _source_offset(
                line_offsets, candidates[0].lineno, candidates[0].col_offset
            )
        ]
    else:
        indent = closing_prefix + "    "
    return closing_line_start, "".join(f"{indent}{line},\n" for line in lines)


def _config_version(tree):
    value = _assignment_value(tree, "CONFIG_VERSION")
    if value is None:
        return 0
    if not isinstance(value, ast.Constant) or not isinstance(value.value, int):
        raise ConfigMigrationError("CONFIG_VERSION must be an integer")
    return value.value


def migrate_config_source(source, source_name="config.py"):
    try:
        tree = ast.parse(source, filename=source_name)
    except SyntaxError as error:
        raise ConfigMigrationError(f"Cannot parse {source_name}: {error}") from error

    version = _config_version(tree)
    if version > CONFIG_VERSION:
        raise ConfigMigrationError(
            f"{source_name} uses unsupported CONFIG_VERSION {version}; "
            f"this release supports up to {CONFIG_VERSION}"
        )
    if version == CONFIG_VERSION:
        return source, version, version

    config = _config_dict(tree)
    if config is None:
        raise ConfigMigrationError("MINER_CONFIG must be a dictionary")

    line_offsets = _line_start_offsets(source)
    edits = []
    streamer_settings = _resolve_value(tree, _dict_value(config, "streamer_settings"))
    if (
        isinstance(streamer_settings, ast.Call)
        and _call_name(streamer_settings.func) == "StreamerSettings"
    ):
        existing = {keyword.arg for keyword in streamer_settings.keywords}
        missing = [
            f"{name}={value}"
            for name, value in STREAMER_SETTINGS_DEFAULTS
            if name not in existing
        ]
        if missing:
            edits.append(
                _closing_line_insertion(
                    source, line_offsets, streamer_settings, missing
                )
            )

    priority = _resolve_value(tree, _dict_value(config, "priority"))
    if isinstance(priority, (ast.List, ast.Tuple)):
        existing = {_source(source, item) for item in priority.elts}
        missing = [
            value for value in CONFIG_PRIORITY_ADDITIONS if value not in existing
        ]
        if missing:
            edits.append(
                _closing_line_insertion(source, line_offsets, priority, missing)
            )

    version_node = _assignment_value(tree, "CONFIG_VERSION")
    if version_node is None:
        first_line_end = source.find("\n") + 1
        version_offset = (
            first_line_end if source.startswith(("# -*- coding:", "# coding:")) else 0
        )
        edits.append(
            (version_offset, version_offset, f"CONFIG_VERSION = {CONFIG_VERSION}\n")
        )
    else:
        start = _source_offset(
            line_offsets, version_node.lineno, version_node.col_offset
        )
        end = _source_offset(
            line_offsets, version_node.end_lineno, version_node.end_col_offset
        )
        edits.append((start, end, str(CONFIG_VERSION)))

    migrated = source
    normalized_edits = [
        edit if len(edit) == 3 else (edit[0], edit[0], edit[1]) for edit in edits
    ]
    for start, end, replacement in sorted(normalized_edits, reverse=True):
        migrated = migrated[:start] + replacement + migrated[end:]
    return migrated, version, CONFIG_VERSION


def migrate_config(config_path):
    path = Path(config_path)
    if path.is_symlink():
        raise ConfigMigrationError(
            f"Refusing to migrate symlinked configuration {path}"
        )
    source = path.read_text(encoding="utf-8")
    migrated, old_version, new_version = migrate_config_source(source, path.name)
    if migrated == source:
        return False

    backup = path.with_name(f"{path.name}.v{old_version}.bak")
    if backup.exists():
        raise ConfigMigrationError(f"Refusing to overwrite existing {backup}")

    temporary = path.with_name(path.name + ".migrating")
    shutil.copy2(path, backup)
    try:
        temporary.write_text(migrated, encoding="utf-8")
        os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        backup.unlink(missing_ok=True)
        raise
    return old_version != new_version


def _call_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _find_calls(tree, name):
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _call_name(node.func) == name
    ]


def _source(source, node):
    value = ast.get_source_segment(source, node)
    if value is None:
        raise ConfigMigrationError("Unable to preserve a configuration expression")
    return value


def _is_runner_import(node):
    return (
        isinstance(node, ast.ImportFrom)
        and node.module == "TwitchChannelPointsMiner"
        and any(alias.name == "TwitchChannelPointsMiner" for alias in node.names)
    )


def _render_dict(
    source, call, positional_names=(), ignore_args=False, default_entries=()
):
    if not ignore_args and len(call.args) > len(positional_names):
        raise ConfigMigrationError(
            f"Too many positional arguments in {_call_name(call.func)}()"
        )
    entries = (
        []
        if ignore_args
        else [
            (name, _source(source, value))
            for name, value in zip(positional_names, call.args)
        ]
    )
    for keyword in call.keywords:
        if keyword.arg is None:
            raise ConfigMigrationError("Expanded **kwargs cannot be converted safely")
        entries.append((keyword.arg, _source(source, keyword.value)))
    existing_names = {name for name, _ in entries}
    entries.extend(
        (name, value) for name, value in default_entries if name not in existing_names
    )
    if not entries:
        return "{}"
    body = "\n".join(f"    {name!r}: {value}," for name, value in entries)
    return "{\n" + body + "\n}"


def _undefined_names(source, source_name):
    tree = ast.parse(source, filename=source_name)
    loaded = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    defined = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del))
    }
    for node in tree.body:
        if isinstance(node, ast.Import):
            defined.update(
                alias.asname or alias.name.split(".")[0] for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom):
            defined.update(alias.asname or alias.name for alias in node.names)
    return sorted(loaded - defined - set(dir(builtins)))


def convert_runner_source(source, source_name="run.py"):
    try:
        tree = ast.parse(source, filename=source_name)
    except SyntaxError as error:
        raise ConfigMigrationError(f"Cannot parse {source_name}: {error}") from error

    constructors = _find_calls(tree, "TwitchChannelPointsMiner")
    mines = _find_calls(tree, "mine")
    analytics = _find_calls(tree, "analytics")
    if len(constructors) != 1 or len(mines) != 1 or len(analytics) > 1:
        raise ConfigMigrationError(
            "Expected exactly one TwitchChannelPointsMiner() and mine() call, "
            "and at most one analytics() call"
        )

    mine = mines[0]
    if len(mine.args) > 1:
        raise ConfigMigrationError("mine() has more than one positional argument")
    streamers = _source(source, mine.args[0]) if mine.args else "[]"
    imports = [
        _source(source, node)
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        and not _is_runner_import(node)
    ]
    has_streamer_source_import = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "TwitchChannelPointsMiner.classes.Settings"
        and any(
            alias.name == "StreamerSource" and alias.asname is None
            for alias in node.names
        )
        for node in tree.body
    )
    if not has_streamer_source_import:
        imports.append(
            "from TwitchChannelPointsMiner.classes.Settings import StreamerSource"
        )
    output = [
        "# -*- coding: utf-8 -*-",
        f"# Automatically converted from {source_name}; review before editing.",
        "",
        *imports,
        "",
        "MINER_CONFIG = "
        + _render_dict(
            source,
            constructors[0],
            ("username",),
            default_entries=(
                (
                    "streamer_source_priority",
                    "[StreamerSource.STREAMERS, StreamerSource.FOLLOWERS, "
                    "StreamerSource.CATEGORIES, StreamerSource.BADGES]",
                ),
            ),
        ),
        "",
        f"STREAMERS = {streamers}",
        "",
        f"MINE_CONFIG = {_render_dict(source, mine, ignore_args=True)}",
        "",
        "ANALYTICS_CONFIG = "
        + (_render_dict(source, analytics[0]) if analytics else "None"),
        "",
    ]
    converted = "\n".join(output)
    undefined = _undefined_names(converted, source_name)
    if undefined:
        names = ", ".join(undefined)
        raise ConfigMigrationError(
            f"Cannot convert {source_name}: configuration references names that "
            f"are not imported: {names}"
        )
    return converted


def convert_runner(runner_path, config_path):
    runner = Path(runner_path)
    config = Path(config_path)
    backup = runner.with_name(runner.name + ".bak")
    if config.exists():
        raise ConfigMigrationError(f"Refusing to overwrite existing {config}")
    if backup.exists():
        raise ConfigMigrationError(f"Refusing to overwrite existing {backup}")
    converted = convert_runner_source(
        runner.read_text(encoding="utf-8"), source_name=runner.name
    )
    config.parent.mkdir(parents=True, exist_ok=True)
    temporary = config.with_name(config.name + ".migrating")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as config_file:
            config_file.write(converted)
        os.replace(temporary, config)
        os.chmod(config, 0o600)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    marker = config.with_name(".converted-from-run-py")
    try:
        digest = hashlib.sha256(runner.read_bytes()).hexdigest()
        marker.write_text(
            f"source={runner.resolve()}\nsha256={digest}\n", encoding="utf-8"
        )
        runner.rename(backup)
    except Exception:
        marker.unlink(missing_ok=True)
        config.unlink(missing_ok=True)
        raise
    return config
