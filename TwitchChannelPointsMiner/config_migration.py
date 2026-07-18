# -*- coding: utf-8 -*-

"""Convert a conventional user-owned run.py into a declarative config.py."""

import ast
import builtins
import hashlib
import os
from pathlib import Path


class ConfigMigrationError(ValueError):
    pass


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
