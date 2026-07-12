#!/usr/bin/env python3
"""Scrape active Twitch Drops campaigns from a twitchdrops.app game page."""

import argparse
import hashlib
import html
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

import requests


BASE_URL = "https://twitchdrops.app/game/"
TWITCH_LOGIN_PATTERN = re.compile(
    r'href=["\']https?://(?:www\.)?twitch\.tv/([^?"\'/#]+)[^"\']*["\']',
    re.IGNORECASE,
)


def clean_text(value):
    value = re.sub(r"<[^>]+>", " ", value or "")
    return " ".join(html.unescape(value).split())


def first_match(pattern, value, default=None, flags=re.DOTALL):
    match = re.search(pattern, value, flags)
    return clean_text(match.group(1)) if match else default


def game_url(category):
    category = str(category).strip()
    if category.startswith("http://") or category.startswith("https://"):
        parsed = urlparse(category)
        if parsed.netloc.lower() not in {"twitchdrops.app", "www.twitchdrops.app"}:
            raise ValueError("game URL must use twitchdrops.app")
        if not parsed.path.startswith("/game/"):
            raise ValueError("game URL must use the /game/<category> path")
        return f"https://twitchdrops.app{parsed.path.rstrip('/')}"
    slug = category.lower().strip(" /").replace(" ", "-")
    if not slug:
        raise ValueError("category cannot be empty")
    return BASE_URL + quote(slug, safe="-")


def section(source, heading, next_headings):
    start = re.search(rf"<h2[^>]*>\s*{re.escape(heading)}\s*</h2>", source, re.I)
    if not start:
        return ""
    end = len(source)
    for next_heading in next_headings:
        match = re.search(
            rf"<h2[^>]*>\s*{re.escape(next_heading)}\s*</h2>",
            source[start.end() :],
            re.I,
        )
        if match:
            end = min(end, start.end() + match.start())
    return source[start.end() : end]


def div_blocks(source, class_name):
    opening = re.compile(
        rf'<div\b[^>]*class=["\'][^"\']*\b{re.escape(class_name)}\b[^"\']*["\'][^>]*>',
        re.I,
    )
    tag = re.compile(r"</?div\b[^>]*>", re.I)
    for match in opening.finditer(source):
        depth = 1
        for candidate in tag.finditer(source, match.end()):
            depth += -1 if candidate.group(0).startswith("</") else 1
            if depth == 0:
                yield source[match.start() : candidate.end()]
                break


def parse_drop(block):
    image_match = re.search(r'<img\b[^>]*\bsrc=["\']([^"\']+)', block, re.I)
    return {
        "name": first_match(
            r'class=["\'][^"\']*\bdrop-name\b[^"\']*["\'][^>]*>(.*?)</div>', block
        ),
        "requirement": first_match(
            r'class=["\'][^"\']*\bdrop-time\b[^"\']*["\'][^>]*>(.*?)</div>', block
        ),
        "campaign": first_match(
            r'class=["\'][^"\']*\bdrop-campaign\b[^"\']*["\'][^>]*>(.*?)</div>', block
        ),
        "image_url": html.unescape(image_match.group(1)) if image_match else None,
    }


def parse_campaign(block, game_name):
    name = first_match(
        r'class=["\'][^"\']*\bcb-name\b[^"\']*["\'][^>]*>(.*?)</span>', block
    )
    end_match = re.search(r'\bdata-end-ts=["\'](\d+)["\']', block, re.I)
    end_timestamp = int(end_match.group(1)) if end_match else None
    channels_block = first_match(
        r'class=["\'][^"\']*\bcb-channels\b[^"\']*["\'][^>]*>(.*?)</div>',
        block,
        default="",
    )
    all_channels = "All Channels" in channels_block
    logins = [] if all_channels else TWITCH_LOGIN_PATTERN.findall(block)
    logins = list(dict.fromkeys(login.lower() for login in logins))
    identity = "|".join([game_name or "", name or "", str(end_timestamp or "")])
    return {
        "id": "twitchdrops-app-" + hashlib.sha256(identity.encode()).hexdigest()[:16],
        "name": name,
        "owner": first_match(
            r'class=["\'][^"\']*\bcb-owner\b[^"\']*["\'][^>]*>(.*?)</span>', block
        ),
        "dates": first_match(
            r'class=["\'][^"\']*\bcb-dates\b[^"\']*["\'][^>]*>(.*?)</span>', block
        ),
        "ends_at": (
            datetime.fromtimestamp(end_timestamp / 1000, timezone.utc).isoformat()
            if end_timestamp
            else None
        ),
        "all_channels": all_channels,
        "channels": logins,
        "description": first_match(
            r'class=["\'][^"\']*\bcb-desc\b[^"\']*["\'][^>]*>(.*?)</div>', block
        ),
    }


def parse_game_page(source, url):
    game_name = first_match(r"<main\b.*?<h1[^>]*>(.*?)</h1>", source)
    if not game_name:
        raise ValueError("page does not contain a game heading")
    active_source = section(
        source,
        "Active Campaigns",
        [
            "How to get these drops",
            "Upcoming Campaigns",
            "Past Drops",
            "Past Campaigns",
        ],
    )
    active_heading = re.search(
        r"<h2[^>]*>\s*Active Campaigns\s*</h2>", source, re.IGNORECASE
    )
    viewer_source = source[: active_heading.start()] if active_heading else source
    drops = [parse_drop(block) for block in div_blocks(viewer_source, "drop-card")]
    drops = [drop for drop in drops if drop["name"]]
    campaign_blocks = list(div_blocks(active_source, "campaign-banner"))
    all_campaigns = [parse_campaign(block, game_name) for block in campaign_blocks]
    watch_campaign_names = {
        drop["campaign"].casefold()
        for drop in drops
        if drop["campaign"]
        and (drop["requirement"] or "").casefold().startswith("watch ")
    }
    campaigns = [
        campaign
        for campaign in all_campaigns
        if (campaign["name"] or "").casefold() in watch_campaign_names
    ]
    for campaign in campaigns:
        campaign_name = (campaign["name"] or "").casefold()
        campaign["drops"] = [
            drop
            for drop in drops
            if (drop["campaign"] or "").casefold() == campaign_name
        ]
    non_watch_campaigns = [
        campaign for campaign in all_campaigns if campaign not in campaigns
    ]
    return {
        "source": url,
        "game": game_name,
        "campaign_count": len(campaigns),
        "non_watch_campaign_count": len(non_watch_campaigns),
        "drop_count": len(drops),
        "campaigns": campaigns,
        "non_watch_campaigns": non_watch_campaigns,
        "drops": drops,
    }


class TwitchDropsAppScraper(object):
    __slots__ = ["session", "timeout"]

    def __init__(self, session=None, timeout=20):
        self.session = session or requests.Session()
        self.timeout = timeout

    def scrape(self, category):
        url = game_url(category)
        response = self.session.get(
            url,
            timeout=self.timeout,
            headers={"Accept": "text/html", "User-Agent": "TwitchDropsAppScraper/1.0"},
        )
        response.raise_for_status()
        return parse_game_page(response.text, url)


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
        f"Scraped {report['campaign_count']} active campaigns and {report['drop_count']} drops"
    )
    print(f"Saved report to {output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
