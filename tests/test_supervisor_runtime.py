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


@pytest.mark.parametrize("service_type", [minerctl.Systemd, minerctl.Launchd])
def test_installed_service_preserves_the_selected_miner_python(
    tmp_path, monkeypatch, service_type
):
    selected_python = str(tmp_path / "custom env/bin/python")
    existing_python = tmp_path / "venv/bin/python3"
    existing_python.parent.mkdir(parents=True)
    existing_python.write_text("", encoding="utf-8")
    service = service_type()
    monkeypatch.setattr(
        minerctl.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )
    if isinstance(service, minerctl.Systemd):
        monkeypatch.setattr(service, "unit_dir", tmp_path / "units")
        monkeypatch.setattr(service, "ctl", lambda *args, **kwargs: None)
    else:
        monkeypatch.setattr(service, "agent_dir", tmp_path / "agents")
        monkeypatch.setattr(service, "bootstrap", lambda label: None)

    service.install(selected_python, tmp_path, 30, "08:57", 24)

    if isinstance(service, minerctl.Systemd):
        unit = (service.unit_dir / service.sup).read_text(encoding="utf-8")
        command = next(
            line.removeprefix("ExecStart=")
            for line in unit.splitlines()
            if line.startswith("ExecStart=")
        )
        argv = shlex.split(command)
    else:
        argv = plistlib.loads(service.plist(service.sup).read_bytes())[
            "ProgramArguments"
        ]
    assert argv[0] == selected_python
    parser = argparse.ArgumentParser()
    parser.add_argument("--miner-dir", type=Path)
    parser.add_argument("--python", default="")
    args = parser.parse_args(argv[2:])
    args.entry = "config/config.py"
    supervisor = miner_supervisor.Supervisor(args)
    assert supervisor.python == selected_python


def test_systemd_reinstall_applies_updated_commands_and_timer_schedules(
    tmp_path, monkeypatch
):
    service = minerctl.Systemd()
    monkeypatch.setattr(service, "unit_dir", tmp_path / "units")
    loaded = {}
    running = {}
    enabled = set()

    def ctl(*args, **kwargs):
        if args[0] == "daemon-reload":
            loaded.update(
                (unit.name, unit.read_text(encoding="utf-8"))
                for unit in service.unit_dir.iterdir()
            )
        elif args[0] == "enable":
            units = [arg for arg in args[1:] if not arg.startswith("--")]
            enabled.update(units)
            if "--now" in args:
                # Starting an active unit leaves its process/timer unchanged.
                for unit in units:
                    running.setdefault(unit, loaded[unit])
        elif args[0] == "restart":
            for unit in args[1:]:
                running[unit] = loaded[unit]

    monkeypatch.setattr(service, "ctl", ctl)
    monkeypatch.setattr(
        minerctl.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )
    first_python = str(tmp_path / "first env/bin/python")
    first_checkout = tmp_path / "first checkout"
    service.install(first_python, first_checkout, 30, "08:57", 24)

    expected_units = {
        service.sup,
        f"{service.health}.timer",
        f"{service.summary}.timer",
    }
    assert enabled == expected_units
    assert set(running) == expected_units
    assert first_python in running[service.sup]
    assert str(first_checkout) in running[service.sup]

    second_python = str(tmp_path / "second env/bin/python")
    second_checkout = tmp_path / "second checkout"
    service.install(second_python, second_checkout, 10, "12:34", 48)

    assert second_python in running[service.sup]
    assert str(second_checkout) in running[service.sup]
    assert "OnUnitActiveSec=10min" in running[f"{service.health}.timer"]
    assert "OnCalendar=*-*-* 12:34:00" in running[f"{service.summary}.timer"]


