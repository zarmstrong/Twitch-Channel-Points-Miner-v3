# -*- coding: utf-8 -*-

"""Convert a conventional user-owned run.py into a declarative config.py."""

import ast
import builtins
import errno
import hashlib
import logging
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)
_LINE_PATTERN = re.compile(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+")
# PEP 263: an encoding declaration is a comment on line 1, or on line 2 when
# line 1 is blank or a comment too.
_ENCODING_DECLARATION = re.compile(r"[ \t\f]*#.*?coding[:=][ \t]*[-\w.]+")
_BLANK_OR_COMMENT_LINE = re.compile(r"[ \t\f]*(?:#|$)")

CONFIG_VERSION = 12
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
    ("gift_sub_thanks", "False"),
    ("gift_sub_message", "None"),
)
# emoji is left out on purpose: its runtime default depends on the platform
# (off on Windows consoles), and an omitted argument keeps that default
# without the config needing any name that could be shadowed.
LOGGER_SETTINGS_DEFAULTS = (
    ("save", "True"),
    ("less", "False"),
    ("console_format", '"text"'),
    ("console_level", "logging.INFO"),
    ("console_username", "False"),
    ("time_zone", "None"),
    ("date_format", '"dd/mm/yy"'),
    ("file_format", '"text"'),
    ("file_level", "logging.DEBUG"),
    ("colored", "False"),
    ("color_palette", "ColorPalette()"),
    ("auto_clear", "True"),
    ("log_retention_days", "7"),
    ("daily_report", "False"),
    ("daily_report_time", '"00:00"'),
    ("username", "None"),
)
LOGGER_NOTIFICATION_SETTINGS = (
    "telegram",
    "discord",
    "webhook",
    "matrix",
    "pushover",
    "gotify",
    "email",
    "ntfy",
    "apprise",
)
LOGGER_NOTIFICATION_TEMPLATES = {
    "telegram": 'Telegram(chat_id=123456789, token="TOKEN", events=[])',
    "discord": 'Discord(webhook_api="https://discord.com/api/webhooks/...", events=[])',
    "webhook": 'Webhook(endpoint="https://example.com/webhook", method="POST", events=[])',
    "matrix": 'Matrix(username="USER", password="PASSWORD", homeserver="matrix.org", room_id="ROOM", events=[])',
    "pushover": 'Pushover(userkey="USER_KEY", token="TOKEN", priority=0, sound="pushover", events=[])',
    "gotify": 'Gotify(endpoint="https://example.com/message?token=TOKEN", priority=0, events=[])',
    "email": 'Email(host="smtp.example.com", port=587, sender="miner@example.com", recipients=["you@example.com"], events=[])',
    "ntfy": 'Ntfy(topic="YOUR_NTFY_TOPIC", events=[])',
    "apprise": 'Apprise(urls=["discord://WEBHOOK_ID/WEBHOOK_TOKEN"], events=[])',
}
BET_SETTINGS_DEFAULTS = (
    ("strategy", "None"),
    ("percentage", "None"),
    ("percentage_gap", "None"),
    ("max_points", "None"),
    ("minimum_points", "None"),
    ("stealth_mode", "None"),
    ("filter_condition", "None"),
    ("delay", "None"),
    ("delay_mode", "None"),
)
CONFIG_PRIORITY_ADDITIONS = ("Priority.FAVORITE",)
CONFIG_STREAMER_SOURCE_ADDITIONS = (
    "StreamerSource.STREAMERS",
    "StreamerSource.FOLLOWERS",
    "StreamerSource.CATEGORIES",
    "StreamerSource.BADGES",
    "StreamerSource.WILDCARD_CATEGORIES",
)
MINER_CONFIG_DEFAULTS = (
    ("claim_drops_startup", "False"),
    ("enable_analytics", "False"),
    ("disable_ssl_cert_verification", "False"),
    ("disable_at_in_nickname", "False"),
    ("update_check", "True"),
    ("update_check_interval_hours", "24"),
    ("streams_watched", "2"),
    ("android_login", "False"),
)
MINE_CONFIG_DEFAULTS = (
    ("blacklist", "[]"),
    ("followers", "False"),
    ("followers_order", '"ASC"'),
    ("categories", "[]"),
    ("category_drops_enabled", "True"),
    ("category_limit", "30"),
    ("category_sort", '"VIEWERS_DESC"'),
    ("category_campaign_order", '"ORDER"'),
    ("category_chat", "None"),
    ("category_log_level", "logging.INFO"),
    ("drop_item_art", "False"),
    ("print_open_drop_campaigns_on_load", "False"),
    ("scrape_drop_progress_on_load", "False"),
    ("log_drop_checks", "False"),
    ("track_category_streamer_points", "False"),
    ("category_refresh_interval_hours", "6"),
    ("drop_progress_stall_minutes", "10"),
    ("drop_pick_stickiness_minutes", "15"),
    ("watch_status_interval_minutes", "15"),
    ("category_drops_override_streamers", "False"),
    ("drop_badge_catalog", "True"),
    ("drop_badge_refresh_interval_hours", "1"),
    ("auto_mine_badge_drops", "False"),
    ("badge_drop_streamer_limit", "1"),
    ("wildcard_categories", "False"),
    ("wildcard_category_limit", "10"),
    ("wildcard_category_streamer_limit", "1"),
    ("wildcard_category_pin_active", "True"),
)
ANALYTICS_CONFIG_DEFAULTS = (
    ("host", '"127.0.0.1"'),
    ("port", "5000"),
    ("refresh", "5"),
    ("days_ago", "7"),
    ("password", "None"),
    ("log_poll_interval", "5"),
)


