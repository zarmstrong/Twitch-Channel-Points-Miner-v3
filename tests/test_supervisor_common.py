import sys
from pathlib import Path

import pytest

SUPERVISOR_DIR = Path(__file__).resolve().parent.parent / "supervisor"
if str(SUPERVISOR_DIR) not in sys.path:
    sys.path.insert(0, str(SUPERVISOR_DIR))

import miner_common  # noqa: E402


@pytest.fixture
def miner_dir(tmp_path):
    return tmp_path / "Twitch-Channel-Points-Miner-v3"


def test_paths_default_entry_is_v3_config_file(miner_dir):
    p = miner_common.Paths(miner_dir)
    assert p.entry == p.miner_dir / "config" / "config.py"
    assert p.config_dir == p.miner_dir / "config"


def test_paths_entry_override_sets_config_dir(miner_dir):
    p = miner_common.Paths(miner_dir, "alt/mine.py")
    assert p.config_dir == p.miner_dir / "alt"


def test_mentions_path_requires_exact_path_boundary(miner_dir):
    config = miner_dir / "config"
    assert miner_common.mentions_path(f'python "{config}" --x', config)
    assert miner_common.mentions_path(f"python -m mod --config-dir {config}", config)
    assert miner_common.mentions_path(f"python x {config}", config)
    # a sibling folder whose name starts with the same text must not match
    assert not miner_common.mentions_path(f"python x {config}-old", config)


def test_wrapper_command_for_this_miner_matches(miner_dir):
    config = miner_dir / "config"
    cmd = f'python.exe "{miner_common.WRAPPER}" "{miner_dir}" "{config}" "{miner_dir}/supervisor-logs/stop.flag"'
    assert miner_common.is_supervised_miner_command(cmd, config)


def test_direct_runner_command_with_absolute_config_matches(miner_dir):
    config = miner_dir / "config"
    cmd = f"/usr/bin/python3 -m TwitchChannelPointsMiner.runner --config-dir {config}"
    assert miner_common.is_supervised_miner_command(cmd, config)


def test_hand_started_runner_with_relative_config_does_not_match(miner_dir):
    config = miner_dir / "config"
    cmd = "python -m TwitchChannelPointsMiner.runner --config-dir ./config"
    assert not miner_common.is_supervised_miner_command(cmd, config)


def test_other_checkout_does_not_match(miner_dir):
    config = miner_dir / "config"
    other = miner_dir.parent / "other-copy" / "config"
    cmd = f"python -m TwitchChannelPointsMiner.runner --config-dir {other}"
    assert not miner_common.is_supervised_miner_command(cmd, config)


def test_legacy_run_py_command_does_not_match(miner_dir):
    config = miner_dir / "config"
    cmd = f"python {miner_dir / 'run.py'}"
    assert not miner_common.is_supervised_miner_command(cmd, config)


def test_supervisor_and_ctl_commands_are_excluded(miner_dir):
    config = miner_dir / "config"
    cmd = f"python {SUPERVISOR_DIR / 'miner_supervisor.py'} --miner-dir {miner_dir} # {config} TwitchChannelPointsMiner.runner"
    assert not miner_common.is_supervised_miner_command(cmd, config)
    cmd = f"python {SUPERVISOR_DIR / 'minerctl.py'} status {config} miner_wrapper.py"
    assert not miner_common.is_supervised_miner_command(cmd, config)


def test_wrapper_passes_only_runner_arguments_to_argparse(tmp_path):
    # The wrapper must hand the runner the same argv `python -m` would: the
    # module name must not reach argparse ("unrecognized arguments").
    import subprocess

    miner = tmp_path / "miner"
    package = miner / "TwitchChannelPointsMiner"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "runner.py").write_text(
        "import argparse, os, sys\n"
        "parser = argparse.ArgumentParser()\n"
        "parser.add_argument('--config-dir')\n"
        "args = parser.parse_args()\n"
        "print(os.path.basename(sys.argv[0]), args.config_dir, os.getcwd())\n",
        encoding="utf-8",
    )
    config = miner / "config"
    config.mkdir()

    result = subprocess.run(
        [
            sys.executable,
            str(SUPERVISOR_DIR / "miner_wrapper.py"),
            str(miner),
            str(config),
            str(tmp_path / "stop.flag"),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["runner.py", str(config), str(miner)]
