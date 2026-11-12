import argparse
import plistlib
import shlex
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

SUPERVISOR_DIR = Path(__file__).resolve().parent.parent / "supervisor"
if str(SUPERVISOR_DIR) not in sys.path:
    sys.path.insert(0, str(SUPERVISOR_DIR))

import miner_common  # noqa: E402
import miner_supervisor  # noqa: E402
import minerctl  # noqa: E402


@pytest.fixture
def installer(tmp_path, monkeypatch):
    paths = miner_common.Paths(tmp_path)
    paths.config_dir.mkdir()
    paths.entry.write_text("", encoding="utf-8")
    alerts = tmp_path / "miner-alerts.json"
    monkeypatch.setattr(miner_common, "ALERT_CONFIG", alerts)
    monkeypatch.setattr(minerctl, "ALERT_CONFIG", alerts)
    monkeypatch.setattr(minerctl.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    jobs = []
    commands = []
    monkeypatch.setattr(
        minerctl,
        "backend",
        lambda: SimpleNamespace(install=lambda *args: jobs.append(args)),
    )
    monkeypatch.setattr(
        minerctl, "ensure_venv", lambda path: "/selected-env/bin/python"
    )

    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(minerctl.subprocess, "run", run)
    args = SimpleNamespace(
        python="", no_prompt=True, health_minutes=30, summary_time="08:57", hours=24
    )
    return SimpleNamespace(
        paths=paths, alerts=alerts, commands=commands, jobs=jobs, args=args
    )


@pytest.mark.parametrize(
    "config",
    [
        {"appriseUrls": ["tgram://BOT_TOKEN/CHAT_ID"]},
        {"appriseUrgentUrls": ["pover://USER_KEY@APP_TOKEN"]},
        {"pushoverToken": "example-token", "pushoverUser": "example-user"},
        {},
    ],
)
def test_install_prepares_only_configured_optional_alerts(installer, config):
    miner_common.write_json(installer.alerts, config)

    assert minerctl.cmd_install(installer.paths, installer.args) == 0

    requirements = [command[-1] for command in installer.commands]
    expected = [str(installer.paths.requirements)]
    if config.get("appriseUrls") or config.get("appriseUrgentUrls"):
        expected.append(str(installer.paths.miner_dir / "requirements-optional.txt"))
    assert requirements == expected
    assert all(
        command[0] == "/selected-env/bin/python" for command in installer.commands
    )
    assert len(installer.jobs) == 1


def test_install_prepares_alerts_entered_at_the_prompt(installer, monkeypatch):
    installer.args.no_prompt = False
    monkeypatch.setattr(minerctl.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda prompt: "tgram://BOT_TOKEN/CHAT_ID")

    assert minerctl.cmd_install(installer.paths, installer.args) == 0

    assert miner_common.load_alert_config() == {
        "appriseUrls": ["tgram://BOT_TOKEN/CHAT_ID"]
    }
    assert installer.commands[-1][-1] == str(
        installer.paths.miner_dir / "requirements-optional.txt"
    )


def test_failed_alert_dependency_install_does_not_start_jobs(installer, monkeypatch):
    miner_common.write_json(
        installer.alerts, {"appriseUrls": ["tgram://BOT_TOKEN/CHAT_ID"]}
    )

    def run(command, **kwargs):
        optional = command[-1].endswith("requirements-optional.txt")
        return SimpleNamespace(returncode=1 if optional else 0)

    monkeypatch.setattr(minerctl.subprocess, "run", run)

    with pytest.raises(SystemExit, match="optional|Apprise"):
        minerctl.cmd_install(installer.paths, installer.args)

    assert installer.jobs == []