class ConfigMigrationError(ValueError):
    pass


class GeneratedConfigError(ConfigMigrationError):
    """The migration's own output doesn't compile; the input was valid."""


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
    value = _resolve_value(tree, _assignment_value(tree, "MINER_CONFIG"))
    return value if isinstance(value, ast.Dict) else None


def _dict_value(node, name):
    if not isinstance(node, ast.Dict):
        return None
    for key, value in zip(node.keys, node.values):
        if isinstance(key, ast.Constant) and key.value == name:
            return value
    return None


def _dict_names(node):
    return {
        key.value
        for key in node.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }


def _dict_key_value(tree, key):
    resolved = _resolve_value(tree, key)
    if isinstance(resolved, ast.Constant) and isinstance(resolved.value, str):
        return resolved.value
    return None


def _dict_has_literal_string_keys(node):
    return isinstance(node, ast.Dict) and all(
        isinstance(key, ast.Constant) and isinstance(key.value, str)
        for key in node.keys
    )


def _call_keyword(node, name):
    if not isinstance(node, ast.Call):
        return None
    for keyword in node.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _resolve_value(tree, node):
    seen = set()
    while isinstance(node, ast.Name) and node.id not in seen:
        seen.add(node.id)
        resolved = _assignment_value(tree, node.id)
        if resolved is None:
            break
        node = resolved
    return node


def _missing_call_defaults(call, call_name, defaults):
    if (
        not isinstance(call, ast.Call)
        or _call_name(call.func) != call_name
        or any(keyword.arg is None for keyword in call.keywords)
    ):
        return []
    existing = {keyword.arg for keyword in call.keywords}
    return [f"{name}={value}" for name, value in defaults if name not in existing]


def _commented_call_defaults(source, line_offsets, call, names):
    if not isinstance(call, ast.Call) or any(
        keyword.arg is None for keyword in call.keywords
    ):
        return []
    existing = {keyword.arg for keyword in call.keywords}
    missing = [name for name in names if name not in existing]
    if not missing:
        return []

    closing_offset = _source_offset(
        line_offsets, call.end_lineno, call.end_col_offset - 1
    )
    closing_line_start = line_offsets[call.end_lineno - 1]
    closing_prefix = source[closing_line_start:closing_offset]
    if closing_prefix.strip():
        # Keep compact calls compact. Adding line comments before their closing
        # delimiter can comment out surrounding syntax, especially when the
        # call is nested in a single-line dictionary.
        return []

    candidates = [*call.args, *call.keywords]
    if candidates:
        first = candidates[0]
        indent = source[
            line_offsets[first.lineno - 1] : _source_offset(
                line_offsets, first.lineno, first.col_offset
            )
        ]
    else:
        call_line_start = line_offsets[call.lineno - 1]
        indent = (
            source[
                call_line_start : _source_offset(
                    line_offsets, call.lineno, call.col_offset
                )
            ]
            + "    "
        )
    return [
        (
            closing_line_start,
            "".join(
                f"{indent}# {name}={LOGGER_NOTIFICATION_TEMPLATES[name]},\n"
                for name in missing
            ),
        )
    ]


def _name_is_imported(statements, name):
    """Whether a top-level import among statements binds name."""
    for node in statements:
        if isinstance(node, ast.Import):
            if any(
                (alias.asname or alias.name.split(".")[0]) == name
                for alias in node.names
            ):
                return True
        elif isinstance(node, ast.ImportFrom):
            if any((alias.asname or alias.name) == name for alias in node.names):
                return True
    return False


def module_header_end(source, tree):
    """Where a statement inserted at the top of a module can go.

    Returns (0-based line, UTF-8 byte column). A shebang only works on line 1,
    an encoding declaration only on line 1 or 2, the docstring only as the
    first statement, and `from __future__` imports must come before every
    other statement, so all of them stay above. The column is not 0 only when
    the first other statement shares the header's last logical line (after a
    `;`, possibly past a backslash continuation).
    """
    lines = [line.rstrip("\r\n") for line in _LINE_PATTERN.findall(source)[:2]]
    header = 0
    if lines and (lines[0].startswith("#!") or _ENCODING_DECLARATION.match(lines[0])):
        header = 1
    if (
        len(lines) > 1
        and _BLANK_OR_COMMENT_LINE.match(lines[0])
        and _ENCODING_DECLARATION.match(lines[1])
    ):
        header = 2
    body = tree.body
    index = 0
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        index = 1
    while (
        index < len(body)
        and isinstance(body[index], ast.ImportFrom)
        and body[index].module == "__future__"
    ):
        index += 1
    if index:
        last = body[index - 1]
        if index < len(body) and _same_logical_line(source, last, body[index]):
            following = body[index]
            return following.lineno - 1, following.col_offset
        line_offsets = _line_start_offsets(source)
        extra = _continued_lines(source, line_offsets, last)
        header = max(header, last.end_lineno + extra)
    return header, 0


def _continued_lines(source, line_offsets, node):
    """How many physical lines a trailing backslash joins to node's last
    line (a docstring followed by a backslash, then a blank or comment line).
    Inserting on one of them would land inside node's logical line."""
    lines = line_offsets.lines
    index = node.end_lineno - 1
    rest = source[
        _source_offset(line_offsets, node.end_lineno, node.end_col_offset) : (
            line_offsets[index] + len(lines[index]) if index < len(lines) else None
        )
    ]
    extra = 0
    while _code_only(rest.rstrip("\r\n")).rstrip(" \t\f").endswith("\\"):
        index += 1
        if index >= len(lines):
            break
        extra += 1
        rest = lines[index]
    return extra


