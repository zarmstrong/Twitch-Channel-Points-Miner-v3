"""Offline proof for the minute progress path, claims, and shared cache writers."""

import copy
import importlib
import logging
from threading import Event, Lock, Thread, current_thread
from types import SimpleNamespace

import pytest

from test_twitch_drop_claim import bare_twitch, campaign_data
from test_websockets_pool_on_message import _user_drop_event, _websocket
from TwitchChannelPointsMiner.classes.CopyOnWriteList import CopyOnWriteList
from TwitchChannelPointsMiner.classes.entities.Campaign import Campaign
from TwitchChannelPointsMiner.classes.entities.Drop import Drop
from TwitchChannelPointsMiner.classes.gql.Errors import (
    InvalidJsonShapeException,
    RetryError,
)
from TwitchChannelPointsMiner.classes.gql.Integration import GQL
from TwitchChannelPointsMiner.classes.gql.data.Parser import Parser
from TwitchChannelPointsMiner.classes.gql.data.response.Drops import (
    InventoryResponse,
)
from TwitchChannelPointsMiner.classes.Twitch import Twitch
from TwitchChannelPointsMiner.classes.WebSocketsPool import WebSocketsPool
from TwitchChannelPointsMiner.constants import GQLOperations

module = importlib.import_module("TwitchChannelPointsMiner.classes.Twitch")
FULL_INTERVAL = 15 * 60


def session(current=None, drop_id="drop-1", required=100, errors=None):
    return SimpleNamespace(
        has_session=current is not None,
        drop_id=drop_id,
        current_minutes_watched=current,
        required_minutes_watched=required,
        errors=errors or [],
    )


def progress_campaign(current=0, instance=None, claimed=False, eligible=True):
    data = campaign_data()
    data["timeBasedDrops"][0]["requiredMinutesWatched"] = 100
    data["timeBasedDrops"][0]["self"] = {
        "hasPreconditionsMet": eligible,
        "currentMinutesWatched": current,
        "dropInstanceID": instance,
        "isClaimed": claimed,
    }
    return data


class SyncLoop:
    """Run the real sync loop, inventory cache and claim wrapper on a fake clock."""

    def __init__(self, monkeypatch):
        self.twitch = bare_twitch(monkeypatch)
        self.twitch.running = True
        self.twitch.prompt_claim_pass_lock = Lock()
        self.twitch.awarded_game_event_drops = {}
        self.twitch.log_drop_checks = True
        self.now = 10_000.0
        self.tick = 0
        self.reads = []
        self.requests = []
        self.claims = []
        self.history = []
        self.inventory_errors = []
        self.inventory = progress_campaign()
        self.report = session()
        self.claim_result = True
        self.before_tick = lambda loop: None
        self.inventory_for_tick = lambda loop: {
            "dropCampaignsInProgress": [loop.inventory]
        }
        self.request_for_channel = lambda loop, channel: loop.report
        self.step_seconds = 60
        self.streamer = SimpleNamespace(
            username="watched",
            channel_id="177128531",
            is_online=True,
            is_watching=True,
            settings=SimpleNamespace(claim_drops=True),
            stream=SimpleNamespace(
                campaigns=[],
                campaigns_ids=["campaign-1"],
                game={"displayName": "Example Game"},
            ),
        )
        self.streamers = CopyOnWriteList([self.streamer])
        self.tracked_drop = None

        def get_inventory():
            self.reads.append(self.tick)
            payload = self.inventory_for_tick(self)
            if isinstance(payload, Exception):
                raise payload
            if payload == {}:
                raise RetryError("Inventory", [])
            return InventoryResponse(
                None, inventory=copy.deepcopy(payload), errors=self.inventory_errors
            )

        def get_session(channel):
            self.requests.append((self.tick, channel))
            result = self.request_for_channel(self, channel)
            if isinstance(result, Exception):
                raise result
            return result

        def claim(drop_instance_id):
            self.claims.append((self.tick, drop_instance_id))
            return SimpleNamespace(
                status="ELIGIBLE_FOR_ALL" if self.claim_result else None,
                errors=[] if self.claim_result else ["transient error"],
            )

        self.twitch.gql = SimpleNamespace(
            get_inventory=get_inventory,
            get_drop_current_session=get_session,
            claim_drop_rewards=claim,
        )
        monkeypatch.setattr(module.time, "time", lambda: 1_000_000 + self.now)
        monkeypatch.setattr(module.time, "monotonic", lambda: self.now)
        monkeypatch.setattr(Twitch, "claim_all_drops_from_inventory", lambda self: None)
        monkeypatch.setattr(
            Twitch, "_Twitch__get_drops_dashboard", lambda self: [campaign_data()]
        )
        monkeypatch.setattr(
            Twitch,
            "_Twitch__get_campaigns_details",
            lambda self, *a, **kw: [progress_campaign()],
        )
        for name in (
            "__save_drop_snapshot_from_dict",
            "__merge_completed_drop_campaigns",
            "__record_completed_badge_campaigns",
            "__check_connection_handler",
        ):
            monkeypatch.setattr(Twitch, "_Twitch" + name, lambda self, *a, **kw: None)

        def sleep(twitch, seconds, chunk_size=3):
            if self.streamer.stream.campaigns and self.tracked_drop is None:
                drops = self.streamer.stream.campaigns[0].drops
                if drops:
                    self.tracked_drop = drops[0]
            self.history.append(
                {
                    "minutes": (
                        self.tracked_drop.current_minutes_watched
                        if self.tracked_drop
                        else None
                    ),
                    "stamp": getattr(
                        twitch, "drop_inventory_progress_updated_at", None
                    ),
                    "pending": copy.deepcopy(
                        getattr(twitch, "drop_claims_pending", {})
                    ),
                    "cache": getattr(twitch, "drop_inventory_progress", {}),
                    "printable": (
                        self.tracked_drop.is_printable if self.tracked_drop else None
                    ),
                }
            )
            self.tick += 1
            self.now += self.step_seconds
            if self.tick >= self.passes:
                twitch.running = False
            else:
                self.before_tick(self)

        monkeypatch.setattr(Twitch, "_Twitch__chuncked_sleep", sleep)

    def run(self, passes):
        self.passes = passes
        self.twitch.sync_campaigns(self.streamers)
        return self

    def event(self, current, drop_id="drop-1", required=100, channel_id=None):
        self.twitch.record_drop_progress_event(
            {
                "drop_id": drop_id,
                "current_progress_min": current,
                "required_progress_min": required,
                "channel_id": channel_id or self.streamer.channel_id,
            }
        )


