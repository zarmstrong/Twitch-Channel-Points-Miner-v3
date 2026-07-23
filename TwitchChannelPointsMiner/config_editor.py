# -*- coding: utf-8 -*-

"""Small, source-preserving edits for dashboard-managed configuration lists."""

import ast
import json
import os
import re
import tempfile
import threading
from pathlib import Path

CONFIG_FILE_MUTEX = threading.Lock()
STREAMER_RE = re.compile(r"^[A-Za-z0-9_]{1,25}$")
CATEGORY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*(?:\|[A-Za-z0-9_]{1,25})?$")


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
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        return node.args[0].value
    return None


def read_web_config(config_path):
    source = Path(config_path).read_text(encoding="utf-8")
    _tree, streamers, categories = _config_lists(source)
    return {
        "streamers": [
            value
            for value in (_streamer_value(node) for node in streamers.elts)
            if value is not None
        ],
        "categories": [
            node.value
            for node in categories.elts
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ],
    }


def _offset(lines, lineno, byte_column):
    prefix = lines[lineno - 1].encode("utf-8")[:byte_column].decode("utf-8")
    return sum(len(line) for line in lines[: lineno - 1]) + len(prefix)


def _append_string(source, list_node, value):
    lines = source.splitlines(keepends=True)
    close = _offset(lines, list_node.end_lineno, list_node.end_col_offset - 1)
    literal = json.dumps(value, ensure_ascii=False)
    if not list_node.elts:
        return source[:close] + literal + source[close:]

    last = list_node.elts[-1]
    last_end = _offset(lines, last.end_lineno, last.end_col_offset)
    if list_node.lineno == list_node.end_lineno:
        separator = " " if source[last_end:close].strip().startswith(",") else ", "
        return source[:close] + separator + literal + source[close:]

    closing_line_start = sum(len(line) for line in lines[: list_node.end_lineno - 1])
    closing_indent = lines[list_node.end_lineno - 1][
        : len(lines[list_node.end_lineno - 1])
        - len(lines[list_node.end_lineno - 1].lstrip())
    ]
    item_indent = closing_indent + "    "
    comma = "" if source[last_end:closing_line_start].lstrip().startswith(",") else ","
    return (
        source[:last_end]
        + comma
        + source[last_end:closing_line_start]
        + f"{item_indent}{literal},\n"
        + source[closing_line_start:]
    )


def add_web_config_value(config_path, kind, value):
    value = value.strip()
    validators = {"streamers": STREAMER_RE, "categories": CATEGORY_RE}
    if kind not in validators:
        raise ConfigEditError("Unsupported configuration list.")
    if not validators[kind].fullmatch(value):
        label = "streamer username" if kind == "streamers" else "category slug"
        raise ConfigEditError(f"Invalid {label}.")

    path = Path(config_path)
    with CONFIG_FILE_MUTEX:
        source = path.read_text(encoding="utf-8")
        _tree, streamers, categories = _config_lists(source)
        node = streamers if kind == "streamers" else categories
        existing = (
            [_streamer_value(item) for item in node.elts]
            if kind == "streamers"
            else [
                item.value
                for item in node.elts
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            ]
        )
        if value.lower() in {item.lower() for item in existing if item is not None}:
            raise ConfigEditError(f"{value} is already configured.")

        updated = _append_string(source, node, value)
        compile(updated, str(path), "exec")
        mode = path.stat().st_mode
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=str(path.parent), text=True
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
                temporary.write(updated)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.chmod(temporary_name, mode)
            os.replace(temporary_name, path)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
    return read_web_config(path)
