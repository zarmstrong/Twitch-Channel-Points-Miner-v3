# For documentation on Twitch GraphQL API see:
# https://www.apollographql.com/docs/
# https://github.com/mauricew/twitch-graphql-api
# Full list of available methods: https://azr.ivr.fi/schema/query.doc.html (a bit outdated)


import copy
import json
import logging
import os
import random
import re
import string
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from secrets import choice, token_hex
from threading import Event, Lock
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
import validators
from colorama import Fore

from TwitchChannelPointsMiner.classes.entities.Campaign import Campaign
from TwitchChannelPointsMiner.classes.entities.CommunityGoal import CommunityGoal
from TwitchChannelPointsMiner.classes.entities.Drop import Drop
from TwitchChannelPointsMiner.classes.Exceptions import (
    StreamerDoesNotExistException,
    StreamerIsOfflineException,
)
from TwitchChannelPointsMiner.classes.Settings import (
    Events,
    FollowersOrder,
    Priority,
    Settings,
)
from TwitchChannelPointsMiner.classes.TwitchLogin import TwitchLogin
from TwitchChannelPointsMiner.constants import (
    CLIENT_ID,
    CLIENT_VERSION,
    URL,
    GQLOperations,
)
from TwitchChannelPointsMiner.utils import (
    _millify,
    create_chunks,
    internet_connection_available,
)
from twitchdrops_app_scraper import TwitchDropsAppScraper

# import json

# from urllib.parse import quote
# from base64 import urlsafe_b64decode
# from datetime import datetime


logger = logging.getLogger(__name__)
JsonType = Dict[str, Any]
TWITCHDROPS_APP_CHECK_DELAY_SECONDS = (0.5, 2.0)