def test_steady_loop_uses_channel_session_and_refreshes_progress(monkeypatch):
    loop = SyncLoop(monkeypatch)
    loop.before_tick = lambda loop: setattr(loop, "report", session(loop.tick))
    loop.run(5)
    assert loop.reads == [0]
    assert loop.requests == [(tick, "177128531") for tick in range(1, 5)]
    assert [row["minutes"] for row in loop.history] == [0, 1, 2, 3, 4]
    assert [row["stamp"] for row in loop.history] == [
        1_010_000 + 60 * tick for tick in range(5)
    ]
    assert loop.twitch.drop_inventory_minutes_by_drop_id == {"drop-1": 4}


def test_pubsub_window_includes_recent_and_since_previous_step(monkeypatch):
    loop = SyncLoop(monkeypatch)
    loop.report = session(30)

    def before(loop):
        if loop.tick == 1:
            loop.event(10)
        if loop.tick == 2:
            loop.event(20)  # newest per ID, same event covers tick 3 too
        if loop.tick == 4:
            # A long step still includes events since the previous step.
            loop.now += 200
            loop.event(25)
            loop.now += 200

    loop.before_tick = before
    loop.run(6)
    assert loop.requests == [(5, "177128531")]
    assert [row["minutes"] for row in loop.history] == [0, 10, 20, 20, 25, 30]


