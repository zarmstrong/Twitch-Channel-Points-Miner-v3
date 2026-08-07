import os
import stat
from pathlib import Path

import windows_launcher


def test_prepare_config_copies_template_once(tmp_path, monkeypatch):
    template = tmp_path / "template.py"
    template.write_text("MINER_CONFIG = {}\n", encoding="utf-8")
    monkeypatch.setattr(windows_launcher, "bundled_file", lambda _name: template)

    config_dir, created = windows_launcher.prepare_config(tmp_path / "application")

    config_path = config_dir / "config.py"
    assert created is True
    assert config_path.read_text(encoding="utf-8") == "MINER_CONFIG = {}\n"
    if os.name != "nt":
        assert stat.S_IMODE(config_path.stat().st_mode) == 0o600

    config_path.write_text("user configuration\n", encoding="utf-8")
    _, created_again = windows_launcher.prepare_config(tmp_path / "application")

    assert created_again is False
    assert config_path.read_text(encoding="utf-8") == "user configuration\n"


def test_application_directory_uses_source_directory(monkeypatch):
    monkeypatch.delattr(windows_launcher.sys, "frozen", raising=False)

    assert windows_launcher.application_directory() == Path(
        windows_launcher.__file__
    ).resolve().parent


def test_main_forwards_command_line_arguments(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.py").write_text("", encoding="utf-8")
    runner_calls = []
    monkeypatch.setattr(windows_launcher, "application_directory", lambda: tmp_path)
    monkeypatch.setattr(windows_launcher.os, "chdir", lambda _path: None)
    monkeypatch.setattr(
        windows_launcher, "runner_main", lambda argv: runner_calls.append(argv) or 0
    )
    monkeypatch.setattr(
        windows_launcher.sys,
        "argv",
        ["TwitchChannelPointsMiner.exe", "--convert-only"],
    )

    assert windows_launcher.main() == 0
    assert runner_calls == [
        [
            "--config-dir",
            str(config_dir),
            "--legacy-runner",
            str(tmp_path / "run.py"),
            "--convert-only",
        ]
    ]


def test_first_run_pauses_on_windows(monkeypatch):
    prompts = []
    monkeypatch.setattr(windows_launcher.os, "name", "nt")
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt))

    windows_launcher.pause_for_first_run()

    assert prompts == ["Press Enter to close this window..."]


def test_first_run_does_not_pause_on_other_platforms(monkeypatch):
    monkeypatch.setattr(windows_launcher.os, "name", "posix")
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt: (_ for _ in ()).throw(AssertionError("unexpected pause")),
    )

    windows_launcher.pause_for_first_run()
