#!/usr/bin/env python3
"""Developer CLI for inspecting a twitchdrops.app game page."""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from TwitchChannelPointsMiner.classes.TwitchDropsApp import (
    TwitchDropsAppScraper,
    game_url,
    parse_game_page,
)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "category", help="category slug, name, or twitchdrops.app game URL"
    )
    parser.add_argument("--output", type=Path, help="JSON report path")
    parser.add_argument(
        "--input", type=Path, help="parse a saved HTML page instead of fetching"
    )
    parser.add_argument(
        "--timeout", type=float, default=20, help="request timeout in seconds"
    )
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    return args


def main():
    args = parse_args()
    try:
        url = game_url(args.category)
        if args.input:
            report = parse_game_page(args.input.read_text(encoding="utf-8"), url)
        else:
            report = TwitchDropsAppScraper(timeout=args.timeout).scrape(args.category)
    except (OSError, ValueError, requests.RequestException) as error:
        print(f"Scrape failed: {error}", file=sys.stderr)
        return 2

    report["generated_at"] = datetime.now(timezone.utc).isoformat()
    output = args.output
    if output is None:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        output = Path("logs") / f"twitchdrops-app-report-{timestamp}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(
        f"Scraped {report['campaign_count']} active campaigns and "
        f"{report['drop_count']} drops"
    )
    print(f"Saved report to {output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
