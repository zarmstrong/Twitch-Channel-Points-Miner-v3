"""Parse legacy pages and fetch normalized Twitch Drops data from a gist."""

import copy
import hashlib
import html
import re
from datetime import datetime, timezone
from urllib.parse import quote, urlparse

import requests

BASE_URL = "https://twitchdrops.app/game/"
HOME_URL = "https://twitchdrops.app/"
DROPS_GIST_URL = (
    "https://gist.githubusercontent.com/zarmstrong/"
    "72433778ae596815f4c6ff5e1d278cd2/raw/twitch-drops.json"
)
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


def is_watch_drop(drop):
    return (drop.get("requirement") or "").casefold().startswith("watch ")


def parse_campaign(block, game_name, starts_at=None):
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
        "starts_at": starts_at,
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
        "drops": [],
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
    upcoming_source = section(
        source,
        "Upcoming Campaigns",
        ["Past Drops", "Past Campaigns", "Frequently Asked Questions"],
    )
    content_boundary = re.search(
        r"<h2[^>]*>\s*(?:Active Campaigns|How to get these drops|"
        r"Upcoming Campaigns|Past Drops|Past Campaigns)\s*</h2>",
        source,
        re.IGNORECASE,
    )
    viewer_source = source[: content_boundary.start()] if content_boundary else source
    drops = [parse_drop(block) for block in div_blocks(viewer_source, "drop-card")]
    drops = [drop for drop in drops if drop["name"]]
    campaign_blocks = list(div_blocks(active_source, "campaign-banner"))
    all_campaigns = [parse_campaign(block, game_name) for block in campaign_blocks]
    page_path = urlparse(url).path.rstrip("/")
    page_timing = re.search(
        rf'<a\b[^>]*href=["\']{re.escape(page_path)}["\'][^>]*'
        r'data-end=["\']([^"\']+)["\'][^>]*data-start=["\']([^"\']+)["\']',
        source,
        re.IGNORECASE,
    )
    page_start = page_timing.group(2) if page_timing else None
    upcoming_campaigns = [
        parse_campaign(block, game_name, starts_at=page_start)
        for block in div_blocks(upcoming_source, "campaign-banner")
    ]
    unassigned_watch_drops = [
        drop for drop in drops if not drop["campaign"] and is_watch_drop(drop)
    ]
    if len(all_campaigns) == 1 and unassigned_watch_drops:
        campaign_name = all_campaigns[0]["name"]
        for drop in unassigned_watch_drops:
            drop["campaign"] = campaign_name
    watch_campaign_names = {
        drop["campaign"].casefold()
        for drop in drops
        if drop["campaign"] and is_watch_drop(drop)
    }
    for campaign in all_campaigns:
        campaign_name = (campaign["name"] or "").casefold()
        campaign["drops"] = [
            drop
            for drop in drops
            if (drop["campaign"] or "").casefold() == campaign_name
        ]
    campaigns = [
        campaign
        for campaign in all_campaigns
        if (campaign["name"] or "").casefold() in watch_campaign_names
    ]
    for campaign in campaigns:
        campaign["drops"] = [drop for drop in campaign["drops"] if is_watch_drop(drop)]
    if len(upcoming_campaigns) == 1:
        upcoming_campaigns[0]["drops"] = [drop for drop in drops if is_watch_drop(drop)]
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
        "upcoming_campaigns": upcoming_campaigns,
        "non_watch_campaigns": non_watch_campaigns,
        "drops": drops,
    }


def parse_front_page(source):
    """Return active and upcoming games advertised by the site index."""
    games = []
    seen = set()
    for block in re.findall(
        r'<a\b[^>]*class=["\'][^"\']*\bgame-card\b[^"\']*["\'][^>]*>',
        source,
        re.IGNORECASE,
    ):
        if "game-card--expired" in block:
            continue

        def attribute(name):
            match = re.search(
                rf'\b{re.escape(name)}=["\']([^"\']*)["\']', block, re.IGNORECASE
            )
            return html.unescape(match.group(1)) if match else None

        slug = attribute("data-slug")
        href = attribute("href")
        if not slug or not href or slug in seen:
            continue
        seen.add(slug)
        games.append(
            {
                "slug": slug,
                "game": attribute("data-game"),
                "url": f"https://twitchdrops.app{href}",
                "starts_at": attribute("data-start"),
                "ends_at": attribute("data-end"),
                "upcoming": " upcoming" in block,
                "drop_count": (
                    int(attribute("data-drops"))
                    if str(attribute("data-drops") or "").isdigit()
                    else None
                ),
            }
        )
    return games


class TwitchDropsGistScraper(object):
    __slots__ = ["session", "timeout", "_payload"]

    def __init__(self, session=None, timeout=20):
        self.session = session or requests.Session()
        self.timeout = timeout
        self._payload = None

    def _fetch(self):
        if self._payload is not None:
            return self._payload
        response = self.session.get(
            DROPS_GIST_URL,
            timeout=self.timeout,
            headers={
                "Accept": "application/json",
                "User-Agent": "TwitchDropsMiner/1.0",
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Twitch Drops gist did not contain a JSON object")
        if not isinstance(payload.get("indexed_games"), list) or not isinstance(
            payload.get("games"), list
        ):
            raise ValueError("Twitch Drops gist is missing indexed_games or games")
        self._payload = payload
        return payload

    def scrape(self, category):
        url = game_url(category)
        slug = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
        for report in self._fetch()["games"]:
            if not isinstance(report, dict):
                continue
            source = str(report.get("source") or "")
            report_slug = urlparse(source).path.rstrip("/").rsplit("/", 1)[-1]
            if report_slug == slug:
                return copy.deepcopy(report)
        raise ValueError(f"Twitch Drops gist does not contain game '{slug}'")

    def scrape_front_page(self):
        return copy.deepcopy(self._fetch()["indexed_games"])


# Preserve the public import used by developer tools and downstream integrations.
TwitchDropsAppScraper = TwitchDropsGistScraper
