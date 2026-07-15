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
