import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from TwitchChannelPointsMiner.classes.AnalyticsServer import (
    MINER_INSTANCE_HEADER,
    AnalyticsServer,
)
from TwitchChannelPointsMiner.classes.Settings import Settings

ROOT = Path(__file__).resolve().parents[1]


def test_every_response_names_the_server_process():
    server = AnalyticsServer(password=None)
    client = server.app.test_client()

    asset = client.head("/static/style.css")
    refused = client.post("/config/restart")

    assert asset.headers[MINER_INSTANCE_HEADER] == server.instance_id
    assert refused.headers[MINER_INSTANCE_HEADER] == server.instance_id


def test_each_server_process_gets_its_own_instance_id():
    first = AnalyticsServer(password=None)
    second = AnalyticsServer(password=None)

    assert first.instance_id != second.instance_id


def test_dashboard_page_embeds_the_instance_that_served_it(monkeypatch):
    monkeypatch.setattr(Settings, "logger", SimpleNamespace(date_format="dd/mm/yy"))
    server = AnalyticsServer(password=None)

    page = server.app.test_client().get("/").get_data(as_text=True)

    assert f'var minerInstance = "{server.instance_id}";' in page


# Runs waitForMinerRestart() from script.js with a fake clock, setTimeout and
# $.ajax. Each scripted poll answer is an instance id, null (no header) or
# "down" (no answer).
HARNESS = r"""
const input = JSON.parse(require("fs").readFileSync(0, "utf8"));
let now = 0;
const timers = [];
global.Date = { now: () => now };
global.setTimeout = (fn, ms) => timers.push({ at: now + ms, fn });
const answers = input.answers.slice();
let polls = 0;
global.$ = {
  ajax(options) {
    polls += 1;
    const answer = answers.length ? answers.shift() : input.rest;
    const handlers = {};
    const request = {
      done(fn) { handlers.done = fn; return request; },
      fail(fn) { handlers.fail = fn; return request; },
    };
    timers.push({ at: now, fn: () => {
      if (answer === "down") return handlers.fail({ status: 0 });
      handlers.done("", "success", {
        getResponseHeader: (name) =>
          name === "X-Miner-Instance" ? answer : null,
      });
    }});
    return request;
  },
};
eval(input.source);
let result = null;
waitForMinerRestart(
  input.previous,
  () => { result = "back"; },
  () => { result = "gave up"; },
);
while (result === null && timers.length) {
  timers.sort((a, b) => a.at - b.at);
  const timer = timers.shift();
  now = timer.at;
  timer.fn();
}
console.log(JSON.stringify({ result, polls, seconds: now / 1000 }));
"""


def _wait_for_restart(previous, answers, rest="old"):
    script = (ROOT / "assets" / "script.js").read_text(encoding="utf-8")
    source = script.split("var RESTART_POLL_INTERVAL_MS", 1)[1].split(
        "function restartMiner()", 1
    )[0]
    completed = subprocess.run(
        [shutil.which("node"), "-e", HARNESS],
        input=json.dumps(
            {
                "source": "var RESTART_POLL_INTERVAL_MS" + source,
                "previous": previous,
                "answers": answers,
                "rest": rest,
            }
        ),
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(completed.stdout)


needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="needs Node.js to run script.js"
)


@needs_node
def test_reload_waits_for_a_new_process_not_the_old_one_still_answering():
    outcome = _wait_for_restart("old", ["old", "old", "new"])

    assert outcome["result"] == "back"
    assert outcome["polls"] == 3


@needs_node
def test_reload_ignores_a_blip_where_the_old_process_answers_again():
    outcome = _wait_for_restart("old", ["old", "down", "old", "down", "new"])

    assert outcome["result"] == "back"
    assert outcome["polls"] == 5


@needs_node
def test_reload_waits_out_a_slow_start_instead_of_a_fixed_delay():
    # 30 s of no answer: longer than the old fixed 15 s reload.
    outcome = _wait_for_restart("old", ["old"] + ["down"] * 15 + ["new"])

    assert outcome["result"] == "back"
    assert outcome["seconds"] > 30


@needs_node
def test_without_the_header_the_server_coming_back_counts_as_the_restart():
    outcome = _wait_for_restart("old", [None, "down", None])

    assert outcome["result"] == "back"
    assert outcome["polls"] == 3


@needs_node
def test_without_a_page_instance_the_server_coming_back_counts_as_the_restart():
    outcome = _wait_for_restart(None, ["new", "down", "new"])

    assert outcome["result"] == "back"
    assert outcome["polls"] == 3


@needs_node
def test_reload_gives_up_after_three_minutes():
    outcome = _wait_for_restart("old", [], rest="old")

    assert outcome["result"] == "gave up"
    assert outcome["seconds"] == pytest.approx(180, abs=2)
