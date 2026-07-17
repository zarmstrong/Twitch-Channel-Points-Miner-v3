#!/usr/bin/env python3
"""Catalog twitchdrops.app rewards and confirm badges with Twitch Helix."""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from TwitchChannelPointsMiner.classes.Twitch import Twitch
from TwitchChannelPointsMiner.classes.DropBadgeCatalog import (
    enrich_game_report,
    flatten_badges,
)
from TwitchChannelPointsMiner.classes.TwitchDropsApp import TwitchDropsAppScraper
from TwitchChannelPointsMiner.constants import CLIENT_ID, USER_AGENTS


CACHE_VERSION = 1
WEB_USER_AGENT = USER_AGENTS["Windows"]["CHROME"]
GLOBAL_BADGES_URL = "https://api.twitch.tv/helix/chat/badges/global"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def parse_timestamp(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def cache_entry_is_fresh(entry: dict | None, ttl_hours: float) -> bool:
    if not isinstance(entry, dict) or "data" not in entry:
        return False
    fetched_at = parse_timestamp(entry.get("fetched_at"))
    if fetched_at is None:
        return False
    return (utc_now() - fetched_at).total_seconds() < ttl_hours * 3600


def load_cache(path: Path) -> dict:
    if not path.is_file():
        return {"version": CACHE_VERSION, "sources": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid cache file {path}: {error}") from error
    if data.get("version") != CACHE_VERSION:
        return {"version": CACHE_VERSION, "sources": {}}
    data.setdefault("sources", {})
    return data


def save_cache(path: Path, cache: dict) -> None:
    cache["version"] = CACHE_VERSION
    cache["saved_at"] = iso_now()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    os.replace(temporary, path)


def cached_value(
    sources: dict,
    key: str,
    ttl_hours: float,
    refresh: bool,
    fetch,
) -> tuple[object, dict]:
    entry = sources.get(key)
    if not refresh and cache_entry_is_fresh(entry, ttl_hours):
        return entry["data"], {
            "key": key,
            "status": "fresh_cache",
            "fetched_at": entry.get("fetched_at"),
        }
    try:
        data = fetch()
    except (OSError, requests.RequestException, ValueError) as error:
        if isinstance(entry, dict) and "data" in entry:
            return entry["data"], {
                "key": key,
                "status": "stale_cache_after_error",
                "fetched_at": entry.get("fetched_at"),
                "error": f"{type(error).__name__}: {error}",
            }
        raise
    sources[key] = {"fetched_at": iso_now(), "data": data}
    return data, {
        "key": key,
        "status": "refreshed",
        "fetched_at": sources[key]["fetched_at"],
    }


def load_miner_auth(username: str, cookie_file: Path) -> tuple[Twitch, str]:
    twitch = Twitch(username, WEB_USER_AGENT)
    twitch.cookies_file = str(cookie_file)
    twitch.gql.on_unauthorized = None
    twitch.twitch_login.load_cookies(twitch.cookies_file)
    token = twitch.twitch_login.get_auth_token()
    if not token:
        raise ValueError("cookie file did not contain an auth token")
    return twitch, token


def fetch_global_badges(token: str, session=None) -> list[dict]:
    session = session or requests.Session()
    response = session.get(
        GLOBAL_BADGES_URL,
        headers={"Authorization": f"Bearer {token}", "Client-Id": CLIENT_ID},
        timeout=(5, 30),
    )
    response.raise_for_status()
    payload = response.json()
    badges = payload.get("data")
    if not isinstance(badges, list):
        raise ValueError("Twitch global badge response did not contain data")
    return badges


def catalog_counts(game_reports: list[dict]) -> dict[str, int]:
    unique_drops = {}
    campaigns = 0
    for report in game_reports:
        campaigns += len(report.get("campaigns", []) or [])
        campaigns += len(report.get("upcoming_campaigns", []) or [])
        for drop in report.get("drops", []) or []:
            key = (report.get("game"), drop.get("name"), drop.get("requirement"))
            unique_drops[key] = drop
    badge_count = sum(
        drop.get("badge_classification", {}).get("status") == "BADGE"
        for drop in unique_drops.values()
    )
    return {
        "games": len(game_reports),
        "campaigns": campaigns,
        "drops": len(unique_drops),
        "badges": badge_count,
        "unknown": len(unique_drops) - badge_count,
    }


def print_catalog(game_reports: list[dict]) -> None:
    for report in game_reports:
        drops = report.get("drops", []) or []
        if not drops:
            continue
        print(f"\n{report.get('game')}")
        for drop in drops:
            classification = drop.get("badge_classification", {})
            marker = " [BADGE]" if classification.get("status") == "BADGE" else ""
            requirement = drop.get("requirement") or "unknown"
            print(f"  {drop.get('name')}{marker} — {requirement}")
            for match in classification.get("matches", []):
                print(
                    f"    Twitch badge: {match.get('title')} "
                    f"(set={match.get('set_id')}, match={match.get('reason')})"
                )


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("username", help="Twitch username associated with the cookie")
    parser.add_argument(
        "--cookie-file",
        type=Path,
        help="miner cookie file (default: cookies/<username>.pkl)",
    )
    parser.add_argument(
        "--cache-file",
        type=Path,
        default=Path(".cache/twitch-drop-badge-catalog.json"),
        help="persistent source cache",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="JSON report path (default: logs/twitch-drop-badges-<timestamp>.json)",
    )
    parser.add_argument(
        "--site-cache-hours",
        type=float,
        default=6,
        help="reuse twitchdrops.app data for this many hours (default: 6)",
    )
    parser.add_argument(
        "--badge-cache-hours",
        type=float,
        default=24,
        help="reuse Twitch badge data for this many hours (default: 24)",
    )
    parser.add_argument(
        "--request-delay",
        type=float,
        default=0.25,
        help="delay between uncached game-page requests (default: 0.25)",
    )
    parser.add_argument(
        "--game",
        action="append",
        default=[],
        help="only process this twitchdrops.app slug; may be repeated",
    )
    parser.add_argument("--refresh", action="store_true", help="ignore fresh cache")
    parser.add_argument("--quiet", action="store_true", help="only print totals")
    args = parser.parse_args()
    for name in ("site_cache_hours", "badge_cache_hours", "request_delay"):
        if getattr(args, name) < 0:
            parser.error(f"--{name.replace('_', '-')} cannot be negative")
    return args


def main() -> int:
    args = parse_args()
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output = args.output or Path("logs") / f"twitch-drop-badges-{timestamp}.json"
    cookie_file = (
        args.cookie_file.expanduser().resolve()
        if args.cookie_file
        else Path("cookies") / f"{args.username}.pkl"
    )
    try:
        cache = load_cache(args.cache_file)
        sources = cache["sources"]
        cache_events = []

        def refresh_badges():
            if not cookie_file.is_file():
                raise ValueError(f"cookie file does not exist: {cookie_file}")
            _, token = load_miner_auth(args.username, cookie_file)
            return fetch_global_badges(token)

        badge_sets, event = cached_value(
            sources,
            "twitch_global_badges",
            args.badge_cache_hours,
            args.refresh,
            refresh_badges,
        )
        cache_events.append(event)
        badges = flatten_badges(badge_sets)
        if event["status"] == "refreshed":
            save_cache(args.cache_file, cache)

        scraper = TwitchDropsAppScraper(timeout=30)
        indexed_games, event = cached_value(
            sources,
            "twitchdrops_front_page",
            args.site_cache_hours,
            args.refresh,
            scraper.scrape_front_page,
        )
        cache_events.append(event)
        if event["status"] == "refreshed":
            save_cache(args.cache_file, cache)
        selected_slugs = {slug.casefold() for slug in args.game}
        if selected_slugs:
            indexed_games = [
                game
                for game in indexed_games
                if str(game.get("slug") or "").casefold() in selected_slugs
            ]

        game_reports = []
        for index, game in enumerate(indexed_games):
            slug = str(game.get("slug") or "")
            key = f"twitchdrops_game:{slug}"
            entry_was_fresh = cache_entry_is_fresh(
                sources.get(key), args.site_cache_hours
            )
            report, event = cached_value(
                sources,
                key,
                args.site_cache_hours,
                args.refresh,
                lambda url=game["url"]: scraper.scrape(url),
            )
            cache_events.append(event)
            game_reports.append(enrich_game_report(report, badges))
            if event["status"] == "refreshed":
                save_cache(args.cache_file, cache)
            if (
                not entry_was_fresh
                and event["status"] == "refreshed"
                and index + 1 < len(indexed_games)
                and args.request_delay
            ):
                time.sleep(args.request_delay)

        counts = catalog_counts(game_reports)
        report = {
            "generated_at": iso_now(),
            "counts": counts,
            "source_status": cache_events,
            "badge_catalog": {
                "source": GLOBAL_BADGES_URL,
                "set_count": len(badge_sets),
                "version_count": len(badges),
                "sets": badge_sets,
            },
            "indexed_games": indexed_games,
            "games": game_reports,
        }
        save_cache(args.cache_file, cache)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except (OSError, ValueError, requests.RequestException) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    if not args.quiet:
        print_catalog(game_reports)
    print(
        f"\nFound {counts['games']} games, {counts['campaigns']} campaigns, "
        f"{counts['drops']} drops, {counts['badges']} confirmed badges, and "
        f"{counts['unknown']} unknown rewards."
    )
    print(f"Cache: {args.cache_file.resolve()}")
    print(f"Full report: {output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