def _line_after(line_offsets, node, extra):
    """Offset of the line after node's last line and the extra lines joined
    to it, or the end of the source."""
    index = node.end_lineno + extra
    if index < len(line_offsets):
        return line_offsets[index]
    lines = line_offsets.lines
    return line_offsets[len(lines) - 1] + len(lines[-1]) if lines else 0


def _same_logical_line(source, first, second):
    """Whether second starts on first's logical line: after a `;`, on the
    same physical line or past a backslash continuation. Inserted statements
    then go right before second, so they end that logical line."""
    if second.lineno == first.end_lineno:
        return True
    line_offsets = _line_start_offsets(source)
    between = _code_only(
        source[
            _source_offset(line_offsets, first.end_lineno, first.end_col_offset) : (
                _source_offset(line_offsets, second.lineno, second.col_offset)
            )
        ]
    )
    joined = re.sub(r"\\(?:\r\n|\r|\n)", "", between)
    return "\n" not in joined and "\r" not in joined


def _header_offset(source, line_offsets, tree):
    line, col_offset = module_header_end(source, tree)
    return _source_offset(line_offsets, line + 1, col_offset)


def _first_statement_edited(line_offsets, tree, edits):
    """Index of the first top-level statement any of edits changes."""
    offsets = [edit[0] for edit in edits]
    for index, node in enumerate(tree.body):
        # A decorated def or class starts at its first decorator.
        first = (getattr(node, "decorator_list", None) or [node])[0]
        start = _source_offset(line_offsets, first.lineno, first.col_offset)
        end = _source_offset(line_offsets, node.end_lineno, node.end_col_offset)
        if any(start <= offset <= end for offset in offsets):
            return index
    return len(tree.body)