class Twitch(object):
    __slots__ = [
        "cookies_file",
        "user_agent",
        "twitch_login",
        "running",
        "device_id",
        # "integrity",
        # "integrity_expire",
        "client_session",
        "client_version",
        "twilight_build_id_pattern",
        "analytics_mutex",
        "drop_progress_last_saved",
        "drop_status_last_saved",
        "track_drop_item_art",
        "scrape_drop_progress_on_load",
        "log_drop_checks",
        "category_log_level",
        "discovered_open_drop_campaigns",
        "awarded_game_event_drops",
        "twitchdrops_app_campaigns",
        "category_campaign_eligibility",
        "restart_requested",
    ]

    def __init__(self, username, user_agent, password=None):
        if (
            not isinstance(username, str)
            or re.fullmatch(r"[A-Za-z0-9_]{1,25}", username) is None
        ):
            raise ValueError("Invalid Twitch username")
        cookies_path = os.path.join(Path().absolute(), "cookies")
        Path(cookies_path).mkdir(parents=True, exist_ok=True)
        self.cookies_file = os.path.join(cookies_path, f"{username}.pkl")
        self.user_agent = user_agent
        self.device_id = "".join(
            choice(string.ascii_letters + string.digits) for _ in range(32)
        )
        self.twitch_login = TwitchLogin(
            CLIENT_ID, self.device_id, username, self.user_agent, password=password
        )
        self.running = True
        # self.integrity = None
        # self.integrity_expire = 0
        self.client_session = token_hex(16)
        self.client_version = CLIENT_VERSION
        self.twilight_build_id_pattern = re.compile(
            r'window\.__twilightBuildID\s*=\s*"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"'
        )
        self.analytics_mutex = Lock()
        self.drop_progress_last_saved = {}
        self.drop_status_last_saved = {}
        self.track_drop_item_art = False
        self.scrape_drop_progress_on_load = False
        self.log_drop_checks = False
        self.category_log_level = logging.INFO
        self.discovered_open_drop_campaigns = None
        self.awarded_game_event_drops = {}
        self.twitchdrops_app_campaigns = {}
        self.category_campaign_eligibility = {}
        self.restart_requested = Event()

    @staticmethod
    def __is_unauthorized_response(response):
        return isinstance(response, dict) and response.get("status") == 401

    def __request_authentication_restart(self):
        if self.restart_requested.is_set():
            return

        logger.error(
            f"Twitch rejected the saved authorization token for "
            f"{self.twitch_login.username}. The cached login will be cleared and "
            "the miner restarted; follow the reauthentication instructions in "
            "the logs.",
            extra={
                "emoji": ":warning:",
                "event": Events.CONFIGURATION,
                "force_alert": True,
            },
        )
        try:
            Path(self.cookies_file).unlink(missing_ok=True)
        except OSError as error:
            logger.error(
                f"Unable to clear the cached Twitch login: {error}. Restarting "
                "without clearing it could cause a restart loop, so the miner "
                "will stop instead.",
                extra={
                    "emoji": ":warning:",
                    "event": Events.CONFIGURATION,
                    "force_alert": True,
                },
            )
            self.running = False
            return

        self.restart_requested.set()
        self.running = False

    def __log_drop_check(self, message, level=logging.INFO):
        if self.log_drop_checks is True:
            logger.log(level, f"[drops-check] {message}")

    def __log_drop_check_json(
        self, label, payload, level=logging.DEBUG, category_log=False
    ):
        if self.log_drop_checks is not True:
            return
        try:
            serialized = json.dumps(payload)
        except (TypeError, ValueError):
            serialized = str(payload)
        logger.log(
            level,
            f"[drops-check] {label}: {serialized}",
            extra={"category_log": category_log},
        )

    def __log_category(self, message, **kwargs):
        extra = kwargs.setdefault("extra", {})
        extra["category_log"] = True
        logger.log(self.category_log_level, message, **kwargs)

    def __replace_category_campaign_eligibility(
        self, game_slug: str, eligibility_by_login: dict
    ):
        """Atomically replace cached eligibility for one game category."""
        updated_eligibility = {
            key: value
            for key, value in self.category_campaign_eligibility.items()
            if key[0] != game_slug
        }
        updated_eligibility.update(
            {
                (game_slug, login): eligibility
                for login, eligibility in eligibility_by_login.items()
            }
        )
        self.category_campaign_eligibility = updated_eligibility

    def __drop_tracking_key(self, drop, campaign_name=None, category_name=None):
        return "|".join(
            [
                str(drop.id or ""),
                str(drop.item_art_url or ""),
                str(drop.name or ""),
                str(campaign_name or ""),
                str(category_name or ""),
            ]
        )

    def __should_save_drop_snapshot(self, tracking_key, minutes_watched, status):
        last_saved_minutes = self.drop_progress_last_saved.get(tracking_key, -1)
        last_saved_status = self.drop_status_last_saved.get(tracking_key)
        if minutes_watched > last_saved_minutes:
            return True

        # Terminal state updates can happen without minute changes.
        if status in ["captured", "failed"] and status != last_saved_status:
            return True

        return False

    def __mark_drop_snapshot_saved(self, tracking_key, minutes_watched, status):
        self.drop_progress_last_saved[tracking_key] = minutes_watched
        self.drop_status_last_saved[tracking_key] = status

    def __slugify(self, value: str) -> str:
        normalized = unicodedata.normalize("NFKD", str(value))
        ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
        return re.sub(r"[^a-z0-9]+", "-", ascii_value.lower()).strip("-")

    def __drop_variant_entries(self, drop_dict):
        benefit_edges = drop_dict.get("benefitEdges", []) or []
        variants = []

        if benefit_edges == []:
            variants.append(
                {
                    "name": drop_dict.get("name"),
                    "benefit": drop_dict.get("name"),
                    "item_art_url": None,
                }
            )
            return variants

        for edge in benefit_edges:
            benefit = edge.get("benefit", {}) if isinstance(edge, dict) else {}
            variants.append(
                {
                    "name": benefit.get("name") or drop_dict.get("name"),
                    "benefit": benefit.get("name") or drop_dict.get("name"),
                    "item_art_url": self.__extract_edge_art_url(benefit),
                }
            )

        return variants

    def __extract_edge_art_url(self, benefit):
        if not isinstance(benefit, dict):
            return None

        for key in [
            "imageAssetURL",
            "imageAssetUrl",
            "imageURL",
            "imageUrl",
            "thumbnailURL",
            "thumbnailUrl",
        ]:
            if benefit.get(key):
                return benefit.get(key)
        return None

    def __drop_variant_entries_from_drop(self, drop):
        variants = []
        benefit_edges = getattr(drop, "benefit_edges", []) or []

        if benefit_edges == []:
            variants.append(
                {
                    "name": drop.name,
                    "benefit": drop.benefit,
                    "item_art_url": drop.item_art_url,
                }
            )
            return variants

        for edge in benefit_edges:
            benefit = edge.get("benefit", {}) if isinstance(edge, dict) else {}
            variants.append(
                {
                    "name": benefit.get("name") or drop.name,
                    "benefit": benefit.get("name") or drop.benefit,
                    "item_art_url": self.__extract_edge_art_url(benefit)
                    or drop.item_art_url,
                }
            )

        return variants

    def __save_drop_snapshot_from_dict(
        self,
        drop_dict,
        campaign=None,
        streamer_username=None,
        campaign_name_override=None,
        category_name_override=None,
        status_override=None,
    ):
        drop_self = drop_dict.get("self")
        if not isinstance(drop_self, dict):
            return

        try:
            drop = Drop(drop_dict)
            drop.update(drop_self)
        except (KeyError, TypeError, ValueError):
            return

        if drop.has_preconditions_met is False:
            return

        is_done = (
            drop.is_claimed is True
            or drop.current_minutes_watched >= drop.minutes_required
        )
        is_expired = (
            getattr(drop, "end_at", None) is not None
            and datetime.utcnow() > drop.end_at
        )

        progress_key = self.__drop_tracking_key(
            drop,
            campaign_name=campaign_name_override,
            category_name=category_name_override,
        )
        status = (
            "captured"
            if is_done is True
            else (
                "failed"
                if is_expired and drop.current_minutes_watched < drop.minutes_required
                else "in_progress"
            )
        )

        effective_status = status_override or status

        for variant in self.__drop_variant_entries_from_drop(drop):
            variant_key = "|".join(
                [
                    progress_key,
                    str(variant.get("name") or ""),
                    str(variant.get("item_art_url") or ""),
                ]
            )
            if (
                self.__should_save_drop_snapshot(
                    variant_key,
                    drop.current_minutes_watched,
                    effective_status,
                )
                is False
            ):
                continue

            self.__mark_drop_snapshot_saved(
                variant_key,
                drop.current_minutes_watched,
                effective_status,
            )
            self.__save_drop_progress_analytics(
                drop,
                campaign=campaign,
                streamer_username=streamer_username,
                campaign_name_override=campaign_name_override,
                category_name_override=category_name_override,
                status_override=effective_status,
                item_name_override=variant.get("name"),
                benefit_override=variant.get("benefit"),
                item_art_url_override=variant.get("item_art_url"),
            )

    def scrape_drop_progress_from_inventory(self, reason="startup"):
        self.__log_drop_check(f"inventory scrape started ({reason})")
        inventory = self.__get_inventory()
        if inventory in [None, {}] or inventory.get("dropCampaignsInProgress") in [
            None,
            {},
        ]:
            reconciled = self.__reconcile_awarded_game_event_drops(inventory)
            if reconciled > 0:
                self.__log_drop_check(
                    f"inventory scrape reconciled {reconciled} awarded gameEventDrops"
                )
            self.__log_drop_check("inventory scrape finished: no campaigns in progress")
            return 0

        saved = 0
        for progress in inventory.get("dropCampaignsInProgress", []):
            progress_game = progress.get("game") or {}
            category_name_override = (
                progress_game.get("displayName")
                or progress_game.get("name")
                or "Unknown"
            )
            campaign_name_override = progress.get("name")

            for drop_dict in progress.get("timeBasedDrops", []):
                drop_self = drop_dict.get("self")
                if not isinstance(drop_self, dict):
                    continue

                try:
                    drop = Drop(drop_dict)
                    drop.update(drop_self)
                except (KeyError, TypeError, ValueError):
                    continue

                is_done = (
                    drop.is_claimed is True
                    or drop.current_minutes_watched >= drop.minutes_required
                )
                is_expired = (
                    getattr(drop, "end_at", None) is not None
                    and datetime.utcnow() > drop.end_at
                )

                if drop.has_preconditions_met is not False:
                    progress_key = self.__drop_tracking_key(
                        drop,
                        campaign_name=campaign_name_override,
                        category_name=category_name_override,
                    )
                    status = (
                        "captured"
                        if is_done is True
                        else (
                            "failed"
                            if is_expired
                            and drop.current_minutes_watched < drop.minutes_required
                            else "in_progress"
                        )
                    )
                    variant_saved = False
                    for variant in self.__drop_variant_entries_from_drop(drop):
                        variant_key = "|".join(
                            [
                                progress_key,
                                str(variant.get("name") or ""),
                                str(variant.get("item_art_url") or ""),
                            ]
                        )
                        if (
                            self.__should_save_drop_snapshot(
                                variant_key,
                                drop.current_minutes_watched,
                                status,
                            )
                            is False
                        ):
                            continue

                        self.__mark_drop_snapshot_saved(
                            variant_key,
                            drop.current_minutes_watched,
                            status,
                        )
                        self.__save_drop_progress_analytics(
                            drop,
                            campaign=None,
                            streamer_username=None,
                            campaign_name_override=campaign_name_override,
                            category_name_override=category_name_override,
                            status_override=status,
                            item_name_override=variant.get("name"),
                            benefit_override=variant.get("benefit"),
                            item_art_url_override=variant.get("item_art_url"),
                        )
                        variant_saved = True

                    if variant_saved is True:
                        saved += 1

        reconciled = self.__reconcile_awarded_game_event_drops(inventory)
        if reconciled > 0:
            self.__log_drop_check(
                f"inventory scrape reconciled {reconciled} awarded gameEventDrops"
            )

        self.__log_drop_check(
            f"inventory scrape finished: saved {saved} progress snapshots"
        )
        return saved

    def login(self):
        if not os.path.isfile(self.cookies_file):
            if self.twitch_login.login_flow():
                self.twitch_login.save_cookies(self.cookies_file)
        else:
            self.twitch_login.load_cookies(self.cookies_file)
            self.twitch_login.set_token(self.twitch_login.get_auth_token())

    # === STREAMER / STREAM / INFO === #
    def update_stream(self, streamer):
        if streamer.stream.update_required() is True:
            stream_info = self.get_stream_info(streamer)
            if stream_info is not None:
                streamer.stream.update(
                    broadcast_id=stream_info["stream"]["id"],
                    title=stream_info["broadcastSettings"]["title"],
                    game=stream_info["broadcastSettings"]["game"],
                    tags=stream_info["stream"]["tags"],
                    viewers_count=stream_info["stream"]["viewersCount"],
                )

                event_properties = {
                    "channel_id": streamer.channel_id,
                    "broadcast_id": streamer.stream.broadcast_id,
                    "player": "site",
                    "user_id": self.twitch_login.get_user_id(),
                    "live": True,
                    "channel": streamer.username,
                }

                if (
                    streamer.stream.game_name() is not None
                    and streamer.stream.game_id() is not None
                    and streamer.settings.claim_drops is True
                ):
                    event_properties["game"] = streamer.stream.game_name()
                    event_properties["game_id"] = streamer.stream.game_id()
                    # Update also the campaigns_ids so we are sure to tracking the correct campaign
                    streamer.stream.campaigns_ids = (
                        self.__get_campaign_ids_from_streamer(streamer)
                    )

                streamer.stream.payload = [
                    {"event": "minute-watched", "properties": event_properties}
                ]

    def get_spade_url(self, streamer):
        try:
            # fixes AttributeError: 'NoneType' object has no attribute 'group'
            # headers = {"User-Agent": self.user_agent}
            from TwitchChannelPointsMiner.constants import USER_AGENTS

            headers = {"User-Agent": USER_AGENTS["Linux"]["FIREFOX"]}

            main_page_request = requests.get(
                streamer.streamer_url, headers=headers, timeout=(5, 20)
            )
            response = main_page_request.text
            # logger.info(response)
            regex_settings = "(https://static.twitchcdn.net/config/settings.*?js|https://assets.twitch.tv/config/settings.*?.js)"
            settings_url = re.search(regex_settings, response).group(1)

            settings_request = requests.get(
                settings_url, headers=headers, timeout=(5, 20)
            )
            response = settings_request.text
            regex_spade = '"spade_url":"(.*?)"'
            streamer.stream.spade_url = re.search(regex_spade, response).group(1)
        except requests.exceptions.RequestException as e:
            logger.error(f"Something went wrong during extraction of 'spade_url': {e}")

    def get_broadcast_id(self, streamer):
        json_data = copy.deepcopy(GQLOperations.WithIsStreamLiveQuery)
        json_data["variables"] = {"id": streamer.channel_id}
        response = self.post_gql_request(json_data)
        if response != {}:
            stream = response["data"]["user"]["stream"]
            if stream is not None:
                return stream["id"]
            else:
                raise StreamerIsOfflineException

    def get_stream_info(self, streamer):
        json_data = copy.deepcopy(GQLOperations.VideoPlayerStreamInfoOverlayChannel)
        json_data["variables"] = {"channel": streamer.username}
        response = self.post_gql_request(json_data)
        if response != {}:
            if response["data"]["user"]["stream"] is None:
                raise StreamerIsOfflineException
            else:
                return response["data"]["user"]

    def check_streamer_online(self, streamer):
        if time.time() < streamer.offline_at + 60:
            return

        if streamer.is_online is False:
            try:
                self.get_spade_url(streamer)
                self.update_stream(streamer)
                streamer.set_online(self.__streamer_drops_description(streamer))
            except StreamerIsOfflineException:
                streamer.set_offline()
        else:
            try:
                self.update_stream(streamer)
            except StreamerIsOfflineException:
                streamer.set_offline()

    def __streamer_drops_description(self, streamer):
        if getattr(streamer, "from_category", False) is not True:
            return None

        game_name = streamer.stream.game_name()
        if not game_name:
            return None

        game_slug = self.__slugify(game_name)
        selected_eligibility = self.category_campaign_eligibility.get(
            (game_slug, streamer.username)
        )
        if selected_eligibility is not None:
            eligible_campaigns, total_campaigns = selected_eligibility
            if total_campaigns > 1:
                return (
                    f"{game_name} drops "
                    f"({eligible_campaigns} of {total_campaigns} campaigns)"
                )

        fallback_campaigns = self.twitchdrops_app_campaigns.get(game_slug, [])
        if len(fallback_campaigns) > 1:
            eligible_campaigns = sum(
                1
                for campaign in fallback_campaigns
                if not campaign.get("channels")
                or streamer.username
                in {
                    str(login).lower().strip() for login in campaign.get("channels", [])
                }
            )
            return (
                f"{game_name} drops "
                f"({eligible_campaigns} of {len(fallback_campaigns)} campaigns)"
            )

        game_campaigns = [
            campaign
            for campaign in self.discovered_open_drop_campaigns or []
            if self.__slugify(
                (campaign.get("game") or {}).get("displayName")
                or (campaign.get("game") or {}).get("name")
                or ""
            )
            == game_slug
        ]
        if len(game_campaigns) <= 1:
            return f"{game_name} drops"

        eligible_campaign_ids = {
            str(campaign_id) for campaign_id in streamer.stream.campaigns_ids
        }
        eligible_campaigns = sum(
            1
            for campaign in game_campaigns
            if str(campaign.get("id")) in eligible_campaign_ids
        )
        return (
            f"{game_name} drops "
            f"({eligible_campaigns} of {len(game_campaigns)} campaigns)"
        )

    def get_channel_id(self, streamer_username):
        json_data = copy.deepcopy(GQLOperations.GetIDFromLogin)
        json_data["variables"]["login"] = streamer_username
        json_response = self.post_gql_request(json_data)
        if (
            "data" not in json_response
            or "user" not in json_response["data"]
            or json_response["data"]["user"] is None
        ):
            raise StreamerDoesNotExistException
        else:
            return json_response["data"]["user"]["id"]

    def get_followers(
        self, limit: int = 100, order: FollowersOrder = FollowersOrder.ASC
    ):
        json_data = copy.deepcopy(GQLOperations.ChannelFollows)
        json_data["variables"] = {"limit": limit, "order": str(order)}
        has_next = True
        last_cursor = ""
        follows = []
        while has_next is True:
            json_data["variables"]["cursor"] = last_cursor
            json_response = self.post_gql_request(json_data)
            try:
                follows_response = json_response["data"]["user"]["follows"]
                last_cursor = None
                for follow in follows_response["edges"]:
                    follows.append(follow["node"]["login"].lower())
                    last_cursor = follow["cursor"]

                has_next = follows_response["pageInfo"]["hasNextPage"]
            except KeyError:
                return []
        return follows

    def __normalize_category(self, category: str) -> str:
        if category is None:
            return ""

        category = category.strip()
        if category.startswith("http://") or category.startswith("https://"):
            parsed_url = urlparse(category)
            parts = [part for part in parsed_url.path.split("/") if part]
            if "category" in parts:
                index = parts.index("category")
                if index + 1 < len(parts):
                    return parts[index + 1].strip().lower()
            is_twitchdrops_app = parsed_url.netloc.lower().endswith("twitchdrops.app")
            if is_twitchdrops_app and "game" in parts:
                index = parts.index("game")
                if index + 1 < len(parts):
                    return parts[index + 1].strip().lower()
        return category.lower().strip()

    def __split_category_streamer_selector(self, category_spec: str):
        if category_spec is None:
            return "", None

        raw_value = str(category_spec).strip()
        if raw_value == "":
            return "", None

        if raw_value.startswith("http://") or raw_value.startswith("https://"):
            return raw_value, None

        category_name = raw_value
        streamer_name = None

        if "|" in raw_value:
            category_name, streamer_name = raw_value.rsplit("|", 1)

        category_name = category_name.strip().lstrip("[").rstrip("]")

        if streamer_name is not None:
            streamer_name = (
                streamer_name.strip().lstrip("[").rstrip("]").strip().lstrip("@")
            )
            streamer_name = streamer_name.lower() if streamer_name else None

        return category_name, streamer_name

    def __has_incomplete_drop_in_campaign(self, campaign_dict: dict) -> bool:
        for drop_dict in campaign_dict.get("timeBasedDrops", []) or []:
            drop_self = drop_dict.get("self")
            if not isinstance(drop_self, dict):
                continue

            try:
                drop = Drop(drop_dict)
                drop.update(drop_self)
            except (KeyError, TypeError, ValueError):
                continue

            if drop.has_preconditions_met is False:
                continue

            if (
                drop.is_claimed is False
                and drop.current_minutes_watched < drop.minutes_required
            ):
                if (
                    getattr(drop, "end_at", None) is None
                    or datetime.utcnow() <= drop.end_at
                ):
                    return True

        return False

    def __active_drop_category_slugs_from_inventory(self, inventory: dict) -> set:
        active_slugs = set()
        for campaign in inventory.get("dropCampaignsInProgress", []) or []:
            if not isinstance(campaign, dict):
                continue

            game = campaign.get("game") or {}
            game_name = (game.get("displayName") or game.get("name") or "").strip()
            if game_name == "":
                continue

            if self.__has_incomplete_drop_in_campaign(campaign) is True:
                active_slugs.add(self.__slugify(game_name))

        return active_slugs

    def __completed_drop_ids_from_inventory(self, inventory: dict) -> set:
        completed_ids = set()

        for campaign in inventory.get("dropCampaignsInProgress", []) or []:
            if not isinstance(campaign, dict):
                continue
            for drop in campaign.get("timeBasedDrops", []) or []:
                if not isinstance(drop, dict):
                    continue
                drop_self = drop.get("self") or {}
                if drop_self.get("isClaimed") is True and drop.get("id"):
                    completed_ids.add(str(drop["id"]))

        return completed_ids

    def __merge_campaign_inventory_progress(
        self, campaign: dict, inventory_campaign: dict
    ) -> dict:
        """Keep fresh campaign drops while applying the user's inventory progress."""
        if not campaign.get("timeBasedDrops"):
            return inventory_campaign
        if not inventory_campaign.get("timeBasedDrops"):
            return campaign

        merged_campaign = copy.deepcopy(campaign)
        merged_drops = merged_campaign.get("timeBasedDrops", [])
        merged_drop_ids = {
            str(drop.get("id"))
            for drop in merged_drops
            if isinstance(drop, dict) and drop.get("id") not in [None, ""]
        }
        inventory_drops = {
            str(drop.get("id")): drop
            for drop in inventory_campaign.get("timeBasedDrops", []) or []
            if isinstance(drop, dict) and drop.get("id") not in [None, ""]
        }

        for drop in merged_drops:
            if not isinstance(drop, dict) or drop.get("id") in [None, ""]:
                continue
            inventory_drop = inventory_drops.get(str(drop["id"]))
            if isinstance(inventory_drop, dict) and isinstance(
                inventory_drop.get("self"), dict
            ):
                drop["self"] = inventory_drop["self"]

        for drop_id, drop in inventory_drops.items():
            if drop_id not in merged_drop_ids:
                merged_drops.append(copy.deepcopy(drop))

        return merged_campaign

    def __awarded_benefits(self, inventory: dict) -> Tuple[set, set]:
        benefit_ids = set()
        benefit_fingerprints = set()
        awarded_drops = list(self.awarded_game_event_drops.values())

        for awarded_drop in inventory.get("gameEventDrops", []) or []:
            if isinstance(awarded_drop, dict):
                awarded_drops.append(awarded_drop)

        for awarded_drop in awarded_drops:
            benefit_id = awarded_drop.get("id")
            if benefit_id not in [None, ""]:
                benefit_ids.add(str(benefit_id))

            name = str(awarded_drop.get("name") or "").strip().lower()
            image_url = str(awarded_drop.get("imageURL") or "").strip()
            if name:
                benefit_fingerprints.add((name, image_url))
                benefit_fingerprints.add((name, ""))

        return benefit_ids, benefit_fingerprints

    def __drop_benefits_were_awarded(
        self, drop: dict, benefit_ids: set, benefit_fingerprints: set
    ) -> bool:
        benefits = []
        for edge in drop.get("benefitEdges", []) or []:
            benefit = edge.get("benefit") if isinstance(edge, dict) else None
            if isinstance(benefit, dict):
                benefits.append(benefit)

        if benefits == []:
            return False

        for benefit in benefits:
            benefit_id = benefit.get("id")
            if benefit_id not in [None, ""] and str(benefit_id) in benefit_ids:
                continue

            name = str(benefit.get("name") or "").strip().lower()
            image_url = str(benefit.get("imageAssetURL") or "").strip()
            if name and (
                (name, image_url) in benefit_fingerprints
                or (name, "") in benefit_fingerprints
            ):
                continue

            return False

        return True

    def __active_incomplete_drop_deadline(
        self,
        campaign: dict,
        completed_drop_ids: set,
        awarded_benefit_ids: set,
        awarded_benefit_fingerprints: set,
    ) -> Optional[datetime]:
        now = datetime.utcnow()
        earliest_deadline = None
        campaign_benefit_counts = {}
        for campaign_drop in campaign.get("timeBasedDrops", []) or []:
            if not isinstance(campaign_drop, dict):
                continue
            seen_drop_benefits = set()
            for edge in campaign_drop.get("benefitEdges", []) or []:
                benefit = edge.get("benefit") if isinstance(edge, dict) else None
                if not isinstance(benefit, dict):
                    continue
                benefit_key = str(benefit.get("id") or "")
                if benefit_key and benefit_key not in seen_drop_benefits:
                    seen_drop_benefits.add(benefit_key)
                    campaign_benefit_counts[benefit_key] = (
                        campaign_benefit_counts.get(benefit_key, 0) + 1
                    )

        for drop in campaign.get("timeBasedDrops", []) or []:
            if not isinstance(drop, dict):
                continue

            drop_id = drop.get("id")
            if drop_id not in [None, ""] and str(drop_id) in completed_drop_ids:
                continue

            minutes_required = drop.get("requiredMinutesWatched") or 0
            if minutes_required <= 0:
                continue

            starts_at = self.__parse_twitch_datetime(drop.get("startAt"))
            ends_at = self.__parse_twitch_datetime(drop.get("endAt"))
            if starts_at is not None and now < starts_at:
                continue
            if ends_at is not None and now > ends_at:
                continue

            minutes_watched = 0
            drop_self = drop.get("self")
            if isinstance(drop_self, dict):
                if drop_self.get("isClaimed") is True:
                    continue
                if drop_self.get("hasPreconditionsMet") is False:
                    continue
                minutes_watched = drop_self.get("currentMinutesWatched") or 0
                if minutes_watched >= minutes_required:
                    continue
            else:
                benefit_ids = {
                    str(benefit.get("id"))
                    for edge in drop.get("benefitEdges", []) or []
                    for benefit in [
                        edge.get("benefit") if isinstance(edge, dict) else None
                    ]
                    if isinstance(benefit, dict) and benefit.get("id") not in [None, ""]
                }
                has_repeated_benefit = any(
                    campaign_benefit_counts.get(benefit_id, 0) > 1
                    for benefit_id in benefit_ids
                )
                if has_repeated_benefit is False and self.__drop_benefits_were_awarded(
                    drop,
                    awarded_benefit_ids,
                    awarded_benefit_fingerprints,
                ):
                    continue

            remaining_minutes = max(minutes_required - minutes_watched, 0)
            time_left_minutes = (
                (ends_at - now).total_seconds() / 60 if ends_at is not None else None
            )
            campaign_name = campaign.get("name") or "Unknown Campaign"
            drop_name = drop.get("name") or "Unknown Drop"

            if time_left_minutes is not None and remaining_minutes > time_left_minutes:
                logger.info(
                    f"{Fore.RED}Not enough time for {campaign_name} - {drop_name}: "
                    f"needs {remaining_minutes:.0f}m, "
                    f"{max(time_left_minutes, 0):.0f}m left{Fore.RESET}",
                    extra={"emoji": ":red_circle:"},
                )
                continue

            time_left_label = (
                f"{max(time_left_minutes, 0):.0f}m left"
                if time_left_minutes is not None
                else "no known deadline"
            )
            logger.info(
                f"{Fore.BLUE}Enough time for {campaign_name} - {drop_name}: "
                f"needs {remaining_minutes:.0f}m, {time_left_label}{Fore.RESET}",
                extra={"emoji": ":large_blue_circle:"},
            )
            deadline = ends_at or datetime.max
            if earliest_deadline is None or deadline < earliest_deadline:
                earliest_deadline = deadline

        return earliest_deadline

    def __active_drop_category_slugs_from_campaigns(
        self,
        inventory: dict,
        requested_category_slugs: set,
    ) -> Dict[str, datetime]:
        active_deadlines = {}
        twitch_category_slugs = set()
        dashboard_campaigns = self.__get_drops_dashboard(status="OPEN")
        raw_query_campaigns, _ = self.__get_reward_campaigns_raw_query()
        helix_campaigns, _ = self.__get_open_drop_campaigns_from_helix()
        campaigns_by_id = {}
        for campaign in dashboard_campaigns + raw_query_campaigns + helix_campaigns:
            if not isinstance(campaign, dict):
                continue
            campaign_id = campaign.get("id")
            if campaign_id in [None, ""]:
                continue
            if self.__is_open_drop_campaign(campaign) is not True:
                continue
            campaign_id = str(campaign_id)
            existing = campaigns_by_id.get(campaign_id)
            if existing is None or (
                not existing.get("timeBasedDrops") and campaign.get("timeBasedDrops")
            ):
                campaigns_by_id[campaign_id] = campaign

        # Campaigns can be extended after Twitch has already returned a populated
        # dashboard summary. Always refresh their details so a newly appended drop
        # is not hidden by that stale summary.
        campaigns_to_refresh = []
        for campaign in campaigns_by_id.values():
            game = campaign.get("game") or {}
            game_name = (game.get("displayName") or game.get("name") or "").strip()
            if game_name and self.__slugify(game_name) in requested_category_slugs:
                campaigns_to_refresh.append(campaign)

        for campaign in self.__get_campaigns_details(campaigns_to_refresh):
            if not isinstance(campaign, dict):
                continue
            campaign_id = campaign.get("id")
            if campaign_id not in [None, ""]:
                campaigns_by_id[str(campaign_id)] = campaign

        inventory_campaigns = {
            str(campaign.get("id")): campaign
            for campaign in inventory.get("dropCampaignsInProgress", []) or []
            if isinstance(campaign, dict) and campaign.get("id") not in [None, ""]
        }
        for campaign_id, campaign in inventory_campaigns.items():
            if campaign_id not in campaigns_by_id:
                campaigns_by_id[campaign_id] = campaign
        completed_drop_ids = self.__completed_drop_ids_from_inventory(inventory)
        awarded_benefit_ids, awarded_benefit_fingerprints = self.__awarded_benefits(
            inventory
        )
        campaign_evaluations = []

        for campaign_id, campaign in campaigns_by_id.items():
            if not isinstance(campaign, dict):
                continue
            inventory_campaign = inventory_campaigns.get(campaign_id)
            if inventory_campaign is not None:
                campaign = self.__merge_campaign_inventory_progress(
                    campaign, inventory_campaign
                )
            game = campaign.get("game") or {}
            game_name = (game.get("displayName") or game.get("name") or "").strip()
            game_slug = self.__slugify(game_name) if game_name else ""
            if game_slug:
                twitch_category_slugs.add(game_slug)
            if game_slug not in requested_category_slugs:
                continue

            deadline = self.__active_incomplete_drop_deadline(
                campaign,
                completed_drop_ids,
                awarded_benefit_ids,
                awarded_benefit_fingerprints,
            )
            campaign_evaluations.append(
                {
                    "campaign": campaign.get("name"),
                    "campaign_id": campaign_id,
                    "game": game_name,
                    "drops": [
                        drop.get("name")
                        for drop in campaign.get("timeBasedDrops", []) or []
                        if isinstance(drop, dict)
                    ],
                    "active_incomplete": deadline is not None,
                }
            )
            if deadline is None:
                continue

            current_deadline = active_deadlines.get(game_slug)
            if current_deadline is None or deadline < current_deadline:
                active_deadlines[game_slug] = deadline

        self.__log_drop_check_json(
            "Twitch campaign evaluation for configured categories",
            campaign_evaluations,
            level=self.category_log_level,
            category_log=True,
        )

        return active_deadlines, twitch_category_slugs

    def __twitchdrops_app_fallback(
        self, categories, active_twitch_category_slugs, awarded_benefit_fingerprints
    ):
        deadlines = {}
        self.twitchdrops_app_campaigns = {}
        scraper = TwitchDropsAppScraper(timeout=20)
        now = datetime.utcnow()
        awarded_names = {
            name
            for name, image_url in awarded_benefit_fingerprints
            if name and image_url == ""
        }
        checked_twitchdrops_app = False

        for category in categories:
            category_name, _ = self.__split_category_streamer_selector(category)
            normalized = self.__normalize_category(category_name)
            requested_slug = self.__slugify(normalized.replace("-", " "))
            if not requested_slug or requested_slug in active_twitch_category_slugs:
                continue

            if checked_twitchdrops_app:
                time.sleep(random.uniform(*TWITCHDROPS_APP_CHECK_DELAY_SECONDS))
            checked_twitchdrops_app = True

            try:
                report = scraper.scrape(normalized)
            except (ValueError, requests.RequestException) as error:
                self.__log_drop_check(
                    f"twitchdrops.app fallback failed for '{category_name}': {error}",
                    level=logging.DEBUG,
                )
                continue

            campaigns = []
            completed_campaigns = []
            campaign_evaluations = []
            for campaign in report.get("campaigns", []):
                ends_at = self.__parse_twitch_datetime(campaign.get("ends_at"))
                if ends_at is None or ends_at <= now:
                    continue
                drop_names = {
                    str(drop.get("name") or "").strip().lower()
                    for drop in campaign.get("drops", [])
                    if str(drop.get("name") or "").strip()
                }
                missing_drop_names = sorted(drop_names - awarded_names)
                campaign_evaluations.append(
                    {
                        "campaign": campaign.get("name"),
                        "drops": sorted(drop_names),
                        "missing_or_unawarded": missing_drop_names,
                        "fully_collected": bool(drop_names) and not missing_drop_names,
                    }
                )
                if drop_names and drop_names.issubset(awarded_names):
                    completed_campaigns.append(
                        {
                            "campaign": campaign.get("name"),
                            "drops": sorted(drop_names),
                        }
                    )
                    continue
                campaigns.append(campaign)
                deadline = deadlines.get(requested_slug)
                if deadline is None or ends_at < deadline:
                    deadlines[requested_slug] = ends_at

            self.__log_drop_check_json(
                f"twitchdrops.app campaign evaluation for '{category_name}'",
                campaign_evaluations,
                level=self.category_log_level,
                category_log=True,
            )

            if completed_campaigns:
                self.__log_category(
                    f"Skipped {len(completed_campaigns)} fully collected "
                    f"twitchdrops.app campaigns for '{category_name}'",
                    extra={"emoji": ":white_check_mark:"},
                )
                self.__log_drop_check_json(
                    f"twitchdrops.app completed campaigns for '{category_name}'",
                    completed_campaigns,
                    level=self.category_log_level,
                    category_log=True,
                )

            if campaigns:
                self.twitchdrops_app_campaigns[requested_slug] = campaigns
                channel_count = len(
                    {
                        login
                        for campaign in campaigns
                        for login in campaign.get("channels", [])
                    }
                )
                self.__log_category(
                    "Using twitchdrops.app fallback for "
                    f"'{report.get('game') or category_name}': "
                    f"{len(campaigns)} campaigns, {channel_count} restricted channels",
                    extra={"emoji": ":globe_with_meridians:"},
                )

        return deadlines

    def filter_categories_with_active_drops(
        self, categories: List[str], order="ORDER", drops_enabled: bool = True
    ) -> List[str]:
        if not categories:
            return []

        inventory = self.__get_inventory()
        if not isinstance(inventory, dict) or inventory == {}:
            logger.warning(
                "Unable to load drops inventory; using configured categories without campaign filter",
                extra={"emoji": ":warning:"},
            )
            return categories

        requested_category_slugs = set()
        for category in categories:
            category_name, _ = self.__split_category_streamer_selector(category)
            normalized_category = self.__normalize_category(category_name)
            if normalized_category:
                requested_category_slugs.add(
                    self.__slugify(normalized_category.replace("-", " "))
                )
        (
            active_category_deadlines,
            _twitch_category_slugs,
        ) = self.__active_drop_category_slugs_from_campaigns(
            inventory,
            requested_category_slugs,
        )
        _, awarded_benefit_fingerprints = self.__awarded_benefits(inventory)
        active_category_deadlines.update(
            self.__twitchdrops_app_fallback(
                categories,
                set(active_category_deadlines),
                awarded_benefit_fingerprints,
            )
        )
        if active_category_deadlines == {}:
            for requested_slug in requested_category_slugs:
                self.__replace_category_campaign_eligibility(requested_slug, {})
            self.__log_category(
                "No active incomplete drop campaigns found for category discovery",
                extra={"emoji": ":sleeping:"},
            )
            return []

        filtered_categories = []
        for category in categories:
            category_name, _ = self.__split_category_streamer_selector(category)
            normalized_category = self.__normalize_category(category_name)
            if normalized_category == "":
                continue
            requested_slug = self.__slugify(normalized_category.replace("-", " "))
            if requested_slug in active_category_deadlines:
                filtered_categories.append(category)
            else:
                self.__replace_category_campaign_eligibility(requested_slug, {})
                self.__log_category(
                    f"Skip category '{category}' because no active incomplete campaign matches it",
                    extra={"emoji": ":no_entry:"},
                )

        order_name = getattr(order, "value", str(order)).upper().strip()
        if "." in order_name:
            order_name = order_name.split(".")[-1]
        if order_name == "EXPIRATION":
            filtered_categories.sort(
                key=lambda category: active_category_deadlines.get(
                    self.__slugify(
                        self.__normalize_category(
                            self.__split_category_streamer_selector(category)[0]
                        ).replace("-", " ")
                    ),
                    datetime.max,
                )
            )

        return filtered_categories

    def __describe_campaigns(self, campaigns):
        labels = []
        for campaign in campaigns or []:
            game_name = None
            if getattr(campaign, "game", None) not in [None, {}]:
                game_name = campaign.game.get("displayName") or campaign.game.get(
                    "name"
                )

            if game_name and campaign.name:
                labels.append(f"{game_name} drop campaign '{campaign.name}'")
            elif campaign.name:
                labels.append(f"drop campaign '{campaign.name}'")
            elif game_name:
                labels.append(f"{game_name} drop campaign")

        seen = set()
        unique_labels = []
        for label in labels:
            if label not in seen:
                seen.add(label)
                unique_labels.append(label)

        return ", ".join(unique_labels)

    def __campaign_signature(self, campaigns):
        campaign_ids = [
            campaign.id for campaign in campaigns or [] if getattr(campaign, "id", None)
        ]
        return "|".join(sorted(campaign_ids))

    def __watch_signature(self, streamer):
        game_label = self.__stream_game_label(streamer.stream)
        return "|".join(
            [
                str(streamer.username or ""),
                str(game_label or ""),
            ]
        )

    def __stream_game_label(self, stream):
        if getattr(stream, "game", None) in [None, {}]:
            return "Unknown"
        return stream.game.get("displayName") or stream.game.get("name") or "Unknown"

    def __category_drops_condition(self, streamer):
        if (
            getattr(streamer, "from_category", False) is not True
            or streamer.settings.claim_drops is not True
            or streamer.is_online is not True
        ):
            return False

        game_name = streamer.stream.game_name()
        if not game_name:
            return False

        game_slug = self.__slugify(game_name)
        eligibility = self.category_campaign_eligibility.get(
            (game_slug, streamer.username)
        )
        if eligibility is not None:
            eligible_campaigns, _ = eligibility
            return eligible_campaigns > 0

        # Category discovery has already removed fully collected campaigns.
        # Use its remaining twitchdrops.app campaign data when Twitch's private
        # campaign query fails to populate Stream.campaigns.
        for campaign in self.twitchdrops_app_campaigns.get(game_slug, []):
            channels = {
                str(login).lower().strip()
                for login in campaign.get("channels", []) or []
            }
            if not channels or streamer.username in channels:
                return True

        return False

    def __drops_condition(self, streamer):
        if streamer.drops_condition() is True:
            return True

        category_eligible = self.__category_drops_condition(streamer)
        if category_eligible:
            logger.debug(
                "[drops-check] Using category campaign eligibility fallback for "
                f"streamer={streamer.username} "
                f"game={self.__stream_game_label(streamer.stream)}"
            )
        return category_eligible

    def __log_watched_streamers(self, streamers, streamers_watching):
        points_streams = [streamers[index].username for index in streamers_watching]
        drops_streams = []

        for index in streamers_watching:
            streamer = streamers[index]
            if self.__drops_condition(streamer) is not True:
                continue

            campaigns = self.__describe_campaigns(streamer.stream.campaigns)
            drops_streams.append(
                f"{streamer.username} ({campaigns})"
                if campaigns
                else (
                    f"{streamer.username} "
                    f"({self.__stream_game_label(streamer.stream)} drops)"
                )
            )

        logger.info(
            f"{Fore.GREEN}Watching for points: "
            f"{', '.join(points_streams) if points_streams else 'none'}; "
            "watching for drops: "
            f"{', '.join(drops_streams) if drops_streams else 'none'}{Fore.RESET}",
            extra={
                "emoji": ":eye:",
                "event": Events.DROP_STATUS,
                "skip_telegram": True,
                "skip_discord": True,
                "skip_webhook": True,
                "skip_matrix": True,
                "skip_gotify": True,
            },
        )

    def __helix_get(
        self,
        endpoint: str,
        params: Optional[dict] = None,
        suppress_status_codes: Optional[set] = None,
    ) -> dict:
        try:
            response = requests.get(
                f"https://api.twitch.tv/helix/{endpoint}",
                params=params,
                headers={
                    "Authorization": f"Bearer {self.twitch_login.get_auth_token()}",
                    "Client-Id": CLIENT_ID,
                    "User-Agent": self.user_agent,
                },
                timeout=20,
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            status_code = None
            if hasattr(e, "response") and e.response is not None:
                status_code = e.response.status_code

            if (
                suppress_status_codes is not None
                and status_code in suppress_status_codes
            ):
                logger.debug(
                    f"Skipping unsupported Helix endpoint '{endpoint}' (status: {status_code})"
                )
                return {}

            logger.error(f"Error with Helix endpoint '{endpoint}': {e}")
            return {}

    def get_live_streamers_for_category(
        self,
        category: str,
        drops_enabled: bool = True,
        limit: int = 30,
        sort_by: Any = "VIEWERS_DESC",
    ) -> List[str]:
        if not category:
            return []

        (
            category_name,
            forced_streamer_username,
        ) = self.__split_category_streamer_selector(category)
        if not category_name:
            return []

        normalized_category = self.__normalize_category(category_name)
        query = normalized_category.replace("-", " ")
        categories_response = self.__helix_get(
            "search/categories", {"query": query, "first": 100}
        )
        categories = categories_response.get("data", []) or []

        if categories == []:
            logger.warning(
                f"No category found for '{category_name}'",
                extra={"emoji": ":mag:"},
            )
            return []

        selected_category = None
        for candidate in categories:
            name_slug = self.__slugify(candidate.get("name", ""))
            if name_slug == normalized_category:
                selected_category = candidate
                break

        if selected_category is None:
            for candidate in categories:
                if candidate.get("name", "").lower() == query.lower():
                    selected_category = candidate
                    break

        if selected_category is None:
            selected_category = categories[0]

        game_id = selected_category.get("id")
        game_name = selected_category.get("name", category_name)
        if not game_id:
            return []

        fallback_campaigns = self.twitchdrops_app_campaigns.get(
            self.__slugify(normalized_category.replace("-", " ")), []
        )
        if forced_streamer_username is None and fallback_campaigns:
            if any(campaign.get("channels") for campaign in fallback_campaigns):
                return self.__get_live_restricted_campaign_streamers(
                    fallback_campaigns,
                    game_id,
                    game_name,
                    drops_enabled=drops_enabled,
                )

        if forced_streamer_username is not None:
            stream_response = self.__helix_get(
                "streams",
                {"user_login": forced_streamer_username, "first": 1},
            )
            forced_stream = (stream_response.get("data") or [None])[0]
            if not isinstance(forced_stream, dict):
                self.__log_category(
                    f"Forced category streamer '{forced_streamer_username}' is not live for '{game_name}'",
                    extra={"emoji": ":sleeping:"},
                )
                return []

            if str(forced_stream.get("game_id") or "") != str(game_id):
                self.__log_category(
                    f"Forced category streamer '{forced_streamer_username}' is live but not in '{game_name}'",
                    extra={"emoji": ":no_entry:"},
                )
                return []

            if drops_enabled is True:
                stream_tags = forced_stream.get("tags", []) or []
                normalized_tags = [tag.replace(" ", "").lower() for tag in stream_tags]
                if "dropsenabled" not in normalized_tags:
                    self.__log_category(
                        f"Forced category streamer '{forced_streamer_username}' is in '{game_name}' but missing DropsEnabled tag",
                        extra={"emoji": ":no_entry:"},
                    )
                    return []

            self.__log_category(
                f"Using forced category streamer '{forced_streamer_username}' for '{game_name}'",
                extra={"emoji": ":satellite:"},
            )
            game_slug = self.__slugify(game_name)
            self.__replace_category_campaign_eligibility(
                game_slug,
                ({forced_streamer_username: (1, 1)} if drops_enabled is True else {}),
            )
            return [forced_streamer_username]

        log_suffix = " with DropsEnabled tag" if drops_enabled else ""
        self.__log_category(
            f"Searching live channels for '{game_name}'{log_suffix}",
            extra={"emoji": ":satellite:"},
        )

        sort_key = self.__normalize_category_sort(sort_by)

        stream_candidates = []
        usernames_seen = set()
        cursor = None
        max_results = max(limit, 1)
        search_window = (
            max_results
            if sort_key in ["ORDER", "VIEWERS_DESC"]
            else min(max_results * 3, 300)
        )

        while len(stream_candidates) < search_window:
            first = min(100, search_window - len(stream_candidates))
            params = {
                "type": "live",
                "game_id": game_id,
                "first": first,
            }
            if cursor is not None:
                params["after"] = cursor

            streams_response = self.__helix_get("streams", params)
            streams = streams_response.get("data", []) or []
            if streams == []:
                break

            for stream in streams:
                if drops_enabled is True:
                    stream_tags = stream.get("tags", []) or []
                    normalized_tags = [
                        tag.replace(" ", "").lower() for tag in stream_tags
                    ]
                    if "dropsenabled" not in normalized_tags:
                        continue

                username = (stream.get("user_login") or "").lower().strip()
                if username and username not in usernames_seen:
                    usernames_seen.add(username)
                    stream_candidates.append(stream)
                    if len(stream_candidates) >= search_window:
                        break

            cursor = (streams_response.get("pagination") or {}).get("cursor")
            if not cursor:
                break

        if sort_key == "VIEWERS_ASC":
            stream_candidates = sorted(
                stream_candidates,
                key=lambda x: x.get("viewer_count", 0),
            )
        elif sort_key == "VIEWERS_DESC":
            stream_candidates = sorted(
                stream_candidates,
                key=lambda x: x.get("viewer_count", 0),
                reverse=True,
            )
        elif sort_key == "STARTED_AT_ASC":
            stream_candidates = sorted(
                stream_candidates,
                key=lambda x: x.get("started_at") or "",
            )
        elif sort_key == "STARTED_AT_DESC":
            stream_candidates = sorted(
                stream_candidates,
                key=lambda x: x.get("started_at") or "",
                reverse=True,
            )
        elif sort_key == "RANDOM":
            random.shuffle(stream_candidates)

        usernames = []
        for stream in stream_candidates[:max_results]:
            username = (stream.get("user_login") or "").lower().strip()
            if username:
                usernames.append(username)

        game_slug = self.__slugify(game_name)
        total_campaigns = len(fallback_campaigns) or 1
        self.__replace_category_campaign_eligibility(
            game_slug,
            (
                {username: (total_campaigns, total_campaigns) for username in usernames}
                if drops_enabled is True
                else {}
            ),
        )

        self.__log_category(
            f"Found {len(usernames)} live channels for '{game_name}' (sort: {sort_key})",
            extra={"emoji": ":satellite_antenna:"},
        )
        return usernames

    def __get_live_restricted_campaign_streamers(
        self, campaigns, game_id, game_name, drops_enabled=True, target_per_campaign=20
    ):
        campaign_login_lists = [
            list(dict.fromkeys(login.lower() for login in campaign.get("channels", [])))
            for campaign in campaigns
            if campaign.get("channels")
        ]
        campaign_logins = [set(logins) for logins in campaign_login_lists]
        overlap_counts = {}
        first_seen = {}
        for logins in campaign_login_lists:
            for login in logins:
                overlap_counts[login] = overlap_counts.get(login, 0) + 1
                if login not in first_seen:
                    first_seen[login] = len(first_seen)
        ranked_logins = sorted(
            overlap_counts,
            key=lambda login: (-overlap_counts[login], first_seen[login]),
        )

        live_streams = {}
        checked = 0
        for login_chunk in create_chunks(ranked_logins, 100):
            response = self.__helix_get(
                "streams", {"user_login": login_chunk, "first": 100}
            )
            checked += len(login_chunk)
            for stream in response.get("data", []) or []:
                if str(stream.get("game_id") or "") != str(game_id):
                    continue
                if drops_enabled is True:
                    normalized_tags = [
                        tag.replace(" ", "").lower()
                        for tag in stream.get("tags", []) or []
                    ]
                    if "dropsenabled" not in normalized_tags:
                        continue
                login = (stream.get("user_login") or "").lower().strip()
                if login:
                    live_streams[login] = stream

            if all(
                len(logins.intersection(live_streams)) >= target_per_campaign
                for logins in campaign_logins
            ):
                break

        sorted_live_streams = sorted(
            live_streams.values(),
            key=lambda stream: stream.get("viewer_count", 0),
            reverse=True,
        )
        unrestricted_campaigns = sum(
            1 for campaign in campaigns if not campaign.get("channels")
        )
        eligible_campaign_counts = {
            login: unrestricted_campaigns
            + sum(1 for logins in campaign_logins if login in logins)
            for login in live_streams
        }
        usernames = []
        selected = set()
        for logins in campaign_logins:
            campaign_selected = 0
            for stream in sorted_live_streams:
                login = (stream.get("user_login") or "").lower().strip()
                if login not in logins:
                    continue
                campaign_selected += 1
                if login not in selected:
                    selected.add(login)
                    usernames.append(login)
                if campaign_selected >= target_per_campaign:
                    break
        usernames.sort(key=lambda login: -eligible_campaign_counts.get(login, 0))
        game_slug = self.__slugify(game_name)
        self.__replace_category_campaign_eligibility(
            game_slug,
            {
                login: (eligible_campaign_counts[login], len(campaigns))
                for login in usernames
            },
        )
        self.__log_category(
            f"Checked {checked}/{len(ranked_logins)} twitchdrops.app channels for "
            f"'{game_name}'; selected {len(usernames)} live across "
            f"{len(campaigns)} campaigns",
            extra={"emoji": ":satellite_antenna:"},
        )
        return usernames

    def __normalize_category_sort(self, sort_by: Any) -> str:
        if sort_by is None:
            return "VIEWERS_DESC"

        # Support enum values (CategorySort.VIEWERS_DESC) and plain strings ("VIEWERS_DESC").
        sort_name = getattr(sort_by, "name", str(sort_by))
        sort_name = sort_name.upper().strip()
        if "." in sort_name:
            sort_name = sort_name.split(".")[-1]

        allowed = {
            "ORDER",
            "VIEWERS_DESC",
            "VIEWERS_ASC",
            "STARTED_AT_DESC",
            "STARTED_AT_ASC",
            "RANDOM",
        }
        return sort_name if sort_name in allowed else "VIEWERS_DESC"

    def update_raid(self, streamer, raid):
        if streamer.raid != raid:
            streamer.raid = raid
            json_data = copy.deepcopy(GQLOperations.JoinRaid)
            json_data["variables"] = {"input": {"raidID": raid.raid_id}}
            self.post_gql_request(json_data)

            logger.info(
                f"Joining raid from {streamer} to {raid.target_login}!",
                extra={"emoji": ":performing_arts:", "event": Events.JOIN_RAID},
            )

    def viewer_is_mod(self, streamer):
        json_data = copy.deepcopy(GQLOperations.ModViewChannelQuery)
        json_data["variables"] = {"channelLogin": streamer.username}
        response = self.post_gql_request(json_data)
        try:
            streamer.viewer_is_mod = response["data"]["user"]["self"]["isModerator"]
        except (ValueError, KeyError):
            streamer.viewer_is_mod = False

    # === 'GLOBALS' METHODS === #
    # Create chunk of sleep of speed-up the break loop after CTRL+C
    def __chuncked_sleep(self, seconds, chunk_size=3):
        sleep_time = max(seconds, 0) / chunk_size
        for i in range(0, chunk_size):
            time.sleep(sleep_time)
            if self.running is False:
                break

    def __check_connection_handler(self, chunk_size):
        # The success rate It's very hight usually. Why we have failed?
        # Check internet connection ...
        while internet_connection_available() is False:
            random_sleep = random.randint(1, 3)
            logger.warning(
                f"No internet connection available! Retry after {random_sleep}m"
            )
            self.__chuncked_sleep(random_sleep * 60, chunk_size=chunk_size)

    def post_gql_request(self, json_data):
        try:
            response = requests.post(
                GQLOperations.url,
                json=json_data,
                headers={
                    "Authorization": f"OAuth {self.twitch_login.get_auth_token()}",
                    "Client-Id": CLIENT_ID,
                    # "Client-Integrity": self.post_integrity(),
                    "Client-Session-Id": self.client_session,
                    "Client-Version": self.update_client_version(),
                    "User-Agent": self.user_agent,
                    "X-Device-Id": self.device_id,
                },
                timeout=(5, 30),
            )
            logger.debug(
                f"Data: {json_data}, Status code: {response.status_code}, Content: {response.text}"
            )
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(
                f"Error with GQLOperations ({json_data['operationName']}): {e}"
            )
            return {}

    # Request for Integrity Token
    # Twitch needs Authorization, Client-Id, X-Device-Id to generate JWT which is used for authorize gql requests
    # Regenerate Integrity Token 5 minutes before expire
    """def post_integrity(self):
        if (
            self.integrity_expire - datetime.now().timestamp() * 1000 > 5 * 60 * 1000
            and self.integrity is not None
        ):
            return self.integrity
        try:
            response = requests.post(
                GQLOperations.integrity_url,
                json={},
                headers={
                    "Authorization": f"OAuth {self.twitch_login.get_auth_token()}",
                    "Client-Id": CLIENT_ID,
                    "Client-Session-Id": self.client_session,
                    "Client-Version": self.update_client_version(),
                    "User-Agent": self.user_agent,
                    "X-Device-Id": self.device_id,
                },
            )
            logger.debug(
                f"Data: [], Status code: {response.status_code}, Content: {response.text}"
            )
            self.integrity = response.json().get("token", None)
            # logger.info(f"integrity: {self.integrity}")

            if self.isBadBot(self.integrity) is True:
                logger.info(
                    "Uh-oh, Twitch has detected this miner as a \"Bad Bot\". Don't worry.")

            self.integrity_expire = response.json().get("expiration", 0)
            # logger.info(f"integrity_expire: {self.integrity_expire}")
            return self.integrity
        except requests.exceptions.RequestException as e:
            logger.error(f"Error with post_integrity: {e}")
            return self.integrity

    # verify the integrity token's contents for the "is_bad_bot" flag
    def isBadBot(self, integrity):
        stripped_token: str = self.integrity.split('.')[2] + "=="
        messy_json: str = urlsafe_b64decode(
            stripped_token.encode()).decode(errors="ignore")
        match = re.search(r'(.+)(?<="}).+$', messy_json)
        if match is None:
            # raise MinerException("Unable to parse the integrity token")
            logger.info("Unable to parse the integrity token. Don't worry.")
            return
        decoded_header = json.loads(match.group(1))
        # logger.info(f"decoded_header: {decoded_header}")
        if decoded_header.get("is_bad_bot", "false") != "false":
            return True
        else:
            return False"""

    def update_client_version(self):
        try:
            response = requests.get(URL, timeout=(5, 20))
            if response.status_code != 200:
                logger.debug(
                    f"Error with update_client_version: {response.status_code}"
                )
                return self.client_version
            matcher = re.search(self.twilight_build_id_pattern, response.text)
            if not matcher:
                logger.debug("Error with update_client_version: no match")
                return self.client_version
            self.client_version = matcher.group(1)
            logger.debug(f"Client version: {self.client_version}")
            return self.client_version
        except requests.exceptions.RequestException as e:
            logger.error(f"Error with update_client_version: {e}")
            return self.client_version

    def send_minute_watched_events(self, streamers, priority, chunk_size=3):
        while self.running:
            iteration_started_at = time.time()
            try:
                streamers_index = [
                    i
                    for i in range(0, len(streamers))
                    if streamers[i].is_online is True
                    and (
                        streamers[i].online_at == 0
                        or (time.time() - streamers[i].online_at) > 30
                    )
                ]

                for index in streamers_index:
                    if (streamers[index].stream.update_elapsed() / 60) > 10:
                        # Why this user It's currently online but the last updated was more than 10minutes ago?
                        # Please perform a manually update and check if the user it's online
                        self.check_streamer_online(streamers[index])

                """
                Twitch has a limit - you can't watch more than 2 channels at one time.
                We'll take the first two streamers from the final list as they have the highest priority.
                """
                has_category_streamers = any(
                    getattr(streamer, "from_category", False) is True
                    for streamer in streamers
                )

                # Drops progress is counted on one stream at a time. When category
                # discovery is enabled, keep watch selection to a single stream.
                max_watch_amount = 1 if has_category_streamers else 2
                streamers_watching = set()

                def remaining_watch_amount():
                    return max_watch_amount - len(streamers_watching)

                for prior in priority:
                    if remaining_watch_amount() <= 0:
                        break

                    if prior == Priority.ORDER:
                        # Get the first 2 items, they are already in order
                        streamers_watching.update(
                            streamers_index[: remaining_watch_amount()]
                        )

                    elif prior in [
                        Priority.POINTS_ASCENDING,
                        Priority.POINTS_DESCENDING,
                    ]:
                        items = [
                            {"points": streamers[index].channel_points, "index": index}
                            for index in streamers_index
                        ]
                        items = sorted(
                            items,
                            key=lambda x: x["points"],
                            reverse=(
                                True if prior == Priority.POINTS_DESCENDING else False
                            ),
                        )
                        streamers_watching.update(
                            [item["index"] for item in items][
                                : remaining_watch_amount()
                            ]
                        )

                    elif prior == Priority.STREAK:
                        """
                        Check if we need need to change priority based on watch streak
                        Viewers receive points for returning for x consecutive streams.
                        Each stream must be at least 10 minutes long and it must have been at least 30 minutes since the last stream ended.
                        Watch at least 6m for get the +10
                        """
                        for index in streamers_index:
                            if (
                                streamers[index].settings.watch_streak is True
                                and streamers[index].stream.watch_streak_missing is True
                                and (
                                    streamers[index].offline_at == 0
                                    or (
                                        (time.time() - streamers[index].offline_at)
                                        // 60
                                    )
                                    > 30
                                )
                                # fix #425
                                and streamers[index].stream.minute_watched < 11
                            ):
                                streamers_watching.add(index)
                                if remaining_watch_amount() <= 0:
                                    break

                    elif prior == Priority.DROPS:
                        for index in streamers_index:
                            if self.__drops_condition(streamers[index]) is True:
                                streamers_watching.add(index)
                                if remaining_watch_amount() <= 0:
                                    break

                    elif prior == Priority.SUBSCRIBED:
                        streamers_with_multiplier = [
                            index
                            for index in streamers_index
                            if streamers[index].viewer_has_points_multiplier()
                        ]
                        streamers_with_multiplier = sorted(
                            streamers_with_multiplier,
                            key=lambda x: streamers[x].total_points_multiplier(),
                            reverse=True,
                        )
                        streamers_watching.update(
                            streamers_with_multiplier[: remaining_watch_amount()]
                        )

                streamers_watching = list(streamers_watching)[:max_watch_amount]

                # Safety net: never watch more than one category-discovered streamer
                # in the same loop iteration.
                category_picks = 0
                filtered_streamers_watching = []
                for index in streamers_watching:
                    if getattr(streamers[index], "from_category", False) is True:
                        if category_picks >= 1:
                            continue
                        category_picks += 1
                    filtered_streamers_watching.append(index)
                streamers_watching = filtered_streamers_watching

                self.__log_watched_streamers(streamers, streamers_watching)

                for index in streamers_watching:
                    # next_iteration = time.time() + 60 / len(streamers_watching)
                    next_iteration = time.time() + 20 / len(streamers_watching)

                    try:
                        ####################################
                        # Start of fix for 2024/5 API Change
                        # Create the JSON data for the GraphQL request
                        json_data = copy.deepcopy(GQLOperations.PlaybackAccessToken)
                        json_data["variables"] = {
                            "login": streamers[index].username,
                            "isLive": True,
                            "isVod": False,
                            "vodID": "",
                            "playerType": "site",
                            # "playerType": "picture-by-picture",
                        }

                        # Get signature and value using the post_gql_request method
                        try:
                            responsePlaybackAccessToken = self.post_gql_request(
                                json_data
                            )
                            logger.debug(
                                f"Sent PlaybackAccessToken request for {streamers[index]}"
                            )

                            if self.__is_unauthorized_response(
                                responsePlaybackAccessToken
                            ):
                                self.__request_authentication_restart()
                                return

                            if "data" not in responsePlaybackAccessToken:
                                logger.error(
                                    f"Invalid response from Twitch: {responsePlaybackAccessToken}"
                                )
                                continue

                            streamPlaybackAccessToken = responsePlaybackAccessToken[
                                "data"
                            ].get("streamPlaybackAccessToken", {})
                            signature = streamPlaybackAccessToken.get("signature")
                            value = streamPlaybackAccessToken.get("value")

                            if not signature or not value:
                                logger.error(
                                    f"Missing signature or value in Twitch response: {responsePlaybackAccessToken}"
                                )
                                continue

                        except Exception as e:
                            logger.error(
                                f"Error fetching PlaybackAccessToken for {streamers[index]}: {str(e)}"
                            )
                            continue

                        # encoded_value = quote(json.dumps(value))

                        # Construct the URL for the broadcast qualities
                        RequestBroadcastQualitiesURL = f"https://usher.ttvnw.net/api/channel/hls/{streamers[index].username}.m3u8?sig={signature}&token={value}"

                        # Get list of video qualities
                        responseBroadcastQualities = requests.get(
                            RequestBroadcastQualitiesURL,
                            headers={"User-Agent": self.user_agent},
                            timeout=20,
                        )  # timeout=60
                        logger.debug(
                            f"Send RequestBroadcastQualitiesURL request for {streamers[index]} - Status code: {responseBroadcastQualities.status_code}"
                        )
                        if responseBroadcastQualities.status_code != 200:
                            continue
                        BroadcastQualities = responseBroadcastQualities.text

                        # Just takes the last line, which should be the URL for the lowest quality
                        BroadcastLowestQualityURL = BroadcastQualities.split("\n")[-1]
                        if not validators.url(BroadcastLowestQualityURL):
                            continue

                        # Get list of video URLs
                        responseStreamURLList = requests.get(
                            BroadcastLowestQualityURL,
                            headers={"User-Agent": self.user_agent},
                            timeout=20,
                        )  # timeout=60
                        logger.debug(
                            f"Send BroadcastLowestQualityURL request for {streamers[index]} - Status code: {responseStreamURLList.status_code}"
                        )
                        if responseStreamURLList.status_code != 200:
                            continue
                        StreamURLList = responseStreamURLList.text

                        # Just takes the last line, which should be the URL for the lowest quality
                        StreamLowestQualityURL = StreamURLList.split("\n")[-2]
                        if not validators.url(StreamLowestQualityURL):
                            continue

                        # Perform a HEAD request to simulate watching the stream
                        responseStreamLowestQualityURL = requests.head(
                            StreamLowestQualityURL,
                            headers={"User-Agent": self.user_agent},
                            timeout=20,
                        )  # timeout=60
                        logger.debug(
                            f"Send StreamLowestQualityURL request for {streamers[index]} - Status code: {responseStreamLowestQualityURL.status_code}"
                        )
                        if responseStreamLowestQualityURL.status_code != 200:
                            continue
                        # End of fix for 2024/5 API Change
                        ##################################
                        response = requests.post(
                            streamers[index].stream.spade_url,
                            data=streamers[index].stream.encode_payload(),
                            headers={"User-Agent": self.user_agent},
                            # timeout=60,
                            timeout=20,
                        )
                        logger.debug(
                            f"Send minute watched request for {streamers[index]} - Status code: {response.status_code}"
                        )
                        if response.status_code == 204:
                            streamers[index].stream.update_minute_watched()

                            """
                            Remember, you can only earn progress towards a time-based Drop on one participating channel at a time.  [ ! ! ! ]
                            You can also check your progress towards Drops within a campaign anytime by viewing the Drops Inventory.
                            For time-based Drops, if you are unable to claim the Drop in time, you will be able to claim it from the inventory page until the Drops campaign ends.
                            """

                            for campaign in streamers[index].stream.campaigns:
                                for drop in campaign.drops:
                                    if (
                                        drop.has_preconditions_met is not False
                                        and drop.is_claimed is False
                                        and drop.current_minutes_watched > 0
                                    ):
                                        progress_key = drop.id
                                        last_saved_minutes = (
                                            self.drop_progress_last_saved.get(
                                                progress_key,
                                                -1,
                                            )
                                        )
                                        if (
                                            drop.current_minutes_watched
                                            > last_saved_minutes
                                        ):
                                            self.drop_progress_last_saved[
                                                progress_key
                                            ] = drop.current_minutes_watched
                                            self.__save_drop_progress_analytics(
                                                drop,
                                                campaign=campaign,
                                                streamer_username=streamers[
                                                    index
                                                ].username,
                                            )

                                    # We could add .has_preconditions_met condition inside is_printable
                                    if (
                                        drop.has_preconditions_met is not False
                                        and drop.is_printable is True
                                    ):
                                        drop_messages = [
                                            f"{streamers[index]} is streaming {streamers[index].stream}",
                                            f"Campaign: {campaign}",
                                            f"Drop: {drop}",
                                            f"{drop.progress_bar()}",
                                        ]
                                        for single_line in drop_messages:
                                            logger.info(
                                                single_line,
                                                extra={
                                                    "event": Events.DROP_STATUS,
                                                    "skip_telegram": True,
                                                    "skip_discord": True,
                                                    "skip_webhook": True,
                                                    "skip_matrix": True,
                                                    "skip_gotify": True,
                                                },
                                            )

                                        if Settings.logger.telegram is not None:
                                            Settings.logger.telegram.send(
                                                "\n".join(drop_messages),
                                                Events.DROP_STATUS,
                                            )

                                        if Settings.logger.discord is not None:
                                            Settings.logger.discord.send(
                                                "\n".join(drop_messages),
                                                Events.DROP_STATUS,
                                            )
                                        if Settings.logger.webhook is not None:
                                            Settings.logger.webhook.send(
                                                "\n".join(drop_messages),
                                                Events.DROP_STATUS,
                                            )
                                        if Settings.logger.gotify is not None:
                                            Settings.logger.gotify.send(
                                                "\n".join(drop_messages),
                                                Events.DROP_STATUS,
                                            )

                    except requests.exceptions.ConnectionError as e:
                        logger.error(f"Error while trying to send minute watched: {e}")
                        self.__check_connection_handler(chunk_size)
                    except requests.exceptions.Timeout as e:
                        logger.error(f"Error while trying to send minute watched: {e}")

                    self.__chuncked_sleep(
                        next_iteration - time.time(), chunk_size=chunk_size
                    )

                if streamers_watching == []:
                    # self.__chuncked_sleep(60, chunk_size=chunk_size)
                    self.__chuncked_sleep(20, chunk_size=chunk_size)
            except Exception:
                logger.error("Exception raised in send minute watched", exc_info=True)
            finally:
                # Early failures use ``continue`` and skip the per-stream sleep.
                # Keep the watcher from hammering Twitch when an upstream request
                # or persisted query fails.
                self.__chuncked_sleep(
                    20 - (time.time() - iteration_started_at),
                    chunk_size=chunk_size,
                )

    # === CHANNEL POINTS / PREDICTION === #
    # Load the amount of current points for a channel, check if a bonus is available
    def load_channel_points_context(self, streamer):
        json_data = copy.deepcopy(GQLOperations.ChannelPointsContext)
        json_data["variables"] = {"channelLogin": streamer.username}

        response = self.post_gql_request(json_data)
        if not isinstance(response, dict) or response == {}:
            return

        data = response.get("data")
        if not isinstance(data, dict) or "community" not in data:
            logger.debug(
                f"Channel points response for {streamer.username} did not contain "
                "community data; keeping the current point state"
            )
            return

        community = data["community"]
        if community is None:
            raise StreamerDoesNotExistException

        channel = community.get("channel") if isinstance(community, dict) else None
        viewer = channel.get("self") if isinstance(channel, dict) else None
        community_points = (
            viewer.get("communityPoints") if isinstance(viewer, dict) else None
        )
        if not isinstance(community_points, dict):
            logger.debug(
                f"Channel points are unavailable for {streamer.username}; keeping "
                "the current point state"
            )
            return

        balance = community_points.get("balance")
        if balance is None:
            logger.debug(
                f"Channel points response for {streamer.username} has no balance; "
                "keeping the current point state"
            )
            return

        streamer.channel_points = balance
        streamer.activeMultipliers = community_points.get("activeMultipliers")

        if streamer.settings.community_goals is True:
            points_settings = channel.get("communityPointsSettings") or {}
            goals = (
                points_settings.get("goals", [])
                if isinstance(points_settings, dict)
                else []
            )
            streamer.community_goals = {
                goal["id"]: CommunityGoal.from_gql(goal) for goal in goals
            }

        available_claim = community_points.get("availableClaim")
        if isinstance(available_claim, dict) and available_claim.get("id") is not None:
            self.claim_bonus(streamer, available_claim["id"])

        if streamer.settings.community_goals is True:
            self.contribute_to_community_goals(streamer)

    def make_predictions(self, event):
        decision = event.bet.calculate(event.streamer.channel_points)
        # selector_index = 0 if decision["choice"] == "A" else 1

        logger.info(
            f"Going to complete bet for {event}",
            extra={
                "emoji": ":four_leaf_clover:",
                "event": Events.BET_GENERAL,
            },
        )
        if event.status == "ACTIVE":
            skip, compared_value = event.bet.skip()
            if skip is True:
                logger.info(
                    f"Skip betting for the event {event}",
                    extra={
                        "emoji": ":pushpin:",
                        "event": Events.BET_FILTERS,
                    },
                )
                logger.info(
                    f"Skip settings {event.bet.settings.filter_condition}, current value is: {compared_value}",
                    extra={
                        "emoji": ":pushpin:",
                        "event": Events.BET_FILTERS,
                    },
                )
            else:
                if decision["amount"] >= 10:
                    logger.info(
                        # f"Place {_millify(decision['amount'])} channel points on: {event.bet.get_outcome(selector_index)}",
                        f"Place {_millify(decision['amount'])} channel points on: {event.bet.get_outcome(decision['choice'])}",
                        extra={
                            "emoji": ":four_leaf_clover:",
                            "event": Events.BET_GENERAL,
                        },
                    )

                    json_data = copy.deepcopy(GQLOperations.MakePrediction)
                    json_data["variables"] = {
                        "input": {
                            "eventID": event.event_id,
                            "outcomeID": decision["id"],
                            "points": decision["amount"],
                            "transactionID": token_hex(16),
                        }
                    }
                    response = self.post_gql_request(json_data)
                    if (
                        "data" in response
                        and "makePrediction" in response["data"]
                        and "error" in response["data"]["makePrediction"]
                        and response["data"]["makePrediction"]["error"] is not None
                    ):
                        error_code = response["data"]["makePrediction"]["error"]["code"]
                        logger.error(
                            f"Failed to place bet, error: {error_code}",
                            extra={
                                "emoji": ":four_leaf_clover:",
                                "event": Events.BET_FAILED,
                            },
                        )
                else:
                    logger.info(
                        f"Bet won't be placed as the amount {_millify(decision['amount'])} is less than the minimum required 10",
                        extra={
                            "emoji": ":four_leaf_clover:",
                            "event": Events.BET_GENERAL,
                        },
                    )
        else:
            logger.info(
                f"Oh no! The event is not active anymore! Current status: {event.status}",
                extra={
                    "emoji": ":disappointed_relieved:",
                    "event": Events.BET_FAILED,
                },
            )

    def claim_bonus(self, streamer, claim_id):
        if Settings.logger.less is False:
            logger.info(
                f"Claiming the bonus for {streamer}!",
                extra={"emoji": ":gift:", "event": Events.BONUS_CLAIM},
            )

        json_data = copy.deepcopy(GQLOperations.ClaimCommunityPoints)
        json_data["variables"] = {
            "input": {"channelID": streamer.channel_id, "claimID": claim_id}
        }
        self.post_gql_request(json_data)

    # === MOMENTS === #
    def claim_moment(self, streamer, moment_id):
        if Settings.logger.less is False:
            logger.info(
                f"Claiming the moment for {streamer}!",
                extra={"emoji": ":video_camera:", "event": Events.MOMENT_CLAIM},
            )

        json_data = copy.deepcopy(GQLOperations.CommunityMomentCallout_Claim)
        json_data["variables"] = {"input": {"momentID": moment_id}}
        self.post_gql_request(json_data)

    # === CAMPAIGNS / DROPS / INVENTORY === #
    def log_open_drop_campaigns(self):
        dashboard_campaigns, dashboard_response = self.__get_drops_dashboard(
            return_raw=True
        )
        raw_query_campaigns, raw_query_debug = self.__get_reward_campaigns_raw_query()
        helix_campaigns, helix_pages = self.__get_open_drop_campaigns_from_helix(
            return_raw=True
        )
        campaigns_by_id = {}
        for campaign in dashboard_campaigns + raw_query_campaigns + helix_campaigns:
            if not isinstance(campaign, dict):
                continue
            campaign_id = campaign.get("id")
            if campaign_id not in [None, ""]:
                campaigns_by_id[str(campaign_id)] = campaign

        all_campaigns = list(campaigns_by_id.values())
        open_campaigns = [
            campaign
            for campaign in all_campaigns
            if isinstance(campaign, dict)
            and self.__is_open_drop_campaign(campaign) is True
        ]
        self.discovered_open_drop_campaigns = open_campaigns

        # Emit raw GraphQL payloads to help inspect Twitch campaign data changes.
        self.__log_drop_check_json(
            "graphql ViewerDropsDashboard raw response", dashboard_response
        )
        self.__log_drop_check_json("graphql raw query debug", raw_query_debug)
        self.__log_drop_check_json("helix open campaigns raw pages", helix_pages)
        self.__log_drop_check_json("helix open campaigns extracted", helix_campaigns)
        self.__log_drop_check_json(
            "graphql merged all extracted campaigns", all_campaigns
        )
        self.__log_drop_check_json("graphql merged OPEN campaigns", open_campaigns)

        try:
            open_campaigns_raw = json.dumps(open_campaigns)
        except (TypeError, ValueError):
            open_campaigns_raw = str(open_campaigns)
        logger.debug(
            f"Open drop campaigns raw payload: {open_campaigns_raw}",
            extra={"emoji": ":mag:"},
        )

        if open_campaigns == []:
            logger.info(
                "Open drop campaigns on load: none found",
                extra={"emoji": ":sleeping:"},
            )
            return []

        logger.debug(
            f"Open drop campaigns on load: {len(open_campaigns)} found (from {len(all_campaigns)} extracted)",
            extra={"emoji": ":gift:"},
        )

        for index, campaign in enumerate(open_campaigns, start=1):
            game = campaign.get("game") or {}
            game_name = game.get("displayName") or game.get("name") or "Unknown Game"
            campaign_name = campaign.get("name") or "Unknown Campaign"
            campaign_id = campaign.get("id") or "n/a"
            status = campaign.get("status") or "IN_PROGRESS"
            end_at = campaign.get("endAt") or "n/a"

            logger.debug(
                (
                    f"[{index}/{len(open_campaigns)}] "
                    f"{game_name} - {campaign_name} "
                    f"(status={status}, end={end_at}, id={campaign_id})"
                ),
                extra={"emoji": ":page_facing_up:"},
            )

        return open_campaigns

    def __get_campaign_ids_from_streamer(self, streamer):
        game_slug = self.__slugify(streamer.stream.game_name() or "")
        if game_slug == "":
            return []

        campaign_ids = set()
        possible_campaigns = list(self.discovered_open_drop_campaigns or [])
        possible_campaigns.extend(self.twitchdrops_app_campaigns.get(game_slug, []))
        for campaign in possible_campaigns:
            if not isinstance(campaign, dict):
                continue
            game = campaign.get("game") or {}
            campaign_game_slug = self.__slugify(
                game.get("displayName")
                or game.get("name")
                or campaign.get("game_name")
                or ""
            )
            if campaign_game_slug not in ("", game_slug):
                continue
            channels = {
                str(login).lower().strip()
                for login in campaign.get("channels", []) or []
            }
            campaign_id = campaign.get("id")
            if campaign_id and (not channels or streamer.username in channels):
                campaign_ids.add(str(campaign_id))
        return list(campaign_ids)

    def __get_campaigns_from_channel_id(self, channel_id: str) -> List[dict]:
        # Twitch removed the channel-specific viewerDropCampaigns field. Campaign
        # discovery now comes from the dashboard, raw query, Helix, and
        # twitchdrops.app paths used by log_open_drop_campaigns().
        return []

    def __get_reward_campaigns_raw_query(self):
        query_variants = [
            {
                "name": "root_rewardCampaignsAvailableToUser_object",
                "query": (
                    "query{"
                    "rewardCampaignsAvailableToUser{"
                    "id name status startsAt endsAt game{id slug displayName name}"
                    "}"
                    "}"
                ),
                "path": ["data", "rewardCampaignsAvailableToUser"],
                "is_connection": False,
            },
        ]

        campaigns_by_id = {}
        debug = {"variants": []}

        for variant in query_variants:
            cursor = None
            pages = 0
            variant_errors = []
            variant_count = 0

            while pages < 20:
                pages += 1
                payload = {
                    "operationName": "MinerRewardCampaignsRaw",
                    "query": variant["query"],
                    "variables": (
                        {"first": 100, "after": cursor}
                        if variant["is_connection"] is True
                        else {}
                    ),
                }

                response = self.post_gql_request(payload)
                if not isinstance(response, dict):
                    break

                if response.get("errors"):
                    variant_errors.extend(response.get("errors") or [])
                    break

                node = response
                for key in variant["path"]:
                    if not isinstance(node, dict):
                        node = None
                        break
                    node = node.get(key)

                if node in [None, {}]:
                    break

                if variant["is_connection"] is True and isinstance(node, dict):
                    edges = node.get("edges", []) or []
                    for edge in edges:
                        campaign = edge.get("node") if isinstance(edge, dict) else None
                        if not isinstance(campaign, dict):
                            continue
                        campaign_id = campaign.get("id")
                        if campaign_id in [None, ""]:
                            continue
                        campaigns_by_id[str(campaign_id)] = campaign
                        variant_count += 1

                    page_info = (
                        node.get("pageInfo", {}) if isinstance(node, dict) else {}
                    )
                    has_next = (
                        page_info.get("hasNextPage") is True
                        if isinstance(page_info, dict)
                        else False
                    )
                    cursor = (
                        page_info.get("endCursor")
                        if isinstance(page_info, dict)
                        else None
                    )
                    if has_next is not True or cursor in [None, ""]:
                        break
                elif isinstance(node, list):
                    for campaign in node:
                        if not isinstance(campaign, dict):
                            continue
                        campaign_id = campaign.get("id")
                        if campaign_id in [None, ""]:
                            continue
                        campaigns_by_id[str(campaign_id)] = campaign
                        variant_count += 1
                    break
                elif isinstance(node, dict):
                    campaign_id = node.get("id")
                    if campaign_id not in [None, ""]:
                        campaigns_by_id[str(campaign_id)] = node
                        variant_count += 1
                    break
                else:
                    break

            debug["variants"].append(
                {
                    "name": variant["name"],
                    "pages": pages,
                    "captured": variant_count,
                    "errors": variant_errors,
                }
            )

        campaigns = list(campaigns_by_id.values())
        debug["total_unique"] = len(campaigns)
        return campaigns, debug

    def __get_inventory(self):
        response = self.post_gql_request(GQLOperations.Inventory)
        try:
            inventory = (
                response["data"]["currentUser"]["inventory"] if response != {} else {}
            )
            for awarded_drop in inventory.get("gameEventDrops", []) or []:
                if not isinstance(awarded_drop, dict):
                    continue
                cache_key = awarded_drop.get("id")
                if cache_key in [None, ""]:
                    cache_key = "|".join(
                        [
                            str(awarded_drop.get("name") or ""),
                            str(awarded_drop.get("imageURL") or ""),
                        ]
                    )
                if cache_key:
                    self.awarded_game_event_drops[str(cache_key)] = awarded_drop
            if self.log_drop_checks is True:
                campaigns_in_progress = (
                    inventory.get("dropCampaignsInProgress", []) or []
                )
                awarded_rewards = inventory.get("gameEventDrops", []) or []
                completed_campaigns = (
                    inventory.get("completedRewardCampaigns", []) or []
                )
                self.__log_drop_check(
                    "inventory summary"
                    f" campaigns_in_progress={len(campaigns_in_progress)}"
                    f" game_event_rewards={len(awarded_rewards)}"
                    f" cached_game_event_rewards={len(self.awarded_game_event_drops)}"
                    f" completed_campaigns={len(completed_campaigns)}",
                    level=logging.DEBUG,
                )
            return inventory
        except (ValueError, KeyError, TypeError):
            self.__log_drop_check("failed to parse inventory response")
            return {}

    def __get_drops_dashboard(self, status=None, return_raw: bool = False):
        response = self.post_gql_request(GQLOperations.ViewerDropsDashboard)
        data = response.get("data", {}) if isinstance(response, dict) else {}
        current_user = data.get("currentUser", {})
        campaigns = []

        for key in [
            "dropCampaigns",
            "rewardCampaigns",
            "dropCampaignsInProgress",
            "rewardCampaignsInProgress",
        ]:
            campaigns.extend(current_user.get(key, []) or [])

        # Twitch may place globally available reward campaigns here.
        campaigns.extend(data.get("rewardCampaignsAvailableToUser", []) or [])
        campaigns.extend(current_user.get("rewardCampaignsAvailableToUser", []) or [])

        campaigns_by_id = {}
        for campaign in campaigns:
            campaign_id = campaign.get("id")
            if campaign_id:
                campaigns_by_id[campaign_id] = campaign
        campaigns = list(campaigns_by_id.values())

        if status is not None:
            normalized_status = status.upper()
            if normalized_status == "OPEN":
                campaigns = [
                    campaign
                    for campaign in campaigns
                    if isinstance(campaign, dict)
                    and self.__is_open_drop_campaign(campaign) is True
                ]
            else:
                campaigns = (
                    list(
                        filter(
                            lambda x: isinstance(x, dict)
                            and str(x.get("status") or "").upper() == normalized_status,
                            campaigns,
                        )
                    )
                    or []
                )

        if return_raw is True:
            return campaigns, response

        return campaigns

    def __is_open_drop_campaign(self, campaign: dict) -> bool:
        status = str(campaign.get("status") or "").upper()
        now = datetime.utcnow()

        starts_at_raw = campaign.get("startsAt") or campaign.get("startAt")
        ends_at_raw = campaign.get("endsAt") or campaign.get("endAt")

        starts_at = self.__parse_twitch_datetime(starts_at_raw)
        ends_at = self.__parse_twitch_datetime(ends_at_raw)

        if starts_at is not None and now < starts_at:
            return False

        if ends_at is not None and now > ends_at:
            return False

        if status in ["", "UNKNOWN"]:
            return True

        if status in ["ACTIVE", "IN_PROGRESS", "ONGOING", "ENABLED"]:
            return True

        if status in ["EXPIRED", "ENDED", "INACTIVE", "DISABLED"]:
            return False

        # Keep unknown statuses to avoid hiding campaigns unexpectedly.
        return True

    def __parse_twitch_datetime(
        self, datetime_str: Optional[str]
    ) -> Optional[datetime]:
        if not datetime_str:
            return None

        for fmt in ["%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"]:
            try:
                return datetime.strptime(datetime_str, fmt)
            except (TypeError, ValueError):
                continue

        for fmt in ["%Y-%m-%dT%H:%M:%S.%f+00:00", "%Y-%m-%dT%H:%M:%S+00:00"]:
            try:
                return datetime.strptime(datetime_str, fmt)
            except (TypeError, ValueError):
                continue

        return None

    def __get_open_drop_campaigns_from_helix(
        self, return_raw: bool = False
    ) -> Tuple[List[dict], List[dict]]:
        campaigns = []
        pages = []
        cursor = None
        max_pages = 25

        for _ in range(max_pages):
            params = {"first": 100}
            if cursor is not None:
                params["after"] = cursor

            response = self.__helix_get(
                "drops/campaigns", params, suppress_status_codes={404}
            )
            if not isinstance(response, dict) or response == {}:
                break

            pages.append(response)
            page_campaigns = response.get("data", []) or []
            for campaign in page_campaigns:
                if isinstance(campaign, dict) and self.__is_open_drop_campaign(
                    campaign
                ):
                    campaigns.append(campaign)

            pagination = (
                response.get("pagination", {}) if isinstance(response, dict) else {}
            )
            next_cursor = (
                pagination.get("cursor") if isinstance(pagination, dict) else None
            )
            if not next_cursor:
                break
            cursor = next_cursor

        deduped_campaigns = []
        seen_ids = set()
        for campaign in campaigns:
            campaign_id = str(campaign.get("id") or "")
            if campaign_id != "":
                if campaign_id in seen_ids:
                    continue
                seen_ids.add(campaign_id)
            deduped_campaigns.append(campaign)

        if return_raw is True:
            return deduped_campaigns, pages

        return deduped_campaigns, []

    def __get_campaigns_details(
        self,
        campaigns,
        campaign_channel_login_by_id: Optional[Dict[str, str]] = None,
    ):
        def extract_drop_campaign(response_item):
            drop_campaign = (
                response_item.get("data", {}).get("user", {}).get("dropCampaign")
            )
            if drop_campaign is None:
                drop_campaign = (
                    response_item.get("data", {})
                    .get("currentUser", {})
                    .get("dropCampaign")
                )
            if drop_campaign is None:
                drop_campaign = response_item.get("data", {}).get("dropCampaign")
            return drop_campaign

        result = []
        misses = 0
        viewer_context = str(
            self.twitch_login.get_user_id() or self.twitch_login.username
        )
        campaign_channel_login_by_id = campaign_channel_login_by_id or {}
        chunks = create_chunks(campaigns, 20)
        for chunk in chunks:
            json_data = []
            requested_logins = []
            for campaign in chunk:
                campaign_id = str(campaign.get("id") or "")
                channel_login = str(
                    campaign_channel_login_by_id.get(campaign_id) or viewer_context
                )
                requested_logins.append(channel_login)

                json_data.append(copy.deepcopy(GQLOperations.DropCampaignDetails))
                json_data[-1]["variables"] = {
                    "dropID": campaign["id"],
                    "channelLogin": str(channel_login),
                }

            response = self.post_gql_request(json_data)
            if not isinstance(response, list):
                logger.debug("Unexpected campaigns response format, skipping chunk")
                misses += len(chunk)
                continue
            retry_campaigns = []
            for index, campaign in enumerate(chunk):
                response_item = response[index] if index < len(response) else {}
                drop_campaign = extract_drop_campaign(response_item)
                if drop_campaign is not None:
                    result.append(drop_campaign)
                elif requested_logins[index] != viewer_context:
                    retry_campaigns.append(campaign)
                else:
                    misses += 1

            if retry_campaigns:
                retry_data = []
                for campaign in retry_campaigns:
                    retry_data.append(copy.deepcopy(GQLOperations.DropCampaignDetails))
                    retry_data[-1]["variables"] = {
                        "dropID": campaign["id"],
                        "channelLogin": viewer_context,
                    }

                retry_response = self.post_gql_request(retry_data)
                if not isinstance(retry_response, list):
                    misses += len(retry_campaigns)
                    continue
                for index in range(len(retry_campaigns)):
                    response_item = (
                        retry_response[index] if index < len(retry_response) else {}
                    )
                    drop_campaign = extract_drop_campaign(response_item)
                    if drop_campaign is not None:
                        result.append(drop_campaign)
                    else:
                        misses += 1

        if self.log_drop_checks is True and misses > 0:
            self.__log_drop_check(
                f"DropCampaignDetails misses={misses} campaigns={len(campaigns)}",
                level=logging.DEBUG,
            )

        return result

    def __sync_campaigns(self, campaigns):
        self.__log_drop_check("sync cycle: checking inventory for drop progress")
        # We need the inventory only for get the real updated value/progress
        # Get data from inventory and sync current status with streamers.campaigns
        inventory = self.__get_inventory()
        if inventory not in [None, {}] and inventory["dropCampaignsInProgress"] not in [
            None,
            {},
        ]:
            campaigns_by_id = {campaign.id: campaign for campaign in campaigns}
            for campaign in campaigns:
                campaign.clear_drops()  # Remove all the claimed drops

            # Iterate all campaigns currently in progress from our inventory.
            for progress in inventory["dropCampaignsInProgress"]:
                campaign_ref = campaigns_by_id.get(progress.get("id"))

                if campaign_ref is not None:
                    campaign_ref.in_inventory = True
                    campaign_ref.sync_drops(
                        progress.get("timeBasedDrops", []), self.claim_drop
                    )
                    # Remove all the claimed drops
                    campaign_ref.clear_drops()

                # Persist progress snapshots from inventory sync as fallback path.
                # This captures progress even when dashboard campaign details are stale.
                progress_game = progress.get("game") or {}
                category_name_override = (
                    progress_game.get("displayName")
                    or progress_game.get("name")
                    or "Unknown"
                )
                campaign_name_override = progress.get("name")

                for drop_dict in progress.get("timeBasedDrops", []):
                    drop_self = drop_dict.get("self") or {}
                    if isinstance(drop_self, dict):
                        is_claimed = drop_self.get("isClaimed") is True
                        drop_instance_id = drop_self.get("dropInstanceID")
                        is_claimable = (is_claimed is False) and (
                            drop_instance_id is not None
                        )
                        if is_claimable is True:
                            try:
                                drop_to_claim = Drop(drop_dict)
                                drop_to_claim.update(drop_self)
                                self.claim_drop(
                                    drop_to_claim,
                                    campaign=campaign_ref,
                                    streamer_username=None,
                                    campaign_name_override=campaign_name_override,
                                    category_name_override=category_name_override,
                                )
                            except (KeyError, TypeError, ValueError):
                                pass

                    self.__save_drop_snapshot_from_dict(
                        drop_dict,
                        campaign=campaign_ref,
                        streamer_username=None,
                        campaign_name_override=campaign_name_override,
                        category_name_override=category_name_override,
                    )
        return campaigns

    def __save_drop_claim_analytics(
        self,
        drop,
        campaign=None,
        streamer_username=None,
        status="captured",
        campaign_name_override=None,
        category_name_override=None,
        status_override=None,
        item_name_override=None,
        benefit_override=None,
        item_art_url_override=None,
    ):
        if Settings.enable_analytics is not True:
            return

        category_name = category_name_override or "Unknown"
        campaign_name = campaign_name_override
        if campaign is not None:
            campaign_name = campaign_name or campaign.name
            game = campaign.game or {}
            category_name = (
                category_name_override
                or game.get("displayName")
                or game.get("name")
                or "Unknown"
            )

        # Ignore uncategorized drop entries to keep category views clean.
        if category_name == "Unknown":
            return

        payload = {
            "x": round(time.time() * 1000),
            "datetime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "category": category_name,
            "campaign": campaign_name,
            "item_name": item_name_override or drop.name,
            "benefit": benefit_override or drop.benefit,
            "streamer": streamer_username,
            "drop_id": drop.id,
            "status": status_override or status,
            "current_minutes_watched": min(
                drop.current_minutes_watched, drop.minutes_required
            ),
            "minutes_required": drop.minutes_required,
            "percentage_progress": min(100, drop.percentage_progress),
            "drop_end_at": (
                drop.end_at.strftime("%Y-%m-%dT%H:%M:%SZ")
                if getattr(drop, "end_at", None) is not None
                else None
            ),
            "failed_to_achieve": (
                status != "captured"
                and getattr(drop, "end_at", None) is not None
                and datetime.utcnow() > drop.end_at
                and drop.current_minutes_watched < drop.minutes_required
            ),
            "item_art_url": (
                item_art_url_override
                if item_art_url_override is not None
                and self.track_drop_item_art is True
                else (drop.item_art_url if self.track_drop_item_art is True else None)
            ),
        }

        analytics_file = os.path.join(Settings.analytics_path, "drops_by_category.json")
        temp_file = analytics_file + ".temp"

        with self.analytics_mutex:
            with open(temp_file, "w", encoding="utf-8") as temp_handle:
                data = {}
                if os.path.isfile(analytics_file):
                    with open(analytics_file, "r", encoding="utf-8") as current:
                        try:
                            data = json.load(current)
                        except json.JSONDecodeError:
                            data = {}

                if "drops" not in data:
                    data["drops"] = []

                compacted = {}
                for item in data["drops"]:
                    identity_key = "|".join(
                        [
                            str(item.get("drop_id") or ""),
                            str(item.get("item_art_url") or ""),
                            str(item.get("item_name") or ""),
                            str(item.get("campaign") or ""),
                            str(item.get("category") or ""),
                        ]
                    )
                    previous = compacted.get(identity_key)
                    if previous is None or (item.get("x") or 0) >= (
                        previous.get("x") or 0
                    ):
                        compacted[identity_key] = item

                data["drops"] = list(compacted.values())

                identity_index = next(
                    (
                        i
                        for i, item in enumerate(data["drops"])
                        if item.get("drop_id") == payload.get("drop_id")
                        and item.get("item_art_url") == payload.get("item_art_url")
                        and item.get("item_name") == payload.get("item_name")
                        and item.get("campaign") == payload.get("campaign")
                        and item.get("category") == payload.get("category")
                    ),
                    None,
                )

                if identity_index is None:
                    data["drops"].append(payload)
                else:
                    data["drops"][identity_index] = payload

                json.dump(data, temp_handle, indent=4)

            os.replace(temp_file, analytics_file)

    def __save_drop_progress_analytics(
        self,
        drop,
        campaign=None,
        streamer_username=None,
        campaign_name_override=None,
        category_name_override=None,
        status_override=None,
        item_name_override=None,
        benefit_override=None,
        item_art_url_override=None,
    ):
        self.__save_drop_claim_analytics(
            drop,
            campaign=campaign,
            streamer_username=streamer_username,
            status="in_progress",
            campaign_name_override=campaign_name_override,
            category_name_override=category_name_override,
            status_override=status_override,
            item_name_override=item_name_override,
            benefit_override=benefit_override,
            item_art_url_override=item_art_url_override,
        )

    def __reconcile_awarded_game_event_drops(self, inventory):
        if Settings.enable_analytics is not True:
            return 0

        if not isinstance(inventory, dict):
            return 0

        rewards = inventory.get("gameEventDrops") or []
        if not isinstance(rewards, list) or rewards == []:
            return 0

        awarded_by_name = {}
        for reward in rewards:
            if not isinstance(reward, dict):
                continue
            if reward.get("lastAwardedAt") in [None, ""]:
                continue

            reward_name = reward.get("name")
            if reward_name in [None, ""]:
                continue

            reward_image = reward.get("imageURL")
            if reward_name not in awarded_by_name:
                awarded_by_name[reward_name] = set()
            if reward_image not in [None, ""]:
                awarded_by_name[reward_name].add(reward_image)

        if awarded_by_name == {}:
            return 0

        analytics_file = os.path.join(Settings.analytics_path, "drops_by_category.json")
        if os.path.isfile(analytics_file) is False:
            return 0

        temp_file = analytics_file + ".temp"
        now_ms = round(time.time() * 1000)
        now_dt = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        updated = 0

        with self.analytics_mutex:
            with open(analytics_file, "r", encoding="utf-8") as current:
                try:
                    data = json.load(current)
                except json.JSONDecodeError:
                    data = {}

            drops = data.get("drops")
            if not isinstance(drops, list):
                drops = []

            for item in drops:
                if not isinstance(item, dict):
                    continue
                if item.get("status") == "captured":
                    continue

                item_name = item.get("item_name")
                if item_name not in awarded_by_name:
                    continue

                known_images = awarded_by_name.get(item_name, set())
                item_art_url = item.get("item_art_url")
                if (
                    known_images
                    and item_art_url not in [None, ""]
                    and item_art_url not in known_images
                ):
                    continue

                item["status"] = "captured"
                item["failed_to_achieve"] = False
                item["x"] = now_ms
                item["datetime"] = now_dt

                minutes_required = item.get("minutes_required") or 0
                current_minutes = item.get("current_minutes_watched") or 0
                item["current_minutes_watched"] = max(current_minutes, minutes_required)

                percentage_progress = item.get("percentage_progress") or 0
                item["percentage_progress"] = max(percentage_progress, 100)
                updated += 1

            data["drops"] = drops

            with open(temp_file, "w", encoding="utf-8") as temp_handle:
                json.dump(data, temp_handle, indent=4)

            os.replace(temp_file, analytics_file)

        if updated > 0:
            self.__log_drop_check(
                f"reconciled {updated} analytics entries from awarded gameEventDrops"
            )

        return updated

    def claim_drop(
        self,
        drop,
        campaign=None,
        streamer_username=None,
        campaign_name_override=None,
        category_name_override=None,
    ):
        logger.info(
            f"Claim {drop}", extra={"emoji": ":package:", "event": Events.DROP_CLAIM}
        )

        json_data = copy.deepcopy(GQLOperations.DropsPage_ClaimDropRewards)
        json_data["variables"] = {"input": {"dropInstanceID": drop.drop_instance_id}}
        response = self.post_gql_request(json_data)
        try:
            # response["data"]["claimDropRewards"] can be null and respose["data"]["errors"] != []
            # or response["data"]["claimDropRewards"]["status"] === DROP_INSTANCE_ALREADY_CLAIMED
            if ("claimDropRewards" in response["data"]) and (
                response["data"]["claimDropRewards"] is None
            ):
                return False
            elif ("errors" in response["data"]) and (response["data"]["errors"] != []):
                return False
            elif ("claimDropRewards" in response["data"]) and (
                response["data"]["claimDropRewards"]["status"]
                in ["ELIGIBLE_FOR_ALL", "DROP_INSTANCE_ALREADY_CLAIMED"]
            ):
                for variant in self.__drop_variant_entries_from_drop(drop):
                    self.__save_drop_claim_analytics(
                        drop,
                        campaign=campaign,
                        streamer_username=streamer_username,
                        campaign_name_override=campaign_name_override,
                        category_name_override=category_name_override,
                        item_name_override=variant.get("name"),
                        benefit_override=variant.get("benefit"),
                        item_art_url_override=variant.get("item_art_url"),
                    )
                return True
            else:
                return False
        except (ValueError, KeyError):
            return False

    def claim_all_drops_from_inventory(self):
        inventory = self.__get_inventory()
        if inventory not in [None, {}]:
            if inventory["dropCampaignsInProgress"] not in [None, {}]:
                for campaign in inventory["dropCampaignsInProgress"]:
                    progress_game = campaign.get("game") or {}
                    category_name_override = (
                        progress_game.get("displayName")
                        or progress_game.get("name")
                        or "Unknown"
                    )
                    campaign_name_override = campaign.get("name")
                    for drop_dict in campaign["timeBasedDrops"]:
                        drop = Drop(drop_dict)
                        drop.update(drop_dict["self"])
                        if drop.is_claimable is True:
                            drop.is_claimed = self.claim_drop(
                                drop,
                                campaign_name_override=campaign_name_override,
                                category_name_override=category_name_override,
                            )
                            time.sleep(random.uniform(5, 10))

    def sync_campaigns(self, streamers, chunk_size=3):
        campaigns_update = 0
        campaigns = []
        while self.running:
            try:
                # Get update from dashboard each 60minutes
                if (
                    campaigns_update == 0
                    # or ((time.time() - campaigns_update) / 60) > 60
                    # TEMPORARY AUTO DROP CLAIMING FIX
                    # 30 minutes instead of 60 minutes
                    or ((time.time() - campaigns_update) / 30) > 30
                    #####################################
                ):
                    campaigns_update = time.time()

                    # TEMPORARY AUTO DROP CLAIMING FIX
                    self.claim_all_drops_from_inventory()
                    #####################################

                    # Get full details from all dashboard campaigns.
                    # Inventory only exposes currently tracked campaigns, so we also
                    # expand every dashboard campaign to avoid missing completed or inactive items.
                    campaigns_details = self.__get_campaigns_details(
                        self.__get_drops_dashboard()
                    )

                    for campaign_details in campaigns_details:
                        if campaign_details is None:
                            continue
                        progress_game = campaign_details.get("game") or {}
                        category_name_override = (
                            progress_game.get("displayName")
                            or progress_game.get("name")
                            or "Unknown"
                        )
                        campaign_name_override = campaign_details.get("name")
                        for drop_dict in campaign_details.get("timeBasedDrops", []):
                            self.__save_drop_snapshot_from_dict(
                                drop_dict,
                                campaign=None,
                                streamer_username=None,
                                campaign_name_override=campaign_name_override,
                                category_name_override=category_name_override,
                            )

                    campaigns = []

                    # Going to clear array and structure. Remove all the timeBasedDrops expired or not started yet
                    for index in range(0, len(campaigns_details)):
                        if campaigns_details[index] is not None:
                            campaign = Campaign(campaigns_details[index])
                            if campaign.dt_match is True:
                                # Remove all the drops already claimed or with dt not matching
                                campaign.clear_drops()
                                if campaign.drops != []:
                                    campaigns.append(campaign)
                        else:
                            continue

                # Divide et impera :)
                campaigns = self.__sync_campaigns(campaigns)

                # Check if user It's currently streaming the same game present in campaigns_details
                for i in range(0, len(streamers)):
                    if (
                        streamers[i].settings.claim_drops is True
                        and streamers[i].is_online is True
                        and streamers[i].stream.campaigns_ids != []
                    ):
                        previous_campaigns = list(streamers[i].stream.campaigns)
                        # yes! The streamer[i] have the drops_tags enabled and we It's currently stream a game with campaign active!
                        # With 'campaigns_ids' we are also sure that this streamer have the campaign active.
                        # yes! The streamer[index] have the drops_tags enabled and we It's currently stream a game with campaign active!
                        current_campaigns = list(
                            filter(
                                lambda x: x.drops != []
                                and x.game == streamers[i].stream.game
                                and x.id in streamers[i].stream.campaigns_ids,
                                campaigns,
                            )
                        )

                        previous_signature = self.__campaign_signature(
                            previous_campaigns
                        )
                        current_signature = self.__campaign_signature(current_campaigns)
                        game_label = self.__stream_game_label(streamers[i].stream)
                        if (
                            previous_signature != current_signature
                            and streamers[i].from_category is True
                        ):
                            if previous_signature != "" and current_signature == "":
                                logger.info(
                                    f"{Fore.RED}Stopped watching category channel {streamers[i].username} for {game_label} because all drops were captured for {self.__describe_campaigns(previous_campaigns)}{Fore.RESET}",
                                    extra={
                                        "event": Events.DROP_STATUS,
                                        "skip_telegram": True,
                                        "skip_discord": True,
                                        "skip_webhook": True,
                                        "skip_matrix": True,
                                        "skip_gotify": True,
                                    },
                                )
                            elif previous_signature == "" and current_signature != "":
                                logger.info(
                                    f"{Fore.GREEN}Watching category channel {streamers[i].username} for {game_label} - {self.__describe_campaigns(current_campaigns)}{Fore.RESET}",
                                    extra={
                                        "event": Events.DROP_STATUS,
                                        "skip_telegram": True,
                                        "skip_discord": True,
                                        "skip_webhook": True,
                                        "skip_matrix": True,
                                        "skip_gotify": True,
                                    },
                                )

                        streamers[i].stream.campaigns = current_campaigns

            except (ValueError, KeyError, requests.exceptions.ConnectionError) as e:
                logger.error(f"Error while syncing inventory: {e}")
                campaigns = []
                self.__check_connection_handler(chunk_size)

            self.__chuncked_sleep(60, chunk_size=chunk_size)

    def contribute_to_community_goals(self, streamer):
        # Don't bother doing the request if no goal is currently started or in stock
        if any(
            goal.status == "STARTED" and goal.is_in_stock
            for goal in streamer.community_goals.values()
        ):
            json_data = copy.deepcopy(GQLOperations.UserPointsContribution)
            json_data["variables"] = {"channelLogin": streamer.username}
            response = self.post_gql_request(json_data)
            user_goal_contributions = response["data"]["user"]["channel"]["self"][
                "communityPoints"
            ]["goalContributions"]

            logger.debug(
                f"Found {len(user_goal_contributions)} community goals for the current stream"
            )

            for goal_contribution in user_goal_contributions:
                goal_id = goal_contribution["goal"]["id"]
                goal = streamer.community_goals[goal_id]
                if goal is None:
                    # TODO should this trigger a new load context request
                    logger.error(
                        f"Unable to find context data for community goal {goal_id}"
                    )
                else:
                    user_stream_contribution = goal_contribution[
                        "userPointsContributedThisStream"
                    ]
                    user_left_to_contribute = (
                        goal.per_stream_user_maximum_contribution
                        - user_stream_contribution
                    )
                    amount = min(
                        goal.amount_left(),
                        user_left_to_contribute,
                        streamer.channel_points,
                    )
                    if amount > 0:
                        self.contribute_to_community_goal(
                            streamer, goal_id, goal.title, amount
                        )
                    else:
                        logger.debug(
                            f"Not contributing to community goal {goal.title}, user channel points {streamer.channel_points}, user stream contribution {user_stream_contribution}, all users total contribution {goal.points_contributed}"
                        )

    def contribute_to_community_goal(self, streamer, goal_id, title, amount):
        json_data = copy.deepcopy(GQLOperations.ContributeCommunityPointsCommunityGoal)
        json_data["variables"] = {
            "input": {
                "amount": amount,
                "channelID": streamer.channel_id,
                "goalID": goal_id,
                "transactionID": token_hex(16),
            }
        }

        response = self.post_gql_request(json_data)

        error = response["data"]["contributeCommunityPointsCommunityGoal"]["error"]
        if error:
            logger.error(
                f"Unable to contribute channel points to community goal '{title}', reason '{error}'"
            )
        else:
            logger.info(
                f"Contributed {amount} channel points to community goal '{title}'"
            )
            streamer.channel_points -= amount
