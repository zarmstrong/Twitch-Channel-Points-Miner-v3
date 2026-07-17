#!/usr/bin/env python3
"""Capture Twitch drops GraphQL responses without starting the miner."""

import argparse
import copy
import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from TwitchChannelPointsMiner.classes.Twitch import Twitch
from TwitchChannelPointsMiner.constants import CLIENT_ID, GQLOperations, USER_AGENTS


WEB_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
WEB_USER_AGENT = USER_AGENTS["Windows"]["CHROME"]
AVAILABLE_DROPS_OPERATION = getattr(
    GQLOperations,
    "DropsHighlightService_AvailableDrops",
    {
        "operationName": "DropsHighlightService_AvailableDrops",
        "variables": {"channelID": None},
        "extensions": {
            "persistedQuery": {
                "version": 1,
                "sha256Hash": (
                    "9a62a09bce5b53e26e64a671e530bc599cb6aab1e5ba3cbd5d85966d3940716f"
                ),
            }
        },
    },
)
AVAILABLE_BADGES_OPERATION = {
    "operationName": "AvailableBadges",
    "query": (
        "query AvailableBadges { currentUser { availableBadges { "
        "id setID version title description imageURL } } }"
    ),
    "variables": {},
}


OPERATIONS = {
    "inventory": GQLOperations.Inventory,
    "dashboard": GQLOperations.ViewerDropsDashboard,
    "available": AVAILABLE_DROPS_OPERATION,
    "badges": AVAILABLE_BADGES_OPERATION,
}


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Run the drops-related persisted GraphQL queries and save their raw "
            "responses, HTTP metadata, and errors to a JSON report."
        )
    )
    parser.add_argument(
        "username", help="Twitch login whose saved cookie should be used"
    )
    parser.add_argument(
        "--operation",
        choices=["all", *OPERATIONS],
        default="all",
        help="query to run (default: all read-only drops queries)",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="number of samples to capture (default: 1)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=0,
        help="seconds between samples (default: 0)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="report path (default: logs/drops-gql-report-<timestamp>.json)",
    )
    parser.add_argument(
        "--reauth",
        action="store_true",
        help="ignore the saved cookie and start Twitch device authorization",
    )
    parser.add_argument(
        "--channel",
        help="live streamer login used to query channel-advertised drop campaigns",
    )
    parser.add_argument(
        "--channel-id",
        help="Twitch channel ID; bypasses channel login resolution when supplied",
    )
    parser.add_argument(
        "--web-auth-token-file",
        type=Path,
        default=Path(".twitch-web-auth-token"),
        help="file containing a Twitch Web auth-token (default: ./.twitch-web-auth-token)",
    )
    parser.add_argument(
        "--cookie-file",
        type=Path,
        help=(
            "miner cookie file to load instead of cookies/<username>.pkl; "
            "takes precedence over the default web auth-token file and cannot "
            "be combined with --reauth"
        ),
    )
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    if args.interval < 0:
        parser.error("--interval cannot be negative")
    if args.channel_id and not args.channel:
        parser.error("--channel-id requires --channel for campaign detail context")
    if args.cookie_file and args.reauth:
        parser.error("--cookie-file cannot be combined with --reauth")
    return args


def graphql_errors(payload):
    if isinstance(payload, dict):
        return payload.get("errors", []) or []
    if isinstance(payload, list):
        return [
            error
            for item in payload
            if isinstance(item, dict)
            for error in (item.get("errors", []) or [])
        ]
    return []


def load_web_auth_token(path):
    token_path = path.expanduser()
    if not token_path.is_file():
        return None

    token = token_path.read_text(encoding="utf-8").strip()
    if token.lower().startswith("oauth "):
        token = token[6:].strip()
    if not token:
        raise ValueError(f"Web auth-token file is empty: {token_path}")

    if token_path.stat().st_mode & 0o077:
        print(
            f"Warning: {token_path} is readable by other users; run "
            f"chmod 600 {token_path}",
            file=sys.stderr,
        )
    return token


def fetch_integrity_token(twitch, session, client_id):
    headers = {
        "Authorization": f"OAuth {twitch.twitch_login.get_auth_token()}",
        "Client-Id": client_id,
        "Client-Session-Id": twitch.client_session,
        "Client-Version": twitch.update_client_version(),
        "User-Agent": WEB_USER_AGENT,
        "X-Device-Id": twitch.device_id,
    }
    attempts = []
    token = None
    for attempt_number in range(1, 3):
        started = time.monotonic()
        try:
            response = session.post(
                GQLOperations.integrity_url, json={}, headers=headers, timeout=30
            )
            payload = response.json()
            token = payload.get("token")
            attempts.append(
                {
                    "attempt": attempt_number,
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                    "http_status": response.status_code,
                    "token_received": bool(token),
                    "expiration": payload.get("expiration"),
                    "request_id": payload.get("request_id"),
                    "response_cookies_received": len(response.cookies),
                }
            )
        except (requests.RequestException, ValueError) as error:
            attempts.append(
                {
                    "attempt": attempt_number,
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                    "request_error": f"{type(error).__name__}: {error}",
                }
            )

    return token, {
        "operation": "integrity",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "token_received": bool(token),
        "attempts": attempts,
    }


