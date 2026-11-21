"""The nightly build's version stamp (.github/scripts/nightly_version.py, #359)."""

import importlib.util
from pathlib import Path

import pytest

from TwitchChannelPointsMiner.utils import is_newer_version

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "nightly_version.py"

spec = importlib.util.spec_from_file_location("nightly_version", SCRIPT)
nightly_version = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nightly_version)

INIT_TEXT = (
    "# -*- coding: utf-8 -*-\n"
    '__version__ = "3.22.0"  # x-release-please-version\n'
    "\n"
    "from .TwitchChannelPointsMiner import TwitchChannelPointsMiner\n"
)
SHA = "795629a0c1d2e3f4a5b6c7d8e9f0a1b2c3d4e5f6"
NIGHTLY = "3.22.0+nightly.20261119.795629a"


def test_nightly_version_appends_date_and_short_sha():
    assert nightly_version.nightly_version("3.22.0", "20261119", SHA) == NIGHTLY


@pytest.mark.parametrize(
    "base, date, sha",
    [
        ("3.22", "20261119", SHA),
        (NIGHTLY, "20261119", SHA),
        ("3.22.0", "2026-11-19", SHA),
        ("3.22.0", "20261119", "79562"),
        ("3.22.0", "20261119", "not-a-sha"),
    ],
)
def test_nightly_version_rejects_malformed_parts(base, date, sha):
    with pytest.raises(ValueError):
        nightly_version.nightly_version(base, date, sha)


def test_stamp_replaces_only_the_version_and_keeps_the_annotation():
    stamped = nightly_version.stamp(INIT_TEXT, NIGHTLY)
    assert stamped == INIT_TEXT.replace('"3.22.0"', f'"{NIGHTLY}"')
    assert "# x-release-please-version" in stamped
    assert nightly_version.read_version(stamped) == NIGHTLY


def test_stamp_keeps_windows_line_endings():
    text = INIT_TEXT.replace("\n", "\r\n")
    assert nightly_version.stamp(text, NIGHTLY) == text.replace("3.22.0", NIGHTLY)


@pytest.mark.parametrize("version", ["3.23.0", "nightly", "3.22.0+nightly", ""])
def test_stamp_rejects_anything_but_a_nightly_version(version):
    with pytest.raises(ValueError):
        nightly_version.stamp(INIT_TEXT, version)


@pytest.mark.parametrize("text", ["", INIT_TEXT + '__version__ = "1.0.0"\n'])
def test_a_missing_or_repeated_version_line_is_an_error(text):
    with pytest.raises(ValueError):
        nightly_version.read_version(text)


def test_the_real_package_has_a_version_the_script_can_read():
    text = nightly_version.INIT_FILE.read_text(encoding="utf-8")
    version = nightly_version.read_version(text)
    assert nightly_version.RELEASE_VERSION.fullmatch(version)
    assert "x-release-please-version" in nightly_version.stamp(text, NIGHTLY)


def test_command_line_computes_and_stamps(tmp_path, capsys):
    init_file = tmp_path / "__init__.py"
    init_file.write_text(INIT_TEXT, encoding="utf-8")

    status = nightly_version.main(
        ["compute", "--date", "20261119", "--sha", SHA], init_file
    )
    assert status == 0
    assert capsys.readouterr().out.strip() == NIGHTLY
    assert init_file.read_text(encoding="utf-8") == INIT_TEXT

    assert nightly_version.main(["stamp", NIGHTLY], init_file) == 0
    assert nightly_version.read_version(init_file.read_text(encoding="utf-8")) == (
        NIGHTLY
    )


def test_command_line_reports_a_bad_version_without_writing(tmp_path, capsys):
    init_file = tmp_path / "__init__.py"
    init_file.write_text(INIT_TEXT, encoding="utf-8")
    assert nightly_version.main(["stamp", "3.23.0"], init_file) == 1
    assert "not a nightly version" in capsys.readouterr().err
    assert init_file.read_text(encoding="utf-8") == INIT_TEXT


def test_a_nightly_build_is_told_about_the_next_release_only():
    # The update check ignores build metadata: a nightly of 3.22.0 is not
    # offered 3.22.0 again, and is offered 3.22.1 once it is published.
    assert is_newer_version("3.22.0", NIGHTLY) is False
    assert is_newer_version("3.22.1", NIGHTLY) is True