def test_interval_runs_full_without_campaign_refresh(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    loop = SyncLoop(monkeypatch)
    # Wall time never reaches the dashboard refresh deadline; monotonic does.
    monkeypatch.setattr(module.time, "time", lambda: 1_000_000)
    loop.run(17)
    assert loop.reads == [0, 15]
    assert "reason=interval" in caplog.text
    assert module.DROP_FULL_INVENTORY_SYNC_SECONDS == FULL_INTERVAL


def test_completion_escalates_and_claims_through_sync_drops(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    loop = SyncLoop(monkeypatch)

    def before(loop):
        loop.report = session(100) if loop.tick == 1 else session()
        loop.inventory = progress_campaign(100, instance="instance-1")

    loop.before_tick = before
    loop.run(3)
    assert loop.reads == [0, 1]
    assert loop.claims == [(1, "instance-1")]
    assert loop.twitch.drop_claims_pending == {}
    assert "reason=drop_complete" in caplog.text


def test_completion_without_instance_is_debounced(monkeypatch):
    loop = SyncLoop(monkeypatch)
    loop.report = session(100)
    loop.before_tick = lambda loop: setattr(loop, "inventory", progress_campaign(99))
    loop.run(4)
    assert loop.reads == [0, 1]
    assert loop.twitch.drop_claims_pending["drop-1"]["last_full"] == 10_060


def test_unknown_drop_escalation_is_limited_per_id(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    loop = SyncLoop(monkeypatch)
    loop.report = session(10, drop_id="unknown")
    loop.run(4)
    assert loop.reads == [0, 1]
    assert "reason=unknown_drop" in caplog.text
    assert not loop.twitch.drop_claims_pending


@pytest.mark.parametrize(
    "failure",
    [
        RetryError("DropCurrentSessionContext", []),
        session(50, errors=["partial response error"]),
        InvalidJsonShapeException([], "invalid session"),
    ],
)
def test_failed_light_pass_falls_back_without_refreshing_failed_cache(
    monkeypatch, failure
):
    loop = SyncLoop(monkeypatch)
    loop.report = failure
    loop.inventory_for_tick = lambda loop: (
        {"dropCampaignsInProgress": [loop.inventory]} if loop.tick == 0 else {}
    )
    loop.run(3)
    assert loop.reads == [0, 1, 2]
    assert loop.requests == [(1, "177128531")]
    assert len({row["stamp"] for row in loop.history}) == 1


def test_three_failures_open_breaker_once_then_recover(monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    loop = SyncLoop(monkeypatch)
    loop.report = RetryError("DropCurrentSessionContext", [])

    def before(loop):
        if loop.tick == 6:
            loop.now = 10_180 + 3600
            loop.report = session(6)

    loop.before_tick = before
    # Isolate breaker timing from dashboard and full interval timing.
    monkeypatch.setattr(module.time, "time", lambda: 1_000_000)
    monkeypatch.setattr(module, "DROP_FULL_INVENTORY_SYNC_SECONDS", 100_000)
    loop.run(7)
    assert loop.reads == [0, 1, 2, 3, 4, 5]
    assert [tick for tick, _ in loop.requests] == [1, 2, 3, 6]
    assert loop.twitch.drop_session_failures == 0
    assert caplog.text.count("falling back to full inventory reads") == 1


@pytest.mark.parametrize("failed_channel", ["177128531", "second"])
def test_two_channels_stop_after_first_failure_and_count_per_pass(
    monkeypatch, failed_channel
):
    loop = SyncLoop(monkeypatch)
    second = copy.copy(loop.streamer)
    second.channel_id = "second"
    second.stream = copy.copy(loop.streamer.stream)
    loop.streamers.append(second)
    loop.request_for_channel = lambda loop, channel: (
        RetryError("DropCurrentSessionContext", [])
        if channel == failed_channel
        else session()
    )
    loop.run(6)
    assert loop.requests == [
        (tick, channel)
        for tick in [1, 2, 3]
        for channel in (
            ["177128531"] if failed_channel == "177128531" else ["177128531", "second"]
        )
    ]
    assert loop.reads == list(range(6))
    assert loop.twitch.drop_session_failures == 3


def test_lower_minutes_never_move_progress_backwards(monkeypatch):
    loop = SyncLoop(monkeypatch)
    loop.inventory = progress_campaign(50)
    loop.report = session(49)
    loop.run(3)
    assert loop.reads == [0]
    assert len(loop.requests) == 2
    assert [row["minutes"] for row in loop.history] == [50, 50, 50]
    assert loop.twitch.drop_inventory_minutes_by_drop_id["drop-1"] == 50


def test_cache_patch_replaces_containers_and_removes_completed_tuple(monkeypatch):
    twitch = bare_twitch(monkeypatch)
    twitch._Twitch__cache_drop_inventory_progress(
        {"dropCampaignsInProgress": [progress_campaign(1)]}
    )
    progress = twitch.drop_inventory_progress
    minutes = twitch.drop_inventory_minutes_by_drop_id
    rows = progress["example-game"]
    twitch._Twitch__patch_light_drop_progress({"drop-1": (25, 100, False)})
    assert twitch.drop_inventory_progress is not progress
    assert twitch.drop_inventory_minutes_by_drop_id is not minutes
    assert twitch.drop_inventory_progress["example-game"] is not rows
    assert rows[0][4] == 1 and minutes["drop-1"] == 1
    assert twitch.drop_inventory_progress["example-game"][0][4] == 25
    twitch._Twitch__patch_light_drop_progress({"drop-1": (100, 100, False)})
    assert "example-game" not in twitch.drop_inventory_progress
    assert twitch.drop_inventory_minutes_by_drop_id == {}


@pytest.mark.parametrize(
    "sequence", [[0, 1, 2, 25, 26, 50, 75, 100], [50, 49, 50, 100, 100]]
)
def test_update_minutes_matches_full_update_and_preserves_claim_state(sequence):
    data = progress_campaign()["timeBasedDrops"][0]
    light, full = Drop(data), Drop(data)
    light.update({**data["self"], "dropInstanceID": "keep", "currentSubs": 2})
    light.has_preconditions_met = False
    state = (
        light.drop_instance_id,
        light.is_claimed,
        light.is_claimable,
        light.has_preconditions_met,
        light.current_subs,
    )
    for current in sequence:
        light.update_minutes(current)
        full.update({**data["self"], "currentMinutesWatched": current})
        assert (
            light.current_minutes_watched,
            light.percentage_progress,
            light.is_printable,
        ) == (full.current_minutes_watched, full.percentage_progress, full.is_printable)
        assert (
            light.drop_instance_id,
            light.is_claimed,
            light.is_claimable,
            light.has_preconditions_met,
            light.current_subs,
        ) == state


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        "bad",
        {},
        {"drop_id": ""},
        {"drop_id": "   "},
        {"drop_id": 42, "current_progress_min": 1},
        {"drop_id": "d", "current_progress_min": True},
        {"drop_id": "d", "current_progress_min": -1},
        {"drop_id": "d", "current_progress_min": "1"},
    ],
)
def test_event_rejects_malformed_payload_without_raising(monkeypatch, payload):
    twitch = bare_twitch(monkeypatch)
    twitch.record_drop_progress_event(payload)
    assert not getattr(twitch, "drop_progress_events", {})


def test_websocket_forwards_progress_and_events_prune_old_entries(monkeypatch):
    loop = SyncLoop(monkeypatch)
    websocket = _websocket([])
    websocket.twitch = loop.twitch
    WebSocketsPool.on_message(
        websocket,
        _user_drop_event(
            "drop-progress",
            {
                "drop_id": "drop-1",
                "current_progress_min": 10,
                "required_progress_min": 100,
                "channel_id": "untracked-channel",
            },
        ),
    )
    assert loop.twitch.drop_progress_events["drop-1"] == (
        "drop-1",
        10,
        100,
        "untracked-channel",
        loop.now,
    )
    loop.event(12)
    assert loop.twitch.drop_progress_events["drop-1"][1] == 12
    loop.now += 601
    loop.event(1, drop_id="new")
    assert set(loop.twitch.drop_progress_events) == {"new"}


@pytest.mark.parametrize(
    "session_data",
    [
        {
            "dropID": "202df333-e051-11f1-9e2f-0a58a9feac02",
            "currentMinutesWatched": 191,
            "requiredMinutesWatched": 240,
            "channel": {"id": "177128531", "name": "bobito217"},
            "game": {"id": "491931", "displayName": "Escape from Tarkov"},
        },
        None,
        {},
        {"dropID": "partial"},
    ],
)
def test_session_parser_tolerates_null_and_missing_fields(session_data):
    result = Parser().parse_drop_current_session_response(
        {
            "data": {
                "currentUser": {"id": "269303282", "dropCurrentSession": session_data}
            },
            "extensions": {
                "durationMilliseconds": 44,
                "operationName": "DropCurrentSessionContext",
            },
        }
    )
    assert result.has_session is (session_data is not None)
    assert result.drop_id == (session_data or {}).get("dropID")
    assert result.current_minutes_watched == (session_data or {}).get(
        "currentMinutesWatched"
    )
    assert result.required_minutes_watched == (session_data or {}).get(
        "requiredMinutesWatched"
    )
    assert result.errors == []


def test_session_parser_carries_partial_and_error_only_responses():
    parser = Parser()
    for data in [None, {"currentUser": {"dropCurrentSession": {"dropID": "d"}}}]:
        result = parser.parse_drop_current_session_response(
            {
                "data": data,
                "errors": [{"message": "unavailable"}],
            }
        )
        assert result.errors[0].message == "unavailable"


def test_session_integration_sends_channel_and_keeps_operation_immutable(monkeypatch):
    gql = object.__new__(GQL)
    gql.parser = Parser()
    sent = []
    monkeypatch.setattr(
        GQL,
        "post_gql_request_single",
        lambda self, op, payload, parser, attempt_strategy=None: sent.append(
            (op, payload, attempt_strategy)
        )
        or parser(
            {
                "data": {"currentUser": {"dropCurrentSession": None}},
            }
        ),
    )
    original = copy.deepcopy(GQLOperations.DropCurrentSessionContext)
    assert gql.get_drop_current_session("177128531").has_session is False
    assert sent[0][0] == "DropCurrentSessionContext"
    assert sent[0][1]["variables"] == {"channelID": "177128531", "channelLogin": ""}
    # The light check falls back to Inventory on failure; it never retries.
    assert sent[0][2].attempts == 1
    assert GQLOperations.DropCurrentSessionContext == original
    assert original["extensions"]["persistedQuery"] == {
        "version": 1,
        "sha256Hash": "4d06b702d25d652afb9ef835d2a550031f1cf762b193523a92166f40ea3d142b",
    }
    assert "DropCurrentSessionContext" not in GQLOperations.raw_queries


def test_failed_tracked_claim_retries_next_tick_with_null_session(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    loop = SyncLoop(monkeypatch)

    def before(loop):
        loop.report = session(100) if loop.tick == 1 else session()
        loop.inventory = progress_campaign(100, instance="instance-1")
        loop.claim_result = loop.tick != 1

    loop.before_tick = before
    loop.run(4)
    assert loop.reads == [0, 1, 2]
    assert [tick for tick, _ in loop.requests] == [1, 3]
    assert "pending_claim" in caplog.text
    assert loop.history[1]["pending"]["drop-1"]["instance_id"] == "instance-1"
    assert not loop.history[2]["pending"]


@pytest.mark.parametrize("locked", [True, False])
def test_eligibility_changes_escalate_and_are_rate_limited(monkeypatch, locked, caplog):
    caplog.set_level(logging.DEBUG)
    loop = SyncLoop(monkeypatch)
    loop.inventory = progress_campaign(0, eligible=not locked)
    if not locked:
        loop.inventory["timeBasedDrops"][0]["requiredSubs"] = 1
    loop.report = session(1)
    loop.run(4)
    assert loop.reads == [0, 1]
    assert "reason=eligibility_changed" in caplog.text


def test_newly_unlocked_drop_gets_cache_membership_on_same_tick(monkeypatch):
    loop = SyncLoop(monkeypatch)
    loop.inventory = progress_campaign(0, eligible=False)
    loop.report = session(10)
    loop.before_tick = lambda loop: setattr(loop, "inventory", progress_campaign(10))
    loop.run(3)
    assert loop.reads == [0, 1]
    assert loop.twitch.drop_inventory_progress["example-game"][0][4] == 10
    assert loop.tracked_drop.has_preconditions_met is True


def test_full_read_failure_retries_until_success_before_using_light(
    monkeypatch, caplog
):
    caplog.set_level(logging.DEBUG)
    loop = SyncLoop(monkeypatch)
    loop.inventory_for_tick = lambda loop: (
        {} if loop.tick < 2 else {"dropCampaignsInProgress": [loop.inventory]}
    )
    loop.run(4)
    assert loop.reads == [0, 1, 2]
    assert loop.requests == [(3, "177128531")]
    assert loop.history[0]["stamp"] is None and loop.history[1]["stamp"] is None
    assert caplog.text.count("reason=full_retry") == 2


@pytest.mark.parametrize("writer", ["inventory", "forget"])
def test_cache_writers_serialize_read_build_publish(monkeypatch, writer):
    twitch = bare_twitch(monkeypatch)
    twitch._Twitch__cache_drop_inventory_progress(
        {"dropCampaignsInProgress": [progress_campaign(1)]}
    )
    read_snapshot = Event()
    writer_attempted = Event()
    errors = []

    class ObservedCacheLock:
        def __init__(self):
            self.lock = Lock()
            self.owner = None

        def __enter__(self):
            if current_thread() is thread:
                # Signal the actual acquisition attempt, so an unlocked writer
                # cannot accidentally pass by publishing after the light patch.
                writer_attempted.set()
            self.lock.acquire()
            self.owner = current_thread()
            return self

        def __exit__(self, *args):
            self.owner = None
            self.lock.release()

    twitch.drop_inventory_cache_lock = ObservedCacheLock()

    class InterleavedProgress(dict):
        def values(self):
            # Light patch has read the old containers, while holding its lock.
            assert twitch.drop_inventory_cache_lock.owner is current_thread()
            read_snapshot.set()
            assert writer_attempted.wait(2)
            assert twitch.drop_inventory_cache_lock.owner is current_thread()
            return super().values()

    twitch.drop_inventory_progress = InterleavedProgress(twitch.drop_inventory_progress)

    def other_writer():
        try:
            assert read_snapshot.wait(2)
            if writer == "inventory":
                twitch._Twitch__cache_drop_inventory_progress(
                    {"dropCampaignsInProgress": [progress_campaign(80)]}
                )
            else:
                twitch._Twitch__forget_inventory_minutes(SimpleNamespace(id="drop-1"))
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=other_writer, daemon=True)
    thread.start()
    try:
        twitch._Twitch__patch_light_drop_progress({"drop-1": (2, 100, False)})
    finally:
        thread.join(timeout=3)
    assert not thread.is_alive()
    assert not errors
    assert twitch.drop_inventory_minutes_by_drop_id == (
        {"drop-1": 80} if writer == "inventory" else {}
    )
    if writer == "inventory":
        assert twitch.drop_inventory_progress["example-game"][0][4] == 80


def test_untracked_fallback_claim_failure_drives_pending_full_pass(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    loop = SyncLoop(monkeypatch)
    loop.inventory = progress_campaign(100, instance="instance-1")
    loop.claim_result = False
    # Campaign exists locally, but doesn't contain the inventory's new drop.
    local = progress_campaign()
    local["timeBasedDrops"][0]["id"] = "other-local-drop"
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_campaigns_details",
        lambda self, *a, **kw: [local],
    )
    loop.before_tick = lambda loop: setattr(loop, "claim_result", True)
    loop.run(3)
    assert loop.reads == [0, 1]
    assert loop.claims == [(0, "instance-1"), (1, "instance-1")]
    assert loop.tracked_drop.id == "other-local-drop"
    assert loop.history[0]["pending"]["drop-1"]["instance_id"] == "instance-1"
    assert not loop.twitch.drop_claims_pending
    assert "reason=pending_claim" in caplog.text


def test_lagging_inventory_cannot_cancel_completion_retries(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    loop = SyncLoop(monkeypatch)
    # Exercise repeated completion retries independently of dashboard refreshes.
    monkeypatch.setattr(module.time, "time", lambda: 1_000_000)

    def before(loop):
        loop.report = session(100) if loop.tick == 1 else session()
        loop.inventory = progress_campaign(
            99 if loop.tick < 16 else 100,
            instance=None if loop.tick < 16 else "instance-1",
        )

    loop.before_tick = before
    loop.run(18)
    assert loop.reads == [0, 1, 6, 11, 16]
    assert loop.history[2]["minutes"] == 99
    assert loop.history[11]["pending"]["drop-1"]["since"] == 10_060
    assert loop.claims == [(16, "instance-1")]
    assert not loop.twitch.drop_claims_pending
    assert loop.requests[-1][0] == 17


@pytest.mark.parametrize("resolution", ["accepted", "claimed", "absent", "failed"])
def test_tracker_clear_rules_require_usable_inventory(monkeypatch, resolution):
    twitch = bare_twitch(monkeypatch)
    twitch._Twitch__update_pending_drop_claim("drop-1")
    data = progress_campaign(99, claimed=resolution == "claimed")
    inventory = {"dropCampaignsInProgress": [] if resolution == "absent" else [data]}
    if resolution == "accepted":
        twitch.accepted_drop_ids = {"drop-1"}
    if resolution == "failed":
        inventory = {}
    monkeypatch.setattr(Twitch, "_Twitch__get_inventory", lambda self: inventory)
    monkeypatch.setattr(
        Twitch, "_Twitch__save_drop_snapshot_from_dict", lambda self, *a, **kw: None
    )
    twitch._Twitch__sync_campaigns([])
    assert bool(twitch.drop_claims_pending) is (resolution == "failed")


def test_untracked_pubsub_completion_survives_lagging_inventory(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    loop = SyncLoop(monkeypatch)
    unknown = progress_campaign(99)
    unknown["id"] = "untracked-campaign"
    unknown["timeBasedDrops"][0]["id"] = "unknown"
    loop.inventory_for_tick = lambda loop: {
        "dropCampaignsInProgress": [loop.inventory, unknown]
    }
    loop.before_tick = lambda loop: (
        loop.event(100, drop_id="unknown") if loop.tick == 1 else None
    )
    loop.run(8)
    assert loop.reads == [0, 1, 6]
    assert (
        "reason=unknown_drop" in caplog.text and "reason=drop_complete" in caplog.text
    )
    assert loop.twitch.drop_claims_pending["unknown"]["since"] == 10_060


@pytest.mark.parametrize("required", [None, True, -1, 0, "100"])
def test_invalid_requirement_still_records_event_without_inferring_completion(
    monkeypatch, required
):
    loop = SyncLoop(monkeypatch)
    loop.event(100, drop_id="unknown", required=required)
    assert loop.twitch.drop_progress_events["unknown"][2] is None
    loop.run(4)
    assert loop.reads == [0, 1]
    assert not loop.twitch.drop_claims_pending


def test_completion_discovered_by_full_pass_does_not_reset_debounce(monkeypatch):
    loop = SyncLoop(monkeypatch)
    loop.inventory = progress_campaign(100)
    # A separate unknown report forces another full read at tick 2.
    loop.before_tick = lambda loop: setattr(
        loop, "report", session(1, drop_id="unknown") if loop.tick == 2 else session()
    )
    loop.run(12)
    assert loop.reads == [0, 2, 5, 10]
    assert loop.history[2]["pending"]["drop-1"]["last_full"] == 10_000
    assert loop.history[10]["pending"]["drop-1"]["since"] == 10_000


def test_tracker_preserves_known_instance_and_expires_old_entries(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    loop = SyncLoop(monkeypatch)
    twitch = loop.twitch
    twitch._Twitch__update_pending_drop_claim("drop-1", "instance-1")
    loop.now += 60
    twitch._Twitch__update_pending_drop_claim("drop-1")
    assert twitch.drop_claims_pending["drop-1"] == {
        "instance_id": "instance-1",
        "since": 10_000,
        "last_full": None,
        "generation": 2,
    }
    loop.now += 24 * 60 * 60
    twitch._Twitch__pending_drop_progress_reason([], loop.now)
    assert twitch.drop_claims_pending == {}
    assert "expired pending claim" in caplog.text


@pytest.mark.parametrize("unwatched", ["offline", "not_watching", "no_channel"])
def test_no_watched_channel_makes_no_request_but_keeps_data_fresh(
    monkeypatch, unwatched
):
    loop = SyncLoop(monkeypatch)
    if unwatched == "offline":
        loop.streamer.is_online = False
    elif unwatched == "not_watching":
        loop.streamer.is_watching = False
    else:
        loop.streamer.channel_id = ""
    loop.twitch.drop_session_failures = 2
    loop.run(3)
    assert loop.reads == [0]
    assert loop.requests == []
    assert loop.history[-1]["stamp"] == 1_010_120
    assert loop.twitch.drop_session_failures == 2


def test_pubsub_still_works_while_session_source_disabled(monkeypatch):
    loop = SyncLoop(monkeypatch)
    loop.twitch.drop_session_failures = 3
    loop.twitch.drop_session_disabled_until = loop.now + 3600
    loop.before_tick = lambda loop: loop.event(loop.tick) if loop.tick < 3 else None
    loop.run(6)
    assert loop.requests == []
    assert loop.reads == [0, 4, 5]
    assert loop.history[2]["minutes"] == 2
    assert loop.twitch.drop_session_failures == 3


def test_two_successful_channel_sessions_update_every_matching_drop(monkeypatch):
    loop = SyncLoop(monkeypatch)
    other = copy.copy(loop.streamer)
    other.channel_id = "second"
    other.stream = copy.copy(loop.streamer.stream)
    loop.streamers.append(other)
    loop.request_for_channel = lambda loop, channel: session(
        20 if channel == "second" else 10
    )
    loop.run(2)
    assert loop.reads == [0]
    assert loop.requests == [(1, "177128531"), (1, "second")]
    assert loop.tracked_drop.current_minutes_watched == 20


def test_tracked_drop_uses_own_required_minutes(monkeypatch):
    loop = SyncLoop(monkeypatch)
    loop.report = session(50, required=1)
    loop.run(3)
    assert loop.reads == [0]
    assert not loop.twitch.drop_claims_pending
    assert loop.tracked_drop.current_minutes_watched == 50


def test_all_matching_drops_update_and_any_locked_copy_escalates(monkeypatch):
    loop = SyncLoop(monkeypatch)
    campaign = Campaign(progress_campaign())
    locked = campaign.drops[0]
    locked.has_preconditions_met = False
    unlocked = Drop(progress_campaign()["timeBasedDrops"][0])
    unlocked.has_preconditions_met = True
    campaign.drops.append(unlocked)
    loop.twitch._Twitch__cache_drop_inventory_progress(
        {"dropCampaignsInProgress": [progress_campaign()]}
    )
    loop.report = session(10)
    reason = loop.twitch._Twitch__light_drop_progress(
        [campaign], loop.streamers, [], loop.now
    )
    assert reason == "eligibility_changed"
    assert locked.current_minutes_watched == unlocked.current_minutes_watched == 10
    assert loop.twitch.drop_inventory_minutes_by_drop_id["drop-1"] == 0


def test_successful_null_session_resets_failure_counter_and_refreshes_timestamp(
    monkeypatch,
):
    loop = SyncLoop(monkeypatch)
    loop.twitch.drop_session_failures = 2
    loop.run(3)
    assert loop.reads == [0]
    assert len(loop.requests) == 2
    assert loop.twitch.drop_session_failures == 0
    assert loop.history[-1]["stamp"] == 1_010_120


def test_completion_from_successful_channel_survives_other_channel_failure(monkeypatch):
    loop = SyncLoop(monkeypatch)
    second = copy.copy(loop.streamer)
    second.channel_id = "second"
    loop.streamers.append(second)
    loop.request_for_channel = lambda loop, channel: (
        session(100)
        if channel == "177128531"
        else RetryError("DropCurrentSessionContext", [])
    )
    campaign = Campaign(progress_campaign())
    loop.twitch.drop_inventory_progress_updated_at = 123
    reason = loop.twitch._Twitch__light_drop_progress(
        [campaign], loop.streamers, [], loop.now
    )
    assert reason == "light_unavailable"
    assert loop.twitch.drop_inventory_progress_updated_at == 123
    assert loop.twitch.drop_claims_pending["drop-1"]["instance_id"] is None


@pytest.mark.parametrize("finished_kind", ["accepted", "unlinked", "inventory"])
def test_fix1_finished_claimable_drop_uses_light_passes(monkeypatch, finished_kind):
    loop = SyncLoop(monkeypatch)
    if finished_kind == "unlinked":
        loop.inventory = progress_campaign(100, instance="instance-1")
        loop.twitch.gql.claim_drop_rewards = lambda instance: SimpleNamespace(
            status="ELIGIBLE_FOR_ALL", errors=[], is_user_account_connected=False
        )
    else:

        def before(loop):
            drop = loop.tracked_drop
            drop.drop_instance_id = "accepted-instance"
            drop.is_claimable = True
            if finished_kind == "accepted":
                loop.twitch.accepted_drop_ids = frozenset({drop.id})
            else:
                loop.twitch.drop_inventory_finished_ids = frozenset({drop.id})

        loop.before_tick = before
    loop.run(4)
    assert loop.reads == [0]
    assert [tick for tick, _ in loop.requests] == [1, 2, 3]
    assert loop.tracked_drop.is_claimable is True
    assert not loop.twitch.drop_claims_pending


@pytest.mark.parametrize("resolution", ["claimed", "absent"])
def test_fix1_completion_debounce_survives_tracker_recreation(monkeypatch, resolution):
    loop = SyncLoop(monkeypatch)
    loop.report = session(100)
    # Start incomplete, then report completion while inventory says it is gone
    # or already claimed. An absent drop remains tracked in the local campaign.
    loop.inventory_for_tick = lambda loop: {
        "dropCampaignsInProgress": (
            [loop.inventory]
            if loop.tick == 0
            else (
                [progress_campaign(100, claimed=True)]
                if resolution == "claimed"
                else []
            )
        )
    }
    loop.run(8)
    if resolution == "claimed":
        assert loop.reads == [0, 1]
        assert loop.twitch.drop_claims_pending == {}
        assert "drop-1" in loop.twitch._Twitch__finished_drop_ids()
    else:
        assert loop.reads == [0, 1, 6]
        assert loop.history[2]["pending"]["drop-1"]["last_full"] == 10_060
        assert loop.twitch.drop_complete_last_full["drop-1"] == 10_360
    assert not loop.history[1]["pending"]


def test_fix1_completion_stamp_and_light_history_have_separate_lifetimes(monkeypatch):
    loop = SyncLoop(monkeypatch)
    twitch = loop.twitch
    twitch._Twitch__update_pending_drop_claim("drop-1")
    twitch._Twitch__mark_pending_drop_full_pass(loop.now)
    twitch.drop_light_applied_minutes = {"drop-1": (100, loop.now)}
    twitch._Twitch__update_pending_drop_claim("drop-1", clear=True)
    twitch._Twitch__prune_light_drop_minutes(loop.now)
    assert twitch.drop_light_applied_minutes == {}
    assert twitch.drop_complete_last_full == {"drop-1": 10_000}
    loop.now += 60
    twitch._Twitch__update_pending_drop_claim("drop-1")
    assert twitch.drop_claims_pending["drop-1"]["last_full"] == 10_000
    loop.now += 24 * 60 * 60
    twitch._Twitch__pending_drop_progress_reason([], loop.now)
    assert twitch.drop_complete_last_full == {}


@pytest.mark.parametrize(
    "covered_channel", ["177128531", "another-device", ["invalid-channel"]]
)
def test_fix1_pubsub_covers_only_its_channel(monkeypatch, covered_channel):
    loop = SyncLoop(monkeypatch)
    second = copy.copy(loop.streamer)
    second.channel_id = "second"
    second.stream = copy.copy(loop.streamer.stream)
    loop.streamers.append(second)
    loop.report = session(50)
    loop.before_tick = lambda loop: loop.event(10, channel_id=covered_channel)
    loop.run(2)
    expected_channels = (
        ["second"] if covered_channel == "177128531" else ["177128531", "second"]
    )
    assert loop.requests == [(1, channel) for channel in expected_channels]
    assert loop.tracked_drop.current_minutes_watched == 50
    assert loop.reads == [0]


@pytest.mark.parametrize("all_covered", [True, False])
def test_fix1_disabled_source_requires_pubsub_for_every_watched_channel(
    monkeypatch, all_covered
):
    loop = SyncLoop(monkeypatch)
    second = copy.copy(loop.streamer)
    second.channel_id = "second"
    second.stream = copy.copy(loop.streamer.stream)
    loop.streamers.append(second)
    loop.twitch.drop_session_disabled_until = loop.now + 3600
    loop.twitch.drop_session_failures = 3

    def before(loop):
        loop.event(10)
        if all_covered:
            loop.event(20, drop_id="second-drop", channel_id="second")
            # Keep this coverage event from adding an unknown-drop escalation.
            loop.twitch.accepted_drop_ids = frozenset({"second-drop"})

    loop.before_tick = before
    loop.run(3)
    assert loop.requests == []
    assert loop.reads == ([0] if all_covered else [0, 1, 2])
    assert loop.twitch.drop_session_failures == 3


def test_fix1_cache_patch_does_not_add_missing_minutes_key(monkeypatch):
    twitch = bare_twitch(monkeypatch)
    twitch._Twitch__cache_drop_inventory_progress(
        {"dropCampaignsInProgress": [progress_campaign(1)]}
    )
    twitch.drop_inventory_minutes_by_drop_id = {}
    missing = twitch.drop_inventory_minutes_by_drop_id
    twitch._Twitch__patch_light_drop_progress({"drop-1": (25, 100, False)})
    assert twitch.drop_inventory_minutes_by_drop_id == {}
    assert twitch.drop_inventory_minutes_by_drop_id is not missing
    assert twitch.drop_inventory_progress["example-game"][0][4] == 25


@pytest.mark.parametrize("finished_kind", ["accepted", "inventory"])
def test_fix1_finished_report_cannot_recreate_tracker_or_cache(
    monkeypatch, finished_kind
):
    loop = SyncLoop(monkeypatch)
    loop.report = session(100)

    def before(loop):
        if finished_kind == "accepted":
            loop.twitch.accepted_drop_ids = frozenset({"drop-1"})
        else:
            loop.twitch.drop_inventory_finished_ids = frozenset({"drop-1"})
        loop.twitch._Twitch__forget_inventory_minutes(SimpleNamespace(id="drop-1"))

    loop.before_tick = before
    loop.run(4)
    assert loop.reads == [0]
    assert not loop.twitch.drop_claims_pending
    assert "drop-1" not in loop.twitch.drop_inventory_minutes_by_drop_id
    assert loop.tracked_drop.current_minutes_watched == 0


@pytest.mark.parametrize("source", ["session", "pubsub"])
@pytest.mark.parametrize("current", [50, 100])
def test_fix1_lagging_inventory_restores_minutes_without_reprinting(
    monkeypatch, source, current
):
    loop = SyncLoop(monkeypatch)
    twitch = loop.twitch
    campaign = Campaign(progress_campaign(current - 1))
    drop = campaign.drops[0]
    drop.update(progress_campaign(current - 1)["timeBasedDrops"][0]["self"])
    twitch._Twitch__cache_drop_inventory_progress(
        {"dropCampaignsInProgress": [progress_campaign(current - 1)]}
    )
    loop.report = session(current)
    if source == "pubsub":
        loop.event(current)
    events = twitch._Twitch__recent_drop_progress_events(loop.now, loop.now - 60)
    twitch._Twitch__light_drop_progress([campaign], loop.streamers, events, loop.now)
    assert drop.is_printable is True
    # Simulate the full read's lagging model/cache publication. It leaves the
    # completion tracker in place, so the light notification history survives.
    drop.update(progress_campaign(current - 1)["timeBasedDrops"][0]["self"])
    twitch._Twitch__cache_drop_inventory_progress(
        {"dropCampaignsInProgress": [progress_campaign(current - 1)]}
    )
    loop.now += 60
    twitch._Twitch__light_drop_progress([campaign], loop.streamers, events, loop.now)
    assert drop.current_minutes_watched == current
    assert drop.is_printable is False
    if current == 100:
        assert twitch.drop_inventory_minutes_by_drop_id == {}
        assert "example-game" not in twitch.drop_inventory_progress
    else:
        assert twitch.drop_inventory_minutes_by_drop_id == {"drop-1": 50}
        assert twitch.drop_inventory_progress["example-game"][0][4] == 50
    assert twitch.drop_light_applied_minutes["drop-1"][0] == current


@pytest.mark.parametrize("following_report", ["same", "null", "lower"])
def test_fix1_light_pass_clears_printable_without_new_progress(
    monkeypatch, following_report
):
    loop = SyncLoop(monkeypatch)

    def before(loop):
        if loop.tick == 1:
            loop.report = session(25)
        else:
            # Force the previous flag on to prove an equal, absent or stale
            # report clears it rather than the watcher consuming it.
            loop.tracked_drop.is_printable = True
            loop.report = {
                "same": session(25),
                "null": session(),
                "lower": session(24),
            }[following_report]

    loop.before_tick = before
    loop.run(4)
    assert loop.reads == [0]
    assert [row["printable"] for row in loop.history] == [False, False, False, False]
    # 25 from bootstrap does not print; the explicit stale flags still reset.
    assert loop.tracked_drop.current_minutes_watched == 25


def test_fix1_light_history_expires_after_24_hours(monkeypatch):
    loop = SyncLoop(monkeypatch)
    twitch = loop.twitch
    campaign = Campaign(progress_campaign(49))
    drop = campaign.drops[0]
    drop.update(progress_campaign(49)["timeBasedDrops"][0]["self"])
    twitch.drop_light_applied_minutes = {"drop-1": (50, loop.now - 24 * 60 * 60 - 1)}
    twitch._Twitch__cache_drop_inventory_progress(
        {"dropCampaignsInProgress": [progress_campaign(49)]}
    )
    loop.report = session(50)
    twitch._Twitch__light_drop_progress([campaign], loop.streamers, [], loop.now)
    assert drop.is_printable is True
    assert twitch.drop_light_applied_minutes["drop-1"] == (50, loop.now)


@pytest.mark.parametrize(
    "path,loaded",
    [
        ("no_errors", True),
        (["currentUser", "inventory", "gameEventDrops"], True),
        (["currentUser", "inventory", "dropCampaignsInProgress"], False),
        (["currentUser", "inventory"], False),
        (["currentUser"], False),
        ([], False),
        (None, False),
    ],
)
def test_fix1_nullable_inventory_respects_error_paths_without_clearing_tracker(
    monkeypatch, path, loaded
):
    loop = SyncLoop(monkeypatch)
    twitch = loop.twitch
    # A pending completion not due for five minutes must survive null inventories.
    twitch._Twitch__update_pending_drop_claim("drop-1")
    twitch._Twitch__mark_pending_drop_full_pass(loop.now)
    loop.inventory_for_tick = lambda loop: {"dropCampaignsInProgress": None}
    if path != "no_errors":
        loop.inventory_errors = [SimpleNamespace(path=path, message="unavailable")]
    loop.run(3)
    assert loop.reads == ([0] if loaded else [0, 1, 2])
    assert loop.requests == ([(1, "177128531"), (2, "177128531")] if loaded else [])
    assert twitch.drop_full_inventory_loaded is loaded
    assert "drop-1" in twitch.drop_claims_pending
    assert twitch.drop_claims_pending["drop-1"]["since"] == 10_000
    assert twitch.drop_claims_pending["drop-1"]["last_full"] == 10_000
    # Cache publication by __get_inventory is preserved even on errored nulls.
    assert loop.history[-1]["stamp"] == 1_010_120


def test_fix1_inventory_error_evidence_is_per_thread_and_keeps_payload_shape(
    monkeypatch,
):
    loop = SyncLoop(monkeypatch)
    twitch = loop.twitch
    inventory = {"dropCampaignsInProgress": None}
    other = bare_twitch(monkeypatch)
    other.awarded_game_event_drops = {}
    other.gql = SimpleNamespace(
        get_inventory=lambda: InventoryResponse(None, inventory=dict(inventory))
    )
    twitch.gql.get_inventory = lambda: InventoryResponse(
        None,
        inventory=dict(inventory),
        errors=[SimpleNamespace(path=["currentUser", "inventory"], message="error")],
    )
    loaded = Event()
    other_read_done = Event()
    errors = []
    original = Twitch._Twitch__get_inventory

    def interleave(self):
        result = original(self)
        if self is twitch:
            loaded.set()
            assert other_read_done.wait(2)
        return result

    monkeypatch.setattr(Twitch, "_Twitch__get_inventory", interleave)

    def read_elsewhere():
        try:
            assert loaded.wait(2)
            assert other._Twitch__get_inventory() == inventory
        except BaseException as error:
            errors.append(error)
        finally:
            other_read_done.set()

    thread = Thread(target=read_elsewhere, daemon=True)
    thread.start()
    try:
        twitch._Twitch__sync_campaigns([])
    finally:
        thread.join(timeout=3)
    assert not thread.is_alive() and not errors
    assert twitch.drop_full_inventory_loaded is False
    # The existing completed-campaign normalization is the only shape change.
    assert module._drop_inventory_state.inventory == {
        "dropCampaignsInProgress": None,
        "completedRewardCampaigns": None,
    }


def test_fix1_inventory_error_evidence_resets_between_calls(monkeypatch):
    loop = SyncLoop(monkeypatch)
    twitch = loop.twitch
    loop.inventory_for_tick = lambda loop: {"dropCampaignsInProgress": None}
    loop.inventory_errors = [SimpleNamespace(path=None, message="failed")]
    twitch._Twitch__sync_campaigns([])
    assert twitch.drop_full_inventory_loaded is False
    loop.inventory_errors = []
    twitch._Twitch__sync_campaigns([])
    assert twitch.drop_full_inventory_loaded is True
    assert module._drop_inventory_state.error_paths == frozenset()


@pytest.mark.parametrize("status", [503, "timeout"])
def test_session_request_is_sent_once_when_transport_fails(status):
    import requests
    from TwitchChannelPointsMiner.classes.ClientSession import ClientSession
    from TwitchChannelPointsMiner.utils.AttemptStrategy import AttemptStrategy

    posts = []

    class Failing:
        status_code = 503
        text = "unavailable"

        def raise_for_status(self):
            error = requests.HTTPError("503")
            error.response = self
            raise error

        def json(self):
            return {}

    def post(url, json, headers):
        posts.append(json.get("operationName"))
        if status == "timeout":
            raise requests.exceptions.Timeout("timed out")
        return Failing()

    login = SimpleNamespace(get_auth_token=lambda: "token", get_user_id=lambda: "1")
    gql = GQL(
        ClientSession(
            login=login,
            user_agent="test-agent",
            version="test-version",
            device_id="test-device",
            session_id="test-session",
        ),
        attempt_strategy=AttemptStrategy(attempts=3, attempt_interval_seconds=0),
        post_request=post,
    )
    with pytest.raises(RetryError):
        gql.get_drop_current_session("177128531")
    assert posts == ["DropCurrentSessionContext"]
    # The shared strategy itself is untouched for every other operation.
    assert gql.attempt_strategy.attempts == 3


def test_untracked_cached_drop_keeps_moving_so_stall_check_stays_honest(monkeypatch):
    loop = SyncLoop(monkeypatch)
    twitch = loop.twitch
    other = progress_campaign(10)
    other["id"] = "campaign-2"
    other["timeBasedDrops"][0]["id"] = "drop-2"
    twitch._Twitch__cache_drop_inventory_progress(
        {"dropCampaignsInProgress": [progress_campaign(1), other]}
    )
    campaign = Campaign(progress_campaign(1))
    for minutes in (11, 12):
        loop.now += 60
        twitch._Twitch__light_drop_progress(
            [campaign],
            loop.streamers,
            [("drop-2", minutes, 100, "177128531", loop.now)],
            loop.now,
        )
        assert twitch.drop_inventory_minutes_by_drop_id["drop-2"] == minutes
        assert [
            row[4]
            for row in twitch.drop_inventory_progress["example-game"]
            if row[2] == "drop-2"
        ] == [minutes]
    # A drop the inventory never cached gains no cache membership.
    twitch._Twitch__light_drop_progress(
        [campaign],
        loop.streamers,
        [("drop-9", 5, 100, "177128531", loop.now)],
        loop.now,
    )
    assert "drop-9" not in twitch.drop_inventory_minutes_by_drop_id


def test_duplicate_reports_in_one_pass_keep_the_milestone(monkeypatch):
    loop = SyncLoop(monkeypatch)
    twitch = loop.twitch
    campaign = Campaign(progress_campaign(49))
    drop = campaign.drops[0]
    drop.update(progress_campaign(49)["timeBasedDrops"][0]["self"])
    twitch._Twitch__cache_drop_inventory_progress(
        {"dropCampaignsInProgress": [progress_campaign(49)]}
    )
    # PubSub from another channel and the watched channel's session report the
    # same drop at 50 in the same pass.
    loop.report = session(50)
    events = [("drop-1", 50, 100, "other-channel", loop.now)]
    twitch._Twitch__light_drop_progress([campaign], loop.streamers, events, loop.now)
    assert drop.current_minutes_watched == 50
    assert drop.is_printable is True
    # An equal report on a later pass still clears it.
    loop.now += 60
    twitch._Twitch__light_drop_progress([campaign], loop.streamers, [], loop.now)
    assert drop.is_printable is False


def test_stale_light_report_never_lowers_newer_cached_minutes(monkeypatch):
    twitch = bare_twitch(monkeypatch)
    # Another thread's inventory read (discovery, a PubSub claim sweep)
    # publishes 60 minutes before the sync thread's light patch takes the
    # cache lock with an older report of 55.
    twitch._Twitch__cache_drop_inventory_progress(
        {"dropCampaignsInProgress": [progress_campaign(60)]}
    )
    twitch._Twitch__patch_light_drop_progress({"drop-1": (55, 100, False)})
    assert twitch.drop_inventory_minutes_by_drop_id == {"drop-1": 60}
    assert twitch.drop_inventory_progress["example-game"][0][4] == 60
    # A newer light report still moves them forward.
    twitch._Twitch__patch_light_drop_progress({"drop-1": (61, 100, False)})
    assert twitch.drop_inventory_minutes_by_drop_id == {"drop-1": 61}
    assert twitch.drop_inventory_progress["example-game"][0][4] == 61


@pytest.mark.parametrize("stale_read_lands", ["after_accept", "before_accept"])
def test_accepted_drop_stays_out_of_caches_that_light_passes_keep_fresh(
    monkeypatch, stale_read_lands
):
    loop = SyncLoop(monkeypatch)
    twitch = loop.twitch
    campaign = Campaign(progress_campaign(50))
    drop = campaign.drops[0]
    # A discovery read captured before the claim: incomplete, no instance.
    stale = {"dropCampaignsInProgress": [progress_campaign(50)]}
    if stale_read_lands == "before_accept":
        twitch._Twitch__cache_drop_inventory_progress(stale)
        twitch._Twitch__mark_drop_accepted(drop)
    else:
        twitch._Twitch__mark_drop_accepted(drop)
        twitch._Twitch__cache_drop_inventory_progress(stale)
    assert "drop-1" not in twitch.drop_inventory_minutes_by_drop_id
    assert "example-game" not in twitch.drop_inventory_progress
    # Null light replies then mark the cache fresh without bringing it back.
    loop.report = session(None)
    loop.now += 60
    before = twitch.drop_inventory_progress_updated_at
    twitch._Twitch__light_drop_progress([campaign], loop.streamers, [], loop.now)
    assert twitch.drop_inventory_progress_updated_at > before
    assert "drop-1" not in twitch.drop_inventory_minutes_by_drop_id
    assert "example-game" not in twitch.drop_inventory_progress


def test_light_patch_drops_finished_rows_left_by_any_writer(monkeypatch):
    twitch = bare_twitch(monkeypatch)
    twitch._Twitch__cache_drop_inventory_progress(
        {"dropCampaignsInProgress": [progress_campaign(50)]}
    )
    # A writer that predates the accepted-ID guard left the row in place.
    twitch.accepted_drop_ids = frozenset({"drop-1"})
    twitch._Twitch__patch_light_drop_progress({})
    assert "example-game" not in twitch.drop_inventory_progress
    assert twitch.drop_inventory_minutes_by_drop_id == {}


def test_older_inventory_keeps_a_claim_failure_recorded_during_its_request(
    monkeypatch, caplog
):
    caplog.set_level(logging.DEBUG)
    loop = SyncLoop(monkeypatch)
    untracked = progress_campaign(100, instance="instance-9")
    untracked["id"] = "campaign-9"
    untracked["timeBasedDrops"][0]["id"] = "drop-9"
    loop.report = session(None)

    def inventory_for_tick(loop):
        if loop.tick == 1:
            # While this sync's (older) request is in flight, the PubSub claim
            # worker reads newer inventory and fails to claim drop-9.
            loop.claim_result = False
            stale = Drop(untracked["timeBasedDrops"][0])
            stale.update(untracked["timeBasedDrops"][0]["self"])
            loop.twitch.claim_drop(stale)
            loop.claim_result = True
        # The older response doesn't list drop-9 yet.
        return {"dropCampaignsInProgress": [loop.inventory]}

    loop.inventory_for_tick = inventory_for_tick
    # Tick 1 is a full pass (interval forced); tick 2 must retry the claim.
    loop.before_tick = lambda loop: (
        setattr(loop.twitch, "drop_full_inventory_loaded", False)
        if loop.tick == 1
        else None
    )
    loop.run(3)
    assert "drop-9" in loop.history[1]["pending"]
    assert loop.reads == [0, 1, 2]
    assert "reason=pending_claim" in caplog.text