def _import_offset(source, line_offsets, tree, first_use):
    """Where missing imports go: below the last top-level import, or else
    below CONFIG_VERSION, that comes before tree.body[first_use], the first
    statement that will use them. Failing both, below the module header."""
    above_first_use = tree.body[:first_use]
    imports = [
        node
        for node in above_first_use
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    if imports:
        node = imports[-1]
        following = _statement_on_same_line(source, line_offsets, tree, node)
        if following is not None:
            return following
        extra = _continued_lines(source, line_offsets, node)
        if extra:
            return _line_after(line_offsets, node, extra)
        end = _source_offset(line_offsets, node.end_lineno, node.end_col_offset)
        # Keep a trailing `;` (`import os;`) with its statement.
        semicolon = re.match(r"[ \t]*;", source[end:])
        if semicolon:
            end += semicolon.end()
        # And a trailing comment with its line: insert below that line.
        comment = re.match(r"[ \t]*#[^\r\n]*(?:\r\n|\r|\n)?", source[end:])
        return end + comment.end() if comment else end
    for node in above_first_use:
        if _assignment_value(ast.Module(body=[node]), "CONFIG_VERSION") is not None:
            following = _statement_on_same_line(source, line_offsets, tree, node)
            if following is not None:
                return following
            return _line_after(
                line_offsets, node, _continued_lines(source, line_offsets, node)
            )
    return _header_offset(source, line_offsets, tree)


def _statement_on_same_line(source, line_offsets, tree, node):
    """Offset of the statement after node when it follows a `;` on node's
    last logical line; inserting below node would put the insertion after it,
    or (past a backslash continuation) in the middle of that logical line."""
    index = tree.body.index(node) + 1
    if index < len(tree.body) and _same_logical_line(source, node, tree.body[index]):
        following = tree.body[index]
        return _source_offset(line_offsets, following.lineno, following.col_offset)
    return None


def _enum_list_additions(
    source,
    tree,
    node,
    additions,
    enum_name,
    import_statement,
    required_imports,
    rebound,
):
    existing_members = {
        item.attr
        for item in node.elts
        if isinstance(item, ast.Attribute) and isinstance(item.attr, str)
    }
    prefix = next(
        (
            _source(source, item.value)
            for item in node.elts
            if isinstance(item, ast.Attribute)
        ),
        None,
    )
    if prefix is None:
        if enum_name in rebound:
            return []
        prefix = enum_name
        if import_statement not in required_imports:
            required_imports.append(import_statement)
    return [
        f"{prefix}.{addition.rsplit('.', 1)[-1]}"
        for addition in additions
        if addition.rsplit(".", 1)[-1] not in existing_members
    ]


class _LineOffsets(list):
    """Character start offsets per line, plus the text of each line.

    Lines end only at the tokenizer's line breaks (CRLF, CR, LF), not at every
    character that str.splitlines() treats as a break.
    """

    __slots__ = ("lines",)


def _line_start_offsets(source):
    lines = _LINE_PATTERN.findall(source)
    offsets = _LineOffsets()
    offset = 0
    for line in lines:
        offsets.append(offset)
        offset += len(line)
    if not offsets or offset == len(source):
        offsets.append(offset)
    offsets.lines = lines
    return offsets


def _source_offset(line_offsets, lineno, col_offset):
    """Convert an ast (UTF-8 byte) column on a line to a character index."""
    start = line_offsets[lineno - 1]
    lines = line_offsets.lines
    line = lines[lineno - 1] if lineno - 1 < len(lines) else ""
    if line.isascii():
        return start + col_offset
    chars = len(line.encode("utf-8")[:col_offset].decode("utf-8", errors="ignore"))
    return start + chars


def _code_only(text):
    """text with its comments blanked out. Only for gaps between a dict's
    keys and values, which hold no strings, so every `#` starts a comment."""
    return re.sub(r"#[^\r\n]*", lambda match: " " * len(match.group()), text)


def _entry_start(source, line_offsets, node, index):
    """Where dict entry index starts, including parentheses around its key
    (or the `**` of an unpacked mapping)."""
    key = node.keys[index]
    value = node.values[index]
    first = key if key is not None else value
    start = _source_offset(line_offsets, first.lineno, first.col_offset)
    if index:
        previous = node.values[index - 1]
        gap_start = _source_offset(
            line_offsets, previous.end_lineno, previous.end_col_offset
        )
    else:
        # Just after the opening brace.
        gap_start = _source_offset(line_offsets, node.lineno, node.col_offset) + 1
    gap = _code_only(source[gap_start:start])
    # The previous value's closing parentheses and the separating comma come
    # first; anything opened after them wraps this entry.
    tail = gap.rsplit(",", 1)[-1] if index else gap
    tail_start = start - len(tail)
    marker = "**" if key is None else "("
    position = tail.find(marker)
    return tail_start + position if position >= 0 else start


# Whitespace, line continuations and comments, then a `)`. A comment runs to
# its line break, so each input has one way to match (no backtracking blowup
# on a run of `#`): the `)` closing a value always follows on a later line.
_CLOSING_PAREN = re.compile(r"(?:\s|\\|#[^\r\n]*[\r\n])*\)")


def _entry_value_end(source, line_offsets, key, value):
    """Where a dict entry's value ends, including parentheses around it."""
    value_start = _source_offset(line_offsets, value.lineno, value.col_offset)
    end = _source_offset(line_offsets, value.end_lineno, value.end_col_offset)
    if key is None:
        return end
    key_end = _source_offset(line_offsets, key.end_lineno, key.end_col_offset)
    between = _code_only(source[key_end:value_start])
    opened = between[between.index(":") + 1 :].count("(")
    while opened:
        closing = _CLOSING_PAREN.match(source, end)
        if closing is None:
            break
        end = closing.end()
        opened -= 1
    return end


def _dict_entry_removal(source, line_offsets, tree, node, name):
    """Return a source edit removing a statically resolvable dictionary entry.

    The edit spans the whole entry, including parentheses around its key or
    value (`"password": ("secret")`), so no stray `(` or `)` is left behind.
    """
    index = next(
        (
            index
            for index, key in enumerate(node.keys)
            if _dict_key_value(tree, key) == name
        ),
        None,
    )
    if index is None:
        return None

    key = node.keys[index]
    value = node.values[index]
    start = _entry_start(source, line_offsets, node, index)
    if index + 1 < len(node.keys):
        return start, _entry_start(source, line_offsets, node, index + 1), ""

    end = _entry_value_end(source, line_offsets, key, value)
    closing = _source_offset(line_offsets, node.end_lineno, node.end_col_offset - 1)
    trailing = _code_only(source[end:closing])
    comma = trailing.find(",")
    if comma >= 0:
        return start, end + comma + 1, ""
    if index:
        previous = node.values[index - 1]
        previous_end = _source_offset(
            line_offsets, previous.end_lineno, previous.end_col_offset
        )
        separator = _code_only(source[previous_end:start]).rfind(",")
        if separator >= 0:
            start = previous_end + separator
    return start, end, ""


def _apply_edits(source, edits):
    migrated = source
    normalized = [
        edit if len(edit) == 3 else (edit[0], edit[0], edit[1]) for edit in edits
    ]
    for start, end, replacement in sorted(normalized, reverse=True):
        migrated = migrated[:start] + replacement + migrated[end:]
    return migrated


def _without_miner_password(source, source_name="config.py"):
    tree = ast.parse(source, filename=source_name)
    config = _config_dict(tree)
    if config is None:
        return source
    edit = _dict_entry_removal(
        source, _line_start_offsets(source), tree, config, "password"
    )
    return _apply_edits(source, [edit]) if edit else source


def _closing_line_insertion(source, line_offsets, node, lines):
    closing_offset = _source_offset(
        line_offsets, node.end_lineno, node.end_col_offset - 1
    )
    closing_line_start = line_offsets[node.end_lineno - 1]
    closing_prefix = source[closing_line_start:closing_offset]
    if isinstance(node, ast.Dict):
        candidates = node.keys
        final_candidates = node.values
    else:
        candidates = [
            *getattr(node, "args", []),
            *getattr(node, "keywords", []),
            *getattr(node, "elts", []),
        ]
        final_candidates = candidates
    if closing_prefix.strip():
        trimmed = closing_prefix.rstrip()
        if candidates and trimmed.endswith(","):
            separator = "" if closing_prefix.endswith((" ", "\t")) else " "
        else:
            separator = ", " if candidates else ""
        return [(closing_offset, separator + ", ".join(lines))]

    indent = closing_prefix + "    "
    if candidates:
        first_indent = source[
            line_offsets[candidates[0].lineno - 1] : _source_offset(
                line_offsets, candidates[0].lineno, candidates[0].col_offset
            )
        ]
        # Unless the first element shares the opening line (`X = {"a": 1,`),
        # where the text before it isn't an indent.
        if not first_indent.strip():
            indent = first_indent
    insertions = [(closing_line_start, "".join(f"{indent}{line},\n" for line in lines))]
    if final_candidates:
        last = final_candidates[-1]
        last_end = _source_offset(line_offsets, last.end_lineno, last.end_col_offset)
        # Step past parentheses around the last element (`"x": ("v")`), so
        # the comma goes after them, not inside where it makes a tuple.
        while True:
            closing = _CLOSING_PAREN.match(source, last_end)
            if closing is None or closing.end() > closing_offset:
                break
            last_end = closing.end()
        if not source[last_end:closing_offset].lstrip().startswith(","):
            insertions.append((last_end, ","))
    return insertions


def _config_version(tree):
    value = _assignment_value(tree, "CONFIG_VERSION")
    if value is None:
        return 0
    if not isinstance(value, ast.Constant) or not isinstance(value.value, int):
        raise ConfigMigrationError("CONFIG_VERSION must be an integer")
    return value.value


def _checked_output(migrated, source_name):
    """Return the migrated source, or raise GeneratedConfigError if it's invalid."""
    try:
        # compile, not ast.parse: only the compiler rejects some errors, such
        # as a __future__ import that no longer comes first.
        compile(migrated, source_name, "exec", dont_inherit=True)
    except (SyntaxError, ValueError) as error:
        raise GeneratedConfigError(
            f"Migration generated invalid {source_name}: {error}"
        ) from error
    return migrated


def migrate_config_source(source, source_name="config.py"):
    try:
        tree = ast.parse(source, filename=source_name)
        # The compiler also rejects some files ast.parse accepts, such as a
        # __future__ import that isn't first, which older migrations could
        # write. Such a file is invalid input, so it can be recovered from a
        # backup; only invalid output counts as GeneratedConfigError.
        compile(tree, source_name, "exec", dont_inherit=True)
    except SyntaxError as error:
        raise ConfigMigrationError(f"Cannot parse {source_name}: {error}") from error

    version = _config_version(tree)
    if version > CONFIG_VERSION:
        raise ConfigMigrationError(
            f"{source_name} uses unsupported CONFIG_VERSION {version}; "
            f"this release supports up to {CONFIG_VERSION}"
        )
    config = _config_dict(tree)
    if config is None:
        raise ConfigMigrationError("MINER_CONFIG must be a dictionary")

    line_offsets = _line_start_offsets(source)
    edits = []
    password_removal = _dict_entry_removal(
        source, line_offsets, tree, config, "password"
    )
    if password_removal:
        edits.append(password_removal)
    # Edits from here on may use the names in required_imports.
    first_default_edit = len(edits)
    if version == CONFIG_VERSION:
        migrated = _checked_output(_apply_edits(source, edits), source_name)
        return migrated, version, version
    config_names = _dict_names(config)
    safe_config = _dict_has_literal_string_keys(config)
    missing_miner_options = (
        [
            f"{name!r}: {value}"
            for name, value in MINER_CONFIG_DEFAULTS
            if name not in config_names
        ]
        if safe_config
        else []
    )
    required_imports = []
    rebound = rebound_names(tree)
    if safe_config and "priority" not in config_names and "Priority" in rebound:
        # The miner's built-in order applies instead, and it has no FAVORITE.
        logger.warning(
            f"{source_name} may bind Priority itself (an assignment, another "
            "import or a wildcard import), so the migration did not add "
            "MINER_CONFIG['priority']. The built-in order applies (STREAK, "
            "DROPS, ORDER, without FAVORITE); add 'priority' to MINER_CONFIG "
            "to change it.",
            extra={"emoji": ":warning:"},
        )
    if safe_config and "priority" not in config_names and "Priority" not in rebound:
        missing_miner_options.append(
            "'priority': [Priority.STREAK, Priority.DROPS, Priority.ORDER, "
            "Priority.FAVORITE]"
        )
        required_imports.append(
            "from TwitchChannelPointsMiner.classes.Settings import Priority"
        )
    if (
        safe_config
        and "streamer_source_priority" not in config_names
        and "StreamerSource" not in rebound
    ):
        missing_miner_options.append(
            "'streamer_source_priority': [StreamerSource.STREAMERS, "
            "StreamerSource.FOLLOWERS, StreamerSource.CATEGORIES, "
            "StreamerSource.BADGES, StreamerSource.WILDCARD_CATEGORIES]"
        )
        required_imports.append(
            "from TwitchChannelPointsMiner.classes.Settings import StreamerSource"
        )
    if missing_miner_options:
        edits.extend(
            _closing_line_insertion(source, line_offsets, config, missing_miner_options)
        )
    streamer_settings = _resolve_value(tree, _dict_value(config, "streamer_settings"))
    missing = _missing_call_defaults(
        streamer_settings, "StreamerSettings", STREAMER_SETTINGS_DEFAULTS
    )
    if missing:
        edits.extend(
            _closing_line_insertion(source, line_offsets, streamer_settings, missing)
        )

    bet_settings = _resolve_value(tree, _call_keyword(streamer_settings, "bet"))
    if bet_settings is not None:
        missing = _missing_call_defaults(
            bet_settings, "BetSettings", BET_SETTINGS_DEFAULTS
        )
        if missing:
            edits.extend(
                _closing_line_insertion(source, line_offsets, bet_settings, missing)
            )

    for logger_settings in _find_calls(tree, "LoggerSettings"):
        logger_names = {
            keyword.arg
            for keyword in logger_settings.keywords
            if keyword.arg is not None
        }
        email_import = "from TwitchChannelPointsMiner.classes.Email import Email"
        if "email" not in logger_names and email_import not in required_imports:
            required_imports.append(email_import)
        missing = [
            default
            for default in _missing_call_defaults(
                logger_settings, "LoggerSettings", LOGGER_SETTINGS_DEFAULTS
            )
            if not _uses_any(default.split("=", 1)[1], rebound)
        ]
        if missing:
            values = [default.split("=", 1)[1] for default in missing]
            if _uses_any_of(values, {"logging"}) and (
                "import logging" not in required_imports
            ):
                required_imports.append("import logging")
            color_palette_import = (
                "from TwitchChannelPointsMiner.logger import ColorPalette"
            )
            if _uses_any_of(values, {"ColorPalette"}) and (
                color_palette_import not in required_imports
            ):
                required_imports.append(color_palette_import)
            edits.extend(
                _closing_line_insertion(source, line_offsets, logger_settings, missing)
            )
        edits.extend(
            _commented_call_defaults(
                source,
                line_offsets,
                logger_settings,
                LOGGER_NOTIFICATION_SETTINGS,
            )
        )

    priority = _resolve_value(tree, _dict_value(config, "priority"))
    if isinstance(priority, (ast.List, ast.Tuple)):
        missing = _enum_list_additions(
            source,
            tree,
            priority,
            CONFIG_PRIORITY_ADDITIONS,
            "Priority",
            "from TwitchChannelPointsMiner.classes.Settings import Priority",
            required_imports,
            rebound,
        )
        if missing:
            edits.extend(
                _closing_line_insertion(source, line_offsets, priority, missing)
            )

    source_priority = _resolve_value(
        tree, _dict_value(config, "streamer_source_priority")
    )
    if isinstance(source_priority, (ast.List, ast.Tuple)):
        missing = _enum_list_additions(
            source,
            tree,
            source_priority,
            CONFIG_STREAMER_SOURCE_ADDITIONS,
            "StreamerSource",
            "from TwitchChannelPointsMiner.classes.Settings import StreamerSource",
            required_imports,
            rebound,
        )
        if missing:
            edits.extend(
                _closing_line_insertion(source, line_offsets, source_priority, missing)
            )

    mine_config = _resolve_value(tree, _assignment_value(tree, "MINE_CONFIG"))
    if isinstance(mine_config, ast.Dict):
        mine_names = _dict_names(mine_config)
        missing_mine_options = (
            [
                f"{name!r}: {value}"
                for name, value in MINE_CONFIG_DEFAULTS
                if name not in mine_names and not _uses_any(value, rebound)
            ]
            if _dict_has_literal_string_keys(mine_config)
            else []
        )
        if missing_mine_options:
            if (
                "category_log_level" not in mine_names
                and "logging" not in rebound
                and "import logging" not in required_imports
            ):
                required_imports.append("import logging")
            edits.extend(
                _closing_line_insertion(
                    source, line_offsets, mine_config, missing_mine_options
                )
            )

    analytics_config = _resolve_value(tree, _assignment_value(tree, "ANALYTICS_CONFIG"))
    if _dict_has_literal_string_keys(analytics_config):
        missing_analytics_options = [
            f"{name!r}: {value}"
            for name, value in ANALYTICS_CONFIG_DEFAULTS
            if name not in _dict_names(analytics_config)
        ]
        if missing_analytics_options:
            edits.extend(
                _closing_line_insertion(
                    source,
                    line_offsets,
                    analytics_config,
                    missing_analytics_options,
                )
            )

    first_use = _first_statement_edited(line_offsets, tree, edits[first_default_edit:])
    # The missing imports go above the first statement that uses them, so an
    # import of the same name counts only when it is already above it (one
    # below would bind the name too late and the config would raise NameError).
    # A name the config binds some other way is never imported over it.
    required_imports = [
        statement
        for statement in required_imports
        if statement.split()[-1] not in rebound
        and not _name_is_imported(tree.body[:first_use], statement.split()[-1])
    ]
    if required_imports:
        import_offset = _import_offset(source, line_offsets, tree, first_use)
        # A line break is only missing after an import's last character; a
        # header offset is a line start or follows `; `.
        prefix = (
            "\n"
            if import_offset and source[import_offset - 1] not in "\r\n\t ;"
            else ""
        )
        edits.append((import_offset, prefix + "\n".join(required_imports) + "\n"))

    version_node = _assignment_value(tree, "CONFIG_VERSION")
    if version_node is None:
        version_offset = _header_offset(source, line_offsets, tree)
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

    migrated = _checked_output(_apply_edits(source, edits), source_name)
    return migrated, version, CONFIG_VERSION


def _backup_version(backup, config_name):
    # Sort config.py.v9.bak before config.py.v11.bak (not lexicographically).
    version = backup.name[len(config_name) + 2 : -len(".bak")]
    return (0, int(version)) if version.isdigit() else (-1, 0)


def migrate_config(config_path):
    path = Path(config_path)
    if path.is_symlink():
        raise ConfigMigrationError(
            f"Refusing to migrate symlinked configuration {path}"
        )
    current_source = path.read_text(encoding="utf-8")
    # Windows editors can save "UTF-8 with BOM"; ast.parse() and compile()
    # reject the U+FEFF, so drop it and write the file back without it.
    had_bom = current_source.startswith("\ufeff")
    current_source = current_source.removeprefix("\ufeff")
    source = current_source
    recovery_backup = None
    try:
        migrated, old_version, new_version = migrate_config_source(source, path.name)
    except ConfigMigrationError as error:
        # Recover from a backup only when config.py itself doesn't parse. A
        # valid file the migration can't handle must stay as it is: recovering
        # would overwrite it with an older backup's settings.
        if isinstance(error, GeneratedConfigError) or not isinstance(
            error.__cause__, SyntaxError
        ):
            raise
        backups = [
            backup
            for backup in sorted(
                path.parent.glob(f"{path.name}.v*.bak"),
                key=lambda candidate: _backup_version(candidate, path.name),
            )
            if not backup.is_symlink() and backup.is_file()
        ]
        if not backups:
            raise
        recovery_backup = backups[-1]
        source = recovery_backup.read_text(encoding="utf-8").removeprefix("\ufeff")
        migrated, old_version, new_version = migrate_config_source(source, path.name)
    if migrated == current_source and not had_bom:
        return False
    # Dropping the BOM is a re-save, not a migration: the version is unchanged,
    # so neither create a backup nor check an existing one from an earlier run.
    bom_only = recovery_backup is None and migrated == current_source

    backup = path.with_name(f"{path.name}.v{old_version}.bak")
    reuse_backup = False
    if recovery_backup is None and not bom_only:
        if backup.is_symlink():
            raise ConfigMigrationError(f"Refusing to use symlinked backup {backup}")
        if backup.exists():
            if not backup.is_file():
                raise ConfigMigrationError(f"Backup path is not a file: {backup}")
            sanitized_source = _without_miner_password(current_source, path.name)
            backup_source = backup.read_text(encoding="utf-8")
            if backup_source == current_source and backup_source != sanitized_source:
                backup.write_text(sanitized_source, encoding="utf-8")
            elif backup_source != sanitized_source:
                raise ConfigMigrationError(
                    f"Refusing to overwrite existing {backup}: its contents do "
                    "not match the current configuration"
                )
            reuse_backup = True

    backup_created = False
    if recovery_backup is None and not reuse_backup and not bom_only:
        sanitized_source = _without_miner_password(current_source, path.name)
        backup.write_text(sanitized_source, encoding="utf-8")
        shutil.copystat(path, backup)
        backup_created = True
    mode = stat.S_IMODE(path.stat().st_mode)
    descriptor = None
    temporary = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".migrating", dir=path.parent
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            handle.write(migrated)
        os.chmod(temporary, mode)
        try:
            os.replace(temporary, path)
        except OSError as error:
            # Docker and Podman reject replacing a directly bind-mounted file
            # with EBUSY. The file itself can still be writable, so fall back
            # to updating that mount in place. Directory mounts continue to
            # use the atomic replacement above.
            if error.errno != errno.EBUSY:
                raise
            with path.open("r+", encoding="utf-8") as handle:
                handle.seek(0)
                handle.write(migrated)
                handle.truncate()
                handle.flush()
                os.fsync(handle.fileno())
            temporary.unlink()
            temporary = None
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if backup_created:
            backup.unlink(missing_ok=True)
        raise
    return True


def _call_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def import_binding(node, alias):
    """(level, module, imported name, bound name) for one imported alias.

    `import a.b` binds a to the package a, the same as `import a`.
    """
    if isinstance(node, ast.ImportFrom):
        return (node.level, node.module, alias.name, alias.asname or alias.name)
    if alias.asname:
        return (0, None, alias.name, alias.asname)
    root = alias.name.split(".")[0]
    return (0, None, root, root)


# The imports the migration adds for the names its defaults use.
_MIGRATION_IMPORTS = {
    "logging": (0, None, "logging", "logging"),
    "ColorPalette": (
        0,
        "TwitchChannelPointsMiner.logger",
        "ColorPalette",
        "ColorPalette",
    ),
    "Priority": (
        0,
        "TwitchChannelPointsMiner.classes.Settings",
        "Priority",
        "Priority",
    ),
    "StreamerSource": (
        0,
        "TwitchChannelPointsMiner.classes.Settings",
        "StreamerSource",
        "StreamerSource",
    ),
    "Email": (0, "TwitchChannelPointsMiner.classes.Email", "Email", "Email"),
}


_NESTED_SCOPES = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.Lambda,
    ast.ClassDef,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)