def run_operation(
    twitch, session, name, operation, client_id, integrity_token=None
):
    payload = copy.deepcopy(operation)
    headers = {
        "Authorization": f"OAuth {twitch.twitch_login.get_auth_token()}",
        "Client-Id": client_id,
        "Client-Session-Id": twitch.client_session,
        "Client-Version": twitch.update_client_version(),
        "User-Agent": WEB_USER_AGENT,
        "X-Device-Id": twitch.device_id,
    }
    if integrity_token:
        headers["Client-Integrity"] = integrity_token
    started = time.monotonic()
    captured_at = datetime.now(timezone.utc).isoformat()

    try:
        response = session.post(
            GQLOperations.url,
            json=payload,
            headers=headers,
            timeout=30,
        )
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        try:
            response_json = response.json()
            decode_error = None
        except ValueError as error:
            response_json = None
            decode_error = str(error)

        errors = graphql_errors(response_json)
        return {
            "operation": name,
            "operation_name": payload.get("operationName"),
            "captured_at": captured_at,
            "elapsed_ms": elapsed_ms,
            "http_status": response.status_code,
            "response_headers": {
                key: value
                for key, value in response.headers.items()
                if key.lower()
                in {
                    "content-type",
                    "date",
                    "server-timing",
                    "timing-allow-origin",
                    "x-request-id",
                }
            },
            "request": payload,
            "graphql_errors": errors,
            "json_decode_error": decode_error,
            "response": response_json,
            "response_text": response.text if response_json is None else None,
        }
    except requests.RequestException as error:
        return {
            "operation": name,
            "operation_name": payload.get("operationName"),
            "captured_at": captured_at,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
            "request": payload,
            "request_error": f"{type(error).__name__}: {error}",
        }


def resolve_channel_id(
    twitch, session, channel_login, client_id, integrity_token=None
):
    operation = copy.deepcopy(GQLOperations.GetIDFromLogin)
    operation["variables"]["login"] = channel_login
    result = run_operation(
        twitch,
        session,
        "resolve_channel",
        operation,
        client_id,
        integrity_token=integrity_token,
    )
    response = result.get("response") or {}
    user = response.get("data", {}).get("user") or {}
    return user.get("id"), result


def extract_campaigns(result, path):
    value = result.get("response")
    for key in path:
        if not isinstance(value, dict):
            return []
        value = value.get(key)
    return [campaign for campaign in (value or []) if isinstance(campaign, dict)]


def campaign_summary(campaign):
    game = campaign.get("game") or {}
    return {
        "id": campaign.get("id"),
        "name": campaign.get("name"),
        "game": game.get("displayName") or game.get("name"),
        "status": campaign.get("status"),
        "starts_at": campaign.get("startAt") or campaign.get("startsAt"),
        "ends_at": campaign.get("endAt") or campaign.get("endsAt"),
        "allow_channels": [
            channel.get("login") or channel.get("id")
            for channel in ((campaign.get("allow") or {}).get("channels") or [])
            if isinstance(channel, dict)
        ],
        "drops": [
            {
                "id": drop.get("id"),
                "name": drop.get("name"),
                "required_minutes_watched": drop.get("requiredMinutesWatched"),
                "benefits": [
                    {
                        "id": benefit.get("id"),
                        "name": benefit.get("name"),
                    }
                    for edge in (drop.get("benefitEdges") or [])
                    for benefit in [
                        edge.get("benefit") if isinstance(edge, dict) else None
                    ]
                    if isinstance(benefit, dict)
                ],
            }
            for drop in (campaign.get("timeBasedDrops") or [])
            if isinstance(drop, dict)
        ],
    }


def normalized_words(value):
    return re.findall(r"[a-z0-9]+", str(value or "").lower())


