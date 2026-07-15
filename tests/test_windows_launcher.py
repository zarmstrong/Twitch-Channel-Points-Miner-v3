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
