import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Runs restartMiner() from script.js against stubs: the confirm dialog is
# accepted at once, $.ajax answers with input.xhr through its error callback,
# and every message, timer and button change is recorded.
HARNESS = r"""
const input = JSON.parse(require("fs").readFileSync(0, "utf8"));
const record = { messages: [], timers: 0, disabled: [] };
global.setTimeout = () => { record.timers += 1; };
global.showConfirmDialog = (options) => options.onConfirm();
global.showConfigMessage = (message, isError) =>
  record.messages.push({ message, isError });
global.waitForMinerRestart = () => { record.timers += 1; };
const button = {
  prop(name, value) { record.disabled.push(value); return button; },
};
global.$ = (selector) => button;
global.$.ajax = (options) => {
  if (input.xhr === "success") options.success();
  else options.error(input.xhr);
};
eval(input.source);
restartMiner();
console.log(JSON.stringify(record));
"""

needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="needs Node.js to run script.js"
)


def _click_restart(xhr):
    script = (ROOT / "assets" / "script.js").read_text(encoding="utf-8")
    source = (
        "function restartMiner()"
        + script.split("function restartMiner()", 1)[1].split(
            "function saveSourceSettings", 1
        )[0]
    )
    completed = subprocess.run(
        [shutil.which("node"), "-e", HARNESS],
        input=json.dumps({"source": source, "xhr": xhr}),
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(completed.stdout)


@needs_node
def test_no_answer_is_reported_as_a_failure_not_a_restart():
    # Status 0 also means the request never reached the miner (connection
    # lost, proxy down), so it must not report success and wait to reload.
    record = _click_restart({"status": 0})

    assert record["timers"] == 0
    assert record["disabled"] == [True, False]
    assert len(record["messages"]) == 1
    assert record["messages"][0]["isError"] is True
    assert "may not have been requested" in record["messages"][0]["message"]


@needs_node
def test_server_error_reports_its_message_and_reenables_the_button():
    record = _click_restart(
        {"status": 503, "responseJSON": {"error": "Restart is unavailable."}}
    )

    assert record["timers"] == 0
    assert record["disabled"] == [True, False]
    assert record["messages"] == [
        {"message": "Restart is unavailable.", "isError": True}
    ]


@needs_node
def test_accepted_restart_reports_success_and_waits_for_the_miner():
    record = _click_restart("success")

    assert record["timers"] == 1
    assert record["disabled"] == [True]
    assert record["messages"][0]["isError"] is False