def earned_badge_matches(benefit_name, game_name, badges):
    benefit_words = normalized_words(benefit_name)
    game_words = set(normalized_words(game_name))
    matches = []
    if not benefit_words:
        return matches

    for badge in badges:
        title = str(badge.get("title") or "").strip()
        title_words = normalized_words(title)
        match_type = None
        if title_words == benefit_words:
            match_type = "exact"
        elif (
            len(title_words) > len(benefit_words)
            and title_words[-len(benefit_words) :] == benefit_words
            and set(title_words[: -len(benefit_words)]).issubset(game_words)
        ):
            match_type = "game_prefixed"
        if match_type:
            matches.append(
                {
                    "match_type": match_type,
                    "badge_id": badge.get("id"),
                    "badge_set_id": badge.get("setID"),
                    "badge_version": badge.get("version"),
                    "badge_title": title,
                    "badge_image_url": badge.get("imageURL"),
                }
            )
    return matches


def build_discovery_summary(operation_results, detail_results):
    campaigns_by_source = {
        "inventory": extract_campaigns(
            operation_results.get("inventory", {}),
            ["data", "currentUser", "inventory", "dropCampaignsInProgress"],
        ),
        "dashboard": extract_campaigns(
            operation_results.get("dashboard", {}),
            ["data", "currentUser", "dropCampaigns"],
        ),
        "channel_available": extract_campaigns(
            operation_results.get("available", {}),
            ["data", "channel", "viewerDropCampaigns"],
        ),
        "campaign_details": [],
    }

    # DropCampaignDetails returns one object rather than a list.
    for result in detail_results:
        campaign = (
            ((result.get("response") or {}).get("data") or {}).get("user") or {}
        ).get("dropCampaign")
        if isinstance(campaign, dict):
            campaigns_by_source["campaign_details"].append(campaign)

    source_ids = {
        source: {
            str(campaign.get("id"))
            for campaign in campaigns
            if campaign.get("id") not in [None, ""]
        }
        for source, campaigns in campaigns_by_source.items()
    }
    all_campaigns = {}
    for source, campaigns in campaigns_by_source.items():
        for campaign in campaigns:
            campaign_id = campaign.get("id")
            if campaign_id in [None, ""]:
                continue
            campaign_id = str(campaign_id)
            entry = all_campaigns.setdefault(
                campaign_id, campaign_summary(campaign)
            )
            entry["sources"] = sorted(
                name for name, ids in source_ids.items() if campaign_id in ids
            )

    badges = extract_campaigns(
        operation_results.get("badges", {}),
        ["data", "currentUser", "availableBadges"],
    )
    for campaign in all_campaigns.values():
        for drop in campaign.get("drops", []):
            for benefit in drop.get("benefits", []):
                benefit["earned_badge_matches"] = earned_badge_matches(
                    benefit.get("name"), campaign.get("game"), badges
                )
                benefit["badge_achieved"] = bool(
                    benefit["earned_badge_matches"]
                )

    return {
        "source_campaign_counts": {
            source: len(campaigns)
            for source, campaigns in campaigns_by_source.items()
        },
        "earned_badge_count": len(badges),
        "earned_badge_titles": sorted(
            str(badge.get("title") or "").strip()
            for badge in badges
            if str(badge.get("title") or "").strip()
        ),
        "campaigns": list(all_campaigns.values()),
    }


