"""
Runs the Twitch miner (v3 stable runner) and stops it cleanly when a stop-flag file appears.

Used by MinerSupervisor.ps1 and miner_supervisor.py:
    python miner_wrapper.py <miner_dir> <miner_dir\\config> <miner_dir\\supervisor-logs\\stop.flag>

It runs TwitchChannelPointsMiner.runner in-process, equivalent to
`python -m TwitchChannelPointsMiner.runner --config-dir <config dir>` from miner_dir.

When the flag file shows up, this triggers the same SIGINT handler the miner uses
for Ctrl+C (TwitchChannelPointsMiner.end): it leaves chat, closes the websockets,
waits for pending bets/JSON writes, prints the final report and exits with code 0
(end() calls sys.exit(0), which propagates through the runner unchanged).
The supervisor force-kills the process if that takes too long.

Needs Python 3.10+ (interrupt_main with a signal number).
"""

import _thread
import os
import runpy
import signal
import sys
import threading
import time


def main() -> None:
    if len(sys.argv) != 4:
        sys.exit("usage: miner_wrapper.py <miner_dir> <config_dir> <stop.flag>")

    miner_dir = os.path.abspath(sys.argv[1])
    config_dir = os.path.abspath(sys.argv[2])
    flag = os.path.abspath(sys.argv[3])

    # A leftover flag from an earlier stop must not kill this run straight away.
    try:
        os.remove(flag)
    except FileNotFoundError:
        pass

    def watch_for_stop() -> None:
        while not os.path.exists(flag):
            time.sleep(2)
        try:
            os.remove(flag)
        except OSError:
            pass
        print("miner_wrapper: stop requested - sending Ctrl+C to the miner", flush=True)
        # Runs the miner's installed SIGINT handler in the main thread.
        _thread.interrupt_main(signal.SIGINT)

    threading.Thread(
        target=watch_for_stop, name="stop-flag-watcher", daemon=True
    ).start()

    # Behave as if started as `python -m TwitchChannelPointsMiner.runner --config-dir ...` from miner_dir.
    # argv[0] is "-m" so the runner's restart_process() (os.execv of sys.argv) relaunches it as a module.
    os.chdir(miner_dir)
    sys.path[0] = miner_dir
    sys.argv = ["-m", "TwitchChannelPointsMiner.runner", "--config-dir", config_dir]
    runpy.run_module("TwitchChannelPointsMiner.runner", run_name="__main__")


if __name__ == "__main__":
    main()