def test_crash_backoff_keeps_supervisor_heartbeat_fresh(tmp_path, monkeypatch):
    elapsed = [0.0]
    epoch = datetime(2026, 11, 12, tzinfo=timezone.utc)
    clock_now = lambda: epoch + timedelta(seconds=elapsed[0])
    monkeypatch.setattr(miner_common, "now", clock_now)
    monkeypatch.setattr(minerctl, "now", clock_now)
    # get_health only asks the service manager on Linux and macOS; pin the
    # platform so the stubbed backend is used on Windows test runs too.
    monkeypatch.setattr(minerctl, "IS_LINUX", True)
    monkeypatch.setattr(minerctl, "IS_MAC", False)
    monkeypatch.setattr(
        minerctl, "backend", lambda: SimpleNamespace(state=lambda: (True, "active"))
    )
    monkeypatch.setattr(minerctl, "pid_alive", lambda pid: True)
    supervisor = miner_supervisor.Supervisor(
        SimpleNamespace(
            miner_dir=tmp_path, entry="config/config.py", python=sys.executable
        )
    )
    supervisor.state.update(
        minerState="crashed-waiting", lastExitTime=miner_common.iso()
    )
    supervisor.save_status()
    heartbeats = [elapsed[0]]
    save_status = supervisor.save_status

    def record_heartbeat():
        heartbeats.append(elapsed[0])
        save_status()

    monkeypatch.setattr(supervisor, "save_status", record_heartbeat)
    with monkeypatch.context() as clock_patch:
        clock_patch.setattr(miner_supervisor.time, "time", lambda: elapsed[0])
        clock_patch.setattr(
            miner_supervisor.time,
            "sleep",
            lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
        )
        supervisor.sleep(900)

    health = minerctl.get_health(supervisor.p)
    assert health["verdict"] == "WARNING", health
    assert all(
        later - earlier <= 60 for earlier, later in zip(heartbeats, heartbeats[1:])
    )
    status = miner_common.read_json(supervisor.p.status)
    assert clock_now() - miner_common.parse_time(status["heartbeat"]) <= timedelta(
        minutes=1
    )


def test_backoff_stops_promptly_when_shutdown_is_requested(tmp_path, monkeypatch):
    supervisor = miner_supervisor.Supervisor(
        SimpleNamespace(
            miner_dir=tmp_path, entry="config/config.py", python=sys.executable
        )
    )
    sleeps = []

    def request_shutdown(seconds):
        sleeps.append(seconds)
        supervisor.shutdown = True

    monkeypatch.setattr(miner_supervisor.time, "sleep", request_shutdown)
    supervisor.sleep(900)
    assert len(sleeps) == 1
    assert sleeps[0] <= 2


def _updating_supervisor(tmp_path, monkeypatch, changed):
    supervisor = miner_supervisor.Supervisor(
        SimpleNamespace(
            miner_dir=tmp_path,
            entry="config/config.py",
            python=sys.executable,
            branch="master",
        )
    )
    supervisor.git = "git"
    heads = iter(["a" * 40, "b" * 40])
    monkeypatch.setattr(
        supervisor,
        "commit",
        lambda rev="HEAD", short=False: (
            "bbbbbbb" if short else ("c" * 40 if rev != "HEAD" else next(heads))
        ),
    )
    replies = {"rev-list": "2", "diff": changed}
    monkeypatch.setattr(supervisor, "git_cmd", lambda *a: (0, replies.get(a[0], "")))
    monkeypatch.setattr(supervisor, "alert", lambda *a, **k: None)
    return supervisor


def test_update_that_changes_the_supervisor_requests_a_reload(tmp_path, monkeypatch):
    supervisor = _updating_supervisor(
        tmp_path, monkeypatch, "supervisor/miner_supervisor.py"
    )

    supervisor.update_miner()

    assert supervisor.restart_requested


def test_update_that_leaves_the_supervisor_alone_keeps_running(tmp_path, monkeypatch):
    supervisor = _updating_supervisor(tmp_path, monkeypatch, "")

    supervisor.update_miner()

    assert not supervisor.restart_requested


def test_main_reexecutes_the_supervisor_after_a_reload_request(monkeypatch):
    lock = SimpleNamespace(closed=False)
    lock.close = lambda: setattr(lock, "closed", True)
    restarted = []
    monkeypatch.setattr(sys, "argv", ["miner_supervisor.py"])
    monkeypatch.setattr(miner_supervisor, "acquire_lock", lambda path: lock)
    monkeypatch.setattr(miner_supervisor.signal, "signal", lambda *a: None)
    monkeypatch.setattr(
        miner_supervisor.Supervisor,
        "loop",
        lambda self: miner_supervisor.RESTART_SELF,
    )
    monkeypatch.setattr(
        miner_supervisor, "restart_self", lambda: restarted.append(lock.closed)
    )

    miner_supervisor.main()

    # The lock is released first, so the new process can take it.
    assert restarted == [True]