def _module_scope_nodes(tree):
    """Yield the nodes of tree that run in, and can bind names of, the module
    namespace: everything except function, lambda, class and comprehension
    bodies. Their names, decorators, defaults, bases and first iterables run
    in the module; a walrus in a comprehension binds there too. Names a
    nested scope declares `global` are yielded as Name stores."""
    stack = [tree]
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, _NESTED_SCOPES) or type(node).__name__ == "TypeAlias":
            children = _enclosing_scope_parts(node)
        else:
            children = list(ast.iter_child_nodes(node))
        if isinstance(node, _NESTED_SCOPES):
            children += [
                ast.Name(id=name, ctx=ast.Store())
                for name in sorted(_assigned_globals(node))
            ]
        stack.extend(children)


_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def _enclosing_scope_parts(node):
    """The parts of a nested scope that run in, and can bind names of, the
    scope around it: a function's defaults, decorators and annotations, a
    class's decorators, bases and keywords, a comprehension's first iterable
    and its walrus targets, a type alias's name."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        arguments = node.args
        parts = [*arguments.defaults]
        parts += [default for default in arguments.kw_defaults if default]
        parts += getattr(node, "decorator_list", [])
        # Annotations run there too, unless postponed; counting them always
        # only refuses more.
        every_argument = [
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
            *filter(None, (arguments.vararg, arguments.kwarg)),
        ]
        parts += [
            argument.annotation
            for argument in every_argument
            if argument.annotation is not None
        ]
        if getattr(node, "returns", None) is not None:
            parts.append(node.returns)
        return parts
    if isinstance(node, ast.ClassDef):
        return [*node.decorator_list, *node.bases, *node.keywords]
    if isinstance(node, _COMPREHENSIONS):
        return [node.generators[0].iter, *_comprehension_walrus_targets(node)]
    if type(node).__name__ == "TypeAlias":
        # The value and type parameters get their own scope.
        return [node.name]
    return list(ast.iter_child_nodes(node))


def _annotation_only_targets(nodes):
    """ids of the Name targets of annotations without a value (`x: int`),
    which declare a name without binding it."""
    return {
        id(node.target)
        for node in nodes
        if isinstance(node, ast.AnnAssign)
        and node.value is None
        and isinstance(node.target, ast.Name)
    }


def _comprehension_walrus_targets(comprehension):
    """The walrus targets in a comprehension that bind in its enclosing
    scope: those in it or in comprehensions nested in it, but not in the
    body of a lambda inside it, which binds its own names. A lambda's
    defaults still run in the comprehension."""
    targets = []
    stack = list(ast.iter_child_nodes(comprehension))
    while stack:
        node = stack.pop()
        if isinstance(node, ast.NamedExpr):
            targets.append(node.target)
        if isinstance(node, _NESTED_SCOPES) and not isinstance(node, _COMPREHENSIONS):
            stack.extend(_enclosing_scope_parts(node))
            continue
        stack.extend(ast.iter_child_nodes(node))
    return targets


def _own_scope_nodes(scope):
    """Yield the nodes that run in scope's own namespace. A nested scope is
    yielded (its name binds here), and only its parts that run here are
    entered."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, _NESTED_SCOPES):
            # Its defaults, decorators and the like still run here.
            stack.extend(_enclosing_scope_parts(node))
        else:
            stack.extend(ast.iter_child_nodes(node))


