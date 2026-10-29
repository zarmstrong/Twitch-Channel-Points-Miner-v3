"""Keep config.example.py compatible with windows_installer.iss.

The installer's CustomizeStarterConfig edits the copied template with plain
text searches (Inno Setup has no Python parser), so a formatting change to
config.example.py can silently break it. These tests mirror that Pascal code
and run it against the template as checked in and as Black formats it.
"""

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / "config.example.py"
INSTALLER = REPO_ROOT / "windows_installer.iss"

INJECTED_STREAMERS = '"alice", "bob"'


def _black_formatted(source):
    black = pytest.importorskip("black")
    mode = black.Mode(target_versions={black.TargetVersion.PY311})
    return black.format_str(source, mode=mode)


def _templates():
    source = TEMPLATE.read_text(encoding="utf-8")
    variants = {"checked-in": source, "black": _black_formatted(source)}
    for name, text in variants.items():
        yield pytest.param(text, id=f"{name}-lf")
        yield pytest.param(text.replace("\n", "\r\n"), id=f"{name}-crlf")


def _splice_streamers(text, streamers):
    """Mirror CustomizeStarterConfig's STREAMERS replacement."""
    start = text.find("STREAMERS = [")
    assert start >= 0
    tail = text[start:]
    ends = []
    line_start_close = tail.find("\n]")
    if line_start_close >= 0:
        ends.append(line_start_close + 1)
    indented_close = tail.find("    ]")
    if indented_close >= 0:
        ends.append(indented_close + len("    ]") - 1)
    assert ends, "no closing bracket marker after STREAMERS = ["
    end = start + min(ends)
    return text[:start] + "STREAMERS = [" + streamers + "]" + text[end + 1 :]


def _enable_analytics(text):
    """Mirror CustomizeStarterConfig's enable-analytics edit."""
    analytics_off = '"enable_analytics": False,'
    if analytics_off not in text:
        analytics_off = "'enable_analytics': False,"
    assert text.count(analytics_off) == 1
    assert text.count("ANALYTICS_CONFIG = None") == 1
    analytics_on = analytics_off.replace("False", "True")
    text = text.replace(analytics_off, analytics_on)
    return text.replace("ANALYTICS_CONFIG = None", "ANALYTICS_CONFIG = {}")


def _assignments(text):
    return {
        node.targets[0].id: node.value
        for node in ast.parse(text).body
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
    }


def _dict_value(node, key):
    for dict_key, value in zip(node.keys, node.values):
        if isinstance(dict_key, ast.Constant) and dict_key.value == key:
            return value
    raise KeyError(key)


@pytest.mark.parametrize("template", list(_templates()))
def test_installer_edits_produce_a_valid_config(template):
    assert '"your-twitch-username"' in template

    config = _enable_analytics(_splice_streamers(template, INJECTED_STREAMERS))

    assignments = _assignments(config)
    assert ast.literal_eval(assignments["STREAMERS"]) == ["alice", "bob"]
    miner_config = assignments["MINER_CONFIG"]
    assert ast.literal_eval(_dict_value(miner_config, "enable_analytics")) is True
    # The splice must stop at STREAMERS' own bracket, not a later list's.
    _dict_value(assignments["MINE_CONFIG"], "categories")
    assert isinstance(assignments["ANALYTICS_CONFIG"], ast.Dict)


def test_installer_searches_for_the_markers_these_tests_mirror():
    installer = INSTALLER.read_text(encoding="utf-8")

    for marker in (
        "Pos('STREAMERS = [', ConfigText)",
        "Pos(#10 + ']',",
        "Pos('    ]',",
        "'\"enable_analytics\": False,'",
        "'''enable_analytics'': False,'",
        "'ANALYTICS_CONFIG = None'",
        "'\"your-twitch-username\"'",
    ):
        assert marker in installer
