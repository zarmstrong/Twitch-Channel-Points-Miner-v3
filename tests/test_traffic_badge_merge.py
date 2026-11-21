"""The badge workflows' traffic-count merge (.github/scripts/merge_traffic_counts.py)
and the workflows that run it (#266)."""

import datetime
import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "merge_traffic_counts.py"
WORKFLOWS = {
    "clones": ROOT / ".github" / "workflows" / "github-clone-count-badge.yml",
    "views": ROOT / ".github" / "workflows" / "github-traffic-count-badge.yml",
}

spec = importlib.util.spec_from_file_location("merge_traffic_counts", SCRIPT)
merge_traffic_counts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(merge_traffic_counts)


def day(offset):
    date = datetime.date(2024, 1, 1) + datetime.timedelta(days=offset)
    return f"{date.isoformat()}T00:00:00Z"


def entry(offset, count=1, uniques=1):
    return {"timestamp": day(offset), "count": count, "uniques": uniques}


@pytest.mark.parametrize("key", ["clones", "views"])
def test_new_days_are_appended_and_known_days_replaced(key):
    before = {"count": 3, "uniques": 3, key: [entry(0), entry(1), entry(2)]}
    now = {"count": 9, "uniques": 9, key: [entry(2, 5, 4), entry(3, 2, 2)]}

    latest = merge_traffic_counts.merge_counts(now, before, key)

    assert latest[key] == [entry(0), entry(1), entry(2, 5, 4), entry(3, 2, 2)]
    assert latest["count"] == 9
    assert latest["uniques"] == 8


def test_an_empty_history_takes_the_latest_counts():
    now = {"count": 4, "uniques": 2, "clones": [entry(0, 3, 1), entry(1, 1, 1)]}

    latest = merge_traffic_counts.merge_counts(
        now, {"count": 0, "uniques": 0, "clones": []}, "clones"
    )

    assert latest["clones"] == now["clones"]
    assert (latest["count"], latest["uniques"]) == (4, 2)


def test_a_history_of_exactly_100_days_is_not_folded():
    before = {"count": 0, "uniques": 0, "views": [entry(d) for d in range(100)]}

    latest = merge_traffic_counts.merge_counts({"views": [entry(100)]}, before, "views")

    assert [e["timestamp"] for e in latest["views"]] == [day(d) for d in range(101)]


def test_a_long_history_folds_older_days_into_months_as_upstream_did():
    # 101 days from 2024-01-01 (to 2024-04-10), plus one new day. The first
    # 101 - 35 = 66 entries are truncated to their month; each one whose month
    # matches the next entry's is folded into it. Entry 65 (2024-03-06)
    # matches 2024-03-07, so March folds into that still-daily entry. The
    # totals are taken before folding.
    before = {"count": 0, "uniques": 0, "clones": [entry(d) for d in range(101)]}
    now = {"clones": [entry(101, 2, 1)]}

    latest = merge_traffic_counts.merge_counts(now, before, "clones")

    clones = latest["clones"]
    assert clones[0] == {"timestamp": "2024-01", "count": 31, "uniques": 31}
    assert clones[1] == {"timestamp": "2024-02", "count": 29, "uniques": 29}
    assert clones[2] == {"timestamp": day(66), "count": 7, "uniques": 7}
    assert clones[3:] == [entry(d) for d in range(67, 101)] + [entry(101, 2, 1)]
    assert sum(e["count"] for e in clones) == 103
    assert (latest["count"], latest["uniques"]) == (103, 102)


def test_main_overwrites_the_latest_file_with_the_merged_history(tmp_path):
    latest_path = tmp_path / "traffic.json"
    history_path = tmp_path / "traffic_before.json"
    latest_path.write_text(json.dumps({"views": [entry(1, 2, 2)]}))
    history_path.write_text(json.dumps({"views": [entry(0)]}))

    code = merge_traffic_counts.main(
        ["merge", "views", str(latest_path), str(history_path)]
    )

    assert code == 0
    merged = json.loads(latest_path.read_text(encoding="utf-8"))
    assert merged == {"views": [entry(0), entry(1, 2, 2)], "count": 3, "uniques": 3}
    assert json.loads(history_path.read_text()) == {"views": [entry(0)]}


@pytest.mark.parametrize("argv", [["merge"], ["merge", "stars", "a", "b"]])
def test_main_rejects_bad_arguments(argv, capsys):
    assert merge_traffic_counts.main(argv) == 2
    assert "usage" in capsys.readouterr().err


@pytest.mark.parametrize("key", sorted(WORKFLOWS))
def test_badge_workflow_runs_no_downloaded_code_and_keeps_the_token_scoped(key):
    text = WORKFLOWS[key].read_text(encoding="utf-8")

    # No code is fetched from another repository or gist and then run.
    assert "raw.githubusercontent.com" not in text
    assert "gist.githubusercontent.com" not in text
    assert f"merge_traffic_counts.py {key} " in text
    # The checkout's token is not left in git config, and nothing is pushed.
    assert "persist-credentials: false" in text
    assert re.search(r"^permissions:\n  contents: read$", text, re.MULTILINE)
    assert "git push" not in text
    # Secrets reach commands only through env, never expanded inside run:.
    assert "github.actor" not in text
    for line in text.splitlines():
        if "secrets." in line:
            assert re.match(r"^\s+[A-Z_]+: \$\{\{ secrets\.[A-Z_]+ \}\}$", line), line