def main():
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        stream=sys.stdout,
        force=True,
    )
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output = args.output or Path("logs") / f"drops-gql-report-{timestamp}.json"
    twitch = Twitch(args.username, WEB_USER_AGENT)
    if args.cookie_file:
        cookie_file = args.cookie_file.expanduser().resolve()
        if not cookie_file.is_file():
            print(f"Cookie file does not exist: {cookie_file}", file=sys.stderr)
            return 2
        twitch.cookies_file = str(cookie_file)
    try:
        web_auth_token = (
            None
            if args.reauth or args.cookie_file
            else load_web_auth_token(args.web_auth_token_file)
        )
    except (OSError, ValueError) as error:
        print(f"Could not load Twitch Web auth-token: {error}", file=sys.stderr)
        return 2

    if web_auth_token:
        print("Loading Twitch Web authentication from token file...", flush=True)
        twitch.twitch_login.set_token(web_auth_token)
        twitch.twitch_login.cookies = [{"name": "auth-token", "value": web_auth_token}]
        twitch.twitch_login.session.cookies.set(
            "auth-token", web_auth_token, domain=".twitch.tv", path="/"
        )
        authentication_type = "web_auth_token"
        probe_client_id = WEB_CLIENT_ID
    elif args.reauth:
        print(
            f"Starting Twitch device authorization for {args.username}...", flush=True
        )
        if twitch.twitch_login.login_flow():
            twitch.twitch_login.save_cookies(twitch.cookies_file)
        authentication_type = "device_authorization"
        probe_client_id = CLIENT_ID
    else:
        if args.cookie_file:
            print(
                f"Loading Twitch authentication from {twitch.cookies_file}...",
                flush=True,
            )
        else:
            print(f"Loading Twitch authentication for {args.username}...", flush=True)
        twitch.login()
        authentication_type = "saved_miner_cookie"
        probe_client_id = CLIENT_ID
    if not twitch.twitch_login.get_auth_token():
        print(
            "No Twitch auth token was loaded; the probe cannot continue.",
            file=sys.stderr,
        )
        return 2

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "username": args.username,
        "authentication_type": authentication_type,
        "cookie_file": str(Path(twitch.cookies_file).resolve()),
        "client_id": probe_client_id,
        "endpoint": GQLOperations.url,
        "samples": [],
    }

    session = twitch.twitch_login.session
    print("Warming up Twitch integrity session...", flush=True)
    integrity_token, integrity_result = fetch_integrity_token(
        twitch, session, probe_client_id
    )
    report["samples"].append(integrity_result)
    if integrity_token:
        print("  Integrity token received.")
    else:
        print(
            "  Twitch did not issue an integrity token; campaign results may be hidden."
        )

    operations = {name: copy.deepcopy(value) for name, value in OPERATIONS.items()}
    if args.channel:
        channel_id = args.channel_id
        resolution = None
        if channel_id:
            print(
                f"Using channel ID {channel_id} for {args.channel}.", flush=True
            )
        else:
            print(f"Resolving channel {args.channel}...", flush=True)
            channel_id, resolution = resolve_channel_id(
                twitch,
                session,
                args.channel,
                probe_client_id,
                integrity_token=integrity_token,
            )
            report["samples"].append(resolution)
        if not channel_id:
            graphql_error_messages = [
                error.get("message", str(error))
                for error in (resolution or {}).get("graphql_errors", [])
                if isinstance(error, dict)
            ]
            response_user = (
                (((resolution or {}).get("response") or {}).get("data") or {}).get(
                    "user"
                )
            )
            print(
                f"Could not resolve Twitch channel {args.channel!r}; "
                f"HTTP status: {(resolution or {}).get('http_status', 'request failed')}; "
                f"user payload: {response_user!r}; "
                f"GraphQL errors: {graphql_error_messages or 'none'}.",
                file=sys.stderr,
            )
            return 2
        operations["available"]["variables"] = {"channelID": channel_id}
        report["channel"] = {"login": args.channel, "id": channel_id}
    elif args.operation in {"all", "available"}:
        print(
            "Skipping the channel-advertised drops query; pass --channel STREAMER "
            "to supply Twitch's required channel ID."
        )
        operations.pop("available")

    selected = (
        operations.items()
        if args.operation == "all"
        else (
            [(args.operation, operations[args.operation])]
            if args.operation in operations
            else []
        )
    )

    selected = list(selected)
    operation_results = {}
    detail_results = []
    for sample_number in range(1, args.repeat + 1):
        for name, operation in selected:
            print(f"[{sample_number}/{args.repeat}] Querying {name}...")
            result = run_operation(
                twitch,
                session,
                name,
                operation,
                probe_client_id,
                integrity_token=integrity_token,
            )
            report["samples"].append(result)
            operation_results[name] = result
            error_count = len(result.get("graphql_errors", []))
            print(
                f"  HTTP {result.get('http_status', 'request failed')}; "
                f"GraphQL errors: {error_count}"
            )
        if sample_number < args.repeat and args.interval:
            time.sleep(args.interval)

    available_campaigns = extract_campaigns(
        operation_results.get("available", {}),
        ["data", "channel", "viewerDropCampaigns"],
    )
    if available_campaigns and args.channel:
        print(
            "Expanding "
            f"{len(available_campaigns)} channel-advertised campaign(s)..."
        )
        for campaign in available_campaigns:
            campaign_id = campaign.get("id")
            if campaign_id in [None, ""]:
                continue
            operation = copy.deepcopy(GQLOperations.DropCampaignDetails)
            operation["variables"] = {
                "dropID": str(campaign_id),
                "channelLogin": args.channel,
            }
            result = run_operation(
                twitch,
                session,
                "available_campaign_details",
                operation,
                probe_client_id,
                integrity_token=integrity_token,
            )
            result["advertised_campaign_id"] = str(campaign_id)
            detail_results.append(result)
            report["samples"].append(result)
            print(
                f"  {campaign.get('name') or campaign_id}: "
                f"HTTP {result.get('http_status', 'request failed')}; "
                f"GraphQL errors: {len(result.get('graphql_errors', []))}"
            )

    report["discovery_summary"] = build_discovery_summary(
        operation_results, detail_results
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as report_file:
        json.dump(report, report_file, indent=2, ensure_ascii=False)
        report_file.write("\n")

    print(f"Saved report to {output.resolve()}")
    print(
        "The report contains response data for this Twitch account; review before sharing."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