def _names_bound_by(node):
    if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
        return [node.id]
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return [
            alias.asname or alias.name.split(".")[0]
            for alias in node.names
            if alias.name != "*"
        ]
    if isinstance(node, ast.ExceptHandler):
        return [node.name] if node.name else []
    match_star = getattr(ast, "MatchStar", None)
    match_as = getattr(ast, "MatchAs", None)
    match_mapping = getattr(ast, "MatchMapping", None)
    if (match_star and isinstance(node, match_star)) or (
        match_as and isinstance(node, match_as)
    ):
        return [node.name] if node.name else []
    if match_mapping and isinstance(node, match_mapping):
        return [node.rest] if node.rest else []
    return []


def _assigned_globals(scope):
    """Names that scope, or a scope nested in it, declares `global` and also
    binds in that same scope. A declaration alone (`global logging` followed
    only by reads) binds nothing."""
    names = set()
    for inner in ast.walk(scope):
        if not isinstance(inner, _NESTED_SCOPES):
            continue
        own = list(_own_scope_nodes(inner))
        declared = {
            name for node in own if isinstance(node, ast.Global) for name in node.names
        }
        if not declared:
            continue
        declared_only = _annotation_only_targets(own)
        bound = {
            name
            for node in own
            if id(node) not in declared_only
            for name in _names_bound_by(node)
        }
        # A walrus in a comprehension binds in the enclosing function.
        bound |= {
            target.id
            for comprehension in own
            if isinstance(comprehension, _COMPREHENSIONS)
            for target in _comprehension_walrus_targets(comprehension)
        }
        names |= declared & bound
    return names


