import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Runs restartMiner() from script.js against stubs: the confirm dialog is
# accepted at once, $.ajax answers with input.xhr (through its success
# callback when it is "success", its error callback otherwise), and
# waitForMinerRestart ends as input.probe says ("back", "gave_up", or left
# waiting). Every message, probe, reload and button change is recorded.
HARNESS = r"""
const input = JSON.parse(require("fs").readFileSync(0, "utf8"));
const record = { messages: [], probes: [], reloads: 0, disabled: [] };
global.minerInstance = "page-instance";
global.location = { reload: () => { record.reloads += 1; } };
global.showConfirmDialog = (options) => options.onConfirm();
global.showConfigMessage = (message, isError) =>
  record.messages.push({ message, isError });
global.waitForMinerRestart = (previousInstance, onBack, onGiveUp) => {
  record.probes.push(previousInstance);
  if (input.probe === "back") onBack();
  else if (input.probe === "gave_up") onGiveUp();
};
const button = {
  prop(name, value) { record.disabled.push(value); return button; },
};
global.$ = (selector) => button;
global.$.ajax = (options) => {
  if (input.xhr === "success")
    options.success({}, "success", {
      getResponseHeader: (name) =>
        name === "X-Miner-Instance" ? "accepting-instance" : null,
    });
  else options.error(input.xhr);
};
eval(input.source);
restartMiner();
console.log(JSON.stringify(record));
"""

needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="needs Node.js to run script.js"
)


def _click_restart(xhr, probe=None):
    script = (ROOT / "assets" / "script.js").read_text(encoding="utf-8")
    source = (
        "function restartMiner()"
        + script.split("function restartMiner()", 1)[1].split(
            "function saveSourceSettings", 1
        )[0]
    )
    completed = subprocess.run(
        [shutil.which("node"), "-e", HARNESS],
        input=json.dumps({"source": source, "xhr": xhr, "probe": probe}),
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(completed.stdout)


@needs_node
def test_no_answer_keeps_the_button_disabled_while_it_checks_for_a_restart():
    # Status 0 is ambiguous: the request may never have reached the miner,
    # or the response was lost after it accepted the restart. Re-enabling
    # the button now could restart the new process a second time.
    record = _click_restart({"status": 0})

    assert record["probes"] == ["page-instance"]
    assert record["disabled"] == [True]
    assert record["reloads"] == 0
    assert record["messages"] == [
        {
            "message": "No answer from the miner. Checking whether it is restarting...",
            "isError": False,
        }
    ]


@needs_node
def test_no_answer_reloads_when_a_new_instance_answers():
    record = _click_restart({"status": 0}, probe="back")

    assert record["probes"] == ["page-instance"]
    assert record["disabled"] == [True]
    assert record["reloads"] == 1
    assert all(not message["isError"] for message in record["messages"])


@needs_node
def test_no_answer_and_no_new_instance_seen_is_reported_as_unconfirmed():
    # Giving up doesn't prove the miner didn't restart (a proxy can strip
    # X-Miner-Instance and the downtime can fall between probes), so the
    # message must not claim it didn't.
    record = _click_restart({"status": 0}, probe="gave_up")

    assert record["disabled"] == [True, False]
    assert record["reloads"] == 0
    assert record["messages"][-1]["isError"] is True
    message = record["messages"][-1]["message"]
    assert "Could not confirm that the miner restarted" in message
    assert "did not restart" not in message


@needs_node
def test_server_error_reports_its_message_and_reenables_the_button():
    record = _click_restart(
        {"status": 503, "responseJSON": {"error": "Restart is unavailable."}}
    )

    assert record["probes"] == []
    assert record["disabled"] == [True, False]
    assert record["messages"] == [
        {"message": "Restart is unavailable.", "isError": True}
    ]


@needs_node
def test_accepted_restart_waits_for_an_instance_other_than_the_one_that_accepted():
    record = _click_restart("success", probe="back")

    assert record["probes"] == ["accepting-instance"]
    assert record["disabled"] == [True]
    assert record["reloads"] == 1
    assert record["messages"][0]["isError"] is False
