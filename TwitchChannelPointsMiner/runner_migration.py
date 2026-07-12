# -*- coding: utf-8 -*-

"""Keep a user-owned run.py compatible with the current example runner."""

import ast
import hashlib
import os
import sys
from pathlib import Path

MARKER_PREFIX = "# TwitchChannelPointsMiner runner schema: "


def _call_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _runner_calls(tree):
    calls = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node.func)
        if name == "TwitchChannelPointsMiner":
            calls.setdefault(name, []).append(node)
        elif name == "mine" and isinstance(node.func, ast.Attribute):
            calls.setdefault(name, []).append(node)
    return calls


def _schema(source):
    calls = _runner_calls(ast.parse(source))
    schema = []
    for call_name in ("TwitchChannelPointsMiner", "mine"):
        matching_calls = calls.get(call_name, [])
        if not matching_calls:
            continue
        call = matching_calls[0]
        schema.append(
            (call_name, tuple(keyword.arg for keyword in call.keywords if keyword.arg))
        )
    return tuple(schema)


def _schema_version(source):
    encoded = repr(_schema(source)).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:12]


def _keyword_comments(source, keyword):
    lines = source.splitlines()
    line_index = keyword.lineno - 1
    leading = []
    previous = line_index - 1
    while previous >= 0 and lines[previous].lstrip().startswith("#"):
        leading.insert(0, lines[previous].strip())
        previous -= 1

    inline = ""
    value_line = lines[keyword.value.end_lineno - 1]
    comment_at = value_line.find("#", keyword.value.end_col_offset)
    if comment_at >= 0:
        inline = value_line[comment_at:].strip()
    return leading, inline


def _example_settings(source):
    calls = _runner_calls(ast.parse(source))
    settings = {}
    for call_name, matching_calls in calls.items():
        call = matching_calls[0]
        settings[call_name] = {
            keyword.arg: _keyword_comments(source, keyword)
            for keyword in call.keywords
            if keyword.arg
        }
    return settings


def _method_defaults(package_root):
    source = (package_root / "TwitchChannelPointsMiner.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    defaults = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in ("__init__", "mine"):
            continue
        call_name = "TwitchChannelPointsMiner" if node.name == "__init__" else "mine"
        positional = node.args.args[-len(node.args.defaults) :]
        defaults[call_name] = {
            argument.arg: value
            for argument, value in zip(positional, node.args.defaults)
        }
    return defaults


def _portable_default(node):
    """Return a default which does not require adding an import to run.py."""
    try:
        ast.literal_eval(node)
        return ast.unparse(node)
    except (ValueError, TypeError):
        pass
    if isinstance(node, ast.Attribute):
        return repr(node.attr)
    return None


def _line_indent(source, offset):
    line_start = source.rfind("\n", 0, offset) + 1
    return source[line_start:offset]


def _node_end_offset(source, node):
    lines = source.splitlines(keepends=True)
    return sum(len(line) for line in lines[: node.end_lineno - 1]) + node.end_col_offset


def _insert_keywords(source, call, additions):
    if not additions:
        return source
    closing_offset = call.end_col_offset - 1
    lines = source.splitlines(keepends=True)
    offset = sum(len(line) for line in lines[: call.end_lineno - 1]) + closing_offset
    indent = _line_indent(source, offset)
    arguments = list(call.args) + [keyword.value for keyword in call.keywords]
    comma = ""
    if arguments:
        trailing_source = source[_node_end_offset(source, arguments[-1]) : offset]
        comma = "" if "," in trailing_source else ","
    rendered = ""
    for name, value, leading_comments, inline_comment in additions:
        for comment in leading_comments:
            if comment not in source:
                rendered += f"\n{indent}    {comment}"
        rendered += f"\n{indent}    {name}={value},"
        if inline_comment:
            rendered += f"  {inline_comment}"
    return source[:offset] + comma + rendered + "\n" + indent + source[offset:]


def migrate_runner(runner_path=None):
    """Migrate run.py and restart before any user configuration is executed."""
    runner = Path(runner_path or sys.argv[0]).resolve()
    if runner.name != "run.py" or not runner.is_file():
        return False

    package_root = Path(__file__).resolve().parent
    example = package_root.parent / "example.py"
    if not example.is_file():
        print(
            f"Runner migration skipped: canonical example not found at {example}.",
            file=sys.stderr,
        )
        return False

    runner_source = runner.read_text(encoding="utf-8")
    example_source = example.read_text(encoding="utf-8")
    version = _schema_version(example_source)
    marker = MARKER_PREFIX + version
    if marker in runner_source:
        return False

    runner_tree = ast.parse(runner_source, filename=str(runner))
    expected = dict(_schema(example_source))
    example_settings = _example_settings(example_source)
    calls = _runner_calls(runner_tree)
    defaults = _method_defaults(package_root)

    edits = []
    for call_name, expected_names in expected.items():
        for call in calls.get(call_name, []):
            present = {keyword.arg for keyword in call.keywords}
            additions = []
            for name in expected_names:
                if name in present:
                    continue
                value = _portable_default(defaults.get(call_name, {}).get(name))
                if value is not None:
                    leading, inline = example_settings.get(call_name, {}).get(
                        name, ([], "")
                    )
                    additions.append((name, value, leading, inline))
            if additions:
                edits.append((call, additions))

    for call, additions in sorted(
        edits, key=lambda item: item[0].end_lineno, reverse=True
    ):
        runner_source = _insert_keywords(runner_source, call, additions)

    runner_source = marker + "\n" + runner_source
    temporary = runner.with_name(runner.name + ".migrating")
    temporary_written = False
    try:
        temporary.write_text(runner_source, encoding="utf-8")
        temporary_written = True
        temporary.chmod(runner.stat().st_mode)
        os.replace(temporary, runner)
    except OSError as error:
        if not temporary_written:
            print(f"Unable to migrate {runner}: {error}", file=sys.stderr)
            return False
        try:
            # A writable Docker bind mount cannot be replaced (EBUSY), but its
            # contents can still be updated through the mounted path.
            runner.write_text(runner_source, encoding="utf-8")
            temporary.unlink()
        except OSError as write_error:
            print(
                f"Unable to update mounted runner {runner}: {write_error}. "
                f"Running the migrated copy at {temporary}; the mounted file is unchanged.",
                file=sys.stderr,
            )
            os.execv(
                sys.executable,
                [sys.executable, str(temporary)] + sys.argv[1:],
            )
            return True
        print(f"Migrated bind-mounted {runner.name}; restarting.")
        os.execv(sys.executable, [sys.executable, str(runner)] + sys.argv[1:])
        return True

    print(f"Migrated {runner.name} to runner schema {version}; restarting.")
    os.execv(sys.executable, [sys.executable, str(runner)] + sys.argv[1:])
    return True
