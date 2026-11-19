"""Merge GitHub's latest 14-day traffic counts into the badge gist's history.

Used by the clone and traffic badge workflows:

    python3 .github/scripts/merge_traffic_counts.py clones clone.json clone_before.json
    python3 .github/scripts/merge_traffic_counts.py views traffic.json traffic_before.json

The first file holds GitHub's latest response and is overwritten with the
merged history. The second holds the history read from the gist.

Adapted from MShawon/github-clone-count-badge (main.py at 5ab676b and the
traffic.py gist at 5f30c10). The workflows used to download and run those
files on every run. The merge behaves the same, so the gists' history carries
on unchanged.

MIT License

Copyright (c) 2021 Monirul Shawon

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

import json
import sys

# Once the history holds more than this many entries, the older ones are
# folded into one entry per month. The newest KEEP_DAILY stay daily.
COMPACT_AFTER = 100
KEEP_DAILY = 35


def merge_counts(now: dict, before: dict, key: str) -> dict:
    """Return `before` updated with the entries in `now`.

    `key` is "clones" or "views". An entry for a timestamp already in the
    history replaces it, and a new one is appended. `count` and `uniques` are
    the totals over the whole history.
    """
    timestamps = {entry["timestamp"]: index for index, entry in enumerate(before[key])}

    latest = dict(before)
    for entry in now[key]:
        if entry["timestamp"] in timestamps:
            latest[key][timestamps[entry["timestamp"]]] = entry
        else:
            latest[key].append(entry)

    latest["count"] = sum(int(entry["count"]) for entry in latest[key])
    latest["uniques"] = sum(int(entry["uniques"]) for entry in latest[key])

    # Like the original, this counts the history read from the gist, not the
    # merged one, and the totals above include the entries before folding.
    if len(timestamps) > COMPACT_AFTER:
        entries = latest[key]
        folded = []
        for index in range(len(timestamps) - KEEP_DAILY):
            entries[index]["timestamp"] = entries[index]["timestamp"][:7]
            if entries[index]["timestamp"] == entries[index + 1]["timestamp"][:7]:
                entries[index + 1]["count"] += entries[index]["count"]
                entries[index + 1]["uniques"] += entries[index]["uniques"]
                folded.append(entries[index])

        for entry in folded:
            entries.remove(entry)

    return latest


def main(argv) -> int:
    if len(argv) != 4 or argv[1] not in ("clones", "views"):
        print(
            "usage: merge_traffic_counts.py clones|views LATEST_JSON HISTORY_JSON",
            file=sys.stderr,
        )
        return 2
    key, latest_path, history_path = argv[1:]

    with open(latest_path, "r", encoding="utf-8") as fh:
        now = json.load(fh)
    with open(history_path, "r", encoding="utf-8") as fh:
        before = json.load(fh)

    latest = merge_counts(now, before, key)

    with open(latest_path, "w", encoding="utf-8") as fh:
        json.dump(latest, fh, ensure_ascii=False, indent=4)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
