#!/usr/bin/env python3
"""Print drop-related data found in a browser HAR file.

Only response bodies are inspected. Request headers, cookies, and POST bodies are
deliberately ignored so the report is safer to share than the original HAR.
"""

import argparse
import base64
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterator

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from TwitchChannelPointsMiner.classes.TwitchDropsApp import (
    div_blocks,
    first_match,
    parse_campaign,
    parse_drop,
)


DROP_WORDS = ("campaign", "drop", "reward", "benefit")
IDENTITY_KEYS = {
    "id",
    "name",
    "title",
    "displayname",
    "campaignid",
    "dropid",
    "rewardid",
    "benefitid",
}


def compact_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def related_word(value: object) -> str | None:
    key = compact_key(value)
    return next((word for word in DROP_WORDS if word in key), None)


def walk_json(value: Any, path: tuple[str, ...] = ()) -> Iterator[dict]:
    """Yield useful campaign/drop/reward/benefit objects with their JSON path."""
    if isinstance(value, dict):
        keys = {compact_key(key) for key in value}
        path_kind = next(
            (related_word(part) for part in reversed(path) if related_word(part)),
            None,
        )
        key_kind = next(
            (related_word(key) for key in value if related_word(key)), None
        )
        has_identity = bool(keys & IDENTITY_KEYS)
        # A parent container such as {"campaigns": [...]} is not itself a
        # campaign. Requiring an identity also avoids printing most GraphQL
        # wrapper objects repeatedly.
        if has_identity and len(value) > 1 and (path_kind or key_kind):
            yield {
                "kind": path_kind or key_kind or "drop-related",
                "path": json_path(path),
                "attributes": value,
            }
        for key, child in value.items():
            yield from walk_json(child, path + (str(key),))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk_json(child, path + (f"[{index}]",))


def json_path(parts: tuple[str, ...]) -> str:
    result = "$"
    for part in parts:
        result += part if part.startswith("[") else f".{part}"
    return result


def decode_content(content: dict) -> str | None:
    text = content.get("text")
    if not isinstance(text, str):
        return None
    if str(content.get("encoding", "")).casefold() != "base64":
        return text
    try:
        return base64.b64decode(text, validate=False).decode("utf-8", "replace")
    except (ValueError, TypeError):
        return None


def parse_json_body(body: str) -> Any | None:
    try:
        return json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return None


def html_records(body: str, url: str) -> list[dict]:
    if "drop-card" not in body and "campaign-banner" not in body:
        return []
    game_name = first_match(r"<main\b.*?<h1[^>]*>(.*?)</h1>", body)
    records = []
    for index, block in enumerate(div_blocks(body, "drop-card")):
        drop = parse_drop(block)
        records.append(
            {
                "kind": "drop",
                "path": f"html.drops[{index}]",
                "attributes": drop,
            }
        )
    for index, block in enumerate(div_blocks(body, "campaign-banner")):
        records.append(
            {
                "kind": "campaign",
                "path": f"html.campaigns[{index}]",
                "attributes": parse_campaign(block, game_name),
            }
        )
    return records


def inspect_har(har: dict, url_filter: re.Pattern | None = None) -> list[dict]:
    entries = har.get("log", {}).get("entries", [])
    if not isinstance(entries, list):
        raise ValueError("HAR does not contain log.entries")

    findings = []
    seen = set()
    for entry_index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        request = entry.get("request", {})
        response = entry.get("response", {})
        url = str(request.get("url") or "")
        if url_filter and not url_filter.search(url):
            continue
        content = response.get("content", {})
        body = decode_content(content) if isinstance(content, dict) else None
        if body is None:
            continue

        parsed = parse_json_body(body)
        records = list(walk_json(parsed)) if parsed is not None else []
        records.extend(html_records(body, url))
        for record in records:
            fingerprint = json.dumps(record["attributes"], sort_keys=True, default=str)
            fingerprint = (url, record["kind"], fingerprint)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            findings.append(
                {
                    "source": {
                        "entry": entry_index,
                        "url": url,
                        "status": response.get("status"),
                        "mime_type": content.get("mimeType"),
                    },
                    **record,
                }
            )
    return findings


def print_findings(findings: list[dict]) -> None:
    if not findings:
        print("No drop-related objects were found in response bodies.")
        return
    for index, finding in enumerate(findings, 1):
        source = finding["source"]
        print("=" * 88)
        print(f"[{index}] {finding['kind'].upper()}  {finding['path']}")
        print(f"URL:    {source['url']}")
        print(f"HTTP:   {source['status']}  MIME: {source['mime_type'] or 'unknown'}")
        print("Attributes:")
        print(json.dumps(finding["attributes"], indent=2, ensure_ascii=False))
    print("=" * 88)
    print(f"Found {len(findings)} unique drop-related objects.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("har", type=Path, help="HAR file exported with content")
    parser.add_argument(
        "--url",
        metavar="REGEX",
        help="only inspect responses whose request URL matches this regex",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the complete report as machine-readable JSON",
    )
    args = parser.parse_args()

    try:
        with args.har.open("r", encoding="utf-8-sig") as file:
            har = json.load(file)
        url_filter = re.compile(args.url, re.IGNORECASE) if args.url else None
        findings = inspect_har(har, url_filter)
    except (OSError, json.JSONDecodeError, ValueError, re.error) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(findings, indent=2, ensure_ascii=False))
    else:
        print_findings(findings)
    return 0 if findings else 1


if __name__ == "__main__":
    raise SystemExit(main())