def rebound_names(tree, expected=None, module_scope=False):
    """Names the config binds other than with the import that should bind them.

    expected maps a bound name to the import binding (see import_binding) a
    reference to it relies on; it defaults to the imports the migration adds.
    For those names only a top-level import with exactly that binding counts
    as fine; for other names, any plain (unaliased) top-level import does.
    Every other binding counts: an assignment, an alias, an import of the
    same name from elsewhere (`from ...entities import Streamer` binds the
    module), a wildcard import from another module, an import inside a
    function, a parameter, a loop or `match`
    target, a type parameter... Such a name may not be that import where a
    written reference runs, so the migration leaves defaults using it out
    (the class's own default applies) and the dashboard refuses the edit.

    The migration writes into calls that can sit in any scope, so by default
    every binding in the file counts. With module_scope=True only bindings in
    the module namespace count (an import under `if` or `try` is top-level
    there): the dashboard writes only into top-level assignments, which a
    function's local names can't shadow.
    """
    if expected is None:
        expected = _MIGRATION_IMPORTS
    if module_scope:
        nodes = list(_module_scope_nodes(tree))
        top_level = {
            id(node) for node in nodes if isinstance(node, (ast.Import, ast.ImportFrom))
        }
    else:
        nodes = ast.walk(tree)
        top_level = {
            id(node)
            for node in tree.body
            if isinstance(node, (ast.Import, ast.ImportFrom))
        }
    nodes = list(nodes)
    declared_only = _annotation_only_targets(nodes)
    names = set()
    for node in nodes:
        if id(node) in declared_only:
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name == "*":
                    # A wildcard can bind any name (a package's wildcard
                    # binds its loaded submodules), so only one from the
                    # expected module is known to bind the expected object.
                    names.update(
                        bound
                        for bound, binding in expected.items()
                        if binding[:2] != (node.level, node.module)
                    )
                    continue
                binding = import_binding(node, alias)
                bound = binding[3]
                if id(node) not in top_level:
                    names.add(bound)
                elif bound in expected:
                    if binding != expected[bound]:
                        names.add(bound)
                elif alias.asname is not None:
                    names.add(bound)
        elif isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            names.add(node.rest)
        elif isinstance(
            node,
            (
                ast.FunctionDef,
                ast.AsyncFunctionDef,
                ast.ClassDef,
                ast.ExceptHandler,
                ast.MatchAs,
                ast.MatchStar,
            ),
        ) or type(node).__name__ in ("TypeVar", "ParamSpec", "TypeVarTuple"):
            if getattr(node, "name", None):
                names.add(node.name)
    return names


def _uses_any(value, names):
    """Whether a default's source reads any of names."""
    return any(
        isinstance(node, ast.Name) and node.id in names
        for node in ast.walk(ast.parse(value, mode="eval"))
    )


def _uses_any_of(values, names):
    return any(_uses_any(value, names) for value in values)


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
                    "StreamerSource.CATEGORIES, StreamerSource.BADGES, "
                    "StreamerSource.WILDCARD_CATEGORIES]",
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
